#!/usr/bin/env python3
"""Freeze or replay M97 cases against a new, immutable M98 source snapshot."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('experiments/m98_outer_retest'))
    parser.add_argument('--paired-root', type=Path, default=Path('experiments/m97_outer_agent_v2'))
    parser.add_argument('--source-root', type=Path, default=Path.cwd())
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--freeze', action='store_true')
    action.add_argument('--run', action='store_true')
    action.add_argument('--report', action='store_true')
    action.add_argument('--worker', nargs=3, metavar=('CASE', 'ARM', 'REPETITION'))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--scope', choices=['all', 'positives', 'negatives'], default='all')
    parser.add_argument('--case-ids', nargs='+')
    parser.add_argument('--repetitions', type=int, nargs='+')
    parser.add_argument('--arms', nargs='+', choices=['full_outer', 'one_proposal'])
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'src' if args.freeze else args.root.resolve() / 'source'
    sys.path.insert(0, str(source))
    from feeder_agents.benchmark import m98_outer_retest as replay
    if args.freeze:
        result = replay.freeze(args.root, args.paired_root, args.source_root)
    elif args.worker:
        replay.worker(args.root, args.worker[0], args.worker[1], int(args.worker[2]))
        return
    elif args.report:
        result = replay.report(args.root)
    else:
        result = replay.launch(args.root, args.workers, args.scope, args.case_ids,
                               args.repetitions, args.arms, args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
