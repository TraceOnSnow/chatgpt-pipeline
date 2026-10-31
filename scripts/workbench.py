#!/usr/bin/env python3
"""Project-local setup, explicit foreground start, and read-only inbox status."""
import argparse
from contextlib import closing
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
PRODUCTION_PROJECT = PROJECT
SYSTEMD_UNIT = 'chatgpt-workbench.service'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_path(value):
    value = Path(value).expanduser()
    require(value.is_absolute() and '..' not in value.parts, 'Use an explicit absolute path without ..')
    require(not any(part.is_symlink() for part in (value, *value.parents)), 'Symlink paths are not supported')
    return value


def workspace_path(value):
    value = safe_path(value)
    root = PROJECT / 'runtime'
    require(value != root and value.is_relative_to(root), 'Workspace must be inside this project runtime')
    require((value / 'config.json').is_file() and (value / 'state.sqlite3').is_file(), 'Existing initialized workspace required; this command never runs notes-init')
    require(not any((value / name).is_symlink() for name in ('state.sqlite3', 'config.json')), 'Workspace state/config must not be a symlink')
    return value


def read_state(workspace):
    # A normal read-only WAL connection may still create -shm/-wal files.
    # Never do that from status/preflight, or silently ignore uncheckpointed data.
    wal = workspace / 'state.sqlite3-wal'
    require(not wal.exists() or wal.stat().st_size == 0,
            'Uncheckpointed WAL exists; use the verified server endpoint or inspect a SQLite backup first')
    with closing(sqlite3.connect((workspace / 'state.sqlite3').as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        counts = dict(db.execute("""SELECT CASE WHEN i.decision='skipped' THEN 'skipped'
            WHEN j.state IN ('queued','running') THEN 'working' ELSE COALESCE(j.state,'todo') END, COUNT(*)
            FROM items i LEFT JOIN jobs j ON j.id=i.job_id GROUP BY 1"""))
        active = dict(db.execute("SELECT state,COUNT(*) FROM jobs WHERE state IN ('queued','running') GROUP BY state"))
        calls = db.execute('SELECT cost,reservation,job_id,stage FROM calls').fetchall()
    config = json.loads((workspace / 'config.json').read_text())
    return {'counts': counts, 'pending_jobs': active, 'calls': len(calls),
            'actual_usd': str(sum((Decimal(row[0]) for row in calls if row[0] is not None), Decimal(0))),
            'unknown_cost_calls': sum(row[0] is None for row in calls),
            'calls_without_response': sum(not (workspace / 'jobs' / row[2] / row[3] / 'response.raw').is_file() for row in calls),
            'budget_usd': config.get('budget_usd')}


def process_info(pid, workspace, port, proc_root=Path('/proc')):
    """Inspect only argv/cwd/start identity; never read /proc/*/environ."""
    result = {'pid': pid, 'verified': False}
    if not isinstance(pid, int) or pid <= 0:
        return {**result, 'reason': 'No valid recorded PID'}
    proc = proc_root / str(pid)
    try:
        require(proc.stat().st_uid == os.getuid(), 'PID belongs to another user')
        argv = [part.decode() for part in (proc / 'cmdline').read_bytes().split(b'\0') if part]
        cwd = (proc / 'cwd').resolve(strict=True)
        start_ticks = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        require(cwd == PROJECT, 'PID belongs to another working directory')
        module = argv.index('-m') if '-m' in argv else -1
        require(module >= 0 and argv[module + 1:module + 3] == ['chatgpt_pipeline', 'workbench-serve'], 'PID is not this project workbench module')
        options = {}
        for flag in ('--workspace', '--port', '--vault'):
            if flag in argv:
                index = argv.index(flag)
                require(index + 1 < len(argv), 'Incomplete workbench launch arguments')
                options[flag] = argv[index + 1]
        selected = Path(options.get('--workspace', ''))
        selected = selected if selected.is_absolute() else cwd / selected
        require(selected == workspace and int(options.get('--port', '8766')) == port, 'PID workspace or port does not match')
        return {**result, 'verified': True, 'python': argv[0], 'start_ticks': start_ticks,
                'model_enabled': '--call' in argv, 'resume_existing': '--no-resume' not in argv,
                'vault': options.get('--vault'), 'key_file_configured': '--key-file' in argv}
    except (OSError, ValueError, IndexError, UnicodeError) as error:
        return {**result, 'reason': str(error) if isinstance(error, ValueError) else type(error).__name__}


def json_get(url):
    # Refuse redirects so status never follows a loopback response to a remote host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(url, timeout=2) as response:
        return json.load(response)


def systemd_info(workspace, port):
    # Temporary workspaces/tests never inspect the real user's global unit.
    if PROJECT != PRODUCTION_PROJECT or workspace != PROJECT / 'runtime/workbench' or port != 8766:
        return None
    checked = subprocess.run(['systemctl', '--user', 'show', SYSTEMD_UNIT,
                              '--property=LoadState,MainPID,ActiveState,SubState,InvocationID,UnitFileState,NRestarts,Result'],
                             capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    result = dict(line.split('=', 1) for line in checked.stdout.splitlines() if '=' in line)
    if result.get('LoadState') == 'not-found':
        return None
    require(checked.returncode == 0 and result.get('LoadState') == 'loaded',
            'Cannot verify user systemd; refusing a duplicate workbench')
    result['MainPID'] = int(result['MainPID'])
    require(result['MainPID'] == 0 or result['MainPID'] > 1, 'Invalid systemd PID')
    return result


def status(workspace, port):
    workspace = workspace_path(workspace)
    require(1 <= port <= 65535, 'Port must be between 1 and 65535')
    unit = systemd_info(workspace, port)
    try:
        require(not (workspace / 'server.pid').is_symlink(), 'PID file must not be a symlink')
        pid = int((workspace / 'server.pid').read_text().strip())
    except (OSError, ValueError):
        pid = None
    if unit is not None:
        pid = unit['MainPID'] or None
    process = process_info(pid, workspace, port)
    launch_file = workspace / 'server-launch.json'
    if launch_file.is_file() and not launch_file.is_symlink() and process['verified']:
        launch = json.loads(launch_file.read_text())
        if launch.get('pid') == pid and launch.get('start_ticks') != process['start_ticks']:
            process = {**process, 'verified': False, 'reason': 'Recorded PID was reused'}
    http = {'ready': False}
    if process['verified']:
        try:
            data = json_get(f'http://127.0.0.1:{port}/api/session')
            require(isinstance(data.get('model_enabled'), bool), 'Unexpected workbench session response')
            # The response contains a CSRF token; only these public configuration fields leave memory.
            http = {'ready': True, 'model_enabled': data['model_enabled'],
                    'vault_enabled': data.get('vault_enabled', False), 'vault': data.get('vault_path')}
            http['configuration_matches'] = (data['model_enabled'] == process.get('model_enabled', data['model_enabled'])
                and data.get('vault_path') == process.get('vault'))
            require(http['configuration_matches'], 'PID and endpoint configurations do not match')
        except (OSError, ValueError, KeyError) as error:
            http['ready'] = False
            http['error'] = type(error).__name__
    else:
        http['reason'] = 'Endpoint not queried without a verified project PID'
    vault = http.get('vault') if http['ready'] else process.get('vault')
    receipts = workspace / 'vault-imports' / hashlib.sha256(str(vault).encode()).hexdigest() if vault else None
    result = {'project': str(PROJECT), 'workspace': str(workspace), 'url': f'http://127.0.0.1:{port}',
              'process': process, 'http': http, 'vault': vault,
              'imported_count': len(list(receipts.glob('*.json'))) if receipts else 0}
    if unit is not None:
        result['service'] = {'manager': 'systemd', 'unit': SYSTEMD_UNIT,
                             'active_state': unit['ActiveState'], 'sub_state': unit['SubState'],
                             'invocation_id': unit['InvocationID'], 'restart_count': unit['NRestarts'],
                             'result': unit['Result'], 'autostart': unit['UnitFileState'] in ('enabled', 'enabled-runtime')}
    try:
        if http['ready']:
            data = json_get(f'http://127.0.0.1:{port}/api/items?view=all')
            usage = data['usage']
            result.update({'counts': data['counts'], 'pending_jobs': {'working': data['counts']['working']},
                'calls_without_response': None, 'state_source': 'verified HTTP read',
                **{name: usage[name] for name in ('calls', 'actual_usd', 'unknown_cost_calls', 'budget_usd')}})
            result['imported_count'] = data.get('imported_count', result['imported_count'])
        else:
            result.update({**read_state(workspace), 'state_source': 'checkpointed SQLite read'})
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        result['state_error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
    return result


def setup(offline=False):
    uv = shutil.which('uv')
    require(uv is not None, 'Existing uv executable is required; no global installation is performed')
    require((PROJECT.parent / 'obsidian-workflow' / 'pyproject.toml').is_file(), 'Sibling obsidian-workflow source package required')
    venv = safe_path(PROJECT / '.venv')
    environment = {**os.environ, 'UV_CACHE_DIR': os.environ.get('UV_CACHE_DIR', str(PROJECT / 'runtime' / 'uv-cache')),
                   'UV_PYTHON_DOWNLOADS': 'never'}
    if not (venv / 'bin/python').exists():
        require(not venv.exists(), 'Existing incomplete .venv must be inspected before replacement')
        subprocess.run([uv, 'venv', '--python', sys.executable, str(venv)], cwd=PROJECT, env=environment, check=True)
    command = [uv, 'pip', 'sync', '--python', str(venv / 'bin/python'), str(PROJECT / 'requirements-workbench.lock')]
    subprocess.run(command + (['--offline'] if offline else []), cwd=PROJECT, env=environment, check=True)
    subprocess.run([uv, 'pip', 'check', '--python', str(venv / 'bin/python')], cwd=PROJECT, env=environment, check=True)
    return {'python': str(venv / 'bin/python'), 'runtime_dependency': 'PyYAML==6.0.3',
            'local_packages': ['chatgpt-pipeline', 'markdown-library'], 'model_calls': 0}


def prepare_start(args):
    workspace = workspace_path(args.workspace)
    vault = safe_path(args.vault)
    require(1 <= args.port <= 65535, 'Port must be between 1 and 65535')
    try:
        from chatgpt_pipeline.notes import Workspace
        from markdown_library import Library, LibraryError
    except ImportError as error:
        raise ValueError('Run scripts/workbench.py setup to install the project runtime first') from error
    Workspace(workspace)  # Validate existing configuration; do not initialize or recover jobs.
    try:
        Library(vault).validate()
    except (LibraryError, OSError) as error:
        raise ValueError(f'Initialized safe Vault required: {error}') from error
    if (workspace / 'server.lock').exists():
        require(not (workspace / 'server.lock').is_symlink(), 'Server lock must not be a symlink')
        with (workspace / 'server.lock').open('rb') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('This workspace already has a running server; no process was stopped') from error
    if args.models == 'enabled':
        require(not any(read_state(workspace)['pending_jobs'].values()),
                'Queued/running jobs exist; inspect them first. No saved job is automatically resumed')
    python = PROJECT / '.venv/bin/python'
    require(python.is_file(), 'Run scripts/workbench.py setup first')
    command = [str(python), '-m', 'chatgpt_pipeline', 'workbench-serve', '--workspace', str(workspace),
               '--port', str(args.port), '--vault', str(vault), '--no-resume']
    if args.models == 'enabled':
        require(args.key_file is not None and args.key_file.expanduser().is_file(), 'Enabled model mode requires an existing --key-file; its content is never read by this launcher')
        command += ['--call', '--key-file', str(args.key_file.expanduser().absolute())]
    else:
        require(args.key_file is None, 'Omit --key-file in disabled model mode')
    return workspace, vault, command


def atomic_json(destination, value):
    with tempfile.NamedTemporaryFile(mode='w', dir=destination.parent, prefix='.launch-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, destination)
            descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)


def start(args):
    workspace, vault, command = prepare_start(args)
    os.chdir(PROJECT)
    pid = os.getpid()
    ticks = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    atomic_json(workspace / 'server-launch.json', {'pid': pid, 'start_ticks': ticks, 'vault': str(vault),
                'port': args.port, 'models': args.models, 'workspace': str(workspace)})
    # exec preserves PID, signals, existing budget/state and ordinary Ctrl+C shutdown.
    atomic_json(workspace / 'server.pid', pid)
    os.execv(command[0], command)


def service_action(action, workspace, port, unit):
    if unit['MainPID']:
        require(process_info(unit['MainPID'], workspace, port)['verified'],
                'Refusing to control a unit whose PID is not this workbench')
    checked = subprocess.run(['systemctl', '--user', action, SYSTEMD_UNIT],
                             capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    require(checked.returncode == 0, 'User systemd action failed; inspect private service journal')
    return status(workspace, port)


def stop(workspace, port):
    current = status(workspace, port)
    require(not any(current.get('pending_jobs', {}).values()), 'Active jobs exist; no process was stopped')
    require('state_error' not in current, 'Cannot verify jobs; no process was stopped')
    unit = systemd_info(workspace, port)
    if unit is not None:
        return service_action('stop', workspace, port, unit)
    original = current['process']
    if not original['verified']:
        return current
    descriptor = None
    try:
        if hasattr(os, 'pidfd_open'):
            descriptor = os.pidfd_open(original['pid'])
        fresh = process_info(original['pid'], workspace, port)
        require(fresh['verified'] and fresh['start_ticks'] == original['start_ticks'],
                'Process identity changed before stop')
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, signal.SIGINT)
        else:
            os.kill(original['pid'], signal.SIGINT)
    except ProcessLookupError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
    for _ in range(100):
        if not process_info(original['pid'], workspace, port)['verified']:
            return status(workspace, port)
        time.sleep(0.05)
    raise ValueError('Stop requested but verified process has not exited; do not start another')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    install = sub.add_parser('setup', help='Install only project-local runtime dependencies')
    install.add_argument('--offline', action='store_true')
    for name in ('start', 'run', 'status', 'stop'):
        action = sub.add_parser(name)
        action.add_argument('--workspace', type=Path, required=True)
        action.add_argument('--port', type=int, required=True)
        if name in ('start', 'run'):
            action.add_argument('--vault', type=Path, required=True)
            action.add_argument('--models', choices=['enabled', 'disabled'], required=True)
            action.add_argument('--key-file', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'start':
            unit = systemd_info(workspace_path(args.workspace), args.port)
            if unit is not None:
                require(args.models == 'enabled' and safe_path(args.vault) == PROJECT.parent / 'personal-records-vault'
                        and args.key_file == Path('/home/trace/.hermes/.env'),
                        'Installed service uses the reviewed original mode/Vault/key path; do not override it through start')
                print(json.dumps(service_action('start', args.workspace, args.port, unit), ensure_ascii=False))
                return 0
        if args.command in ('start', 'run') and Path(sys.prefix) != PROJECT / '.venv':
            python = PROJECT / '.venv/bin/python'
            require(python.is_file(), 'Run scripts/workbench.py setup first')
            os.execv(str(python), [str(python), str(Path(__file__).resolve()), *(argv if argv is not None else sys.argv[1:])])
        if args.command in ('start', 'run'):
            start(args)
            return
        result = setup(args.offline) if args.command == 'setup' else (
            stop(args.workspace, args.port) if args.command == 'stop' else status(args.workspace, args.port))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(json.dumps({'error': type(error).__name__, 'reason': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
