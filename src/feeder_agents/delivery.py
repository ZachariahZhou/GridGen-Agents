"""Shared normal-network delivery entry point for the app and end-to-end studies."""
from .delivery_recovery import generate,recover_delivery


def execute_delivery(brief,root,run_id,request,model,*,feedback=True,recovery=True,
                     rounds=None,recovery_rounds=2,memory=True,local_topology=False,workers=1):
    from .planning_memory import PlanningMemory
    store=None;memory_report=None
    metadata=brief.get('planning_memory')
    if memory and metadata:
        try:store=PlanningMemory(metadata['path'],mode=metadata['mode'])
        except Exception as exc:memory_report=dict(error=str(exc),promoted_episodes=0)
    options=dict(feedback=feedback,rounds=rounds,memory=memory)
    if brief['plan_type']=='feeder':options['workers']=workers
    output=generate(brief,root,run_id,model,request,local_topology,**options)
    initial_directory=output['directory']
    if recovery and 'requirement_ledger' in brief:
        result=recover_delivery(brief,output,request,root,run_id,model,local_topology,
                                max_rounds=recovery_rounds,generation_options=options,planning_memory=store)
    else:
        result=dict(brief=brief,output=output,trace=None)
    if store is not None:
        selected=result['brief'];selected_memory=selected.get('planning_memory')
        if selected_memory:
            try:
                promoted=store.verify_delivery(selected_memory['planner_root'],selected,result['output'])
                memory_report=dict(promoted_episodes=promoted,mode=store.mode)
            except Exception as exc:memory_report=dict(error=str(exc),promoted_episodes=0)
    return dict(result,initial_directory=initial_directory,planning_memory=memory_report)
