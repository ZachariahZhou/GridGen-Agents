"""Keep the public repository readable in English without removing bilingual input."""
from pathlib import Path
import re


def test_public_source_and_documentation_have_no_literal_chinese():
    root=Path(__file__).resolve().parents[1]
    paths=[root/'README.md',root/'app.py',root/'pyproject.toml',root/'THIRD_PARTY.md']
    for folder in ('src','tests','scripts','examples','docs'):
        paths.extend(p for p in (root/folder).rglob('*') if p.suffix in ('.py','.md','.json','.yaml','.toml'))
    han=re.compile(r'[\u3400-\u9fff\uf900-\ufaff]')
    failures=[]
    for path in paths:
        for number,line in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
            if han.search(line):failures.append(f'{path.relative_to(root)}:{number}')
    assert not failures,'Use English prose; preserve bilingual parsing/evidence with Unicode escapes: '+', '.join(failures)
