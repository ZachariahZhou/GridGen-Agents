"""Reviewed prospective releases, not automatic claims of held-out independence."""
import csv,json
from pathlib import Path
from ..artifacts import atomic_json,digest,file_digest
from ..workflow import _runtime_fingerprint
from ..benchmark.freeze import normalized
from .schema import Protocol
from .runner import memory_inventory


def release(candidate,review,development,output):
    candidate=Path(candidate).resolve();review=Path(review).resolve();output=Path(output).resolve()
    protocol=Protocol.model_validate(json.loads(candidate.read_text()))
    if protocol.split!='candidate':raise ValueError('Release requires a candidate protocol')
    with review.open() as f:rows=list(csv.DictReader(f))
    reviewed={r['task_id']:r for r in rows}
    if len(reviewed)!=len(rows) or set(reviewed)!={t.id for t in protocol.tasks}:raise ValueError('Review must cover each task exactly once')
    for r in rows:
        if not r.get('reviewer','').strip() or r.get('approved')!='yes' or r.get('semantic_independence_checked')!='yes':raise ValueError('Human annotation and semantic-independence review required')
    prior_groups=set();prior_text=set()
    paths=[candidate,review]
    for filename in development:
        p=Path(filename).resolve();paths.append(p);data=json.loads(p.read_text())
        for t in data.get('tasks',data.get('cases',[])):
            prior_groups.add(t.get('group',t.get('group_id')));prior_text.add(normalized(t['request']))
    if any(t.group in prior_groups or normalized(t.request) in prior_text for t in protocol.tasks):raise ValueError('Development overlap detected')
    if len({normalized(t.request) for t in protocol.tasks})!=len(protocol.tasks):raise ValueError('Duplicate requests')
    if output.exists() or output.with_suffix('.freeze.json').exists():raise ValueError('Release exists; create a new version')
    values=protocol.model_dump();values['split']='heldout';final=Protocol.model_validate(values)
    atomic_json(output,final.model_dump())
    lock=dict(protocol_hash=digest(final.model_dump()),runtime=_runtime_fingerprint(),files={str(p):file_digest(p) for p in paths},
        memory_evidence=memory_inventory(final),notice='Prospective reviewed author-created suite. Exact overlap and group IDs are checked automatically; semantic independence relies on human review, not a proof of model pretraining exclusion.')
    atomic_json(output.with_suffix('.freeze.json'),lock);return output


def verify(protocol,filename):
    lock=json.loads(Path(filename).read_text())
    if lock['protocol_hash']!=digest(protocol.model_dump()) or lock['runtime']!=_runtime_fingerprint():raise ValueError('Frozen task/protocol/runtime changed')
    for filename,expected in {**lock['files'],**lock['memory_evidence']}.items():
        if file_digest(Path(filename))!=expected:raise ValueError('Frozen evidence changed')
    if lock['memory_evidence']!=memory_inventory(protocol):raise ValueError('Memory evidence changed')
    return lock
