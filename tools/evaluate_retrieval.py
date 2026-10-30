"""Evaluate production lexical+graph search in a temporary, synthetic database."""
import argparse
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from companion_ever.config import RetrievalConfig
from companion_ever.domain.enums import MemoryKind
from companion_ever.services.authz import Authorizer
from companion_ever.services.memory_evaluation import fingerprint
from companion_ever.services.memory_search import MemorySearch
from companion_ever.services.retrieval_service import RetrievalService
from companion_ever.storage.db import init_db
from companion_ever.storage.note_repo import NoteRepo
from companion_ever.storage.user_repo import UserRepo


def run(suite):
    with tempfile.TemporaryDirectory(prefix='memory-eval-') as tmp:
        conn = init_db(Path(tmp) / 'eval.db')
        try:
            users, notes = UserRepo(conn), NoteRepo(conn)
            users.create('synthetic')
            for persona in ('main', 'other'):
                users.add_persona('synthetic', persona)
            service = RetrievalService(notes, Authorizer(users, notes), RetrievalConfig())
            mapping = {n['id']: notes.create(user_id='synthetic', persona_id=n.get('persona', 'main'),
                                            kind=MemoryKind(n['kind']), content=n['content']) for n in suite['notes']}
            reverse = {v: k for k, v in mapping.items()}
            conn.execute("UPDATE note SET created_at='2026-01-01 00:00:00',updated_at='2026-01-01 00:00:00'")
            for a, b, relation in suite['links']:
                conn.execute('INSERT INTO note_link(id,from_id,to_id,link_type) VALUES (?,?,?,?)',
                             (uuid.uuid4().hex, mapping[a], mapping[b], relation))
            rows = []
            for query in suite['queries']:
                started = time.perf_counter()
                result = service.search('synthetic', 'main', MemorySearch(
                    query=query['query'], context_query=query.get('context_query', ''),
                    kinds=query.get('kinds', ['episode', 'fact']), limit=5, max_hops=2))
                elapsed = round((time.perf_counter() - started) * 1000, 3)
                returned = [reverse[n.id] for n in result.notes]
                canonical = {i: group[0] for group in query.get('equivalent_groups', []) for i in group}
                actual = {canonical.get(i, i) for i in returned}
                relevant = {canonical.get(i, i) for i in query['relevant']}
                texts = [str(n.content).strip() for n in result.notes]
                rows.append({'id': query['id'], 'returned': returned,
                             'recall_at_5': len(actual & relevant) / len(relevant) if relevant else None,
                             'irrelevant': len(actual - relevant), 'returned_count': len(returned),
                             'duplicates': len(texts) - len(set(texts)),
                             'empty_expected': not relevant, 'empty_correct': not relevant and not returned,
                             'forbidden_returned': sorted(set(returned) & set(query.get('forbidden', []))),
                             'elapsed_ms': elapsed})
            positive = [r for r in rows if r['recall_at_5'] is not None]
            empty = [r for r in rows if r['empty_expected']]
            count = sum(r['returned_count'] for r in rows)
            return {'suite_sha256': fingerprint(suite), 'query_count': len(rows),
                    'macro_recall_at_5': sum(r['recall_at_5'] for r in positive) / len(positive),
                    'irrelevant_slot_rate': sum(r['irrelevant'] for r in rows) / count if count else 0,
                    'duplicate_slot_rate': sum(r['duplicates'] for r in rows) / count if count else 0,
                    'empty_correct': sum(r['empty_correct'] for r in empty), 'empty_cases': len(empty),
                    'mean_elapsed_ms': sum(r['elapsed_ms'] for r in rows) / len(rows), 'queries': rows,
                    'notice': '仅限固定虚构语料；相同正文视为等价记忆；无关率依赖本集合的完整相关标注，不代表真实聊天表现。'}
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path(__file__).resolve().parents[1] / 'tests/fixtures/memory_retrieval.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run(json.loads(args.input.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'queries'}, ensure_ascii=False))
    return int(any(r['forbidden_returned'] for r in report['queries']))


if __name__ == '__main__':
    raise SystemExit(main())
