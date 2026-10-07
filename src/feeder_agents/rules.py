import json
from importlib.resources import files


def data(name: str):
    value=json.loads(files('feeder_agents').joinpath('data', name).read_text(encoding='utf-8'))
    if name=='conductors.json':
        from .equipment import reference_catalog
        value.update(reference_catalog())
    return value


def load_rules(spec=None) -> dict:
    sources = {s['id']: s for s in data('sources.json')}
    result = {}
    for rule in data('rules.json'):
        if sources[rule['source_id']]['access'] == 'metadata_only':
            raise ValueError('Metadata-only documents cannot ground executable rules')
        if rule['id'] in result:
            raise ValueError('Duplicate rule ID')
        result[rule['id']] = rule
    if spec is not None:
        from .voltage import voltage_profile
        profile = voltage_profile(spec.voltage_kv)
        result['research.unbalance']['parameters']={'max_vuf_percent':spec.phase_design.max_vuf_percent}
        low, high = profile['research_voltage']
        result['research.voltage']['parameters'] = {'min_pu': low, 'max_pu': high}
        result['research.voltage']['version'] = profile['id']
        result['research.voltage']['scope'] = f"Selected {spec.voltage_kv} kV research profile; not regulatory certification"
    return result


def search_sources(query: str = '') -> list[dict]:
    """Local lexical retrieval of curated source summaries; not live web search."""
    terms = query.casefold().split()
    ranked = []
    for source in data('sources.json'):
        text = json.dumps(source, ensure_ascii=False).casefold()
        score = sum(term in text for term in terms)
        if not terms or score:
            ranked.append((score, source))
    return [s for _, s in sorted(ranked, key=lambda x: -x[0])]
