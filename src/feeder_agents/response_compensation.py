"""Exploratory counter-moves for met ports; original acceptance targets stay fixed."""
import copy

from .response_distribution import conductor_options,layout_options
from .response_flexibility import distribution_actions


def compensation_actions(model,original,spec,probes,assessment,search):
    if model['kind']!='distribution':return []
    output=[]
    for row in assessment['targets']:
        observed=row['observed']
        if not row['passed'] or observed is None or observed<=1e-12:continue
        for direction in (-1,1):
            for step in (.02,.1):
                proposal=copy.deepcopy(row);proposal['passed']=False
                bounds=sorted([observed*(1+direction*step),observed*(1+direction*step*1.25)])
                proposal['target'].update(lower=bounds[0],upper=bounds[1])
                # These artificial offsets are proposal instructions only.
                # They never enter the run's plan, scoring or exported targets.
                exploratory=dict(assessment,targets=[proposal])
                candidates=[]
                if 'replace_conductor' in search.allowed_actions:candidates+=conductor_options(model,spec,probes,exploratory)
                if 'scale_layout' in search.allowed_actions:candidates+=layout_options(model,original,spec,exploratory,search)
                if set(search.allowed_actions)&{'scale_subtree','rewire_branch','redistribute_load'}:
                    candidates+=distribution_actions(model,original,spec,probes,exploratory,search)
                per_kind={}
                for action in candidates:
                    kind=action['kind'];per_kind[kind]=per_kind.get(kind,0)+1
                    if per_kind[kind]>2:continue
                    output.append(dict(action,compensation=True,
                        diagnosis='Exploratory counter-move for a currently met port; may appear only in a joint preview. Original target is unchanged. '+action['diagnosis']))
    return output
