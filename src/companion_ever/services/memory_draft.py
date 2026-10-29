"""只生成可审阅的记忆建议；不依赖数据库，不应用任何修改。"""
from __future__ import annotations

import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from companion_ever.prompts import load
from companion_ever.services.llm_json import parse_json_response


class Citation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_id: int
    quote: str = Field(min_length=1)


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    kind: Literal['episode', 'fact']
    action: Literal['create', 'append', 'confirm', 'revise']
    target_id: str | None = None
    content: str = Field(min_length=1)
    event_time: str | None = None
    time_basis: str
    reality: Literal['user_report', 'shared_fiction', 'uncertain']
    evidence: list[Citation] = Field(min_length=1)
    reason: str


class Link(BaseModel):
    model_config = ConfigDict(extra='forbid')
    from_id: str
    to_id: str
    relation: Literal['supports', 'continues', 'supersedes']
    reason: str


class Skipped(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_id: int
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
    if len(ids) != len(set(ids)):
        raise ValueError('消息 ID 重复')
    new = set(case['new_message_ids'])
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
    covered = set()
    for p in draft.proposals:
        if p.action == 'create':
            if p.target_id is not None:
                raise ValueError('新增笔记不能指定更新目标')
        elif p.target_id not in notes or notes[p.target_id]['kind'] != p.kind:
            raise ValueError('更新目标不存在或类型不一致')
        evidence_ids = set()
        for e in p.evidence:
            if e.message_id not in messages or e.quote not in messages[e.message_id]['content']:
                raise ValueError('证据必须是所给消息中的原文')
            evidence_ids.add(e.message_id)
        if not evidence_ids & new_ids:
            raise ValueError('建议必须引用本批新消息，不能重新抽取纯参考前文')
        if not any(messages[i]['role'] == 'user' for i in evidence_ids):
            raise ValueError('不能仅用助手发言建立用户记忆')
        covered.update(evidence_ids & new_ids)
    kinds = {i: n['kind'] for i, n in notes.items()}
    kinds.update({p.id: p.kind for p in draft.proposals})
    pairs = {'supports': ('episode', 'fact'), 'continues': ('episode', 'episode'),
             'supersedes': ('fact', 'fact')}
    for link in draft.links:
        if link.from_id == link.to_id or link.from_id not in kinds or link.to_id not in kinds:
            raise ValueError('关系端点无效')
        if (kinds[link.from_id], kinds[link.to_id]) != pairs[link.relation]:
            raise ValueError('关系与笔记类型不匹配')
        if link.from_id not in proposals and link.to_id not in proposals:
            raise ValueError('关系必须关联本批建议')
        def effective_id(note_id):
            proposal = proposals.get(note_id)
            return proposal.target_id if proposal and proposal.target_id else note_id
        if effective_id(link.from_id) == effective_id(link.to_id):
            raise ValueError('补充/确认操作与目标是同一笔记，不能再建立自关联')
    for skipped in draft.skipped:
        if skipped.message_id not in new_ids:
            raise ValueError('跳过项必须来自新消息')
        if skipped.message_id in covered:
            raise ValueError('同一消息不能既提供记忆证据又被标记跳过')
        covered.add(skipped.message_id)
    required = {i for i in new_ids if messages[i]['role'] == 'user'}
    if not required <= covered:
        raise ValueError('每条用户新消息都应被引用或说明为何跳过')
    return draft


def generate_draft(case: dict, llm) -> Draft:
    validate_case(case)
    # 不把人工期望、审阅意见发送给模型，避免评测答案泄露。
    payload = {k: case[k] for k in ('id', 'messages', 'new_message_ids', 'existing_notes')}
    return validate_draft(case, parse_json_response(llm.chat([
        {'role': 'system', 'content': load('memory_draft_system.txt') + '\n输出 JSON Schema：\n'
         + json.dumps(Draft.model_json_schema(), ensure_ascii=False)},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ])))


def render_review(cases: list[dict], drafts: dict | None = None) -> str:
    lines = ['# 记忆设计审阅稿', '',
             '本地私人样本。以下人工建议由编码助手整理，尚未获用户确认，不是标准答案。',
             '数据库时间为 UTC；显示时间为 UTC+8。整理工具不修改正式记忆。', '',
             '审阅时只需标注：接受 / 修改 / 不保存，以及需要改成什么。', '']
    for case in cases:
        lines.extend([f"## {case['id']} · {case['title']}", '',
                      '### 原话与上下文', ''])
        for m in case['messages']:
            marker = '新消息' if m['id'] in case['new_message_ids'] else '参考前文'
            lines.append(f"- #{m['id']} · {m['local_time']} · {m['role']} · {marker}")
            lines.append('  > ' + m['content'].replace('\n', '\n  > '))
        lines.extend(['', '### 人工建议（待你审阅）', '', case['recommendation'], '',
                      '**不能据此推断：** ' + case['do_not_infer'], '',
                      '**召回验收：** ' + case['retrieval_check'], '',
                      '**关联依据：** ' + case['link_reason'], ''])
        if case.get('existing_notes'):
            lines.extend(['**实验候选笔记（人工构造，并非已入库）：**', ''])
            for n in case['existing_notes']:
                lines.append(f"- {n['id']} ({n['kind']}): {n['content']}")
        if drafts and case['id'] in drafts:
            result = drafts[case['id']]
            lines.extend(['', '### 模型独立草稿（未经人工确认）', ''])
            if 'error_type' in result:
                lines.append('生成失败：' + result['error_type'] + '。这不是空记忆判断。')
            else:
                labels = {'create': '新增', 'append': '补充', 'confirm': '确认已有内容', 'revise': '纠正'}
                for p in result['proposals']:
                    lines.extend([f"**{p['id']} · {labels[p['action']]} · {'经历' if p['kind'] == 'episode' else '长期事实'}**",
                                  '', p['content'], '',
                                  f"- 目标：{p.get('target_id') or '新笔记'}；情境：{p['reality']}",
                                  f"- 事件时间：{p['event_time'] or '未确定/不适用'}；依据：{p['time_basis']}",
                                  '- 理由：' + p['reason']])
                    for e in p['evidence']:
                        lines.append(f"- 证据 #{e['message_id']}：{e['quote']}")
                    lines.append('')
                if not result['proposals']:
                    lines.append('**模型建议：不建立新笔记。**')
                for skip in result['skipped']:
                    lines.append(f"- 不保存 #{skip['message_id']}：{skip['reason']}")
                for link in result['links']:
                    lines.append(f"- 关系：{link['from_id']} → {link['to_id']} ({link['relation']})：{link['reason']}")
                for uncertainty in result['uncertainties']:
                    lines.append('- 待确认：' + uncertainty)
            lines.append('')
        lines.extend(['**你的意见：** 待审阅', '', '---', ''])
    return '\n'.join(lines)
