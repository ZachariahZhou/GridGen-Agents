"""Process-local numerical ablations; no task answers or private witnesses."""
from contextlib import contextmanager


def atomic_edit_count(action):
    if action['kind']=='compound':
        return sum(atomic_edit_count(a) for a in action['actions'])
    if action['kind']=='replace_conductor':
        return len(action['replacements']) if 'replacements' in action else 1
    return 1


def filter_single_edits(actions):
    return [a for a in actions if atomic_edit_count(a)==1]


@contextmanager
def proposal_adapter(arm,seed=None):
    import feeder_agents.response_design as runner
    from ..response_baselines import random_legal_proposer
    if arm not in {'full','fixed','single','random'}:raise ValueError('Unknown response benchmark arm')
    original=runner.propose_response_actions
    if arm=='random':
        if seed is None:raise ValueError('Random arm requires an independent proposal seed')
        replacement=random_legal_proposer(seed)
    elif arm=='single':
        def replacement(*args,**kwargs):
            return original(*args,**dict(kwargs,candidate_filter=lambda a:atomic_edit_count(a)==1))
    else:replacement=original
    runner.propose_response_actions=replacement
    try:yield
    finally:runner.propose_response_actions=original
