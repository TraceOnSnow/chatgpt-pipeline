"""Offline indexing and separate, explicitly bounded model experiments."""
import argparse
import json
from pathlib import Path
import sys
import sqlite3

from .index import ingest, status
from .verify import verify


def main():
    parser = argparse.ArgumentParser(description="ChatGPT export processing pipeline")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)
    paths = sub.add_parser("paths", help="Show planned storage paths without creating them")
    paths.add_argument("--data-root", type=Path, default=Path("/home/trace/data/chatgpt"))
    check = sub.add_parser("verify", help="Verify one published commit without writing")
    check.add_argument("account_root", type=Path)
    check.add_argument("commit", type=Path)
    plan = sub.add_parser("incremental-plan", help="Read-only plan of new/continued chats versus the processed baseline")
    plan.add_argument("account_root", type=Path)
    plan.add_argument("--commit", type=Path, help="Published commit; default is the one matching catalog.json")
    plan.add_argument("--workspace", type=Path, help="Workbench workspace: derive the baseline from registered batches")
    plan.add_argument("--baseline-catalog", type=Path, action="append", default=[],
                      help="Explicit processed catalog (must match a published commit); repeatable")
    plan.add_argument("--vault", type=Path, help="Markdown library root, read only for source registrations")
    plan.add_argument("--profile", type=Path, help="Selection profile used for request-size estimates")
    plan.add_argument("--since", help="Also count unprocessed user messages at or after this ISO time (with timezone)")
    plan.add_argument("--out", type=Path, help="Write the JSON plan to a new file instead of stdout")
    plan.add_argument("--summary", type=Path, help="Also write a readable Markdown summary to a new file")
    prep = sub.add_parser("incremental-prepare",
                          help="Seal a selection batch over the plan's unprocessed messages; no model call")
    prep.add_argument("account_root", type=Path)
    prep.add_argument("--out", type=Path, required=True, help="New batch directory under runtime/")
    prep.add_argument("--commit", type=Path)
    prep.add_argument("--workspace", type=Path)
    prep.add_argument("--baseline-catalog", type=Path, action="append", default=[])
    prep.add_argument("--vault", type=Path)
    prep.add_argument("--profile", type=Path, help="Selection profile, e.g. profiles/personal-memory-v1.json")
    prep.add_argument("--since")
    agent_tasks = sub.add_parser("agent-tasks",
                                 help="Export unattempted jobs of a batch (screen, then reviews) for an agent executor")
    agent_tasks.add_argument("--batch", type=Path, required=True)
    agent_tasks.add_argument("--out", type=Path, required=True)
    agent_record = sub.add_parser("agent-record", help="Validate and save an agent answer for one batch job")
    agent_record.add_argument("--batch", type=Path, required=True)
    agent_record.add_argument("--job", required=True)
    agent_record.add_argument("--response", type=Path, required=True)
    agent_record.add_argument("--executor", required=True, help="Agent and model, e.g. claude-code:claude-opus-5-5")
    agent_record.add_argument("--check", action="store_true", help="Validate only; save nothing")
    archive = sub.add_parser("archive-read", help="Read-only: find conversations by own text/date/voice, or read one")
    archive.add_argument("account_root", type=Path)
    archive.add_argument("--commit", type=Path, help="Published commit; default is the one matching catalog.json")
    archive.add_argument("--conversation", help="Read this conversation (exact ID or unique prefix of 8+ characters)")
    archive.add_argument("--since", help="Own messages at/after this date (YYYY-MM-DD in --tz) or ISO time with timezone")
    archive.add_argument("--until", help="Own messages up to this date (inclusive day) or before this ISO time")
    archive.add_argument("--tz", default="UTC", help="IANA timezone for plain dates and displayed times; default UTC")
    archive.add_argument("--keyword", action="append", default=[], help="Text in your own messages or the title; repeatable")
    archive.add_argument("--any", action="store_true", help="Match any keyword instead of all")
    archive.add_argument("--id", action="append", default=[], help="Limit to this conversation ID or prefix; repeatable")
    voice = archive.add_mutually_exclusive_group()
    voice.add_argument("--voice", dest="voice", action="store_const", const=True, help="Only conversations with voice input")
    voice.add_argument("--no-voice", dest="voice", action="store_const", const=False, help="Only conversations without voice input")
    archive.add_argument("--limit", type=int, default=30)
    archive.add_argument("--snippet-chars", type=int, default=60)
    archive.add_argument("--context", action="store_true", help="With --conversation: add truncated preceding assistant reply")
    archive.add_argument("--context-chars", type=int, default=200)
    archive.add_argument("--format", choices=["json", "markdown"], default="json")
    archive.add_argument("--out", type=Path, help="Write to a new file instead of stdout")
    covered = sub.add_parser("covered-evidence", help="Read-only: build a cover-source --source JSON for one conversation")
    covered.add_argument("account_root", type=Path)
    covered.add_argument("--commit", type=Path, help="Published commit; default is the one matching catalog.json")
    covered.add_argument("--conversation", required=True, help="Exact conversation ID or unique prefix of 8+ characters")
    covered.add_argument("--message", action="append", default=[], help="Covered message ID; repeatable; default all own messages")
    covered.add_argument("--decision", help="Why the current note already covers these messages")
    covered.add_argument("--actor", help="Who decided; cover-source defaults to manual-editorial")
    covered.add_argument("--allow-stale", action="store_true", help="Accept a body the catalog marks stale")
    covered.add_argument("--out", type=Path, help="Write to a new file instead of stdout")
    load = sub.add_parser("ingest", help="Verify all commits and build an offline index")
    load.add_argument("--exports", type=Path, default=Path("/home/trace/data/chatgpt/exports"))
    load.add_argument("--database", type=Path, required=True)
    inspect = sub.add_parser("status", help="Read index counts without changing the database")
    inspect.add_argument("--database", type=Path, required=True)
    prepare = sub.add_parser("sample-prepare", help="Prepare up to five verified samples offline")
    prepare.add_argument("--database", type=Path, required=True)
    prepare.add_argument("--selection", type=Path, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--model", required=True)
    run = sub.add_parser("sample-run", help="Explicitly call the model once per prepared sample; no retries")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--key-file", type=Path)
    run.add_argument("--call", action="store_true", help="Required to make paid model calls")
    review = sub.add_parser("sample-report", help="Audit saved responses and render a report offline")
    review.add_argument("--out", type=Path, required=True)
    bulk_prepare = sub.add_parser("bulk-prepare", help="Prepare a finite verified archive snapshot offline")
    bulk_prepare.add_argument("--database", type=Path, required=True)
    bulk_prepare.add_argument("--out", type=Path, required=True)
    bulk_run = sub.add_parser("bulk-run", help="Run/resume bounded archive selection without repeating attempted calls")
    bulk_run.add_argument("--out", type=Path, required=True)
    bulk_run.add_argument("--key-file", type=Path)
    bulk_run.add_argument("--workers", type=int, default=12)
    bulk_run.add_argument("--budget-usd", default="5")
    bulk_run.add_argument("--call", action="store_true")
    bulk_export = sub.add_parser("bulk-export", help="Export readable selections and recovery status offline")
    bulk_export.add_argument("--out", type=Path, required=True)
    bulk_config = sub.add_parser("bulk-reconfigure", help="Version unsent requests with explicit reasoning=none; no calls")
    bulk_config.add_argument("--out", type=Path, required=True)
    notes_init = sub.add_parser("notes-init", help="Import accepted selections into a durable inbox offline")
    notes_init.add_argument("--batch", type=Path, required=True)
    notes_init.add_argument("--workspace", type=Path, required=True)
    notes_run = sub.add_parser("notes-process", help="Process one explicitly chosen item; at most two calls, no retries")
    notes_run.add_argument("--workspace", type=Path, required=True)
    notes_run.add_argument("--item", required=True)
    notes_run.add_argument("--key-file", type=Path)
    notes_run.add_argument("--call", action="store_true")
    notes_accept = sub.add_parser("notes-accept-calibration", help="Accept an explicitly reviewed note offline, preserving model history")
    notes_accept.add_argument("--workspace", type=Path, required=True)
    notes_accept.add_argument("--job", required=True)
    notes_accept.add_argument("--review-file", type=Path, required=True)
    notes_revise = sub.add_parser("notes-revise", help="Apply an explicit offline note revision, retaining the previous version")
    notes_revise.add_argument("--workspace", type=Path, required=True)
    notes_revise.add_argument("--job", required=True)
    notes_revise.add_argument("--review-file", type=Path, required=True)
    notes_recover = sub.add_parser("notes-recover", help="Plan or explicitly recover only selected failed notes; preserve all attempts")
    notes_recover.add_argument("--workspace", type=Path, required=True)
    recover_choice = notes_recover.add_mutually_exclusive_group(required=True)
    recover_choice.add_argument("--job", action="append", help="Explicit failed job ID, repeatable")
    recover_choice.add_argument("--attention", action="store_true", help="Select the current finite failed set")
    notes_recover.add_argument("--limit", type=int, default=40, help="Refuse a larger batch; maximum 100")
    notes_recover.add_argument("--workers", type=int, default=1, help="Explicit concurrency, 1 to 4; default 1")
    notes_recover.add_argument("--structured", action="store_true", help="Use the separately recorded strict-schema recovery revision")
    notes_recover.add_argument("--recovery-revision", choices=["recovery-v1", "recovery-schema-v2", "recovery-code-v3", "recovery-body-v4", "recovery-provider-v5"],
                               help="Inspect/resume an exact saved recovery contract")
    notes_recover.add_argument("--key-file", type=Path)
    notes_recover.add_argument("--apply", action="store_true", help="Execute this recovery plan; otherwise read only")
    notes_recover.add_argument("--call", action="store_true", help="Permit bounded calls within the existing workspace budget")
    notes_audit = sub.add_parser("notes-quarantine-empty", help="Recheck only an unimported generated empty-body note; archive rather than overwrite")
    notes_audit.add_argument("--workspace", type=Path, required=True)
    notes_audit.add_argument("--job", required=True)
    notes_audit.add_argument("--apply", action="store_true")
    workbench = sub.add_parser("workbench-serve", help="Private local checklist and Markdown downloads")
    workbench.add_argument("--workspace", type=Path, required=True)
    workbench.add_argument("--port", type=int, default=8766)
    workbench.add_argument("--key-file", type=Path)
    workbench.add_argument("--call", action="store_true")
    workbench.add_argument("--vault", type=Path, help="Explicitly initialized Markdown library root; no automatic creation")
    workbench.add_argument("--no-resume", action="store_true", help="Do not resume saved jobs at server startup; model actions remain explicit")
    claude_code = sub.add_parser("claude-code-messages",
                                 help="Read-only: extract the user's own Claude Code messages; no model, Vault or workbench writes")
    claude_code.add_argument("--dir", type=Path, default=Path("/home/trace/.claude/projects/-home-trace-projects"),
                             help="Project session directory; only its top-level *.jsonl files are read")
    claude_code.add_argument("--out", type=Path, required=True, help="New output file; existing files are never overwritten")
    claude_code.add_argument("--format", choices=["jsonl", "json"], default="jsonl")
    claude_code.add_argument("--since", help="Inclusive; YYYY-MM-DD (Asia/Shanghai day) or ISO 8601, naive = Asia/Shanghai")
    claude_code.add_argument("--until", help="Exclusive; a YYYY-MM-DD value includes that whole Asia/Shanghai day")
    claude_code.add_argument("--segments", type=Path, help="Also write a new JSON summary per (session, Asia/Shanghai date)")
    claude_code.add_argument("--no-context", action="store_true", help="Omit the previous assistant reply tail")
    claude_code.add_argument("--library-writes", action="store_true",
                             help="Add library_writes_heuristic: library CLI/MCP and Vault Edit/Write calls after each message")
    claude_code.add_argument("--dictation", action="store_true",
                             help="Add the deterministic dictation_candidate flag (rule constants in claude_code.py)")
    claude_code.add_argument("--uncovered-dictation", type=Path,
                             help="Also write a new JSON list of dictation candidates with no applied library write")
    args = parser.parse_args()
    if args.command == "paths":
        root = args.data_root.expanduser().resolve()
        print(json.dumps({"read_only_exports": str(root / "exports"),
                          "read_only_handoff": str(root / "handoff"),
                          "planned_database": str(root / "processed/index.sqlite3"),
                          "planned_results": str(root / "processed/results"),
                          "planned_logs": str(root / "processed/logs"),
                          "directories_created": False}, ensure_ascii=False, indent=2))
        return
    try:
        if args.command == "verify":
            result = verify(args.account_root, args.commit)
            output = {"verified": True, "account_key": result["account_key"],
                      "commit_sha256": result["commit_sha256"], "catalog_sha256": result["catalog_sha256"],
                      "objects": len(result["packages"]), "conversations": len(result["catalog"]["conversations"])}
        elif args.command == "incremental-plan":
            from .incremental import dumps, plan as incremental_plan, summary_text, write_private
            from .verify import require
            require(args.workspace or args.baseline_catalog, "Pass --workspace to derive a baseline or --baseline-catalog")
            require(args.out is None or args.summary is None or args.out.resolve() != args.summary.resolve(),
                    "--out and --summary must be different files")
            for target in (args.out, args.summary):
                require(target is None or not target.exists(), "Output exists; existing files are never replaced")
            result = incremental_plan(args.account_root, args.commit, workspace=args.workspace,
                                      baseline_catalogs=args.baseline_catalog, vault=args.vault,
                                      profile_path=args.profile, since=args.since)
            if args.out is None:
                sys.stdout.buffer.write(dumps(result))
                return
            written = {"plan": write_private(args.out, dumps(result))}
            if args.summary:
                written["summary"] = write_private(args.summary, summary_text(result).encode())
            output = {"written": written, "counts": result["counts"], "estimate": {
                k: v for k, v in result["estimate"].items() if k not in ("basis", "notes_ledger")}}
        elif args.command == "incremental-prepare":
            from .incremental import prepare_batch
            from .verify import require
            require(args.workspace or args.baseline_catalog, "Pass --workspace to derive a baseline or --baseline-catalog")
            output = prepare_batch(args.account_root, args.out, workspace=args.workspace,
                                   baseline_catalogs=args.baseline_catalog, vault=args.vault,
                                   profile_path=args.profile, since=args.since, commit=args.commit)
        elif args.command == "agent-tasks":
            from .agent_exec import tasks
            output = tasks(args.batch, args.out)
        elif args.command == "agent-record":
            from .agent_exec import record
            output = record(args.batch, args.job, args.response, args.executor, check_only=args.check)
        elif args.command in ("archive-read", "covered-evidence"):
            from . import archive
            from .incremental import dumps, write_private
            from .verify import require
            require(args.out is None or not args.out.exists(), "Output exists; existing files are never replaced")
            if args.command == "covered-evidence":
                result = archive.covered_evidence(args.account_root, args.commit, conversation=args.conversation,
                                                  message_ids=args.message, decision=args.decision,
                                                  actor=args.actor, allow_stale=args.allow_stale)
                data = dumps(result)
                summary = {"conversation_id": result["conversation_id"], "evidence": len(result["evidence"]),
                           "roles": sorted({e["role"] for e in result["evidence"]})}
            else:
                if args.conversation:
                    result = archive.read(args.account_root, args.commit, conversation=args.conversation,
                                          tz=args.tz, context=args.context, context_chars=args.context_chars)
                    summary = {"conversation_id": result["source"]["conversation_id"],
                               "user_messages": result["user_messages"], "voice_messages": result["voice_messages"]}
                    render = archive.read_markdown
                else:
                    result = archive.search(args.account_root, args.commit, since=args.since, until=args.until,
                                            tz=args.tz, keywords=args.keyword, any_keyword=args.any,
                                            conversation_ids=args.id, voice=args.voice, limit=args.limit,
                                            snippet_chars=args.snippet_chars)
                    summary = {"matched": result["matched"], "returned": result["returned"]}
                    render = archive.search_markdown
                data = render(result).encode() if args.format == "markdown" else dumps(result)
            if args.out is None:
                sys.stdout.buffer.write(data)
                return
            output = {"written": write_private(args.out, data), **summary}
        elif args.command == "ingest":
            output = ingest(args.exports, args.database)
        elif args.command == "status":
            output = status(args.database)
        elif args.command == "sample-prepare":
            from .samples import prepare
            output = prepare(args.database, args.selection, args.out, args.model)
        elif args.command == "sample-report":
            from .review import report
            output = report(args.out)
        elif args.command == "bulk-prepare":
            from .bulk import prepare
            output = prepare(args.database, args.out)
        elif args.command == "bulk-export":
            from .bulk import export
            output = export(args.out)
        elif args.command == "bulk-reconfigure":
            from .bulk import reconfigure_pending
            output = reconfigure_pending(args.out)
        elif args.command == "notes-init":
            from .notes import initialize
            output = initialize(args.batch, args.workspace)
        elif args.command == "notes-process":
            if not args.call:
                raise ValueError("notes-process requires --call; no request sent")
            from .notes import Workspace
            workspace = Workspace(args.workspace)
            job = workspace.request(args.item)
            workspace.recover_format(job["id"])
            output = {"job": workspace.run(job["id"], args.key_file), "usage": workspace.usage()}
        elif args.command == "notes-accept-calibration":
            from .notes import Workspace
            output = Workspace(args.workspace).accept_calibration(args.job, args.review_file)
        elif args.command == "notes-revise":
            from .notes import Workspace
            output = Workspace(args.workspace).revise_note(args.job, args.review_file)
        elif args.command == "notes-quarantine-empty":
            from .notes import Workspace
            from .note_recovery import Recovery
            output = Recovery(Workspace(args.workspace)).quarantine_empty(args.job, apply=args.apply)
        elif args.command == "notes-recover":
            from .notes import Workspace
            from .note_recovery import Recovery, StructuredRecovery, CodeSafeRecovery, CompleteRecovery, RoutedRecovery
            from .verify import require
            workspace = Workspace(args.workspace)
            revisions = {cls.revision: cls for cls in (Recovery, StructuredRecovery, CodeSafeRecovery, CompleteRecovery, RoutedRecovery)}
            recovery = revisions[args.recovery_revision](workspace) if args.recovery_revision else (
                CompleteRecovery if args.structured else Recovery)(workspace)
            require(1 <= args.limit <= 100, "Recovery batch limit must be between 1 and 100")
            require(1 <= args.workers <= 4, "Recovery workers must be between 1 and 4")
            if args.attention:
                with workspace.db() as db:
                    selected = [r[0] for r in db.execute("SELECT id FROM jobs WHERE state='attention' ORDER BY created_at")]
            else:
                selected = list(dict.fromkeys(args.job))
            require(len(selected) <= args.limit, "Recovery batch exceeds explicit limit; no requests sent")
            def recover_selected(job_id):
                plan = {"job_id": job_id}
                try:
                    plan = recovery.plan(job_id)
                    if args.apply and plan["eligible"]:
                        recovery.prepare(job_id, allow_calls=args.call)
                        plan["result"] = recovery.run(job_id, args.key_file)
                    return plan
                except (ValueError, OSError, KeyError, TypeError) as error:
                    return {**plan, "eligible": False,
                            "reason": str(error) if isinstance(error, ValueError) else type(error).__name__}
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                plans = list(executor.map(recover_selected, selected))
            output = {"applied": args.apply, "plans": plans, "usage": workspace.usage()}
        elif args.command == "claude-code-messages":
            from .claude_code import run as extract_claude_code
            output = extract_claude_code(args.dir, args.out, args.format, args.since, args.until,
                                         args.segments, not args.no_context, args.library_writes,
                                         args.dictation, args.uncovered_dictation)
        elif args.command == "workbench-serve":
            from .workbench import serve
            serve(args.workspace, args.port, args.key_file, args.call, args.vault, not args.no_resume)
            return
        elif args.command == "bulk-run":
            if not args.call:
                raise ValueError("bulk-run requires --call; no model request was made")
            from .bulk import run
            output = run(args.out, args.key_file, args.workers, args.budget_usd)
        else:
            if not args.call:
                raise ValueError("sample-run requires --call; no model request was made")
            from .samples import run
            output = run(args.out, args.key_file)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.command == "ingest" and (not output["accounts"] or any(a["errors"] for a in output["accounts"])):
            sys.exit(2)
        if args.command == "sample-run" and output["validated"] != output["attempted"]:
            sys.exit(2)
        if args.command == "notes-process" and output["job"]["state"] != "done":
            sys.exit(2)
        if args.command == "notes-recover" and args.apply and any(p.get("result", {}).get("state") != "done" for p in output["plans"]):
            sys.exit(2)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({"error": type(error).__name__, "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
