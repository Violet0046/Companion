"""Reviewed scopes, missing outputs, fingerprints and real search against fixed fixtures."""
import copy
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from companion_ever.memory.extraction.draft import generate_draft, validate_draft
from evaluation.memory import evaluate, fingerprint
from evaluate_retrieval import run


def main():
    root = Path(__file__).parent / 'fixtures'
    suite = json.loads((root / 'memory_cases.json').read_text(encoding='utf-8'))
    responses = {'suite_sha256': fingerprint(suite), 'drafts': {c['id']: c['reference_draft'] for c in suite['cases']}}
    report = evaluate(suite, responses)
    assert report['case_count'] == report['structure_valid'] == 18
    assert report['confirmed_rule_cases'] == report['rule_passed'] == 6
    assert report['statuses']['unreviewed'] == 12
    # C15 explicitly permits an episode, while forbidding a permanent preference.
    case = next(c for c in suite['cases'] if c['id'] == 'C15')
    changed = copy.deepcopy(responses)
    changed['drafts']['C15'] = {'proposals': [{
        'id': 'p1', 'kind': 'episode', 'action': 'create', 'target_id': None,
        'content': '用户本次选择学情侣英语，暂不谈面试。', 'event_time': None,
        'time_basis': '本次选择', 'reality': 'user_report',
        'evidence': [{'message_id': 2, 'quote': '今天学情侣英语吧，先别提面试。'}], 'reason': '示例'}],
        'links': [], 'skipped': [], 'uncertainties': []}
    assert evaluate(suite, changed)['rule_passed'] == 6
    changed['drafts']['C15']['proposals'][0]['kind'] = 'fact'
    assert evaluate(suite, changed)['rule_passed'] == 5
    del changed['drafts']['C08']
    changed['drafts']['C07'] = {'error_type': 'TimeoutError'}
    report = evaluate(suite, changed)
    assert report['rule_passed'] == 3 and report['confirmed_rule_cases'] == 6
    assert report['statuses']['missing'] == report['statuses']['error'] == 1
    changed['suite_sha256'] = 'different'
    try:
        evaluate(suite, changed)
    except ValueError:
        pass
    else:
        raise AssertionError('mixed snapshots accepted')

    # A past user plus a new assistant must never be sufficient new user evidence.
    case = copy.deepcopy(suite['cases'][0])
    case['messages'].append({'id': 2, 'role': 'assistant', 'content': '我替你猜测了新情况'})
    case['new_message_ids'] = [2]
    invalid = copy.deepcopy(case['reference_draft'])
    invalid['proposals'][0]['evidence'].append({'message_id': 2, 'quote': '我替你猜测了新情况'})
    try:
        validate_draft(case, invalid)
    except ValueError:
        pass
    else:
        raise AssertionError('old user plus new assistant evidence accepted')

    case = copy.deepcopy(suite['cases'][0])
    case['messages'][0]['user_id'] = 'PRIVATE_ID'
    case['messages'][0]['recommendation'] = 'PRIVATE_ANSWER'
    case['existing_notes'] = [{'id': 'old', 'kind': 'fact', 'content': '已有事实', 'evaluation': 'PRIVATE_ANSWER'}]
    class FakeLLM:
        def chat(self, messages, **kwargs):
            wire = json.dumps(messages)
            assert 'PRIVATE_ID' not in wire and 'PRIVATE_ANSWER' not in wire
            assert 'reference_draft' not in wire and 'constraints' not in wire
            return json.dumps(case['reference_draft'])
    generate_draft(case, FakeLLM())

    # Strict citation IDs and nonblank quotes, even if spaces occur in the source.
    case['messages'][0]['content'] += ' '
    for citation in ({'message_id': True, 'quote': '我'}, {'message_id': '1', 'quote': '我'},
                     {'message_id': 1, 'quote': ' '}):
        draft = copy.deepcopy(case['reference_draft'])
        draft['proposals'][0]['evidence'] = [citation]
        try:
            validate_draft(case, draft)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid citation accepted')

    search = run(json.loads((root / 'memory_retrieval.json').read_text(encoding='utf-8')))
    assert search['query_count'] == 18 and 0 <= search['macro_recall_at_5'] <= 1
    assert search['empty_correct'] == search['empty_cases'] == 4
    assert search['duplicate_slot_rate'] == 0
    assert not any(r['forbidden_returned'] for r in search['queries'])
    # Expected lexical limitations are reported, not silently removed from the denominator.
    assert any(r['recall_at_5'] == 0 for r in search['queries'])
    print('[OK] review scopes, failures, fingerprints, evidence isolation and retrieval baseline')


if __name__ == '__main__':
    main()
