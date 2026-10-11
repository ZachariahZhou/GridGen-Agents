"""One actionable proposal per user turn, with shared prerequisite access."""
import json

READ_PREFIXES=('describe_', 'read_', 'lookup_', 'inspect_', 'recall_', 'search_', 'list_')
PLAN_TO_EXECUTE={'plan_experiment':'execute_plan','plan_paired_study':'execute_paired_study'}

class ProposalGate:
    def __init__(self):
        self.reset()

    def reset(self):
        self.proposal=None;self.done=False;self.execution=None

    def start(self,name,args):
        if name.startswith(READ_PREFIXES):return not self.done
        if self.done:return False
        if self.proposal is None:
            self.proposal=(name,args)
            return True
        if self.execution:
            allowed=self.execution
            if name!=allowed['name']:self.done=True;return False
            if 'plan_id' in allowed:
                ok=args.get('plan_id')==allowed['plan_id']
            else:
                ok=args.get('execute',True) is True and {k:v for k,v in args.items() if k!='execute'}==allowed['args']
            if not ok:self.done=True
            return ok
        self.done=True;return False

    def finish(self,name,args,output):
        if name.startswith(READ_PREFIXES):return
        if isinstance(output,dict) and 'content' in output:output=output['content']
        if isinstance(output,str):
            try:output=json.loads(output)
            except ValueError:output={}
        if name in PLAN_TO_EXECUTE and isinstance(output,dict) and output.get('plan_id'):
            self.execution=dict(name=PLAN_TO_EXECUTE[name],plan_id=output['plan_id']);return
        if args.get('execute') is False and isinstance(output,dict) and output.get('status') in {'draft','ready'}:
            self.execution=dict(name=name,args={k:v for k,v in args.items() if k!='execute'});return
        self.done=True
