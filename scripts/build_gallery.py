"""Generate the release examples, validate them, and render traceable figures.

Run from the repository root after installing .[transmission,plots]. Models and
solver evidence stay in workspace/; only figures and their provenance enter docs/.
No saved historical cases or external language models are used.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import tempfile

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import networkx as nx

from check_examples import check_all
from feeder_agents.schematic import network_graph, network_view, schematic_layout, display_edges


ROOT = Path(__file__).resolve().parents[1]
SELECTION = (
    ('distribution_radial', 'Rural trunk and laterals'),
    ('distribution_villages', 'Rural clustered feeder'),
    ('hierarchical_urban', 'Urban MV/LV feeder'),
    ('transmission_37', 'Meshed transmission network'),
    ('transmission_139', 'Meshed transmission network'),
    ('transmission_237', 'Two-voltage transmission network'),
)
COLORS = {0.38: '#c49a54', 0.4: '#c49a54', 10.: '#27877b',
          110.: '#709751', 220.: '#447ab0', 500.: '#ad774d'}
INK = '#233b4b'
MUTED = '#687e91'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_manifest():
    paths = sorted(p for p in (ROOT / 'src').rglob('*') if p.suffix in ('.py', '.json'))
    entries = {str(p.relative_to(ROOT)): sha(p) for p in paths}
    fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    return {'sha256': fingerprint, 'files': entries}


def load_example(workspace, name):
    if name.startswith('transmission_'):
        folder = workspace / 'transmission_experiments' / name / 'sample_00000'
        model = folder / 'case.json'
        graph = network_graph(json.loads(model.read_text()), json.loads((folder / 'metadata.json').read_text()))
    else:
        family = 'hierarchical_experiments' if name.startswith('hierarchical_') else 'experiments'
        folder = workspace / family / name / 'sample_00000'
        model = folder / 'feeder.json'
        graph = network_graph(json.loads(model.read_text()))
    active = nx.MultiGraph()
    active.add_nodes_from(graph)
    active.add_edges_from((a, b, k) for a, b, k, d in graph.edges(keys=True, data=True) if d['active'])
    record = {
        'example': name + '.yaml', 'spec_sha256': sha(ROOT / 'examples' / (name + '.yaml')),
        'model_path': str(model.relative_to(workspace)), 'model_sha256': sha(model),
        'buses': len(graph), 'physical_branches': graph.number_of_edges(),
        'energized_branches': active.number_of_edges(),
        'open_branches': sum(not d['active'] for *_, d in graph.edges(data=True)),
        'transformers': sum(d['kind'] == 'transformer' for *_, d in graph.edges(data=True)),
        'voltage_levels_kv': sorted({d['voltage_kv'] for _, d in graph.nodes(data=True)}, reverse=True),
        'active_mean_degree': 2 * active.number_of_edges() / len(active),
        'active_cycle_rank': active.number_of_edges() - len(active) + nx.number_connected_components(active),
        'connected': nx.is_connected(active),
    }
    record['evidence_sha256'] = {p.name: sha(p) for p in (folder / 'validation.json', folder / 'outcome.json', folder / 'simulation.json') if p.exists()}
    return graph, record


def draw(ax, graph, *, label_nodes=False):
    positions = schematic_layout(graph, seed=42)
    from matplotlib.patches import FancyArrowPatch
    for a, b, _, attr, curve in display_edges(graph):
        color = '#9aabba'
        style = '-'
        width = .9 if len(graph) < 100 else .65
        if not attr['active']:
            color, style, width = '#be7c35', '--', 1.2
        elif attr['kind'] == 'transformer':
            color, width = '#9468a4', 1.8
        ax.add_patch(FancyArrowPatch(positions[a], positions[b], arrowstyle='-',
            connectionstyle=f'arc3,rad={curve}', color=color, linewidth=width,
            linestyle=style, shrinkA=0, shrinkB=0, zorder=1))
    for n, attr in graph.nodes(data=True):
        x, y = positions[n]
        role = attr['role']
        marker = 's' if role == 'source' else '^' if role == 'generator' else 'D' if role == 'lv_bus' else 'o'
        size = 32 if len(graph) < 100 else 10
        if marker != 'o': size *= 1.7
        color = INK if role == 'source' else COLORS.get(attr['voltage_kv'], '#507f98')
        ax.scatter(x, y, s=size, marker=marker, facecolors=color,
                   edgecolors='#b95571' if attr.get('pv_kw', 0) else 'white', linewidths=.7, zorder=3)
        if label_nodes:
            label = str(n).replace('user_', 'U')
            if attr.get('aggregated_users'):
                label += f"\n{attr['aggregated_users']} users"
            offset = -13 if str(n).startswith('user_') and int(str(n).split('_')[-1]) % 2 == 0 else 6
            ax.annotate(label, (x, y), xytext=(5, offset), textcoords='offset points',
                        fontsize=7.5, color=INK, zorder=4)
    xs, ys = zip(*positions.values())
    radius = max(max(xs) - min(xs), max(ys) - min(ys), .1) * .62
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    ax.set(xlim=(cx-radius, cx+radius), ylim=(cy-radius, cy+radius), aspect='equal')
    ax.axis('off')


def legend(fig, voltages, *, y=.035, generators=True):
    handles = [Line2D([], [], marker='o', linestyle='', color=COLORS.get(v, '#507f98'), label=f'{v:g} kV', markersize=5) for v in sorted(voltages)]
    roles = [('s', 'Source / slack'), ('D', 'Transformer LV bus')]
    if generators:
        roles.insert(1, ('^', 'Generator bus'))
    handles += [Line2D([], [], marker=m, linestyle='', color=INK, label=t, markersize=6) for m, t in roles]
    handles += [Line2D([], [], color='#9468a4', label='Transformer'),
                Line2D([], [], marker='o', linestyle='', markerfacecolor='none', markeredgecolor='#b95571', label='PV attachment', markersize=6)]
    fig.legend(handles=handles, loc='lower center', bbox_to_anchor=(.5, y), frameon=False, ncol=5, fontsize=9)


def save(fig, output, name):
    for ext in ('png', 'pdf'):
        fig.savefig(output / f'{name}.{ext}', dpi=220, facecolor='white', metadata={'Creator': 'GridGen-Agents release gallery'})
    plt.close(fig)


def render(workspace, output, report):
    loaded = {Path(r['example']).stem: load_example(workspace, Path(r['example']).stem) for r in report['examples']}
    for r in report['examples']:
        loaded[Path(r['example']).stem][1]['accepted'] = r['accepted'] == r['expected']
    style = {'font.family': 'DejaVu Sans', 'font.size': 10, 'text.color': INK,
             'axes.titlecolor': INK, 'pdf.fonttype': 42, 'ps.fonttype': 42}
    with plt.rc_context(style):
        fig, axes = plt.subplots(2, 3, figsize=(15, 10))
        fig.subplots_adjust(left=.04, right=.98, top=.86, bottom=.14, wspace=.2, hspace=.32)
        fig.suptitle('Power networks generated by the current release', x=.04, y=.975, ha='left', fontsize=20, fontweight='bold')
        fig.text(.04, .936, 'Fresh runs of packaged examples | All buses and branches shown | Non-geographic layouts', color=MUTED, fontsize=10)
        for i, (ax, (name, title)) in enumerate(zip(axes.flat, SELECTION)):
            graph, row = loaded[name]
            draw(ax, graph)
            voltage = '/'.join(f'{v:g}' for v in row['voltage_levels_kv'])
            ax.set_title(f"({chr(97+i)}) {title}\n{row['buses']} buses | {voltage} kV | {row['physical_branches']} branches", loc='left', fontsize=10, linespacing=1.7, pad=12)
            ax.text(.5, -.04, f"Mean degree {row['active_mean_degree']:.2f} | Cycle rank {row['active_cycle_rank']} | Checks passed", transform=ax.transAxes, ha='center', fontsize=8.2, color=MUTED)
        legend(fig, {v for name, _ in SELECTION for v in loaded[name][1]['voltage_levels_kv']})
        fig.text(.5, .013, 'Deterministic example demonstrations; not an estimate of natural-language generation success.', ha='center', fontsize=8.5, color=MUTED)
        save(fig, output, 'network_overview')

        full, row = loaded['hierarchical_urban']
        transformer = next(d['id'] for *_, d in full.edges(data=True) if d['kind'] == 'transformer')
        views = [network_view(full, 'mv'), network_view(full, 'full'), network_view(full, 'lv', transformer)]
        fig, axes = plt.subplots(1, 3, figsize=(15, 6.8))
        fig.subplots_adjust(left=.04, right=.98, top=.76, bottom=.19, wspace=.23)
        fig.suptitle('One MV/LV model, three levels of detail', x=.04, y=.965, ha='left', fontsize=20, fontweight='bold')
        users = sum(a['role'] == 'customer' for _, a in full.nodes(data=True))
        fig.text(.04, .91, f"Fresh urban example | {len(full)} buses | 10/0.4 kV | {row['transformers']} transformers | {users} customers", color=MUTED)
        for ax, view, title in zip(axes, views, ['MV backbone (display projection)', 'Full network', f'LV service area: {transformer}']):
            draw(ax, view, label_nodes=view.graph['scope'] != 'full')
            ax.set_title(f'{title}\n{len(view)} displayed buses', loc='left', fontsize=11, linespacing=1.6, pad=12)
        legend(fig, row['voltage_levels_kv'], y=.055, generators=False)
        fig.text(.5, .018, 'MV and LV views are display subsets. The exported electrical model retains every bus and branch.', ha='center', fontsize=9, color=MUTED)
        save(fig, output, 'hierarchy_detail')
    return [record for _, record in loaded.values()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT / 'workspace' / 'gallery')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs' / 'assets')
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix='run_', dir=args.workspace.resolve()))
    before = source_manifest()
    report = check_all(workspace)
    records = render(workspace, args.output, report)
    assert source_manifest() == before, 'Runtime changed while generating the gallery'
    try:
        base_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        base_commit = None
    manifest = {
        'schema_version': 1, 'package_version': importlib.metadata.version('feeder-agents'),
        'base_commit': base_commit, 'runtime': before,
        'render_script_sha256': sha(Path(__file__)),
        'example_runner_sha256': sha(ROOT / 'scripts' / 'check_examples.py'),
        'artifact_directory': str(workspace.relative_to(ROOT)) if workspace.is_relative_to(ROOT) else workspace.name,
        'dependencies': {name: importlib.metadata.version(name) for name in ['networkx','numpy','matplotlib','OpenDSSDirect.py','PYPOWER']},
        'selection': [name + '.yaml' for name, _ in SELECTION],
        'layout_seed': 42, 'source': 'Fresh runs of the packaged specifications using this checkout',
        'external_llm_calls': 0, 'all_seven_examples_accepted': report['all_accepted'],
        'scope': 'Example demonstrations, not a natural-language benchmark or a real-grid fidelity study.',
        'examples': records,
        'figures': {p.name: sha(p) for name in ['network_overview', 'hierarchy_detail'] for ext in ['png', 'pdf'] if (p := args.output / f'{name}.{ext}').exists()},
    }
    (args.output / 'gallery_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    fields = ['example','accepted','buses','physical_branches','energized_branches','open_branches','transformers','active_mean_degree','active_cycle_rank','connected','model_sha256']
    with (args.output / 'gallery_metrics.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
        writer.writeheader(); writer.writerows(records)
    print(json.dumps({'figures': str(args.output), 'models_and_checks': str(workspace), 'examples_accepted': len(records)}))


if __name__ == '__main__':
    main()
