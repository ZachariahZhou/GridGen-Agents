#!/usr/bin/env python3
"""Freeze, execute and summarize the real outer-agent M97 study."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path('experiments/m97_outer_agent_v2'))
    parser.add_argument('--freeze',action='store_true');parser.add_argument('--run',action='store_true');parser.add_argument('--report',action='store_true')
    parser.add_argument('--workers',type=int,default=8);parser.add_argument('--limit',type=int);parser.add_argument('--worker',nargs=3)
    args=parser.parse_args()
    if args.worker:
        sys.path.insert(0,str(args.root.resolve()/"source"))
    from feeder_agents.benchmark.m97_language_runtime import freeze,launch,worker,report
    if args.worker:worker(args.root,args.worker[0],args.worker[1],int(args.worker[2]))
    elif args.freeze:freeze(args.root)
    elif args.run:launch(args.root,args.workers,args.limit)
    elif args.report:report(args.root)
    else:parser.error('Choose --freeze, --run or --report')

if __name__=='__main__':main()
