"""虚构样本：验证草稿边界、证据引用与不泄露人工答案。"""
import copy
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from companion_ever.services.memory_draft import generate_draft, validate_draft, render_review


def main():
    case = {'id': 'example', 'title': '虚构偏好',
            'messages': [
                {'id': 1, 'role': 'user', 'content': '明天去面试', 'local_time': '2026-10-28 20:00+08:00'},
                {'id': 2, 'role': 'user', 'content': '我不喜欢喝咖啡', 'local_time': '2026-10-29 09:00+08:00'}],
            'new_message_ids': [2], 'existing_notes': [],
            'recommendation': 'PRIVATE_EXPECTATION', 'do_not_infer': '',
            'retrieval_check': '', 'link_reason': '',
            'user_review': {'feedback': 'PRIVATE_REVIEW', 'rule': 'review rule', 'scope': 'only this case'}}
    data = {'proposals': [{'id': 'p1', 'kind': 'fact', 'action': 'create', 'target_id': None,
                          'content': '用户不喜欢喝咖啡', 'event_time': None,
                          'time_basis': '未声明偏好开始时间', 'reality': 'user_report',
                          'evidence': [{'message_id': 2, 'quote': '我不喜欢喝咖啡'}],
                          'reason': '用户明确表达偏好'}],
            'links': [], 'skipped': [], 'uncertainties': []}
    class FakeLLM:
        def chat(self, messages):
            assert 'PRIVATE_EXPECTATION' not in json.dumps(messages)
            assert 'PRIVATE_REVIEW' not in json.dumps(messages)
            return json.dumps(data)
    assert generate_draft(case, FakeLLM()).proposals[0].kind == 'fact'
    assert 'PRIVATE_EXPECTATION' in render_review([case])
    assert 'PRIVATE_REVIEW' in render_review([case])
    def reject(modify):
        broken = copy.deepcopy(data)
        modify(broken)
        try:
            validate_draft(case, broken)
        except ValueError:
            return
        raise AssertionError('invalid draft accepted')
    reject(lambda d: d['proposals'][0]['evidence'][0].update(quote='我喜欢喝咖啡'))
    reject(lambda d: d['proposals'][0]['evidence'][0].update(message_id=99))
    reject(lambda d: d['proposals'][0]['evidence'].__setitem__(0, {'message_id': 1, 'quote': '明天去面试'}))
    reject(lambda d: d['proposals'][0].update(action='append', target_id='missing'))
    reject(lambda d: d['links'].append({'from_id': 'p1', 'to_id': 'missing', 'relation': 'supports', 'reason': 'x'}))
    reject(lambda d: d.update(proposals=[]))
    empty = {'proposals': [], 'links': [], 'skipped': [{'message_id': 2, 'reason': '测试跳过'}], 'uncertainties': []}
    assert not validate_draft(case, empty).proposals
    linked_case = copy.deepcopy(case)
    linked_case['existing_notes'] = [{'id': 'old', 'kind': 'episode', 'content': '旧经历'}]
    linked = copy.deepcopy(data)
    linked['proposals'][0].update(kind='episode', action='append', target_id='old')
    linked['links'] = [{'from_id': 'p1', 'to_id': 'old', 'relation': 'continues', 'reason': '重复关系'}]
    try:
        validate_draft(linked_case, linked)
    except ValueError:
        pass
    else:
        raise AssertionError('update target must not produce a self link')
    print('[OK] draft evidence, scope, links, no-answer-leak regressions')


if __name__ == '__main__':
    main()
