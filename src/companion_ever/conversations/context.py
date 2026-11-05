"""system prompt 拼装。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from companion_ever.characters.access import Authorizer
from companion_ever.characters.service import PersonaService
from companion_ever.conversations.messages import ConversationService
from companion_ever.memory.retrieval.service import RetrievalService
from companion_ever.runtime.invariants import (
    check_prompt_invariants,
    strict_mode,
)
from companion_ever.memory.notes.service import NoteService
from companion_ever.memory.summaries.blocks import BlockService
from companion_ever.prompts import CHAT_SYSTEM, compose, load
from companion_ever.utils.time import describe_gap, format_now, parse_sqlite_ts


# 统一的时间转换实现在 utils/timeutil.py（API 层也用同一份，避免两边不一致）
from companion_ever.utils.timeutil import utc_ts_to_local as _utc_ts_to_local_impl


def _utc_ts_to_local(ts_str: str | None, tz_offset_hours: int) -> str:
    """把 SQLite UTC 时间字符串转成用户本地时区。

    SQLite 的 CURRENT_TIMESTAMP 返回 UTC 时间。
    这里转成本地时区以便 LLM 看到的"对话时间"与用户感知一致。
    """
    return _utc_ts_to_local_impl(ts_str, tz_offset_hours)


class PromptService:
    """system prompt 拼装 + LLM 友好的上下文。"""

    def __init__(
        self,
        authorizer: Authorizer,
        conversation_service: ConversationService,
        block_service: BlockService,
        note_service: NoteService,
        retrieval_service: RetrievalService,
        persona_service: PersonaService,
        tz_offset_hours: int = 8,
        recent_message_limit: int = 20,
        history_max_messages: int = 50,
        *, summary_service=None,
    ):
        self.authorizer = authorizer
        self.conversation_service = conversation_service
        self.block_service = block_service
        self.note_service = note_service
        self.retrieval_service = retrieval_service
        self.persona_service = persona_service
        self.tz_offset_hours = tz_offset_hours
        self.recent_message_limit = recent_message_limit
        self.history_max_messages = history_max_messages
        self.summary_service = summary_service

    def build(
        self,
        user_id: str,
        persona_id: str,
        query: str = "",
        current_message_id: int | None = None,
    ) -> dict:
        """拼装完整上下文，返回 LLM 调用所需的结构。

        Args:
            user_id, persona_id: 用户 + persona
            query: 当前用户输入（用于 query-aware 检索；空字符串则跳过检索）

        Returns:
            {
                "system_prompt": str,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    ...
                ]
            }

        Raises:
            PermissionError: user 不存在 / 不拥有 persona
        """
        # 0. 权限校验
        self.authorizer.require_persona(user_id, persona_id)

        # 1. 读所有需要的数据
        persona_basics = self._get_persona_basics(user_id, persona_id)
        # 用 history_max_messages（不是 recent_message_limit）——
        # 防止 context 无限增长，同时保留足够历史给 LLM
        if current_message_id is not None:
            fetched = self.conversation_service.get_messages_between(
                user_id=user_id, persona_id=persona_id, until_id=current_message_id,
                limit=self.history_max_messages + 1)
            # Keep the current user input as the final native message.
            history = list(fetched)
            earlier = [m for m in history if m.id != current_message_id]
            last_conv_at = earlier[-1].created_at if earlier else None
            current = next((m for m in history if m.id == current_message_id), None)
        else:
            history = self._get_recent_messages(user_id, persona_id, limit=self.history_max_messages)
            last_conv_at = self.conversation_service.get_last_conversation_at(user_id, persona_id)

        # 2. 拼装 system_prompt
        # Collect before note retrieval: a concurrent memory commit may duplicate
        # evidence briefly, but must not remove raw text before notes are readable.
        external = self.conversation_service.external_context(user_id, persona_id)
        related_notes = []
        if query:
            related_notes = self.retrieval_service.retrieve(
                user_id=user_id,
                persona_id=persona_id,
                query=query,
            )
        derived = ''
        if self.summary_service is not None:
            derived = self.summary_service.prompt_text(user_id, persona_id)
        external_payload=self._external_context_payload(user_id,persona_id,external)
        system_prompt=self._render_system_prompt(persona_basics,external=bool(external_payload))
        context={'type':'application_context','time':self._render_time_section(last_conv_at,
            last_group_exchange=self.conversation_service.last_group_exchange(user_id,persona_id,before_id=current_message_id))}
        if related_notes:
            context['related_notes']=self._render_related_notes(related_notes,
                self.note_service.sources(user_id,persona_id,[n.id for n in related_notes]))
        if derived:context['summaries']=derived
        if current_message_id is not None and current is not None and current.quoted_message_id is not None:
            quoted=self.conversation_service.quote(user_id,persona_id,current.quoted_message_id)
            context['quoted_message']={**quoted,'created_at':_utc_ts_to_local(quoted['created_at'],self.tz_offset_hours)}
        if external_payload:context['external_context']=external_payload
        # Context is application-supplied data, never a synthetic assistant turn.
        messages=[{'role':'system','content':system_prompt},
                  {'role':'user','content':json.dumps(context,ensure_ascii=False)}]
        for msg in history:
            messages.append({
                "role": msg.role,
                "content": msg.content if msg.id == current_message_id else self._render_message(msg),
            })

        self._check_invariants(
            user_id, persona_id, system_prompt, messages,
            related_notes=related_notes,
            current_message_id=current_message_id,
        )

        return {
            "system_prompt": system_prompt,
            "messages": messages,
        }

    def build_group(self,user,persona,*,history,trigger_message_id,suggestion,conversation_id=None,invocation=None):
        """Group identities stay in the data envelope, never native assistant history."""
        self.authorizer.require_persona(user,persona)
        external = self.conversation_service.external_context(user, persona, current_group_id=conversation_id) if conversation_id is not None else []
        notes=[]
        current=next((m for m in history if m['id']==trigger_message_id),None)
        source=(invocation or {}).get('source_message') or current
        if conversation_id is not None and source is not None:
            notes=self.retrieval_service.retrieve(user,persona,source['content'][:2000],conversation_id=conversation_id,include_private=True)
        derived=self.summary_service.prompt_text(user,persona,conversation_id=conversation_id) if self.summary_service is not None and conversation_id is not None else ''
        external_payload=self._external_context_payload(user,persona,external)
        system=compose(load('conversations/group.txt'),{
            'persona_section':self._render_persona_basics(self._get_persona_basics(user,persona)),
            'self_persona_id':persona})
        if external_payload:system+='\n\n'+load('fragments/external_context.txt').strip()
        payload={'now':format_now(self.tz_offset_hours),'trigger_message_id':trigger_message_id,
                 'root_message':current,'invocation':invocation or {},'messages':history}
        if suggestion:payload['suggestion']=suggestion
        if notes:payload['related_notes']=self._render_related_notes(notes,self.note_service.sources(user,persona,[n.id for n in notes]))
        if derived:payload['summaries']=derived
        if external_payload:payload['external_context']=external_payload
        return {'system_prompt':system,'messages':[{'role':'system','content':system},
            {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]}

    def build_group_coordination(self,user,*,members,eligible_ids,history,trigger_message_id,spoken_ids,remaining,conversation_id=None,manual=False):
        cards=[{'persona_id':m['persona_id'],'joined_after_message_id':m.get('joined_after_message_id',0),'persona':self._get_persona_basics(user,m['persona_id'])} for m in members]
        if self.summary_service is not None and conversation_id is not None:
            for card in cards:
                card['relationship']=self.summary_service.prompt_text(user,card['persona_id'],conversation_id=conversation_id,relationship_only=True)
        system=load('conversations/coordination.txt')
        payload={'now':format_now(self.tz_offset_hours),'remaining':remaining,
            'trigger':'manual_continue' if manual else 'user_message','trigger_message_id':trigger_message_id,
            'already_spoken_ids':spoken_ids,'eligible_persona_ids':eligible_ids,'members':cards,'messages':history}
        return {'system_prompt':system,'messages':[{'role':'system','content':system},
            {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]}

    def build_proactive(self,user_id,persona_id,*,destinations,test=False):
        """One public group at most; private-only context never enters a group-capable request."""
        self.authorizer.require_persona(user_id, persona_id)
        group=next((d for d in destinations if d['kind']=='group'),None)
        derived=self.summary_service.prompt_text(user_id,persona_id,conversation_id=group['conversation_id']) if self.summary_service and group else (
            self.summary_service.prompt_text(user_id,persona_id) if self.summary_service else '')
        system=compose(load('conversations/proactive.txt'),{
            'persona_section':self._render_persona_basics(self._get_persona_basics(user_id,persona_id)),
            'self_persona_id':persona_id})
        payload={'now':format_now(self.tz_offset_hours),'self_persona_id':persona_id,'test_mode':test,'destinations':destinations}
        if derived:payload['summaries']=derived
        return {'system_prompt':system,'messages':[{'role':'system','content':system},
            {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]}

    def _check_invariants(
        self,
        user_id: str,
        persona_id: str,
        system_prompt: str,
        messages: list[dict],
        related_notes: list,
        current_message_id: int | None = None,
    ) -> None:
        """严格检查按需启用，不依赖日志或保存输入正文。"""
        if not strict_mode():
            return

        # --- 不变量检查 ---
        # 本轮输入是最后一条消息；检查其前面的最新历史，
        # 否则会把"历史停在本轮之前"误判成"历史停得太早"。
        last_db_message = None
        try:
            recent = self.conversation_service.get_recent_messages(
                user_id, persona_id, limit=2,
            )
            before = [m for m in recent if m.id != current_message_id]
            if before:
                last_db_message = before[-1].content
        except Exception:
            last_db_message = None

        # 只数"用户/角色记忆"，persona_basics 是人设定义不算
        total_notes = 0
        try:
            total_notes = len(self.note_service.find_notes(
                user_id=user_id, persona_id=persona_id,
                limit=1000,
            ))
        except Exception:
            total_notes = 0

        violations = check_prompt_invariants(
            messages=messages,
            system_prompt=system_prompt,
            retrieved_count=len(related_notes),
            last_db_message=last_db_message,
            total_notes=total_notes,
        )
        if violations and strict_mode():
            raise ValueError(
                "prompt 不变量被违反: " + "; ".join(v.message for v in violations)
            )

    # === 内部 helper ===

    def _external_context_payload(self, user, persona, sources):
        if not sources:
            return []
        sources = self.conversation_service.visible_external_context(user, persona, sources)
        if not sources:
            return []
        payload = []
        for source in sources:
            messages = [{**m, 'created_at': _utc_ts_to_local(m['created_at'], self.tz_offset_hours)}
                        for m in source['messages']]
            payload.append({k: source[k] for k in ('conversation_id', 'kind', 'name', 'omitted_earlier')})
            payload[-1]['messages'] = messages
        return payload

    def _get_recent_messages(
        self,
        user_id: str,
        persona_id: str,
        limit: int,
    ) -> list:
        """获取对话消息（跨 session 的全部历史）。

        关键设计：chat LLM 在对话过程中不应被 session_close 截断——
        之前的实现只取"上一个 session_close marker 之后"的消息，
        导致新 session 看不到旧 session 间的旧对话（要靠 extraction → notes → retrieval 间接补回）。

        现在改为：直接取所有 user/assistant 消息（session_close 行已被
        get_messages_between 自动过滤）。这样 chat LLM 在每轮对话时
        都看到完整历史，对话连贯性更好。

        Extraction 工作流仍然只看 [最新 marker, 上一 marker] 之间的对话——
        这是两个 LLM 职责的清晰分离。

        Args:
            user_id, persona_id: 用户 + persona
            limit: 最多取多少条（避免无限增长，按 id DESC + 反转得到最近 N 条）

        Returns:
            按 id 升序的最近 limit 条 user/assistant 消息
        """
        return self.conversation_service.get_messages_between(
            user_id=user_id,
            persona_id=persona_id,
            limit=limit,
        )

    def _get_persona_basics(
        self,
        user_id: str,
        persona_id: str,
    ) -> dict | None:
        """从角色模块读取设定并解析为 dict。

        Returns:
            dict (parsed JSON) or None (无 persona_basics)
        """
        row = self.persona_service.get_definition(user_id,persona_id)
        if row is None:
            return None
        try:
            return json.loads(row['content'])
        except (ValueError,TypeError):
            return {'_raw':row['content']}

    def _render_message(self, msg) -> str:
        """渲染单条对话消息（仅时间戳 + 内容，不重复 role tag）。

        格式：[2026-09-28 17:30:42] 你好（仅 user 消息带时间戳，assistant 不带）

        注意：
        1. 不再加 `user:` / `assistant:` 前缀——
           role 已经在 messages 列表的 role 字段中，避免 LLM 模仿格式
        2. 时间戳从 SQLite UTC 转成本地时区，与用户感知一致
        """
        # assistant 消息不带时间戳。
        # 原因（实测结论）：
        # 1. 它紧跟在 user 消息后面，时间戳冗余
        # 2. 所有消息都带 [时间戳] 前缀时，模型会学会在自己的回复里
        #    也加上同样的前缀（实测 2/2 出现），
        #    而且它往往会照抄上一条的时间戳 -> 回答里的时间是错的
        # 3. 只留 user 消息带时间戳时，模型不再会模仿（实测 0/2）
        if msg.role == "assistant":
            return msg.content
        ts = _utc_ts_to_local(msg.created_at, self.tz_offset_hours)
        text=f"[{ts}] {msg.content}"
        if msg.quoted_message_id is not None:
            quoted=self.conversation_service.quote(msg.user_id,msg.persona_id,msg.quoted_message_id)
            quoted['created_at']=_utc_ts_to_local(quoted['created_at'],self.tz_offset_hours)
            text+='\n[此历史消息引用的资料，不是新事实] '+json.dumps(quoted,ensure_ascii=False)
        return text

    def _render_system_prompt(self,persona_basics: dict | None,*,external=False) -> str:
        """Only identity, behavior and wire contracts belong in system."""
        system=compose(CHAT_SYSTEM,{'persona_section':self._render_persona_basics(persona_basics)})
        if external:system+='\n\n'+load('fragments/external_context.txt').strip()
        return system

    def _render_time_section(self, last_conv_at: str | None, *, last_group_exchange: dict | None = None) -> str:
        """Channel-scoped times, without turning private inactivity into absence."""
        lines=[f"- **现在**：{format_now(self.tz_offset_hours)}"]
        now=datetime.now(timezone.utc)
        zone=timezone(timedelta(hours=self.tz_offset_hours))
        def display(stamp):
            gap=describe_gap(max(0,int((now-stamp).total_seconds())))
            return stamp.astimezone(zone).strftime('%m月%d日 %H:%M')+'，距今 '+gap
        private_dt=parse_sqlite_ts(last_conv_at)
        if private_dt is not None:
            lines.append('- **本私聊上次交流**：'+display(private_dt))
        group_dt=parse_sqlite_ts(last_group_exchange['created_at']) if last_group_exchange else None
        if group_dt is not None and (private_dt is None or group_dt>private_dt):
            name=json.dumps(last_group_exchange['name'],ensure_ascii=False)
            lines.append(f'- **最近群内交流**：你在群 {name} 回应过，'+display(group_dt))
        if private_dt is None:
            lines.append('- **本私聊记录**：没有更早的私聊消息。')
        return "\n".join(lines)

    def _render_persona_basics(self, persona_basics: dict | None) -> str:
        """人设设定 JSON（结构化展示）。章节标题由模板提供。"""
        if persona_basics is None:
            return "（未设置）"
        return "```json\n" + json.dumps(
            persona_basics, ensure_ascii=False, indent=2
        ) + "\n```"

    def _render_related_notes(self, notes: list, sources: dict | None = None) -> str:
        """相关记忆（query 召回，来自历史 session 抽取的 notes）。"""
        if not notes:
            return ""
        lines = []
        for n in notes[:10]:
            content = (
                n.content
                if isinstance(n.content, str)
                else json.dumps(n.content, ensure_ascii=False)
            )
            origin = ' 来源=' + json.dumps(sources.get(n.id, []), ensure_ascii=False) if sources and sources.get(n.id) else ''
            lines.append(
                f"- ({n.kind.value}, reality={n.reality}, event_time={n.event_time or '未知/不适用'}, importance={n.importance:.2f}){origin} {content}"
            )
        return "\n".join(lines)


__all__ = ["PromptService"]
