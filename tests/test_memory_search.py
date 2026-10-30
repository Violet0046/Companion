"""Shared search: scoped graph traversal, empty recall and direction-aware updates."""
import tempfile
import uuid
from pathlib import Path
from fastapi.testclient import TestClient
from test_chat_workflow import _make_ctx
from companion_ever.domain.enums import MemoryKind
from companion_ever.services.memory_search import MemorySearch


def main():
    with tempfile.TemporaryDirectory() as folder:
        ctx = _make_ctx(str(Path(folder)/'test.db'), check_same_thread=False)
        try:
            ctx.repos.user.create('alice')
            for persona in ('one','two'):
                ctx.services.persona.create_persona('alice',persona,'{}')
            def note(content, kind='fact', persona='one'):
                return ctx.services.note.create_note('alice',persona,MemoryKind(kind),content)
            def link(a,b,kind):
                ctx.conn.execute('INSERT INTO note_link(id,from_id,to_id,link_type) VALUES (?,?,?,?)',
                                 (uuid.uuid4().hex,a,b,kind))
            service = ctx.services.retrieval
            old = note('旧址在杭州')
            new = note('后来搬到另一座城市')
            source = note('搬迁当晚我们讨论过收拾行李','episode')
            link(new,old,'supersedes')
            link(source,new,'supports')
            result = service.search('alice','one',MemorySearch(query='杭州',limit=1))
            assert result.notes[0].id == new
            assert result.hits[0]['path'] and result.hits[0]['reasons']
            result = service.search('alice','one',MemorySearch(query='杭州',purpose='consolidation'))
            assert old in {n.id for n in result.notes}
            assert next(h for h in result.hits if h['note'].id==old)['superseded']
            ctx.services.note.archive_note(old,'alice')
            result = service.search('alice','one',MemorySearch(query='杭州',max_hops=2))
            assert new in {n.id for n in result.notes} and old not in {n.id for n in result.notes}
            assert source in {n.id for n in result.notes}
            assert source not in {n.id for n in service.search('alice','one',MemorySearch(query='杭州',max_hops=1)).notes}
            assert not service.retrieve('alice','one','早上好')
            assert not service.retrieve('alice','one','')
            assert not service.search('alice','one',MemorySearch()).notes
            assert service.search('alice','one',MemorySearch(purpose='consolidation',recent_limit=1)).notes
            foreign = note('杭州私聊机密',persona='two')
            link(foreign,new,'supersedes')  # Deliberately corrupted cross-scope edge.
            scoped = service.search('alice','one',MemorySearch(query='杭州',max_hops=2)).to_dict()
            assert foreign not in str(scoped) and '私聊机密' not in str(scoped)
            assert not service.search('alice','one',MemorySearch(seed_ids=[foreign])).notes
            next_event = note('之后完成搬迁交接','episode')
            link(next_event,source,'continues')
            reverse = service.search('alice','one',MemorySearch(seed_ids=[source],kinds=['episode']))
            assert reverse.notes[0].id==next_event
            # Cycles terminate and produce an explicit uncertainty, never an invented latest fact.
            cycle_a=note('循环测试甲');cycle_b=note('循环测试乙')
            link(cycle_a,cycle_b,'supersedes');link(cycle_b,cycle_a,'supersedes')
            cyclic=service.search('alice','one',MemorySearch(seed_ids=[cycle_a],max_hops=2))
            assert cyclic.warnings and len(cyclic.hits)==2
            duplicate=note('后来搬到另一座城市')
            duplicated=service.search('alice','one',MemorySearch(seed_ids=[new,duplicate],max_hops=0))
            assert len(duplicated.hits)==1
            upper=note('Received FORMAL OFFER')
            assert upper in {n.id for n in service.search('alice','one',MemorySearch(query='formal offer')).notes}
            hub=note('预算测试经历','episode')
            for index in range(75):
                leaf=note(f'独立事实 {index}')
                link(hub,leaf,'supports')
            bounded=service.search('alice','one',MemorySearch(seed_ids=[hub],limit=50,max_hops=2))
            assert len(bounded.hits)<=50 and bounded.truncated
            from companion_ever.api.server import create_app
            from companion_ever.config import WebConfig
            ctx.cfg.idle_session_minutes=0
            with TestClient(create_app(ctx,WebConfig(pin='123'))) as client:
                url='/api/persona/alice/one/memories/search'
                assert client.post(url,json={'query':'杭州'}).status_code==401
                response=client.post(url,headers={'x-pin':'123'},json={'query':'杭州','max_hops':2})
                assert response.status_code==200 and response.json()['backend']=='lexical+graph'
                assert 'reasons' in response.json()['hits'][0]
                assert client.post(url,headers={'x-pin':'123'},json={'max_hops':9}).status_code==422
                assert client.post('/api/persona/alice/missing/memories/search',
                                   headers={'x-pin':'123'},json={}).status_code==403
            print('[OK] shared retrieval, scope, graph, supersession, cycles and API')
        finally:
            ctx.conn.close()


if __name__=='__main__':
    main()
