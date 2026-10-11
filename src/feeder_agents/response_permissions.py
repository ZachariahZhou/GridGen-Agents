"""Explicit permission revisions; no target relaxation or automatic escalation."""
import re
from typing import Literal

from pydantic import Field,model_validator

from .artifacts import digest
from .response_schema import ResponseDesignPlan
from .schemas import StrictModel


class ResponsePermissionGrant(StrictModel):
    tier: Literal['equipment','equipment_topology','equipment_geometry','geometry']
    max_changed_conductors: int = Field(default=0,ge=0,le=2000)
    max_rewired_lines: int | None = Field(default=None,ge=1,le=20)
    max_rewire_length_ratio: float | None = Field(default=None,gt=1,le=3)
    max_node_degree: int | None = Field(default=None,ge=2,le=8)
    max_layout_scale_change: float | None = Field(default=None,gt=0,le=.9)
    max_branch_length_change: float | None = Field(default=None,gt=0,le=.9)
    max_node_displacement_km: float | None = Field(default=None,gt=0,le=10)

    @model_validator(mode='after')
    def explicit_topology_limits(self):
        if self.tier=='geometry' and self.max_changed_conductors:
            raise ValueError('Geometry-only permission cannot grant conductor replacements')
        if self.tier!='geometry' and not self.max_changed_conductors:
            raise ValueError('Equipment permission requires a positive conductor-change cap')
        required=self.tier=='equipment_topology'
        if any((getattr(self,k) is not None)!=required for k in
               ('max_rewired_lines','max_rewire_length_ratio','max_node_degree')):
            raise ValueError('equipment_topology requires all three topology limits; equipment grants omit them')
        if self.tier=='geometry':
            if (self.max_branch_length_change is None)!=(self.max_node_displacement_km is None):
                raise ValueError('Local geometry requires both length and node displacement limits')
            if self.max_layout_scale_change is None and self.max_branch_length_change is None:
                raise ValueError('Geometry permission requires explicit layout or subtree limits')
            return self
        geometry=self.tier=='equipment_geometry'
        if any((getattr(self,k) is not None)!=geometry for k in
               ('max_layout_scale_change','max_branch_length_change','max_node_displacement_km')):
            raise ValueError('equipment_geometry requires all three geometry limits; other grants omit them')
        return self


def prepare_permission_revision(plan,permissions):
    """Return an inspectable revised contract for an explicitly authorized run.

    The caller supplies the grants, not the diagnostic. Existing non-permission
    fields, targets and original construction spec stay fixed. Geometry-only
    grants require the numeric limits for each requested geometry action and
    never introduce conductor permissions.
    This does not authorize a change to an existing run's immutable manifest.
    """
    original=ResponseDesignPlan.model_validate(plan)
    grant=ResponsePermissionGrant.model_validate(permissions)
    if original.base_spec.network_kind!='distribution':
        raise ValueError('Equipment/topology permission tiers currently support distribution only')
    before=original.model_dump(mode='json');revised=original.model_dump(mode='json')
    search=revised['search'];added=[]
    actions=('replace_conductor',) if grant.max_changed_conductors else ()
    if grant.tier=='equipment_topology':actions+=('rewire_branch',)
    if grant.tier=='equipment_geometry':actions+=('scale_layout','scale_subtree')
    if grant.tier=='geometry':
        if grant.max_layout_scale_change is not None:actions+=('scale_layout',)
        if grant.max_branch_length_change is not None:actions+=('scale_subtree',)
    for action in actions:
        if action not in search['allowed_actions']:
            search['allowed_actions'].append(action);added.append(action)
    for field,value in grant.model_dump(exclude={'tier'},exclude_none=True).items():
        if field=='max_changed_conductors' and not value:continue
        search[field]=value
    plan=ResponseDesignPlan.model_validate(revised).model_dump(mode='json')
    changes={key:dict(before=value,after=plan['search'][key]) for key,value in before['search'].items()
             if value!=plan['search'][key]}
    return dict(status='draft',plan=plan,original_plan_hash=digest(before),
        revised_plan_hash=digest(plan),permissions=grant.model_dump(mode='json'),
        added_actions=added,permission_changes=changes,
        scope='New explicit permission contract. Preserve original construction, response targets, probes, injections and all ungranted budgets. No implicit escalation or feasibility guarantee.')


def permission_source_issues(request, original_plan, permissions):
    """Validate a proposed revision against the actual user's amendment text.

    The caller binds ``original_plan`` to its saved parent and supplies the
    immutable human amendment, never the model's rewritten request. Existing
    permissions remain the parent's responsibility; every changed limit needs
    affirmative, numerical source support here.
    """
    from .response_language import _layout_permission,_local_permission_issues,protected_permission_issues,_conductor_limits
    from .research_source import source_sentences
    original=ResponseDesignPlan.model_validate(original_plan)
    grant=ResponsePermissionGrant.model_validate(permissions)
    revised=ResponseDesignPlan.model_validate(prepare_permission_revision(original,grant)['plan'])
    issues=protected_permission_issues(request,revised.search)
    if grant.max_changed_conductors:
        caps=[]
        for clause in source_sentences(request):
            if re.search('do not|don[\\x27\u2019]t|cannot|can[\\x27\u2019]t|never|forbid|forbidden|no (?:new )?(?:equipment|conductor)|\u4e0d\u5141\u8bb8|\u7981\u6b62|\u4e0d\u5f97',clause,re.I):continue
            if not re.search('allow|authorize|replace|\u66f4\u6362|\u5141\u8bb8',clause,re.I):continue
            caps.extend(_conductor_limits(clause))
        if not caps or grant.max_changed_conductors>min(caps):
            issues.append('The user amendment does not grant this numeric conductor-change permission.')
    if grant.max_layout_scale_change is not None:
        bound=_layout_permission(request)
        if bound is None or grant.max_layout_scale_change>bound+1e-9:
            issues.append('The user amendment does not grant this numeric uniform geometry permission.')
    # Check only local actions introduced or explicitly revised by this grant;
    # do not require the user to repeat unrelated saved-parent permissions.
    fields=grant.model_dump(exclude_none=True)
    local_actions=[]
    if 'max_branch_length_change' in fields:local_actions.append('scale_subtree')
    if 'max_rewired_lines' in fields:local_actions.append('rewire_branch')
    local_search=revised.search.model_copy(update={'allowed_actions':local_actions})
    issues.extend(_local_permission_issues(request,local_search))
    return issues
