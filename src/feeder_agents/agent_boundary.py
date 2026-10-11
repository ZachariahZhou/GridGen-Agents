"""Preserve human-source requirements and complete verified tool handoffs."""
import json
from pathlib import Path
import re
import uuid

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphBubbleUp

from .artifacts import atomic_json, digest


LANGUAGE_TOOLS = {'design_from_language', 'design_research_grid', 'design_electrical_response'}
READ_PREFIXES = ('read_', 'describe_', 'inspect_', 'list_', 'lookup_', 'recall_', 'search_')
STRUCTURED_GENERATION_TOOLS = {'generate_cases', 'plan_experiment', 'execute_plan',
    'plan_paired_study', 'execute_paired_study'}


def new_construction_request(text):
    """Distinguish a construction verb from a noun in a revision request."""
    return bool(re.search('^\\s*(?:(?:please|now)\\s+)*(?:generate|create|design)\\b|^\\s*(?:\u8bf7|\u73b0\u5728|\u91cd\u65b0)*(?:\u751f\u6210|\u8bbe\u8ba1)', text, re.I))


def explicit_revision_overrides(text):
    """Accept structured amendments only when present in the human source."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
            if isinstance(value, dict):
                yield value
        except ValueError:
            continue


def human_messages(state):
    messages = state.get('messages', [])
    result = []
    for message in messages:
        role = getattr(message, 'type', None)
        content = getattr(message, 'content', None)
        if isinstance(message, dict):
            role = message.get('role', message.get('type')); content = message.get('content')
        if role not in {'human', 'user'}:
            continue
        if isinstance(content, list):
            content = '\n'.join(item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text')
        if isinstance(content, str) and content.strip():
            result.append(content)
    return result


def source_request(turns):
    """Keep short follow-ups bound to the user's earlier request, never AI paraphrases."""
    if len(turns) == 1:
        return turns[0]
    latest = turns[-1]
    if new_construction_request(latest):
        return latest
    return '\n\n'.join(f'User turn {i + 1}:\n{text}' for i, text in enumerate(turns))


def payload(message):
    try:
        result = json.loads(message.content)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def prior_response_parent(state, root):
    """Only prior observed tool outputs may establish a response amendment parent."""
    from .agent_results import response_parent
    for message in reversed(state.get('messages', [])):
        if getattr(message, 'type', '') != 'tool':
            continue
        record = payload(message)
        candidates = [record.get('result_id'), record.get('delivery', {}).get('result_id')]
        for value in (record.get('outcome', {}).get('directory'), record.get('directory')):
            if value:
                candidates.append(Path(value).name)
        for candidate in candidates:
            if not candidate:
                continue
            try:
                plan, saved = response_parent(root, candidate)
                return candidate, plan, saved
            except (ValueError, OSError, KeyError):
                continue
    return None


def prior_grid_parent(state, root, kind):
    from .agent_results import read_grid_result
    for message in reversed(state.get('messages', [])):
        if getattr(message, 'type', '') != 'tool':
            continue
        record = payload(message)
        candidate = record.get('result_id') or record.get('delivery', {}).get('result_id')
        if not candidate:
            directory = record.get('outcome', {}).get('directory') or record.get('directory')
            candidate = Path(directory).name if directory else None
        if candidate:
            try:
                saved = read_grid_result(root, candidate, kind)
                return saved['result_id']
            except (OSError, ValueError, KeyError):
                continue
    return None


