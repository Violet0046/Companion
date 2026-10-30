"""Task failure diagnostics and bounded retry policy shared by both memory workers."""
import re
import sqlite3


class RetryableTaskError(ValueError):
    """The input changed during generation; a fresh snapshot can succeed."""


class LeaseLostError(RuntimeError,ValueError):
    """Execution ownership expired; this is not a model/evidence failure."""


def failure_kind(exc):
    from openai import APIConnectionError,APIStatusError,APITimeoutError
    if isinstance(exc,RetryableTaskError):
        return 'input_changed'
    if isinstance(exc,(TimeoutError,APITimeoutError)):
        return 'model_timeout'
    if isinstance(exc,(APIConnectionError,ConnectionError)):
        return 'transport'
    if isinstance(exc,APIStatusError):
        return 'configuration' if exc.status_code in (400,401,403,404) else 'provider_status'
    if isinstance(exc,sqlite3.Error):
        return 'storage'
    if isinstance(exc,ValueError):
        return 'validation'
    return 'internal'


def can_retry(exc):
    from openai import APIConnectionError, APIStatusError
    cause=exc;seen=set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if type(cause).__name__ in ('UnsupportedProtocol','InvalidURL'):
            return False
        cause=cause.__cause__
    if isinstance(exc, (RetryableTaskError, APIConnectionError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in (408, 409, 429) or exc.status_code >= 500
    if isinstance(exc, sqlite3.OperationalError):
        return 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
    # Invalid JSON, evidence, credentials and configuration cannot be fixed by
    # sending the identical request three times. Keep the range for manual retry.
    return False


def describe_error(exc):
    parts=[]
    seen=set()
    while exc is not None and id(exc) not in seen and len(parts)<4:
        seen.add(id(exc))
        text=str(exc).strip()
        # Provider bodies may echo request text. Validation errors can also
        # embed a whole private document; only retain the reason and field path.
        if hasattr(exc,'errors'):
            errors=exc.errors(include_input=False,include_url=False)
            text='; '.join('.'.join(map(str,e['loc']))+': '+e['msg'] for e in errors[:5])
        if hasattr(exc,'status_code'):
            text=f'模型服务返回 HTTP {exc.status_code}'
        text=re.sub(r'https?://[^\s\]\)]+','[服务地址]',text)
        text=re.sub(r'(?i)(bearer\s+|api[_-]?key[=: ]+)[^\s,;]+',r'\1[隐藏]',text)
        text=re.sub(r'\bsk-[A-Za-z0-9_-]+','[隐藏]',text)
        parts.append(type(exc).__name__+': '+text[:350])
        exc=exc.__cause__
    return ' <- '.join(parts)[:1000]
