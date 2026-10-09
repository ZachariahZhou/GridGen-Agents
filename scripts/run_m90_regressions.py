"""Replay one packaged structured contract with the current snapshot."""
import argparse,json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
parser=argparse.ArgumentParser();parser.add_argument('--list',action='store_true');parser.add_argument('--case');parser.add_argument('--output',type=Path,default=root/'workspace/m90_replay')
a=parser.parse_args();jobs=json.loads((root/'examples/m90_regression_tasks.json').read_text())['jobs']
if a.list:
    print('\n'.join(j['id'] for j in jobs));sys.exit(0)
if not a.case:parser.error('--case or --list is required')
job=next((j for j in jobs if j['id']==a.case),None)
if job is None:parser.error('Unknown case; use --list')
if job['kind']=='research_task':
    from feeder_agents.research_tasks import run_research_task as run
else:
    from feeder_agents.response_design import run_response_design as run
r=run(job['plan'],a.output,job['id'])
print(json.dumps({k:r[k] for k in ('status','target_met','verification_passed','protected_contract_preserved','directory')},indent=2))
