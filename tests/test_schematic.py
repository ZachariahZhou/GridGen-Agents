import copy
import json

import numpy as np
import pytest

from feeder_agents.hierarchy import HierarchicalSpec, generate_hierarchy
from feeder_agents.transmission import TransmissionSpec, generate_case, case_payload


def test_display_coordinates_do_not_change_feeder_or_drop_open_ties():
    from feeder_agents.schematic import network_graph, schematic_layout, schematic_html
    feeder = generate_hierarchy(HierarchicalSpec(transformer_count=4, users=8, total_kw=16,
        mv_topology={'family':'multi_open_ring','ring_count':2}), 42)
    before = feeder.model_dump_json()
    graph = network_graph(feeder)
    positions = schematic_layout(graph)
    assert len(graph) == len(feeder.buses)
    assert graph.number_of_edges() == len(feeder.lines) + len(feeder.tie_lines) + len(feeder.transformers)
    assert sum(e['kind']=='tie' for *_,e in graph.edges(data=True)) == 2
    assert set(positions) == set(graph)
    assert all(np.isfinite(p).all() for p in positions.values())
    changed = feeder.model_copy(deep=True)
    for bus in changed.buses:
        bus.x_km *= 1000
        bus.y_km += 700
    assert schematic_layout(network_graph(changed)) == positions
    page = schematic_html(graph, positions=positions)
    assert 'NO' in page and 'Non-geographic' in page
    assert feeder.model_dump_json() == before


def test_transmission_display_preserves_parallel_branches_and_transformer_roles():
    from feeder_agents.schematic import network_graph, schematic_layout
    case, metadata = generate_case(TransmissionSpec(n_buses=12, n_generators=3, total_mw=60,
        voltage_layers=[{'kv':220,'buses':6},{'kv':110,'buses':6}]), 3)
    case['branch'] = np.vstack([case['branch'],case['branch'][0]])
    metadata['branch_evidence'].append(copy.deepcopy(metadata['branch_evidence'][0]))
    before = json.dumps(case_payload(case))
    graph = network_graph(case,metadata)
    assert graph.number_of_edges() == len(case['branch'])
    assert sum(e['kind']=='transformer' for *_,e in graph.edges(data=True)) == 2
    assert sum(bool(n['generator']) for _,n in graph.nodes(data=True)) == 3
    assert len(schematic_layout(graph)) == 12
    assert json.dumps(case_payload(case)) == before


def test_saved_view_layout_switch_does_not_rewrite_artifact(tmp_path):
    from feeder_agents.visual_theme import render_saved_view
    f = generate_hierarchy(HierarchicalSpec(transformer_count=2,users=4,total_kw=12),5)
    (tmp_path/'feeder.json').write_text(f.model_dump_json())
    (tmp_path/'simulation.json').write_text('{}')
    (tmp_path/'validation.json').write_text('{"accepted":true}')
    view = tmp_path/'visualization.html'
    view.write_text('frozen original view')
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    assert 'Non-geographic' in render_saved_view(view,layout_mode='topology')
    assert 'Synthetic spatial coordinates' in render_saved_view(view,layout_mode='geographic')
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize('size',[2,501])
def test_distribution_drawing_works_without_optional_scipy(monkeypatch,size):
    import builtins
    import networkx as nx
    from feeder_agents.schematic import schematic_layout,_cached_layout
    original=builtins.__import__
    def no_scipy(name,*args,**kwargs):
        if name=='scipy' or name.startswith('scipy.'):
            raise ModuleNotFoundError('scipy is intentionally unavailable')
        return original(name,*args,**kwargs)
    graph=nx.MultiGraph(nx.path_graph(size))
    graph.graph['domain']='distribution'
    nx.set_node_attributes(graph,10.,'voltage_kv')
    _cached_layout.cache_clear()
    monkeypatch.setattr(builtins,'__import__',no_scipy)
    positions=schematic_layout(graph)
    assert len(positions)==size
    assert all(np.isfinite(p).all() for p in positions.values())


def test_mv_and_local_lv_views_preserve_model_and_aggregate_actual_customers():
    from feeder_agents.schematic import network_graph,network_view,schematic_html
    spec=HierarchicalSpec(transformer_count=6,mv_buses=19,users=36,total_kw=54,
                          mv_topology={'family':'open_ring'})
    feeder=generate_hierarchy(spec,42)
    original=feeder.model_dump_json()
    full=network_graph(feeder)
    original_graph=copy.deepcopy(full)
    mv=network_view(full,'mv')
    assert len(mv)==19 and mv.graph['full_node_count']==len(feeder.buses)
    assert sum(a['kind']=='tie' for *_,a in mv.edges(data=True))==1
    assert sum(a.get('aggregated_users',0) for _,a in mv.nodes(data=True))==36
    assert sum(a.get('aggregated_kw',0) for _,a in mv.nodes(data=True))==pytest.approx(54)
    transformer=feeder.transformers[0]
    local=network_view(full,'lv',transformer.id)
    expected={b.id for b in feeder.buses if b.transformer_id==transformer.id}|{transformer.bus1}
    assert set(local)==expected
    assert 'MV backbone' in schematic_html(mv)
    assert 'Full-model buses' in schematic_html(mv)
    assert feeder.model_dump_json()==original
    assert dict(full.nodes(data=True))==dict(original_graph.nodes(data=True))
    with pytest.raises(ValueError):network_view(full,'lv','missing')


def test_saved_scope_switch_is_read_only_and_supports_geographic_positions(tmp_path):
    from feeder_agents.visual_theme import render_saved_view
    feeder=generate_hierarchy(HierarchicalSpec(transformer_count=2,users=12,total_kw=18),42)
    (tmp_path/'feeder.json').write_text(feeder.model_dump_json())
    (tmp_path/'validation.json').write_text('{"accepted":true}')
    path=tmp_path/'visualization.html';path.write_text('frozen')
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    assert 'MV backbone' in render_saved_view(path,layout_mode='topology',scope='mv')
    assert 'Synthetic spatial coordinates' in render_saved_view(path,layout_mode='geographic',scope='mv')
    assert 'LV service area: tx_1' in render_saved_view(path,layout_mode='topology',scope='lv',transformer_id='tx_1')
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()}==before
