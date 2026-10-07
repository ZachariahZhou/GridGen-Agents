"""Auditable structured calls; parsing failures are distinct from transport failures."""
import copy
import json
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .artifacts import atomic_json
from .planning_diagnostics import PlanningValidationError


class StructuredPlanningError(PlanningValidationError):
    """A returned response failed schema validation, and can be corrected once."""

    def __init__(self, message, cause=None):
        facts = [dict(location=list(e['loc']), error_type=e['type'], observation=e['msg'])
                 for e in cause.errors(include_url=False, include_input=False)] if isinstance(cause, ValidationError) else [
                     dict(location=[], error_type='structured_output_parse', observation=message)]
        super().__init__(message, 'schema_error', facts)


def _plain(value):
    if hasattr(value,'model_dump'):
        return value.model_dump(mode='json')
    if isinstance(value,dict):return {k:_plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [_plain(v) for v in value]
    if value is None or isinstance(value,(str,int,float,bool)):return value
    return str(value)


def _response(raw):
    # Save generated text/tool arguments and usage, never HTTP headers/client settings.
    fields=('content','tool_calls','invalid_tool_calls','usage_metadata')
    saved={k:_plain(raw.get(k) if isinstance(raw,dict) else getattr(raw,k,None)) for k in fields}
    metadata=raw.get('response_metadata',{}) if isinstance(raw,dict) else getattr(raw,'response_metadata',{})
    saved['response_metadata']={k:_plain(metadata[k]) for k in ('model_name','model','finish_reason','token_usage') if k in metadata}
    return saved


def _argument_audit(raw, schema_name):
    """Recover a complete object only for auditing a redundant-closing error."""
    calls=(raw.get('tool_calls') or [])+(raw.get('invalid_tool_calls') or [])
    if len(calls)!=1 or calls[0].get('name')!=schema_name:return None
    arguments=calls[0].get('args')
    if not isinstance(arguments,str):return None
    try:payload,end=json.JSONDecoder().raw_decode(arguments.lstrip())
    except ValueError:return None
    suffix=arguments.lstrip()[end:]
    if not isinstance(payload,dict) or not suffix.strip() or any(c not in '} \t\r\n]' for c in suffix):return None
    return payload


class StructuredPlanner:
    def __init__(self,model,schema,workspace):
        self.schema=schema
        self.runnable=model.with_structured_output(schema,method='function_calling',include_raw=True)
        self.directory=Path(workspace)/'model_calls'/uuid4().hex
        self.paths=[]
        self.record=None

    def invoke(self,messages):
        path=self.directory/f'attempt_{len(self.paths)+1:02d}.json'
        self.paths.append(str(path))
        self.record=dict(schema=self.schema.__name__,messages=_plain(messages),status='calling')
        atomic_json(path,self.record)
        try:
            envelope=self.runnable.invoke(messages)
        except Exception as exc:
            self.record.update(status='call_error',error_type=type(exc).__name__,error=str(exc))
            atomic_json(path,self.record)
            raise
        if isinstance(envelope,dict) and {'raw','parsed','parsing_error'}<=set(envelope):
            self.record['raw']=_response(envelope['raw'])
            parsed=envelope['parsed']
            error=envelope['parsing_error']
        else:
            # Injected/offline models may directly return the structured payload.
            parsed=envelope;error=None
            self.record['structured_response']=_plain(parsed)
        self.record['parsed']=_plain(parsed)
        invalid_calls=(self.record.get('raw') or {}).get('invalid_tool_calls') or []
        if invalid_calls:
            raw=self.record['raw']
            audit=_argument_audit(raw,self.schema.__name__)
            facts=[dict(location=[],error_type='invalid_tool_call',tool_name=call.get('name'),
                supplied_arguments=call.get('args'),observation=call.get('error') or 'Tool arguments are not valid JSON',
                audit_recovery='complete_object_extra_closers' if audit is not None else None)
                for call in invalid_calls]
            message='Invalid tool-call response: '+'; '.join(str(f['observation']) for f in facts)
            failure=StructuredPlanningError(message)
            failure.code='response_encoding';failure.facts=facts
            self.record['argument_audit']=dict(scope='audit_only_never_parsed',candidate=audit)
            self.invalid(message)
            raise failure
        if error is not None:
            self.invalid(str(error))
            raise StructuredPlanningError(str(error), error) from error
        try:
            payload=parsed.model_dump() if hasattr(parsed,'model_dump') else parsed
            # Revalidate original tool arguments when supplied. Dumping a parsed
            # model first loses the distinction between caller input and defaults
            # inserted by validators (needed for research parameter provenance).
            calls=[call for call in (self.record.get('raw') or {}).get('tool_calls') or []
                   if call.get('name')==self.schema.__name__]
            if len(calls)==1 and isinstance(calls[0].get('args'),dict):payload=calls[0]['args']
            result=self.schema.model_validate(payload)
            if not calls and isinstance(parsed,self.schema):
                # Public data was revalidated above; preserve captured private
                # input provenance in direct typed-provider responses as well.
                result.__pydantic_private__=copy.deepcopy(parsed.__pydantic_private__)
        except ValidationError as exc:
            self.invalid(str(exc))
            raise StructuredPlanningError(str(exc), exc) from exc
        self.record['status']='parsed'
        atomic_json(path,self.record)
        return result

    def invalid(self,error):
        self.record.update(status='invalid',validation_error=str(error))
        atomic_json(Path(self.paths[-1]),self.record)

    def valid(self):
        self.record['status']='valid'
        atomic_json(Path(self.paths[-1]),self.record)

    def correction_payload(self):
        return self.record.get('raw',self.record.get('structured_response',self.record.get('parsed')))

    def candidate_payload(self):
        """Recover returned arguments even when parsing failed, for change audits."""
        payload = self.record.get('parsed') or self.record.get('structured_response')
        if isinstance(payload, dict):
            return payload
        raw = self.record.get('raw') or {}
        calls=(raw.get('tool_calls') or []) + (raw.get('invalid_tool_calls') or [])
        if len(calls)!=1:return None
        for call in calls:
            if call.get('name') != self.schema.__name__:
                continue
            args = call.get('args')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    return _argument_audit(raw,self.schema.__name__)
            if isinstance(args, dict):
                return args
        return None
