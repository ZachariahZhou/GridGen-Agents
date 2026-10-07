"""Build closed evidence-backed snapshots from disjoint development seeds."""
import json,sqlite3
from pathlib import Path
from ..artifacts import atomic_json,file_digest
from ..hierarchy import historical_hierarchy_spec,HierarchicalFeeder
from ..transmission import TransmissionSpec
from .runner import read_row


def build_snapshot(training_run,domain,output):
    root=Path(training_run).resolve();output=Path(output).resolve();manifest=json.loads((root/'manifest.json').read_text())
    if manifest['protocol']['split']!='development':raise ValueError('Memory training must not consume held-out evaluation')
    if output.exists():raise ValueError('Snapshot exists; use a new name')
    if domain=='distribution':
        from ..verified_memory import VerifiedMemory
        store=VerifiedMemory(output)
    elif domain=='transmission':
        from ..transmission_memory import TransmissionMemory
        store=TransmissionMemory(output)
    else:raise ValueError('Unknown domain')
    events=0;seeds=set();groups=set();sources={}
    for rowfile in sorted((root/'trials').rglob('row.json')):
        folder=rowfile.parent;row=read_row(folder)
        if row['domain']!=domain or not (folder/'feedback/result.json').exists():continue
        contract=json.loads((folder/'feedback/contract.json').read_text());seeds.add(row['seed']);groups.add(row['group'])
        if domain=='distribution':
            report=json.loads((folder/'feedback/result.json').read_text())
            selected=HierarchicalFeeder.model_validate(json.loads((folder/'feedback'/report['selected_candidate']/'feeder.json').read_text()))
            count=store.ingest(folder/'feedback',historical_hierarchy_spec(contract['spec']),selected)
        else:count=store.ingest(folder/'feedback',TransmissionSpec(**contract['spec']))
        events+=count;sources[str(rowfile)]=file_digest(rowfile)
    if not events:raise ValueError('No verified action events available; do not label an empty snapshot as trained memory')
    with sqlite3.connect(output) as db:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    atomic_json(output.with_suffix('.provenance.json'),dict(domain=domain,seeds=sorted(seeds),groups=sorted(groups),events=events,sources=sources,training_manifest=str(root/'manifest.json'),manifest_hash=file_digest(root/'manifest.json')))
    return dict(path=str(output),events=events,seeds=sorted(seeds))
