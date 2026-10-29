"""记忆抽取的唯一实现：读取会话、验证模型输出、写入事实和画像。"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from companion_ever.storage.memory_job_repo import MemoryJobRepo
from companion_ever.storage.db import transaction

from companion_ever.services.llm_json import parse_json_response
from companion_ever.domain.enums import BlockType, NoteType
from companion_ever.prompts import EXTRACTION_SYSTEM
from companion_ever.utils.time import parse_sqlite_ts


@dataclass
class ExtractionReport:
    facts_created: int = 0
    blocks_updated: int = 0
    conversations_processed: int = 0
    session_start: datetime | None = None
    session_end: datetime | None = None
    errors: list[str] = field(default_factory=list)
    job_id: str | None = None
    job_status: str | None = None


def report_from_state(state: dict) -> ExtractionReport:
    messages = state.get("messages") or []
    return ExtractionReport(
        facts_created=state.get("facts_created", 0),
        blocks_updated=state.get("blocks_updated", 0),
        conversations_processed=len(messages),
        session_start=parse_sqlite_ts(messages[0].created_at) if messages else None,
        session_end=parse_sqlite_ts(messages[-1].created_at) if messages else None,
        errors=list(state.get("errors") or []),
        job_id=state.get("job_id"), job_status=state.get("job_status"),
    )


class ExtractionService:
    def __init__(self, conversation_service, note_service, block_service, llm_client):
        self.conversation_service = conversation_service
        self.note_service = note_service
        self.block_service = block_service
        self.llm_client = llm_client
        self.conn = note_service.note_repo.conn
        self.jobs = MemoryJobRepo(self.conn)

    def extract_last_closed_session(self, user_id: str, persona_id: str) -> ExtractionReport:
        return report_from_state(self.run({"user_id": user_id, "persona_id": persona_id}))

    def run(self, state: dict) -> dict:
        user_id, persona_id = state["user_id"], state["persona_id"]
        result = {**state, "messages": [], "facts_created": 0, "blocks_updated": 0,
                  "errors": [], "extraction_failed": False}
        job = self.jobs.enqueue(user_id, persona_id)
        if not job:
            return result
        result.update(job_id=job['id'], job_status=job['status'])
        claimed = self.jobs.claim(job['id'])
        if not claimed:
            job = self.jobs.get(job['id'])
            result['job_status'] = job['status']
            if job['last_error']:
                result['errors'] = [job['last_error']]
            return result
        token = claimed['lease_token']
        stop = threading.Event()
        def heartbeat():
            try:
                while not stop.wait(30):
                    if not self.jobs.renew(job['id'], token):
                        break
            finally:
                self.conn.close_current()
        thread = threading.Thread(target=heartbeat,daemon=True,name='memory-lease')
        thread.start()
        try:
            return self._execute(result, claimed, token)
        except Exception as exc:
            self.jobs.fail(job['id'], token, f'{type(exc).__name__}: {exc}')
            result.update(facts_created=0, blocks_updated=0, extraction_failed=True, job_status='failed', errors=[f'{type(exc).__name__}: {exc}'])
            return result
        finally:
            stop.set()
            thread.join(timeout=1)

    def _execute(self, result, job, token):
        user_id, persona_id = job['user_id'], job['persona_id']
        messages = self.conversation_service.get_messages_between(
            user_id=user_id, persona_id=persona_id,
            since_id=job['after_message_id'], until_id=job['through_message_id'], limit=200)
        result['messages'] = messages
        blocks = self.block_service.get_blocks(user_id=user_id, persona_id=persona_id)
        existing = {b.block_type.value: b.fields for b in blocks
                    if b.block_type != BlockType.PERSONA_RUNTIME}
        transcript = "\n".join(f"[message_id={m.id}, UTC={m.created_at or ''}] {m.role}: {m.content}" for m in messages)
        prompt = [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content":
                "## 现有状态（上次抽取的结果）\n" + json.dumps(existing, ensure_ascii=False)
                + f"\n\n## 待处理的对话（{len(messages)} 条）\n{transcript}"},
        ]
        data = parse_json_response(self.llm_client.chat(prompt))
        if not isinstance(data, dict) or not all(isinstance(data.get(k), list) for k in ("facts", "blocks")):
            raise ValueError("抽取输出必须包含 facts 和 blocks 数组")
        result["llm_result"] = data
        with transaction(self.conn):
            self.jobs.require_owner(job['id'], token)
            for fact in data["facts"]:
                try:
                    note_type = NoteType(fact["type"])
                    if note_type.value.startswith("persona_"):
                        raise ValueError("会话抽取不能修改角色设定")
                    note_id, created = self.note_service.remember(
                        user_id, persona_id, note_type, fact["content"],
                        source_quote=fact.get("source_quote"),
                        importance=float(fact.get("importance", 0.5)))
                    source_ids = fact.get('source_message_ids')
                    quote = fact.get('source_quote')
                    if source_ids is None:
                        source_ids = [m.id for m in messages if m.role=='user' and quote and quote in m.content]
                    for mid in source_ids:
                        source = next((m for m in messages if m.id==mid and m.role=='user'), None)
                        if source is None or not quote or quote not in source.content:
                            raise ValueError('记忆来源不属于本批用户消息或引文不匹配')
                        self.conn.execute('INSERT OR IGNORE INTO note_evidence(note_id,message_id,quote) VALUES (?,?,?)',
                                          (note_id,mid,quote))
                    result["facts_created"] += int(created)
                except (ValueError, TypeError, KeyError) as exc:
                    result["errors"].append(f"fact 无效: {exc}")
            for update in data["blocks"]:
                try:
                    block_type = BlockType(update["block_type"])
                    fields = dict(update["fields"])
                    if block_type == BlockType.PERSONA_RUNTIME:
                        fields = {k: v for k, v in fields.items() if k == "current_topic"}
                    if fields:
                        self.block_service.update_block_fields(user_id, persona_id, block_type, fields)
                        result["blocks_updated"] += 1
                except (ValueError, TypeError, KeyError) as exc:
                    result["errors"].append(f"block 无效: {exc}")
            if result['errors']:
                raise ValueError('; '.join(result['errors']))
            self.jobs.succeed(job['id'], token)
        result.update(job_status='succeeded', extraction_failed=False)
        return result