class OriginalRequestMiddleware(AgentMiddleware):
    """Tool failures become bounded feedback; successful terminal artifacts end the turn."""

    def __init__(self, root):
        self.root = Path(root)

    def _message(self, request, value):
        return ToolMessage(content=json.dumps(value, ensure_ascii=False),
            tool_call_id=request.tool_call['id'], name=request.tool_call['name'])

    def _prepare(self, request):
        turns = human_messages(request.state)
        if not turns:
            return request, None
        name = request.tool_call['name']; args = dict(request.tool_call.get('args', {}))
        source = source_request(turns)
        if not name.startswith(READ_PREFIXES):
            from .research_source import response_interval_issues
            from .request_contract import request_guard
            issues = response_interval_issues(source)
            blocked = request_guard(source)
            if issues or blocked:
                return request, self._message(request, blocked or dict(status='needs_clarification', issues=issues))
            # Storage labels generated by the model are not electrical design
            # requirements. Make them valid deterministically; never rename an
            # identifier explicitly supplied by the human or an existing parent.
            for field in ('design_id', 'revision_id', 'experiment_id', 'study_id', 'run_id'):
                value=args.get(field)
                if not isinstance(value,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',value):
                    continue
                if value in turns[-1]:
                    return request, self._message(request, dict(status='needs_clarification',
                        issues=[f'The requested {field} is not a valid storage ID. Use 1–64 ASCII letters, digits, underscores or hyphens, starting with a letter or digit.']))
                stem=re.sub(r'[^A-Za-z0-9_-]+','_',value).strip('_-') or 'design'
                prepared=stem[:47]+'_'+digest(value)[:12]
                atomic_json(self.root/'identifier_aliases'/f'{digest(value)}.json',
                    dict(field=field,proposed=value,prepared=prepared,origin='model_generated_storage_label'))
                args[field]=prepared
        if name in LANGUAGE_TOOLS:
            if len(turns) > 1 and not new_construction_request(turns[-1]):
                parent = prior_response_parent(request.state, self.root)
                if parent is not None:
                    return request, self._message(request, dict(status='needs_permission_revision',
                        parent_id=parent[0], issues=['This is a follow-up to a saved response design. '
                        'Use revise_electrical_response_permissions with parent_id and only explicitly granted numeric limits. '
                        'If the user supplied no new permission, report the unchanged unmet target; do not regenerate.']))
            args['request'] = source
        elif name in STRUCTURED_GENERATION_TOOLS:
            # Direct structured APIs remain available to Python callers. Within
            # a conversation, only a complete, human-authored JSON specification
            # can use the raw generator; mixed prose still needs source review.
            try:
                supplied = json.loads(turns[-1])
            except ValueError:
                supplied = None
            exact_spec = (name == 'generate_cases' and isinstance(supplied, dict)
                and set(supplied) <= {'spec', 'experiment_id'} and 'spec' in supplied
                and supplied['spec'] == args.get('spec')
                and ('experiment_id' not in supplied or supplied['experiment_id'] == args.get('experiment_id')))
            if not exact_spec:
                return request, self._message(request, dict(status='needs_source_bound_tool',
                    issues=['Use design_from_language, design_research_grid or design_electrical_response '
                        'with the complete user request. Raw parameter execution cannot establish '
                        'natural-language requirement or research-task fulfillment.']))
        elif name in {'revise_existing_case', 'revise_existing_transmission'}:
            args['feedback'] = turns[-1]
            if name == 'revise_existing_transmission':
                overrides = {key: args[key] for key in ('targets', 'allowed_repairs', 'allow_topology_changes')
                    if args.get(key) is not None and not (key == 'allow_topology_changes' and args[key] is False)}
                if overrides and not any(all(key in value and value[key] == item for key, item in overrides.items())
                        for value in explicit_revision_overrides(turns[-1])):
                    return request, self._message(request, dict(status='needs_clarification',
                        issues=['Transmission target/repair overrides need an explicit structured amendment in the human request. '
                            'Omit these optional overrides to repair within the saved contract; no new topology permission is inferred.']))
            requested = args.get('parent_id', '')
            if not requested or not re.search(r'(?<![\w-])'+re.escape(requested)+r'(?![\w-])', turns[-1]):
                previous = prior_grid_parent(request.state, self.root,
                    'experiment' if name == 'revise_existing_case' else 'transmission')
                if previous:
                    args['parent_id'] = previous
        elif name == 'revise_electrical_response_permissions':
            from .response_permissions import permission_source_issues
            parent = prior_response_parent(request.state, self.root)
            if parent is None:
                return request, self._message(request, dict(status='needs_clarification',
                    issues=['No verified response parent has been observed. Generate or read the original result first.']))
            parent_id, original_plan, _ = parent
            if args.get('parent_id') not in (None, parent_id):
                return request, self._message(request, dict(status='needs_clarification',
                    issues=['The amendment parent differs from the observed original response. Read the intended parent first.']))
            grants = [args.get('permissions', {})] + (args.get('followup_permissions') or [])
            issues = [issue for grant in grants for issue in permission_source_issues(turns[-1], original_plan, grant)]
            if issues:
                return request, self._message(request, dict(status='needs_clarification', issues=issues,
                    notice='No permissions or original targets were changed.'))
            # Parent identity is supplied by verified storage, not regenerated model arguments.
            args.update(parent_id=parent_id, plan=None)
        call = dict(request.tool_call, args=args)
        return request.override(tool_call=call), None

    def _failure(self, request, error):
        error_id = uuid.uuid4().hex
        record = dict(error_id=error_id, tool=request.tool_call['name'], error_type=type(error).__name__,
            error=str(error), args=request.tool_call.get('args', {}))
        atomic_json(self.root/'tool_errors'/f'{error_id}.json', record)
        return self._message(request, dict(status='tool_error', error_type=type(error).__name__,
            error=str(error), error_id=error_id, retryable=False,
            instruction='Inspect the exact tool argument or saved artifact family before another attempt. '
                        'Do not regenerate a verified model merely because a reader failed. '
                        'Use read_grid_result with the exact returned result_id. No success is implied.'))

    def wrap_tool_call(self, request, handler):
        try:
            prepared, blocked = self._prepare(request)
            return blocked if blocked is not None else handler(prepared)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return self._failure(request, exc)

    async def awrap_tool_call(self, request, handler):
        try:
            prepared, blocked = self._prepare(request)
            return blocked if blocked is not None else await handler(prepared)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return self._failure(request, exc)

    @hook_config(can_jump_to=['end'])
    def before_model(self, state, runtime):
        messages = state.get('messages', [])
        turns = human_messages(state)
        if turns and re.search(r'\bthen\b|\bafter(?:wards)?\b|\bcompare\b|\bcomparison\b|'
                '\\b(?:two|three|multiple|several|[2-9])\\s+(?:local\\s+)?revisions\\b|\u7136\u540e|\u4e4b\u540e|\u5206\u522b|\u5bf9\u6bd4|\u6bd4\u8f83', turns[-1], re.I):
            # One verified artifact cannot establish completion of a compound
            # request. Keep orchestration active for its remaining operations.
            return None
        # Consider only the just-completed batch in the current user turn.
        tools = []
        for message in reversed(messages):
            if getattr(message, 'type', '') != 'tool':
                break
            tools.append(payload(message))
        if not tools or any(not record or (record.get('status') not in (None, 'completed')
                and record.get('delivery', {}).get('terminal_reason') != 'no_authorized_actions') for record in tools):
            return None
        deliveries = [record['delivery'] for record in tools if record.get('delivery', {}).get('ready') is True
            or record.get('delivery', {}).get('terminal_reason') == 'no_authorized_actions']
        if not deliveries:
            return None
        text = '\n\n'.join(('Verified model ready.' if item['ready'] else
            'Response target not met. The current contract allows no model edits; no permission was expanded.')+
            '\n\n'+item.get('verified_report', '')+'\n\nArtifact directory: '+item['directory']+
            '\nResult ID: '+item['result_id'] for item in reversed(deliveries))
        return {'jump_to': 'end', 'messages': [AIMessage(content=text)]}

    @hook_config(can_jump_to=['end'])
    async def abefore_model(self, state, runtime):
        return self.before_model(state, runtime)
