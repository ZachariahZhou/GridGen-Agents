"""Independent MV resolution and heterogeneous, count-preserving LV groups."""
import random

import networkx as nx


def transformer_tap_indices(spec, seed):
    from .hierarchy_topology_design import resolved_blueprint, resolve_mv_topology
    edges, _, _, _ = resolved_blueprint(spec,seed)
    graph = nx.Graph(edges)
    terminals = sorted(n for n, d in graph.degree() if n != 0 and d == 1)
    if len(terminals) > spec.transformer_count:
        raise ValueError(f'MV topology has {len(terminals)} terminal branches but only '
                         f'{spec.transformer_count} transformers; increase transformers or choose a corridor topology')
    if spec.mv_topology_policy=='branched_v4' and resolve_mv_topology(spec).family=='branched_network':
        from .joint_structure import spread_transformer_taps
        return spread_transformer_taps(graph,terminals,spec.transformer_count,seed)
    remaining = sorted(set(graph) - {0} - set(terminals))
    chosen = random.Random(f'mv-taps:{seed}').sample(remaining, spec.transformer_count - len(terminals))
    return sorted(terminals + chosen)


def allocate_counts(total, groups, minimum, rng, varied):
    """Each group is occupied. Seeded weights distribute only the remaining users."""
    if total < groups * minimum:
        raise ValueError('Insufficient users for occupied LV branches')
    if not varied:
        return [total // groups + (i < total % groups) for i in range(groups)]
    remaining = total - groups * minimum
    weights = [rng.lognormvariate(0, .65) for _ in range(groups)]
    quotas = [remaining * w / sum(weights) for w in weights]
    result = [minimum + int(q) for q in quotas]
    order = sorted(range(groups), key=lambda i: quotas[i] - int(quotas[i]), reverse=True)
    for i in order[:total - sum(result)]:
        result[i] += 1
    return result


def customer_groups(spec, seed):
    rng = random.Random(f'customer-allocation:{seed}')
    varied = spec.customer_allocation == 'varied'
    stations = allocate_counts(spec.users, spec.transformer_count, spec.lv_branches, rng, varied)
    return [allocate_counts(n, spec.lv_branches, 1, rng, varied) for n in stations]


def lv_chain_for_transformer(spec, seed, index):
    from .hierarchy_topology_design import resolve_lv_topology
    family = resolve_lv_topology(spec)
    if family != 'mixed_radial':
        return family == 'radial_chain'
    order = list(range(spec.transformer_count))
    random.Random(f'lv-mixture:{seed}').shuffle(order)
    return index in order[:(spec.transformer_count + 1) // 2]
