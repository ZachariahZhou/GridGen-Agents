import argparse
import json
import re
from pathlib import Path

import yaml

from .agent import agent_session
from .design import design_from_request
from .memory import MemoryStore
from .reporting import verified_answer
from .rules import load_rules, search_sources
from .schemas import ExperimentSpec, StudyPlan
from .studies import run_study
from .workflow import run_experiment


def main():
    parser = argparse.ArgumentParser(description='Feeder Agents: synthetic research cases')
    parser.add_argument('--workspace', type=Path, default=Path('workspace'))
    sub = parser.add_subparsers(dest='command', required=True)
    benchmark=sub.add_parser('benchmark',help='Run independent requirements against shared-generation baselines')
    benchmark.add_argument('--suite',type=Path,required=True)
    benchmark.add_argument('--id',required=True)
    benchmark.add_argument('--allow-llm',action='store_true',help='Enable configured .env model adapters')
    hierarchy = sub.add_parser('hierarchy', help='Generate MV-transformer-LV-customer research cases')
    hierarchy.add_argument('--spec', type=Path, required=True)
    hierarchy.add_argument('--id', required=True)
    hierarchy.add_argument('--agent-feedback',action='store_true',help='Use configured LLM to diagnose and repair failed candidates')
    hierarchy.add_argument('--feedback-rounds',type=int,default=3)
    hierarchy.add_argument('--local-topology',action='store_true',help='Authorize nearby same-transformer customer service reconnection; enables Agent feedback')
    transmission=sub.add_parser('transmission',help='Generate balanced AC transmission research cases')
    transmission.add_argument('--spec',type=Path,required=True)
    transmission.add_argument('--id',required=True)
    transmission.add_argument('--agent-feedback',action='store_true')
    transmission.add_argument('--feedback-rounds',type=int,default=4)
    hierarchy_inverse=sub.add_parser('hierarchy-inverse',help='Multi-voltage target design with guarded local edits')
    hierarchy_inverse.add_argument('--plan',type=Path,required=True)
    hierarchy_inverse.add_argument('--id',required=True)
    design = sub.add_parser('design', help='Design directly from natural-language requirements')
    design.add_argument('request')
    design.add_argument('--id', required=True)
    design.add_argument('--project',default='default')
    design.add_argument('--nodes',type=int,default=None,help='Optional total buses including one source; omit for automatic/default size')
    design.add_argument('--draft',action='store_true')
    design.add_argument('--local-topology',action='store_true',help='Allow bounded same-transformer LV service reconnection unless the request freezes topology')
    design.add_argument('--workers',type=int,default=1)
    design.add_argument('--planning-memory',choices=['learn','read_only','off'],default='learn',
                        help='Learn project diagnostic experience, read a frozen snapshot, or disable planning memory')
    design.add_argument('--planning-memory-path',type=Path,help='Optional project memory database or frozen snapshot')
    revision = sub.add_parser('revise', help='Versioned feedback revision of an existing experiment')
    revision.add_argument('--parent',required=True)
    revision.add_argument('--id',required=True)
    revision.add_argument('--sample',type=int,default=0)
    revision.add_argument('--feedback',help='Complete natural-language feedback; uses configured .env model')
    revision.add_argument('--plan',type=Path,help='Structured YAML/JSON RevisionPlan; no model required')
    revision.add_argument('--draft',action='store_true')
    generate = sub.add_parser('generate', help='Deterministic generation; no API key needed')
    generate.add_argument('--spec', type=Path, required=True)
    generate.add_argument('--id', required=True)
    generate.add_argument('--workers', type=int, default=1)
    generate.add_argument('--sample-timeout', type=float, default=900, help='Seconds per executing sample (runtime policy, default 900)')
    generate.add_argument('--batch-timeout', type=float, help='Optional total batch execution deadline in seconds')
    generate.add_argument('--cancel-file', type=Path, help='Stop owned batch workers when this file exists')
    generate.add_argument('--resume-interrupted', action='store_true', help='Explicitly retry interrupted samples after archiving their earlier evidence')
    study = sub.add_parser('study', help='Paired PV/load-factor study on frozen networks')
    study.add_argument('--plan', type=Path, required=True)
    study.add_argument('--id', required=True)
    study.add_argument('--workers',type=int,default=1)
    inverse = sub.add_parser('inverse',help='Search a fixed-network multi-condition design objective')
    inverse.add_argument('--plan',type=Path,required=True)
    inverse.add_argument('--id',required=True)
    inverse.add_argument('--workers',type=int,default=1)
    chat = sub.add_parser('chat', help='LangChain natural-language Agent; configured model required')
    chat.add_argument('prompt')
    chat.add_argument('--project', default='default')
    chat.add_argument('--thread', default='default')
    chat.add_argument('--workers', type=int, default=1)
    references = sub.add_parser('references', help='List or run pinned MATPOWER distribution references')
    references.add_argument('--case')
    references.add_argument('--id')
    references.add_argument('--voltage',type=float)
    references.add_argument('--load-scale',type=float,default=1)
    references.add_argument('--evidence',choices=['any','benchmark','actual_derived'],default='any')
    sub.add_parser('styles',help='List executable topology families and composition constraints')
    rules = sub.add_parser('rules')
    rules.add_argument('--query', default='')
    args = parser.parse_args()
    try:
        if args.command=='benchmark':
            from .benchmark.runner import run_benchmark
            suite_payload=yaml.safe_load(args.suite.read_text())
            if suite_payload.get('split')=='heldout' or args.suite.with_suffix('.freeze.json').exists():
                from .benchmark.freeze import verify_freeze
                verify_freeze(args.suite)
            result=run_benchmark(suite_payload,args.workspace,args.id,allow_llm=args.allow_llm)
            output={k:v for k,v in result.items() if k!='rows'}
        elif args.command=='transmission':
            from .transmission import run_transmission
            output=run_transmission(yaml.safe_load(args.spec.read_text()),args.workspace,args.id,agent_feedback=args.agent_feedback,feedback_rounds=args.feedback_rounds)
        elif args.command=='hierarchy-inverse':
            from .hierarchy_inverse import run_hierarchy_inverse
            output=run_hierarchy_inverse(yaml.safe_load(args.plan.read_text()),args.workspace,args.id)
        elif args.command == 'hierarchy':
            from .hierarchy_workflow import run_hierarchy
            output=run_hierarchy(yaml.safe_load(args.spec.read_text()),args.workspace,args.id,agent_feedback=args.agent_feedback,feedback_rounds=args.feedback_rounds,local_topology=args.local_topology)
        elif args.command == 'design':
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',args.project):
                raise ValueError('Invalid project_id')
            output = design_from_request(args.request,args.workspace/'projects'/args.project,args.id,
                                         execute=not args.draft,workers=args.workers,node_count=args.nodes,local_topology=args.local_topology,normalize_requirements=True,planning_mode='adaptive',
                                         planning_memory_mode=args.planning_memory,planning_memory_path=args.planning_memory_path)
        elif args.command == 'revise':
            from .revisions import revise_case
            if bool(args.feedback)==bool(args.plan):
                raise ValueError('Provide exactly one of --feedback or --plan')
            plan=yaml.safe_load(args.plan.read_text(encoding='utf-8')) if args.plan else None
            output=revise_case(args.workspace,args.parent,args.id,args.sample,
                               feedback=args.feedback,plan=plan,execute=not args.draft)
        elif args.command == 'generate':
            spec = ExperimentSpec.model_validate(yaml.safe_load(args.spec.read_text(encoding='utf-8')))
            result = run_experiment(spec, args.workspace, args.id, args.workers, execution_limits=dict(
                sample_timeout_seconds=args.sample_timeout, batch_timeout_seconds=args.batch_timeout,
                cancel_file=args.cancel_file, resume_interrupted=args.resume_interrupted))
            output = {k: v for k, v in result.items() if k != 'samples'}
            output['directory'] = str(args.workspace.resolve() / 'experiments' / args.id)
        elif args.command == 'study':
            plan = StudyPlan.model_validate(yaml.safe_load(args.plan.read_text(encoding='utf-8')))
            result = run_study(plan,args.workspace,args.id,args.workers)
            output = {k:v for k,v in result.items() if k!='samples'}
        elif args.command == 'inverse':
            from .inverse import InverseDesignPlan,run_inverse_design
            plan=InverseDesignPlan.model_validate(yaml.safe_load(args.plan.read_text(encoding='utf-8')))
            output=run_inverse_design(plan,args.workspace,args.id,args.workers)
        elif args.command == 'references':
            from .references import list_references,run_reference_case
            if args.case:
                if not args.id: raise ValueError('--id is required when running a reference')
                output=run_reference_case(args.case,args.workspace,args.id,args.load_scale)
            else:
                output={'cases':list_references(args.voltage,evidence_class=args.evidence)}
        elif args.command == 'styles':
            from .rules import data
            output=data('topology_styles.json')
        elif args.command == 'rules':
            output = {'sources': search_sources(args.query), 'rules': load_rules()}
        else:
            with agent_session(args.workspace, args.project, workers=args.workers) as agent:
                result = agent.invoke({'messages': [{'role': 'user', 'content': args.prompt}]},
                                      {'configurable': {'thread_id': args.thread}, 'recursion_limit': 64})
                output = {'answer': verified_answer(result['messages'])}
        print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        parser.exit(1, f'{type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    main()
