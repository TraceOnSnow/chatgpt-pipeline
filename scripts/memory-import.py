#!/usr/bin/env python3
"""Explicit offline preparation and bounded execution for an official ChatGPT ZIP."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from chatgpt_pipeline import bulk, memory

parser = argparse.ArgumentParser(description=__doc__)
commands = parser.add_subparsers(dest="command", required=True)
p = commands.add_parser("prepare", help="Offline only: freeze sources, profile, chunks and gap report")
p.add_argument("--zip", required=True, dest="zip_path")
p.add_argument("--out", required=True)
p.add_argument("--profile", required=True)
p.add_argument("--account-key", required=True)
p.add_argument("--index", help="Existing read-only pipeline index")
p.add_argument("--include-existing", action="store_true", help="Explicitly screen identical same-account historical content too")
p = commands.add_parser("run", help="Paid bounded run; existing attempts are never resent")
p.add_argument("--out", required=True)
p.add_argument("--key-file")
p.add_argument("--workers", type=int, default=8)
p.add_argument("--budget-usd", required=True)
p.add_argument("--call", action="store_true", help="Explicitly permit paid calls within the saved batch budget")
p = commands.add_parser("export")
p.add_argument("--out", required=True)
p = commands.add_parser("notes-init", help="Initialize the chosen draft workspace after accepted selection")
p.add_argument("--out", required=True)
p.add_argument("--workspace", required=True)
args = parser.parse_args()
if args.command == "prepare":
    result = memory.prepare(args.zip_path, args.out, args.profile, args.account_key, args.index, not args.include_existing)
elif args.command == "run":
    if not args.call:
        parser.error("run requires --call; no request sent")
    result = bulk.run(args.out, args.key_file, args.workers, args.budget_usd)
elif args.command == "export":
    result = bulk.export(args.out)
else:
    result = memory.initialize(args.out, args.workspace)
print(json.dumps(result, ensure_ascii=False, indent=2))
