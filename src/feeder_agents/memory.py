import json
import sqlite3
from pathlib import Path


class MemoryStore:
    """Explicit project-scoped memory; model guesses are not written automatically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS memories '
                       '(project TEXT, key TEXT, value TEXT, PRIMARY KEY(project,key))')

    def put(self, project: str, key: str, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload) > 16000:
            raise ValueError('Memory entry exceeds 16000 characters')
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO memories VALUES (?,?,?)', (project, key, payload))

    def get(self, project: str, key: str):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT value FROM memories WHERE project=? AND key=?', (project, key)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, project: str):
        with sqlite3.connect(self.path) as db:
            rows = db.execute('SELECT key,value FROM memories WHERE project=? ORDER BY key', (project,)).fetchall()
        return {key: json.loads(value) for key, value in rows}


class ExperienceStore:
    """Observed repair outcomes, isolated from user preference memory."""
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS experiences '
                       '(id TEXT PRIMARY KEY, signature TEXT, context TEXT, payload TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS experience_scope ON experiences(signature,context)')

    def record(self,record):
        payload=json.dumps(record,ensure_ascii=False,allow_nan=False)
        if len(payload)>24000:
            raise ValueError('Experience exceeds 24000 characters')
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO experiences VALUES (?,?,?,?)',
                (record['id'],record['signature'],record['context'],payload))

    def lookup(self,signature,context):
        with sqlite3.connect(self.path) as db:
            records=[json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM experiences WHERE signature=? AND context=? ORDER BY rowid DESC LIMIT 100',
                (signature,context))]
        groups={}
        for record in records:
            action=record['action']
            group=groups.setdefault(action,{'action':action,'observations':0,'improvements':0,
                'rollbacks':0,'final_accepted':0,'examples':[]})
            group['observations']+=1
            group['improvements']+=int(record['improved'])
            group['rollbacks']+=int(not record['improved'])
            group['final_accepted']+=int(record['final_accepted'])
            if len(group['examples'])<2:
                group['examples'].append({k:record[k] for k in ('source','before_hash','after_hash')})
        return list(groups.values())[:4]


def experience_context(spec):
    from .artifacts import digest
    payload=spec.model_dump(exclude={'count','seed','max_repairs','repair_policy','document_rules'})
    payload['scenario'].pop('positions_km',None)
    return digest(payload)


def experience_signature(manifest):
    from .artifacts import digest
    return digest({'rules':manifest['rules'],'conductors':manifest['conductors'],
        'runtime':manifest['runtime'],'document_rules':manifest['spec'].get('document_rules',[])})


def collect_experiences(store,signature,context,root,outcomes,configuration_hash):
    from .artifacts import digest
    for outcome in outcomes:
        if not outcome.get('model_hash'):
            continue
        folder=Path(root)/outcome['sample_id']
        history=json.loads((folder/'history.json').read_text())
        before_hash=None; trial_hash=None; choice=None
        for index,item in enumerate(history):
            if item.get('attempt')==0:
                before_hash=item['model_hash']
            if item.get('action')=='repair_decision':
                choice=item.get('chosen_candidate')
            elif 'checks' in item:
                trial_hash=item['model_hash']
            elif item.get('action') in {'commit','rollback'}:
                improved=item['action']=='commit'
                if choice:
                    record={'id':digest([str(Path(root).resolve()),configuration_hash,outcome['sample_id'],index]),
                        'signature':signature,'context':context,'action':choice['action'],'improved':improved,
                        'final_accepted':outcome['accepted'],'final_operational_pass':outcome['operational_pass'],
                        'source':str(folder.resolve()),'before_hash':before_hash,'after_hash':trial_hash,
                        'meaning':'Observed trial improvement/rollback; final acceptance is separate and does not prove causal optimality.'}
                    store.record(record)
                if improved:
                    before_hash=trial_hash
                choice=None
