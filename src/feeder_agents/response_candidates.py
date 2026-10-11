"""Preserve spatial/electrical action directions before amplitude variants."""
from itertools import zip_longest


def direction_key(action):
    if action['kind']=='compound':
        return ('compound',tuple(direction_key(a) for a in action['actions']))
    replacements=tuple(sorted((a['line_id'],a['conductor']) for a in action.get('replacements',[])))
    return (action['kind'],action.get('child_bus'),
            action.get('line_id'),action.get('parent_bus'),action.get('index'),
            action.get('donor'),action.get('receiver'),replacements,
            (1 if action['factor']>1 else -1) if 'factor' in action else None)


def diverse_directions(actions):
    """Retain ranked order within each direction and interleave directions."""
    groups={}
    for action in actions:groups.setdefault(direction_key(action),[]).append(action)
    return [action for group in zip_longest(*groups.values()) for action in group if action is not None]
