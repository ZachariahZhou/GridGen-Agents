#!/usr/bin/env python3
"""Freeze or execute a new paired outer-agent cohort with private explicit oracles."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('experiments/m101_language_evidence_v1'))
    parser.add_argument('--cases', type=Path)
    parser.add_argument('--source-root', type=Path, default=Path.cwd())
    parser.add_argument('--audit-output', type=Path, help='With --report, preserve frozen grades and regrade offline in a new evaluator overlay')
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
    if args.audit_output and not args.report:
        parser.error('--audit-output requires --report')
    source = args.source_root / 'src' if args.freeze or args.audit_output else args.root.resolve() / 'source'
    sys.path.insert(0, str(source))
    from feeder_agents.benchmark import m101_language_evidence as study
    if args.freeze:
        if args.cases is None:
            parser.error('--freeze requires --cases')
        result = study.freeze(args.root, args.cases, args.source_root, args.repetitions or [0, 1])
    elif args.worker:
        study.worker(args.root, args.worker[0], args.worker[1], int(args.worker[2]))
        return
    elif args.report:
        result = study.audit_overlay(args.root, args.audit_output) if args.audit_output else study.report(args.root)
    else:
        result = study.launch(args.root, args.workers, args.scope, args.case_ids,
            args.repetitions, args.arms, args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
