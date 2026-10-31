#!/usr/bin/env python3
"""Polish frozen private game drafts in bounded batches using the existing call ledger."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from chatgpt_pipeline import game_review_polish as polish
p = argparse.ArgumentParser(description=__doc__)
s = p.add_subparsers(dest='command', required=True)
a = s.add_parser('prepare')
a.add_argument('--source-batch', required=True)
a.add_argument('--out', required=True)
a.add_argument('--batch-size', type=int, default=8)
a = s.add_parser('run')
a.add_argument('--out', required=True)
a.add_argument('--call', action='store_true')
a.add_argument('--workers', type=int, default=3)
a.add_argument('--key-file')
a = s.add_parser('export')
a.add_argument('--out', required=True)
args = p.parse_args()
if args.command == 'prepare':
 result = polish.prepare(args.source_batch, args.out, args.batch_size)
elif args.command == 'run':
 result = polish.run(args.out, call=args.call, workers=args.workers, key_file=args.key_file)
else:
 result = polish.export(args.out)
print(json.dumps(result, ensure_ascii=False, indent=2))
