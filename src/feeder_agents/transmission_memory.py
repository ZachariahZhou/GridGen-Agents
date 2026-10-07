"""Version-isolated transmission experience with fresh AC replay on ingestion."""
import json
import sqlite3
import importlib.metadata
from pathlib import Path
from .artifacts import digest,file_digest
from .rules import data


def signature(spec):
    root=Path(__file__).parent
    return digest(dict(spec=spec.model_dump(exclude={'count','seed'}),
        code={n:file_digest(root/n) for n in ('transmission.py','transmission_model.py','transmission_equipment.py','transmission_topology.py','transmission_feedback.py','transmission_memory.py')},
        catalog=digest(data('transmission_lines.json')),line_families=digest(data('transmission_line_families.json')),
        solver={n:importlib.metadata.version(n) for n in ('pypower','numpy','scipy')}))


class TransmissionMemory:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:db.execute('CREATE TABLE IF NOT EXISTS transmission_episodes (id TEXT PRIMARY KEY, signature TEXT, payload TEXT, invalid_reason TEXT)')

    def invalidate(self,key,reason):
        if not reason:raise ValueError('A reason is required')
        with sqlite3.connect(self.path) as db:db.execute('UPDATE transmission_episodes SET invalid_reason=? WHERE id=?',(reason,key))

    def ingest(self,root,spec):
        from .transmission_feedback import verify_saved_feedback
        root=Path(root).resolve();report=verify_saved_feedback(root)
        if json.loads((root/'contract.json').read_text())['spec']!=spec.model_dump():raise ValueError('Experience specification mismatch')
        events=[dict(kind=r['action']['kind'],selected=r['selected']) for r in report['trace'] if 'candidate' in r and 'error' not in r]
        if not events:return 0
        hashes={str(p.relative_to(root)):file_digest(p) for p in sorted(root.rglob('*.json')) if p.name!='memory_status.json'}
        key=digest(dict(root=str(root),result=hashes['result.json']))
        with sqlite3.connect(self.path) as db:db.execute('INSERT OR IGNORE INTO transmission_episodes VALUES (?,?,?,NULL)',(key,signature(spec),json.dumps(dict(root=str(root),hashes=hashes,events=events))))
        return len(events)

    def retrieve(self,spec):
        with sqlite3.connect(self.path) as db:rows=db.execute('SELECT id,payload FROM transmission_episodes WHERE signature=? AND invalid_reason IS NULL ORDER BY id',(signature(spec),)).fetchall()
        groups={}
        for key,payload in rows:
            p=json.loads(payload);root=Path(p['root'])
            try:
                if not all((root/n).resolve().is_relative_to(root.resolve()) and file_digest(root/n)==h for n,h in p['hashes'].items()):continue
            except OSError:continue
            for e in p['events']:
                g=groups.setdefault(e['kind'],dict(kind=e['kind'],trials=0,improved=0,rejected=0,evidence=[]));g['trials']+=1;g['improved']+=int(e['selected']);g['rejected']+=int(not e['selected'])
                if key not in g['evidence'] and len(g['evidence'])<3:g['evidence'].append(key)
        return [dict(**v,smoothed_improvement_rate=(v['improved']+1)/(v['trials']+2)) for v in groups.values()]
