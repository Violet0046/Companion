"""Lightweight model-call records. No prompts, responses, graph artifacts or global store."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import threading


LAYERS = ('chat', 'extraction', 'summary', 'coordination')
STATUSES = ('ok', 'error', 'aborted')
_context = ContextVar('model_call_context', default=None)


@dataclass
class CallContext:
    layer: str
    user_id: str
    persona_id: str
    generation_id: str | None = None
    job_id: str | None = None
    attempt_id: str | None = None
    attempt: int = 1
    purpose: str | None = None
    calls: list = field(default_factory=list)
    _index: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next_index(self):
        with self._lock:
            self._index += 1
            return self._index


@contextmanager
def call_context(layer, user_id, persona_id, **identifiers):
    if layer not in LAYERS:
        raise ValueError('未知调用层级')
    context = CallContext(layer, user_id, persona_id, **identifiers)
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def current_call_context():
    return _context.get()


def default_call_dir(db_path):
    return Path(db_path).parent / 'calls'


def summarize_calls(records):
    result = {'calls': len(records), 'ok': sum(r['status'] == 'ok' for r in records),
              'errors': sum(r['status'] == 'error' for r in records),
              'aborted': sum(r['status'] == 'aborted' for r in records),
              'elapsed_ms': round(sum(r['duration_ms'] for r in records), 2)}
    for key in ('input_tokens', 'output_tokens', 'cached_input_tokens'):
        known = [r[key] for r in records if r.get(key) is not None]
        result[key] = sum(known) if known else None
        result[key + '_reported_calls'] = len(known)
    result['cache_hit_ratio'] = (result['cached_input_tokens'] / result['input_tokens']
        if records and result['cached_input_tokens_reported_calls'] == len(records)
        and result['input_tokens_reported_calls'] == len(records) and result['input_tokens'] else None)
    return result


class CallLogStore:
    def __init__(self, base_dir, *, tz_offset_hours=8):
        self.base_dir = Path(base_dir)
        self.zone = timezone(timedelta(hours=tz_offset_hours))
        self._lock = threading.Lock()
        self.write_error = False

    def today(self):
        return datetime.now(self.zone).date().isoformat()

    def _path(self, day):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('日期必须使用 YYYY-MM-DD')
        return self.base_dir / (day + '.jsonl')

    def append(self, record):
        if os.environ.get('COMPANION_CALL_LOG', '1').lower() in ('0', 'false', 'off', 'no'):
            return
        try:
            day = datetime.fromisoformat(record['started_at']).astimezone(self.zone).date().isoformat()
            content = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
            with self._lock:
                self.base_dir.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(self._path(day), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, 'a+b', buffering=0) as output:
                    size = output.seek(0, 2)
                    prefix = b''
                    if size:
                        output.seek(-1, 2)
                        if output.read(1) != b'\n':
                            prefix = b'\n'
                    payload = prefix + content
                    if output.write(payload) != len(payload):
                        raise OSError('incomplete call-log write')
        except Exception:
            if not self.write_error:
                logging.getLogger(__name__).warning('模型调用记录写入失败，业务继续执行')
            self.write_error = True

    def dates(self):
        if not self.base_dir.is_dir():
            return []
        return sorted((p.stem for p in self.base_dir.glob('????-??-??.jsonl')
                       if p.is_file() and self._valid_date(p.stem)), reverse=True)

    @staticmethod
    def _valid_date(value):
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    def read(self, day, *, user_id=None, persona_id=None, layer=None, status=None, job_id=None):
        path = self._path(day)
        result = []
        try:
            with path.open(encoding='utf-8') as source:
                for line in source:
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict) or row.get('layer') not in LAYERS or row.get('status') not in STATUSES:
                            continue
                        if not all(isinstance(row.get(key), str) for key in ('id', 'started_at', 'user_id', 'persona_id')):
                            continue
                        if type(row.get('duration_ms')) not in (int, float) or row['duration_ms'] < 0:
                            continue
                        if any(value is not None and row.get(key) != value for key, value in
                               (('user_id', user_id), ('persona_id', persona_id), ('layer', layer),
                                ('status', status), ('job_id', job_id))):
                            continue
                        result.append(row)
                    except (ValueError, TypeError):
                        continue  # Ignore an interrupted last line; never rewrite history while reading.
        except FileNotFoundError:
            pass
        return sorted(result, key=lambda r: (r['started_at'], r['id']), reverse=True)
