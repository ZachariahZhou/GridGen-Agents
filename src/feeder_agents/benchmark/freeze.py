"""Prospective benchmark freezing and limited, explicit leakage checks."""
import argparse
import json
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from ..artifacts import file_digest
from ..workflow import _runtime_fingerprint
from .schema import BenchmarkSuite


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC',text).casefold() if c.isalnum())


def audit_suite(suite,prior_paths):
    suite=BenchmarkSuite.model_validate(suite)
    old=[]
    for path in prior_paths:
        prior=BenchmarkSuite.model_validate(json.loads(Path(path).read_text()))
        old.extend((str(path),c) for c in prior.cases)
    groups=[];requests=[];nearest=[];internal=[];seen={}
    for case in suite.cases:
        text=normalized(case.request)
        if text in seen:internal.append(dict(case_id=case.id,other_case_id=seen[text]))
        seen[text]=case.id
        best=None
        for path,previous in old:
            if case.group_id==previous.group_id:groups.append(dict(case_id=case.id,prior=path,prior_case_id=previous.id))
            prior_text=normalized(previous.request)
            if text==prior_text:requests.append(dict(case_id=case.id,prior=path,prior_case_id=previous.id))
            similarity=SequenceMatcher(None,text,prior_text,autojunk=False).ratio()
            if best is None or similarity>best['similarity']:
                best=dict(case_id=case.id,prior=path,prior_case_id=previous.id,similarity=similarity)
        if best:nearest.append(best)
    return dict(passed=not(groups or requests or internal),cases=len(suite.cases),groups=len({c.group_id for c in suite.cases}),
        by_stratum=dict(Counter(c.stratum for c in suite.cases)),by_expected=dict(Counter(c.expected for c in suite.cases)),
        group_overlaps=groups,request_overlaps=requests,internal_duplicates=internal,nearest_development_requests=nearest,
        limitation='Only group IDs and normalized exact duplicates are blocking checks. Similarity is diagnostic, not proof of semantic independence or absence from pretraining.')


def freeze_suite(suite_path,prior_paths,extra_paths=()):
    suite_path=Path(suite_path).resolve();lock=suite_path.with_suffix('.freeze.json')
    if lock.exists():raise FileExistsError('Freeze already exists; version a new suite rather than overwrite')
    report=audit_suite(json.loads(suite_path.read_text()),prior_paths)
    if not report['passed']:raise ValueError('Development overlap or duplicate requests; freeze refused')
    import os
    paths=[suite_path]+[Path(p).resolve() for p in prior_paths]+[Path(p).resolve() for p in extra_paths]
    manifest=dict(version=1,suite=suite_path.name,
        files={os.path.relpath(p,suite_path.parent):file_digest(p) for p in paths},
        runtime=_runtime_fingerprint(),audit=report,
        target_evaluation='Not run at freeze; this is a prospective author-created test, not external expert validation.')
    with lock.open('x') as stream:json.dump(manifest,stream,ensure_ascii=False,indent=2)
    return lock


def verify_freeze(suite_path,lock_path=None):
    suite_path=Path(suite_path).resolve()
    lock=Path(lock_path) if lock_path else suite_path.with_suffix('.freeze.json')
    manifest=json.loads(lock.read_text())
    if manifest['suite']!=suite_path.name or suite_path.name not in manifest['files']:
        raise ValueError('Frozen suite identity differs')
    for name,expected in manifest['files'].items():
        path=lock.parent/name
        if not path.is_file() or file_digest(path)!=expected:raise ValueError('Frozen file changed: '+name)
    if _runtime_fingerprint()!=manifest['runtime']:raise ValueError('Frozen runtime changed; this is no longer the frozen method/scorer version')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',type=Path,required=True)
    parser.add_argument('--against',type=Path,nargs='*',default=[])
    parser.add_argument('--extra',type=Path,nargs='*',default=[])
    parser.add_argument('--freeze',action='store_true')
    parser.add_argument('--verify',action='store_true')
    args=parser.parse_args()
    if args.freeze and args.verify:parser.error('Choose freeze or verify')
    if args.verify:
        manifest=verify_freeze(args.suite);print(json.dumps(dict(verified=True,audit=manifest['audit']),ensure_ascii=False,indent=2))
    elif args.freeze:
        print(freeze_suite(args.suite,args.against,args.extra))
    else:print(json.dumps(audit_suite(json.loads(args.suite.read_text()),args.against),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
