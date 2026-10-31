"""只生成可审阅的记忆建议；不依赖数据库，不应用任何修改。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from companion_ever.prompts import load
from companion_ever.memory.relations import RELATION_KINDS
from companion_ever.domain.enums import LinkType, MemoryKind
from companion_ever.runtime.structured import checked_json


class Citation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_id: StrictInt
    quote: str = Field(min_length=1)


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    kind: MemoryKind
    action: Literal['create', 'append', 'confirm', 'revise']
    target_id: str | None = None
    content: str = Field(min_length=1)
    event_time: str | None = None
    time_basis: str
    reality: Literal['user_report', 'shared_fiction', 'uncertain']
    evidence: list[Citation] = Field(min_length=1)
    reason: str
    importance: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)

    @field_validator('event_time')
    @classmethod
    def event_time_is_iso(cls, value):
        if value is not None:
            datetime.fromisoformat(value.replace('Z','+00:00'))
        return value


class Link(BaseModel):
    model_config = ConfigDict(extra='forbid')
    from_id: str
    to_id: str
    relation: LinkType
    reason: str


class Skipped(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_id: StrictInt
    reason: str


class Draft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    proposals: list[Proposal]
    links: list[Link]
    skipped: list[Skipped]
    uncertainties: list[str]


def validate_case(case: dict) -> None:
    messages = case['messages']
    ids = [m['id'] for m in messages]
    if any(type(i) is not int for i in ids):
        raise ValueError('消息 ID 必须为整数')
    if any(m['role'] not in ('user', 'assistant') or not isinstance(m['content'], str) for m in messages):
        raise ValueError('消息必须包含用户/助手角色与文本')
    if len(ids) != len(set(ids)):
        raise ValueError('消息 ID 重复')
    new = set(case['new_message_ids'])
    if any(type(i) is not int for i in case['new_message_ids']) or len(new) != len(case['new_message_ids']):
        raise ValueError('新消息 ID 必须为不重复的整数')
    if not new or not new <= set(ids):
        raise ValueError('新消息必须是非空的输入消息子集')
    notes = case.get('existing_notes', [])
    if len({n['id'] for n in notes}) != len(notes):
        raise ValueError('候选笔记 ID 重复')
    if any(n['kind'] not in ('episode', 'fact') for n in notes):
        raise ValueError('候选笔记类型无效')


def validate_draft(case: dict, data: dict) -> Draft:
    """验证可核查的引用和关系；不冒充语义质量判定。"""
    validate_case(case)
    draft = Draft.model_validate(data)
    messages = {m['id']: m for m in case['messages']}
    new_ids = set(case['new_message_ids'])
    notes = {n['id']: n for n in case.get('existing_notes', [])}
    proposals = {p.id: p for p in draft.proposals}
    if len(proposals) != len(draft.proposals) or proposals.keys() & notes.keys():
        raise ValueError('建议 ID 必须唯一且不同于已有笔记 ID')
    covered = set();errors=[]
    for p in draft.proposals:
        if not p.id.strip() or not p.content.strip():
            errors.append('建议 ID 与正文不能为空白')
        if p.action == 'create':
            if p.target_id is not None:
                errors.append(f'proposals[{p.id}]：新增笔记不能指定更新目标')
        elif p.target_id not in notes or notes[p.target_id]['kind'] != p.kind:
            errors.append(f'proposals[{p.id}]：更新目标不存在或类型不一致')
        if p.action=='append' and p.kind!='episode':
            errors.append(f'proposals[{p.id}]：append 只能用于 episode；事实纠正请用 revise')
        if p.action in ('confirm','revise') and p.kind!='fact':
            errors.append(f'proposals[{p.id}]：{p.action} 只能用于 fact；同一经历补充请用 append')
        target=notes.get(p.target_id)
        if target and p.action=='confirm' and p.content.strip()!=target['content'].strip():
            errors.append(f'proposals[{p.id}]：confirm 的 content 必须与目标正文逐字一致；修改内容请用 revise')
        if target and p.action in ('append','confirm') and target.get('reality','uncertain') not in ('uncertain',p.reality):
            errors.append(f'proposals[{p.id}]：{p.action} 不能改变目标的现实情境')
        evidence_ids = set()
        for e in p.evidence:
            if not e.quote.strip() or e.message_id not in messages or e.quote not in messages[e.message_id]['content']:
                errors.append(f'proposals[{p.id}].evidence.message_id={e.message_id}：quote 必须是该消息中的连续逐字原文')
                continue
            evidence_ids.add(e.message_id)
        if not evidence_ids & new_ids:
            errors.append(f'proposals[{p.id}]：建议必须引用本批新消息，不能重新抽取纯参考前文')
        if not any(messages[i]['role'] == 'user' for i in evidence_ids & new_ids):
            errors.append(f'proposals[{p.id}]：建议必须引用本批用户原话，旧用户消息加新助手发言不能作为新记忆依据')
        covered.update(evidence_ids & new_ids)
    kinds = {i: n['kind'] for i, n in notes.items()}
    kinds.update({p.id: p.kind.value for p in draft.proposals})
    realities={nid:n.get('reality','uncertain') for nid,n in notes.items()}
    realities.update({p.id:p.reality for p in draft.proposals})
    for proposal in draft.proposals:
        if proposal.action in ('append','confirm') and proposal.target_id in realities:
            realities[proposal.target_id]=proposal.reality
    pairs = RELATION_KINDS
    seen_links = set()
    for index,link in enumerate(draft.links):
        location=f'links[{index}] ({link.from_id} --{link.relation.value}--> {link.to_id})'
        if link.from_id == link.to_id or link.from_id not in kinds or link.to_id not in kinds:
            errors.append(location+'：关系端点无效，必须是两个不同的本批建议/候选笔记 ID')
            continue
        if (kinds[link.from_id], kinds[link.to_id]) != pairs[link.relation]:
            expected=pairs[link.relation]
            errors.append(location+f'：关系与笔记类型不匹配，当前 {kinds[link.from_id]}→{kinds[link.to_id]}，要求 {expected[0]}→{expected[1]}；不符合请移除该边')
        if realities[link.from_id]!=realities[link.to_id] and not (link.relation=='supersedes' and realities[link.to_id]=='uncertain'):
            errors.append(location+'：不能关联不同现实情境的笔记')
        if link.from_id not in proposals and link.to_id not in proposals:
            errors.append(location+'：关系必须关联本批建议')
        def effective_id(note_id):
            proposal = proposals.get(note_id)
            return proposal.target_id if proposal and proposal.target_id else note_id
        if effective_id(link.from_id) == effective_id(link.to_id):
            errors.append(location+'：补充/确认操作与目标是同一笔记，不能再建立自关联')
        edge = (effective_id(link.from_id), effective_id(link.to_id), link.relation)
        if edge in seen_links:
            errors.append(location+'：不能重复建立同一关系')
        seen_links.add(edge)
    for skipped in draft.skipped:
        if skipped.message_id not in new_ids:
            errors.append(f'skipped.message_id={skipped.message_id}：跳过项必须来自新消息')
            continue
        if skipped.message_id in covered:
            errors.append(f'skipped.message_id={skipped.message_id}：同一消息不能既提供记忆证据又被标记跳过')
        covered.add(skipped.message_id)
    required = {i for i in new_ids if messages[i]['role'] == 'user'}
    if not required <= covered:
        errors.append('每条用户新消息都应被引用或说明为何跳过；缺失 message_id='+str(sorted(required-covered)))
    if errors:
        raise ValueError('; '.join(errors))
    return draft


def note_projection(notes) -> list[dict]:
    """候选笔记的白名单投影——只给模型需要的字段。

    正文不能截断：confirm 要求 content 与目标正文逐字一致（见 validate_draft）。
    白名单而非整体透传：评测夹具里的私有字段不能泄露给模型。
    """
    return [{k: n[k] for k in ('id', 'kind', 'content', 'reality', 'event_time',
                               'time_basis', 'revision', 'is_archived') if k in n}
            for n in notes]


def case_payload(case: dict) -> dict:
    """案例：本次任务的静态材料，整个任务不变，只在首轮发出一次。"""
    return {
        'id': case['id'], 'new_message_ids': case['new_message_ids'],
        'required_user_message_ids': [m['id'] for m in case['messages']
                                      if m['id'] in case['new_message_ids'] and m['role']=='user'],
        'relation_rules': {relation.value: {'from_kind':source,'to_kind':target}
                           for relation,(source,target) in RELATION_KINDS.items()},
        'messages': [{k: m[k] for k in ('id', 'role', 'content', 'created_at', 'local_time') if k in m}
                     for m in case['messages']],
    }


def step_payload(*, round_no, existing_notes, tool_results, requires_search_before_write,
                 remaining_tool_rounds, remaining_tool_calls, tools) -> dict:
    """进展：每轮变化的部分——完整候选集、本轮工具结果、剩余预算。

    existing_notes 是合并后的完整候选清单，让"target_id 必须引用候选笔记"
    在每一轮都成立；工具结果只给本轮，上一轮的结果仍在上一条消息里。
    """
    return {
        'round': round_no,
        'existing_notes': existing_notes,
        'tool_results': tool_results,
        'requires_search_before_write': requires_search_before_write,
        'remaining_tool_rounds': remaining_tool_rounds,
        'remaining_tool_calls': remaining_tool_calls,
        'available_tools': tools or [],
    }


def draft_messages(case: dict, *, response_schema=None, tools=None,
                   requires_search_before_write=False,
                   remaining_tool_rounds=0, remaining_tool_calls=0) -> list[dict]:
    """组装首轮请求：system 是协议与规则，user 是案例与首轮进展。

    静态协议（工具说明、调用预算、空召回规则、载荷说明）在
    prompts/memory_draft_system.txt 里；这里只填数据。
    """
    validate_case(case)
    payload = {
        **case_payload(case),
        **step_payload(round_no=1,
                       existing_notes=note_projection(case.get('existing_notes', [])),
                       tool_results=[],
                       requires_search_before_write=requires_search_before_write,
                       remaining_tool_rounds=remaining_tool_rounds,
                       remaining_tool_calls=remaining_tool_calls,
                       tools=tools),
    }
    return [
        {'role': 'system', 'content': load('memory_draft_system.txt') + '\n输出 JSON Schema：\n'
         + json.dumps(response_schema or Draft.model_json_schema(), ensure_ascii=False)},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ]


def generate_draft(case: dict, llm, *, repair_attempts=0, **kwargs) -> Draft:
    return checked_json(llm,draft_messages(case),lambda data:validate_draft(case,data),
                        repair_attempts=repair_attempts,
                        source_rule=load('repair_evidence.txt'),**kwargs)
