"""Independent candidate sampler for explicitly restricted geometry experiments."""
import random
from types import SimpleNamespace

from .artifacts import digest
from .response_geometry import compensation_scale_interval


def draw_legal_primitive(state,original,spec,search,kind,rng):
    """Target-independent draw; complete-candidate contract screening follows.

    Family is supplied by the sampler. Catalog draws mix 90% cap-sized batches
    with 10% arbitrary-size batches, so all production batch sizes have support.
    Geometry amplitudes are uniform in the analytic original-reference interval.
    """
    if kind=='replace_conductor':
        from .response_distribution import compatible_conductors
        from .rules import data
        catalogue=data('conductors.json')
        options=[(e,sorted(n for n in compatible_conductors(e,spec,catalogue) if n!=e.conductor))
            for e in sorted(state['feeder'].lines,key=lambda e:e.id)]
        options=[(e,names) for e,names in options if names]
        if not options:return None
        bounded=min(len(options),search.max_changed_conductors or len(options))
        upper=len(options) if rng.choice(['bounded']*9+['all'])=='all' else bounded
        count=rng.choice(list(range(1,upper+1)))
        moves=[dict(line_id=e.id,conductor=rng.choice(names)) for e,names in rng.sample(options,count)]
        return dict(kind=kind,**moves[0]) if count==1 else dict(kind=kind,replacements=moves)
    if kind not in {'scale_layout','scale_subtree'}:raise ValueError('Unsupported random primitive')
    move=dict(kind=kind)
    if kind=='scale_subtree':
        children=sorted(b.id for b in state['feeder'].buses if b.id!=state['feeder'].source_bus)
        if not children:return None
        move['child_bus']=rng.choice(children)
    interval=compensation_scale_interval(state,original,spec,search,move)
    if interval['empty']:return None
    move['factor']=rng.uniform(interval['lower'],interval['upper'])
    if kind=='scale_layout':
        move['original_scale']=state['feeder'].lines[0].length_km/original['feeder'].lines[0].length_km*move['factor']
    return move


def random_legal_proposer(seed):
    """Independent geometry/catalog proposal grammar with shared AC acceptance.

    No assessment, target or proxy ranking enters sampling. Families and one/two
    primitive moves are uniform; batch sizes use the documented mixture above.
    This is not uniform over physical grids. Rejection and deduplication alter
    returned frequencies; their counts are retained outside the AC budget.
    """
    rng=random.Random(seed)
    def propose(model,spec,probes,assessment,search,original=None,audit=None,seen_physical=None,**kwargs):
        from .response_design import apply_response_action,research_contract_preserved
        from .response_search import physical_fingerprint
        if original is None or model['kind']!='distribution':raise ValueError('Random proposals require an original distribution model')
        if not set(search.allowed_actions)<={'scale_layout','scale_subtree','replace_conductor'}:
            raise ValueError('Random proposer supports only the frozen G/GE spaces')
        contract=SimpleNamespace(search=search,base_spec=spec)
        options=[];physical=set(seen_physical or ())
        physical.add(physical_fingerprint(model))
        for _ in range(search.candidates_per_round*200):
            if not search.allowed_actions:break
            state=model;moves=[]
            for __ in range(rng.randint(1,search.max_joint_actions)):
                move=draw_legal_primitive(state,original,spec,search,rng.choice(search.allowed_actions),rng)
                if move is None:break
                state=apply_response_action(state,spec,move);moves.append(move)
            if not moves:continue
            action=moves[0] if len(moves)==1 else dict(kind='compound',actions=moves)
            valid=research_contract_preserved(original,state,contract)
            fingerprint=physical_fingerprint(state) if valid else None
            reason='random_contract_rejected' if not valid else 'duplicate_before_preview' if fingerprint in physical else 'random_legal'
            if audit is not None:audit.append(dict(event='random_draw',action=action,eligible_for_ac=reason=='random_legal',reason=reason))
            if reason!='random_legal':continue
            physical.add(fingerprint)
            options.append(dict(action,action_id=digest(action)[:16],diagnosis='Independent random geometry/catalog proposal; no response-guided sampling'))
            if len(options)>=search.candidates_per_round:break
        return options
    return propose


def random_geometry_proposer(seed):
    """Sample legal directions/amplitudes without a response proxy or target.

    Draw uniform family, non-source subtree, analytic scale, and move count.
    Rejection filtering changes the returned distribution; an invalid second
    move leaves a valid first move available. This is an independent random
    proposal distribution, NOT uniform over physical grid models. The shared
    runner supplies identical AC acceptance, incumbent/archive policy and caps.
    """
    rng=random.Random(seed)

    def propose(model,spec,probes,assessment,search,original=None,audit=None,seen_physical=None,**kwargs):
        from .response_design import apply_response_action,research_contract_preserved
        from .response_search import physical_fingerprint
        if original is None or model['kind']!='distribution':raise ValueError('Random geometry requires an original distribution model')
        if not set(search.allowed_actions)<={'scale_layout','scale_subtree'}:
            raise ValueError('Random geometry baseline requires the common geometry-only action space')
        contract=SimpleNamespace(search=search,base_spec=spec)
        children=sorted(b.id for b in model['feeder'].buses if b.id!=model['feeder'].source_bus)
        options=[];seen=set();physical=set(seen_physical or ())
        physical.add(physical_fingerprint(model))
        for _ in range(search.candidates_per_round*200):
            if not search.allowed_actions:break
            state=model;moves=[]
            for __ in range(rng.randint(1,search.max_joint_actions)):
                kind=rng.choice(search.allowed_actions)
                move=dict(kind=kind)
                if kind=='scale_subtree':move['child_bus']=rng.choice(children)
                interval=compensation_scale_interval(state,original,spec,search,move)
                if interval['empty']:
                    if audit is not None:audit.append(dict(event='random_draw',action=dict(kind='compound',actions=moves+[move]),
                        eligible_for_ac=False,reason='empty_analytic_interval',interval=interval))
                    break
                move['factor']=rng.uniform(interval['lower'],interval['upper'])
                if kind=='scale_layout':
                    move['original_scale']=state['feeder'].lines[0].length_km/original['feeder'].lines[0].length_km*move['factor']
                trial=apply_response_action(state,spec,move)
                if not research_contract_preserved(original,trial,contract):
                    if audit is not None:audit.append(dict(event='random_draw',action=dict(kind='compound',actions=moves+[move]),
                        eligible_for_ac=False,reason='random_intermediate_contract_rejected'))
                    break
                moves.append(move);state=trial
            if not moves:continue
            action=moves[0] if len(moves)==1 else dict(kind='compound',actions=moves)
            identity=digest(action)[:16]
            if identity in seen:continue
            seen.add(identity)
            fingerprint=physical_fingerprint(state)
            if fingerprint in physical:
                if audit is not None:audit.append(dict(action=action,eligible_for_ac=False,reason='duplicate_before_preview'))
                continue
            physical.add(fingerprint)
            action=dict(action,action_id=identity,diagnosis='Independent random legal geometry proposal; no response target or proxy used for sampling')
            options.append(action)
            if audit is not None:audit.append(dict(action=action,eligible_for_ac=True,reason='Random sample passed original-reference contract'))
            if len(options)==search.candidates_per_round:break
        return options
    return propose
