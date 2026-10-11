"""Deliverable checks must retain selected evidence and disclose auxiliary scope."""
import importlib.util
from pathlib import Path
import pytest
from feeder_agents.artifacts import digest,file_digest,atomic_json


def reader():
    spec=importlib.util.spec_from_file_location('m96_scope',Path(__file__).parents[1]/'scripts/audit_m96_parallel.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.read_core_result


def fixture(root):
    files=['selected/feeder.json','baseline/model/feeder.json','trials/winner/response.json','trials/unused/response.json','history.json']
    for name in files:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}')
    value=dict(directory=str(root.resolve()),winner='winner',artifacts={name:file_digest(root/name) for name in files})
    value['result_hash']=digest(value);atomic_json(root/'result.json',value)


def test_core_scope_checks_selected_trial_and_model(tmp_path):
    fixture(tmp_path);read=reader()
    value=read(tmp_path)
    assert value['_artifact_coverage']==dict(scope='core',checked=4,total=5)
    (tmp_path/'trials/winner/response.json').write_text('changed')
    with pytest.raises(ValueError,match='artifacts'):read(tmp_path)


def test_auxiliary_omission_is_explicit_not_full_integrity_claim(tmp_path):
    fixture(tmp_path);read=reader()
    (tmp_path/'trials/unused/response.json').write_text('changed')
    assert read(tmp_path)['_artifact_coverage']['checked']<5
    (tmp_path/'selected/feeder.json').write_text('changed')
    with pytest.raises(ValueError,match='artifacts'):read(tmp_path)
