"""Repeatable, explicitly scoped checks; these are not a semantic accuracy judge."""
from __future__ import annotations

import hashlib
import json
from collections import Counter

from companion_ever.memory.extraction.draft import validate_case, validate_draft


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def prompt_fingerprint():
    """记忆整理 Agent 的提示词版本——模板文本加上载荷的键集合。

    改了模板或载荷形状，旧模型输出就不再对应这次提示词，
    必须拒绝混用（否则会把上一版的结论算到这一版头上）。
    """
    from companion_ever.prompts import load
    from companion_ever.memory.extraction.draft import case_payload, step_payload
    sample = {'id': 'x', 'new_message_ids': [], 'messages': [], 'existing_notes': []}
    step = step_payload(round_no=1, existing_notes=[], tool_results=[],
                        requires_search_before_write=False, remaining_tool_rounds=0,
                        remaining_tool_calls=0, tools=None)
    return fingerprint({
        'templates': [load('memory_draft_system.txt'), load('repair_evidence.txt')],
        'payload_keys': [sorted(case_payload(sample)), sorted(step)],
    })


def validate_suite(suite):
    if suite.get('version') != 1:
        raise ValueError('不支持的评测格式版本')
    cases = suite['cases']
    if not cases or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('评测案例必须非空且 ID 唯一')
    for case in cases:
        validate_case(case)
        review = case['evaluation']
        if review['status'] not in ('confirmed', 'partial', 'unreviewed'):
            raise ValueError('未知审阅状态')
        if review['status'] == 'unreviewed' and review.get('constraints'):
            raise ValueError('未确认建议不能成为规则评分答案')
        if review['status'] != 'unreviewed' and not review.get('constraints'):
            raise ValueError('已确认案例必须有实际检查规则，不能无条件算通过')
        supported = {'max_proposals', 'min_episodes', 'forbidden_kinds', 'max_links',
                     'all_reality', 'event_time_null', 'dated_episode', 'content_groups'}
        if set(review.get('constraints', {})) - supported:
            raise ValueError('存在未实现的评测约束，不能静默跳过')


def rule_failures(draft, constraints):
    failures = []
    if len(draft.proposals) > constraints.get('max_proposals', float('inf')):
        failures.append('proposal_count')
    if sum(p.kind == 'episode' for p in draft.proposals) < constraints.get('min_episodes', 0):
        failures.append('episode_required')
    if any(p.kind in constraints.get('forbidden_kinds', []) for p in draft.proposals):
        failures.append('forbidden_kind')
    if len(draft.links) > constraints.get('max_links', float('inf')):
        failures.append('unjustified_link')
    if 'all_reality' in constraints and any(p.reality != constraints['all_reality'] for p in draft.proposals):
        failures.append('reality')
    if constraints.get('event_time_null') and any(p.event_time is not None for p in draft.proposals):
        failures.append('invented_event_time')
    if constraints.get('dated_episode') and not any(p.kind == 'episode' and p.event_time for p in draft.proposals):
        failures.append('event_time_required')
    content = '\n'.join(p.content for p in draft.proposals).casefold()
    for index, alternatives in enumerate(constraints.get('content_groups', [])):
        if not any(word.casefold() in content for word in alternatives):
            failures.append(f'content_probe_{index + 1}')
    return failures


def evaluate(suite, responses):
    validate_suite(suite)
    bound_hash = responses.get('suite_sha256')
    if bound_hash and bound_hash != fingerprint(suite):
        raise ValueError('模型结果与评测快照指纹不一致，拒绝混用')
    bound_prompt = responses.get('prompt_sha256')
    if bound_prompt and bound_prompt != prompt_fingerprint():
        raise ValueError('模型结果与提示词版本不一致，拒绝混用（提示词已改，需重新评测）')
    rows = []
    for case in suite['cases']:
        review = case['evaluation']
        row = {'id': case['id'], 'review_status': review['status'],
               'rule_scope': review.get('scope', ''), 'rule_failures': []}
        entry = responses.get('drafts', {}).get(case['id'])
        if entry is None:
            row.update(status='missing', structure_valid=False)
        elif 'error_type' in entry:
            row.update(status='error', structure_valid=False, error_type=entry['error_type'])
        else:
            try:
                draft = validate_draft(case, entry)
            except (ValueError, KeyError, TypeError) as exc:
                row.update(status='invalid', structure_valid=False, error_type=type(exc).__name__)
            else:
                row.update(structure_valid=True, proposal_count=len(draft.proposals), link_count=len(draft.links))
                if review['status'] == 'unreviewed':
                    row['status'] = 'unreviewed'
                else:
                    row['rule_failures'] = rule_failures(draft, review.get('constraints', {}))
                    row['status'] = 'rule_failed' if row['rule_failures'] else 'rule_passed'
        measure = responses.get('measurements', {}).get(case['id'], {})
        row.update({k: measure[k] for k in ('elapsed_seconds', 'prompt_tokens', 'completion_tokens','model_calls','usage') if k in measure})
        rows.append(row)
    eligible = [r for r in rows if r['review_status'] != 'unreviewed']
    passed = sum(r['status'] == 'rule_passed' for r in eligible)
    statuses = Counter(r['status'] for r in rows)
    return {
        'suite_sha256': fingerprint(suite), 'response_bound': bool(bound_hash),
        'prompt_sha256': prompt_fingerprint(), 'prompt_bound': bool(bound_prompt),
        'model': responses.get('model'), 'case_count': len(rows),
        'structure_valid': sum(r['structure_valid'] for r in rows),
        'confirmed_rule_cases': len(eligible), 'rule_passed': passed,
        'rule_pass_rate': passed / len(eligible) if eligible else None,
        'statuses': dict(statuses), 'cases': rows,
        'notice': '规则断言不是语义准确率；内容词组检查仅为可解释探针。未审阅案例不计入规则分母，缺失/超时不能当作空记忆。',
    }


def render_report(report):
    lines = ['# 记忆评测结果', '', report['notice'], '',
             f"- 案例：{report['case_count']}；结构有效：{report['structure_valid']}",
             f"- 已确认范围内规则通过：{report['rule_passed']}/{report['confirmed_rule_cases']}",
             f"- 输入指纹：`{report['suite_sha256']}`",
             f"- 响应绑定同一快照：{report['response_bound']}",
             f"- 提示词指纹：`{report['prompt_sha256']}`",
             f"- 响应绑定同一提示词：{report['prompt_bound']}", '',
             '| 案例 | 审阅范围 | 状态 | 失败规则 | 耗时（秒） |',
             '|---|---|---|---|---|']
    for row in report['cases']:
        lines.append(f"| {row['id']} | {row['review_status']} | {row['status']} | "
                     f"{', '.join(row['rule_failures']) or row.get('error_type', '')} | {row.get('elapsed_seconds', '')} |")
    return '\n'.join(lines) + '\n'
