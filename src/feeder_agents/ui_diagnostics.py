"""Local UI failure records, including errors before a design directory exists."""
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .artifacts import atomic_json


def _secret_values():
    from dotenv import dotenv_values
    values=list(os.environ.items())
    # Match configured_model's startup-directory lookup without exporting file values.
    settings=Path.cwd()/'.env'
    if settings.is_file():
        # The configured model interpolates dotenv values. Redact both forms.
        for interpolate in (False,True):
            try:
                values.extend(dotenv_values(settings,interpolate=interpolate).items())
            except (OSError,UnicodeError,ValueError):
                # Diagnostic logging must not mask the original failure.
                continue
    secrets={value for key,value in values if isinstance(value,str) and len(value)>=8
             and any(word in key.upper() for word in ('KEY','TOKEN','SECRET','PASSWORD'))}
    return sorted(secrets,key=len,reverse=True)


def redact_text(value):
    text=str(value)
    for secret in _secret_values():
        text=text.replace(secret,'[REDACTED]')
    return text


def record_failure(workspace, exc, request, design_id):
    error_id = 'error_' + uuid4().hex[:12]
    detail = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    path = Path(workspace) / 'ui_errors' / f'{error_id}.json'
    atomic_json(path, dict(error_id=error_id, time=datetime.now(timezone.utc).isoformat(), error_type=type(exc).__name__,
                           traceback=redact_text(detail), request=redact_text(request), design_id=redact_text(design_id)))
    return error_id
