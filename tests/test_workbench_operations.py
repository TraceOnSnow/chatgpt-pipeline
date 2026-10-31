"""Launcher/status use disposable workspace metadata; never touch the real service."""
import argparse
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from chatgpt_pipeline import workbench
import test_notes as fixtures

spec = importlib.util.spec_from_file_location('inbox_operations', Path(__file__).parents[1] / 'scripts/workbench.py')
operations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operations)


class WorkbenchOperationsTests(unittest.TestCase):
    setUp = fixtures.NoteTests.setUp
    respond_selection = fixtures.NoteTests.respond_selection
    response = fixtures.NoteTests.response

    def configure(self):
        from markdown_library import Library
        self.addCleanup(patch.stopall)
        patch.object(operations, 'PROJECT', self.base).start()
        self.vault = self.base / 'vault'
        Library.initialize(self.vault)
        (self.base / '.venv/bin').mkdir(parents=True)
        (self.base / '.venv/bin/python').write_text('fixture only')
        self.key = self.base / 'fixture-key.env'
        self.key.write_text('THIS_SECRET_MUST_NOT_BE_READ_OR_PRINTED')
        return argparse.Namespace(workspace=self.directory, vault=self.vault, port=8766, models='disabled', key_file=None)

    def test_start_uses_explicit_vault_mode_and_always_disables_startup_resume(self):
        args = self.configure()
        _, vault, command = operations.prepare_start(args)
        self.assertEqual(vault, self.vault)
        self.assertIn('--no-resume', command)
        self.assertNotIn('--call', command)
        args.models, args.key_file = 'enabled', self.key
        original_read = Path.read_text
        def guarded_read(filename, *values, **options):
            self.assertNotEqual(filename, self.key, 'Launcher must not read credential contents')
            return original_read(filename, *values, **options)
        with patch.object(Path, 'read_text', guarded_read):
            _, _, enabled = operations.prepare_start(args)
        self.assertIn('--call', enabled)
        self.assertEqual(enabled[enabled.index('--key-file') + 1], str(self.key))
        self.workspace.request(self.item)
        with self.assertRaisesRegex(ValueError, 'automatically resumed'):
            operations.prepare_start(args)
        args.models, args.key_file = 'disabled', None
        self.assertNotIn('--call', operations.prepare_start(args)[2])
        self.assertEqual(self.workspace.usage()['calls'], 0)

    def test_start_refuses_uninitialized_symlink_or_already_running_targets(self):
        args = self.configure()
        args.vault = self.base / 'uninitialized'
        with self.assertRaisesRegex(ValueError, 'Initialized safe Vault'):
            operations.prepare_start(args)
        self.assertFalse(args.vault.exists())
        args.vault = self.base / 'linked-vault'
        args.vault.symlink_to(self.vault, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            operations.prepare_start(args)
        args.vault = self.vault
        with (self.directory / 'server.lock').open('wb') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError, 'already has a running server'):
                operations.prepare_start(args)
        self.assertEqual(self.workspace.usage()['calls'], 0)

    def test_process_verification_allowlists_metadata_and_rejects_other_projects(self):
        self.configure()
        proc = self.base / 'proc' / '123'
        proc.mkdir(parents=True)
        (proc / 'cwd').symlink_to(self.base, target_is_directory=True)
        argv = ['/usr/bin/python3', '-m', 'chatgpt_pipeline', 'workbench-serve', '--workspace', str(self.directory), '--port', '8766', '--call', '--key-file', 'SECRET_PATH_NOT_REPORTED', '--vault', str(self.vault), '--no-resume']
        (proc / 'cmdline').write_bytes(b'\0'.join(value.encode() for value in argv))
        (proc / 'stat').write_text('123 (python) ' + ' '.join(['S'] + ['0'] * 18 + ['9876']))
        result = operations.process_info(123, self.directory, 8766, self.base / 'proc')
        self.assertTrue(result['verified'])
        self.assertEqual(result['start_ticks'], '9876')
        self.assertFalse(result['resume_existing'])
        self.assertNotIn('SECRET_PATH', json.dumps(result))
        self.assertFalse(operations.process_info(123, self.directory, 8767, self.base / 'proc')['verified'])
        (proc / 'cmdline').write_bytes(b'python3\0-m\0unrelated_module')
        self.assertFalse(operations.process_info(123, self.directory, 8766, self.base / 'proc')['verified'])

    def test_status_is_read_only_and_filters_csrf_token_and_private_data(self):
        self.configure()
        (self.directory / 'server.pid').write_text('123')
        process = {'pid': 123, 'verified': True, 'start_ticks': '9876', 'vault': str(self.vault)}
        before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.directory.rglob('*') if p.is_file()}
        with patch.object(operations, 'process_info', return_value=process), patch.object(operations, 'json_get', side_effect=[{
            'token': 'SECRET_CSRF', 'model_enabled': False, 'vault_enabled': True, 'vault_path': str(self.vault), 'private_text': 'DO_NOT_REPORT'},
            {'counts': {'todo': 1, 'working': 0}, 'usage': {'calls': 0, 'actual_usd': '0', 'unknown_cost_calls': 0, 'budget_usd': '2'}, 'items': [{'summary': 'DO_NOT_REPORT'}]}]):
            result = operations.status(self.directory, 8766)
        self.assertTrue(result['http']['ready'])
        self.assertEqual(result['counts']['todo'], 1)
        self.assertEqual(result['calls'], 0)
        self.assertEqual(result['imported_count'], 0)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('DO_NOT_REPORT', json.dumps(result))
        self.assertEqual(before, {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.directory.rglob('*') if p.is_file()})
        (self.directory / 'server-launch.json').write_text(json.dumps({'pid': 123, 'start_ticks': 'older'}))
        with patch.object(operations, 'process_info', return_value=process), patch.object(operations, 'json_get') as request:
            self.assertFalse(operations.status(self.directory, 8766)['process']['verified'])
            request.assert_not_called()

    def test_setup_only_uses_project_venv_and_exact_lock_with_explicit_cache(self):
        self.configure()
        project = self.base / 'managed-pipeline'
        (project / '.venv/bin').mkdir(parents=True)
        (project / '.venv/bin/python').write_text('fixture only')
        sibling = self.base / 'obsidian-workflow'
        sibling.mkdir()
        (sibling / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
        with patch.object(operations, 'PROJECT', project), patch.object(operations.shutil, 'which', return_value='/existing/uv'), patch.object(operations.subprocess, 'run') as run, patch.dict(os.environ, {'UV_CACHE_DIR': str(self.base / 'cache')}):
            result = operations.setup(offline=True)
        command = run.call_args_list[0].args[0]
        self.assertIn('--offline', command)
        self.assertIn(str(project / 'requirements-workbench.lock'), command)
        self.assertIn(str(project / '.venv/bin/python'), command)
        self.assertEqual(run.call_args_list[0].kwargs['env']['UV_PYTHON_DOWNLOADS'], 'never')
        self.assertEqual(result['model_calls'], 0)

    def test_temp_workspace_never_queries_global_systemd(self):
        self.configure()
        with patch.object(operations.subprocess, 'run') as run:
            self.assertIsNone(operations.systemd_info(self.directory, 8766))
        run.assert_not_called()

    def test_systemd_status_uses_actual_main_pid_instead_of_old_detached_record(self):
        self.configure()
        (self.directory / 'server.pid').write_text('123')
        (self.directory / 'server-launch.json').write_text(json.dumps({'pid':123,'start_ticks':'obsolete'}))
        unit = {'MainPID':888, 'ActiveState':'active', 'SubState':'running', 'InvocationID':'a'*32,
                'NRestarts':'0', 'Result':'success', 'UnitFileState':'enabled'}
        process = {'pid':888, 'verified':True, 'start_ticks':'new', 'vault':str(self.vault), 'model_enabled':False}
        with patch.object(operations, 'systemd_info', return_value=unit), \
                patch.object(operations, 'process_info', return_value=process) as inspect, \
                patch.object(operations, 'json_get', side_effect=[
                    {'token':'PRIVATE', 'model_enabled':False, 'vault_enabled':True, 'vault_path':str(self.vault)},
                    {'counts':{'working':0}, 'usage':{'calls':0,'actual_usd':'0','unknown_cost_calls':0,'budget_usd':'2'}}]):
            report=operations.status(self.directory,8766)
        self.assertEqual(inspect.call_args.args[0],888)
        self.assertTrue(report['http']['ready'])
        self.assertTrue(report['service']['autostart'])
        self.assertNotIn('PRIVATE',json.dumps(report))

    def test_stop_refuses_active_jobs_before_any_service_or_signal_action(self):
        self.configure()
        with patch.object(operations, 'status', return_value={'pending_jobs':{'working':1}}), \
                patch.object(operations, 'service_action') as control, patch.object(operations.os, 'kill') as kill:
            with self.assertRaisesRegex(ValueError,'Active jobs'):
                operations.stop(self.directory,8766)
        control.assert_not_called()
        kill.assert_not_called()

    def test_systemd_control_refuses_unverified_process(self):
        self.configure()
        with patch.object(operations, 'process_info', return_value={'verified':False}), \
                patch.object(operations.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError,'not this workbench'):
                operations.service_action('stop',self.directory,8766,{'MainPID':888})
        run.assert_not_called()

    def test_systemd_start_routes_to_unit_and_rejects_mode_changes(self):
        self.configure()
        arguments=['start','--workspace',str(self.directory),'--port','8766',
                   '--vault',str(self.base.parent/'personal-records-vault'),'--models','enabled',
                   '--key-file','/home/trace/.hermes/.env']
        with patch.object(operations,'systemd_info',return_value={'MainPID':0}), \
                patch.object(operations,'service_action',return_value={'service':'systemd'}) as control, \
                patch.object(operations.os,'execv') as execute, patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(operations.main(arguments),0)
            control.assert_called_once()
            execute.assert_not_called()
            with patch('sys.stderr',new=io.StringIO()):
                self.assertEqual(operations.main([value if value!='enabled' else 'disabled' for value in arguments]),2)

    def test_offline_state_read_does_not_create_wal_and_refuses_uncheckpointed_data(self):
        self.configure()
        before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.directory.rglob('*') if p.is_file()}
        self.assertEqual(operations.read_state(self.directory)['counts']['todo'], 1)
        self.assertEqual(before, {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.directory.rglob('*') if p.is_file()})
        wal = self.directory / 'state.sqlite3-wal'
        wal.write_bytes(b'uncheckpointed fixture data')
        with patch.object(operations.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(ValueError, 'Uncheckpointed WAL'):
                operations.read_state(self.directory)
            connect.assert_not_called()

    def test_no_resume_server_never_queues_saved_jobs(self):
        self.configure()
        app = MagicMock()
        app.workspace.directory = self.directory
        app.workspace.unfinished.side_effect = AssertionError('Saved jobs must not be inspected/resubmitted')
        server = MagicMock()
        server.__enter__.return_value = server
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(workbench, 'Application', return_value=app), patch.object(workbench, 'ThreadingHTTPServer', return_value=server), patch('sys.stdout', new_callable=io.StringIO):
            workbench.serve(self.directory, port=12345, call=True, vault=self.vault, resume_existing=False)
        app.submit.assert_not_called()
        app.workspace.unfinished.assert_not_called()
        app.close.assert_called_once()

class WorkbenchLauncherSmokeTests(unittest.TestCase):
    respond_selection = fixtures.NoteTests.respond_selection
    response = fixtures.NoteTests.response

    def setUp(self):
        # The real CLI enforces project/runtime. Keep only this tiny synthetic
        # fixture there, and remove it completely after its bounded child exits.
        runtime = operations.PROJECT / 'runtime'
        runtime.mkdir(exist_ok=True)
        original = fixtures.tempfile.TemporaryDirectory
        with patch.object(fixtures.tempfile, 'TemporaryDirectory', side_effect=lambda: original(prefix='launcher-fixture-', dir=runtime)):
            fixtures.NoteTests.setUp(self)

    def test_actual_launcher_exec_pid_status_and_zero_model_calls(self):
        import socket
        import subprocess
        import time
        from markdown_library import Library
        python = operations.PROJECT / '.venv/bin/python'
        if not python.is_file():
            self.skipTest('Run project-local setup before launcher smoke test')
        vault = self.base / 'vault'
        Library.initialize(vault)
        try:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
        except PermissionError:
            self.skipTest('Disposable loopback socket unavailable')
        command = [str(python), str(operations.PROJECT / 'scripts/workbench.py'), 'start',
            '--workspace', str(self.directory), '--vault', str(vault), '--port', str(port), '--models', 'disabled']
        with (self.base / 'fixture-server.log').open('wb') as output:
            process = subprocess.Popen(command, cwd=operations.PROJECT, stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 8
                report = None
                while process.poll() is None and time.monotonic() < deadline:
                    report = operations.status(self.directory, port)
                    if report['http']['ready']:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(report)
                self.assertTrue(report['process']['verified'], report)
                self.assertEqual(report['process']['pid'], process.pid)
                self.assertFalse(report['process']['resume_existing'])
                self.assertTrue(report['http']['ready'], report)
                self.assertEqual(report['vault'], str(vault))
                self.assertEqual(report['calls'], 0)
                self.assertEqual(report['counts']['todo'], 1)
                self.assertEqual(report['imported_count'], 0)
            finally:
                if process.poll() is None:
                    process.terminate()  # Only the child created by this fixture.
                process.wait(timeout=5)
        self.assertEqual(self.workspace.usage()['calls'], 0)


if __name__ == '__main__':
    unittest.main()
