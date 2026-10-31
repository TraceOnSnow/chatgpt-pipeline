#!/usr/bin/env python3
"""Private game-review drafts from honest documentary evidence and the shared call ledger."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from chatgpt_pipeline import game_reviews

p = argparse.ArgumentParser(description=__doc__)
s = p.add_subparsers(dest='command', required=True)
a = s.add_parser('prepare', help='Offline only; verifies source SHA and freezes jobs')
a.add_argument('--manifest', required=True)
a.add_argument('--out', required=True)
a.add_argument('--workspace', required=True)
a = s.add_parser('run', help='Paid execution only with explicit --call')
a.add_argument('--out', required=True)
a.add_argument('--call', action='store_true')
a.add_argument('--workers', type=int, default=4)
a.add_argument('--key-file')
a = s.add_parser('export')
a.add_argument('--out', required=True)
args = p.parse_args()
if args.command == 'prepare':
 result = game_reviews.prepare(args.manifest, args.out, args.workspace)
elif args.command == 'run':
 result = game_reviews.run(args.out, call=args.call, workers=args.workers, key_file=args.key_file)
else:
 result = game_reviews.export(args.out)
print(json.dumps(result, ensure_ascii=False, indent=2))
