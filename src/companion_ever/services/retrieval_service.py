"""Wiki 召回 + 排序。"""
from __future__ import annotations

from datetime import datetime, timezone

from companion_ever.services.authz import Authorizer
from companion_ever.config import RetrievalConfig
from companion_ever.domain.entities import Note
from companion_ever.storage.note_repo import NoteRepo
from companion_ever.utils.text import all_keywords, keyword_hits
from companion_ever.trace import current_trace
from companion_ever.services.memory_search import MemorySearch, MemorySearchResult
from companion_ever.domain.memory import memory_kind, valid_relation
from companion_ever.storage.note_link_repo import NoteLinkRepo


class RetrievalService:
    """Wiki 召回 + 排序。"""

    def __init__(
        self,
        note_repo: NoteRepo,
        authorizer: Authorizer,
        cfg: RetrievalConfig,
    ):
        self.note_repo = note_repo
        self.authorizer = authorizer
        self.cfg = cfg
        self.links = NoteLinkRepo(note_repo.conn)

    def retrieve(self, user_id, persona_id, query, top_k=None):
        """Compatibility entry for prompts; all selection happens in search()."""
        limit = self.cfg.top_k if top_k is None else top_k
        if limit <= 0:
            self.authorizer.require_persona(user_id, persona_id)
            return []
        return self.search(user_id, persona_id, MemorySearch(query=query,limit=min(limit,50))).notes

    def search(self, user_id, persona_id, request):
        """Retrieve seeds, expand at most two hops, then select scoped evidence."""
        import time
        started = time.perf_counter()
        self.authorizer.require_persona(user_id,persona_id)
        request = MemorySearch.model_validate(request)
        if request.purpose == 'chat' and request.recent_limit:
            raise ValueError('近期兜底仅可用于记忆整理；聊天不自动填充无关记忆')
        warnings = []
        nodes, paths, scores, reasons = {}, {}, {}, {}
        edges = {}
        truncated = False
        now = datetime.now(timezone.utc)
        def allowed(note):
            return note is not None and note.user_id==user_id and note.persona_id==persona_id
        def offer(note, reason, score, path):
            if not allowed(note):
                return
            if note.id not in nodes:
                nodes[note.id]=note
                paths[note.id]=path
                scores[note.id]=score
                reasons[note.id]=[reason]
            else:
                if reason not in reasons[note.id]:
                    reasons[note.id].append(reason)
                scores[note.id]=max(scores[note.id],score)
        for note_id in request.seed_ids:
            note=self.note_repo.get(note_id)
            if allowed(note):
                offer(note,'explicit_seed',2.0,[])
        for label,query in [('query',request.query),('context',request.context_query)]:
            if not query.strip():
                continue
            keywords=all_keywords(query)
            found=self.note_repo.search(query,user_id=user_id,persona_id=persona_id,limit=60,include_archived=True)
            ranked=[]
            for note in found:
                content=note.content if isinstance(note.content,str) else ''
                if allowed(note) and (keyword_hits(content.casefold(),[k.casefold() for k in keywords]) or query.strip().casefold() in content.casefold()):
                    ranked.append((self._score(note,query,keywords,now),note))
            ranked.sort(key=lambda item:(-item[0],item[1].id))
            truncated |= len(ranked)>20
            for score,note in ranked[:20]:
                offer(note,label,score,[])
        if request.purpose=='consolidation' and request.recent_limit:
            recent=[n for n in self.note_repo.find(user_id=user_id,persona_id=persona_id,limit=request.recent_limit+1) if allowed(n)]
            for note in recent[:request.recent_limit]:
                offer(note,'recent_context',0.05,[])
        # Scope the traversal itself, not just the final output. Visited nodes bound cycles.
        frontier=list(nodes)
        for depth in range(request.max_hops):
            rows=self.links.neighbours(frontier,user_id,persona_id,limit=101)
            truncated |= len(rows)>100
            following=[]
            for edge in rows[:100]:
                source=self.note_repo.get(edge['from_id'])
                target=self.note_repo.get(edge['to_id'])
                if not allowed(source) or not allowed(target) or not valid_relation(source,target,edge['link_type']):
                    continue
                edges[edge['id']]=edge
                for parent,child in [(source,target),(target,source)]:
                    if parent.id not in frontier or child.id in nodes:
                        continue
                    if len(nodes)>=64:
                        truncated=True
                        continue
                    score=scores[parent.id]*0.8
                    if edge['link_type']=='continues' and child.id==edge['from_id']:
                        score=scores[parent.id]+0.01
                    offer(child,'relation:'+edge['link_type'],score,
                          paths[parent.id]+[edge['id']])
                    following.append(child.id)
            frontier=following
            if not frontier:
                break
        if frontier and request.max_hops:
            boundary=self.links.neighbours(frontier,user_id,persona_id,limit=101)
            if any(e['from_id'] not in nodes or e['to_id'] not in nodes for e in boundary):
                truncated=True
                warnings.append('关系扩展已达跳数或节点上限，结果不是完整事件图')
        # Supersession is directional: newer -> old. Preserve both for consolidation.
        superseded=set()
        successors={}
        for edge in edges.values():
            if edge['link_type']=='supersedes':
                successors.setdefault(edge['to_id'],set()).add(edge['from_id'])
        def terminals(start, seen):
            if start in seen:
                return set(),True
            next_ids=successors.get(start,set())
            if not next_ids:
                return {start},False
            final=set(); cycle=False
            for nxt in next_ids:
                ids,cyclic=terminals(nxt,seen|{start})
                final |= ids; cycle |= cyclic
            return final,cycle
        for old in successors:
            final,cycle=terminals(old,set())
            if cycle:
                warnings.append('supersedes 关系存在循环，不能确定最新事实')
                continue
            active=[i for i in final if i in nodes and not nodes[i].is_archived]
            if active:
                superseded.add(old)
                for new in active:
                    scores[new]=max(scores[new],scores[old]+0.01)
                    reasons[new].append('supersedes:'+old)
                if len(active)>1:
                    warnings.append('同一旧事实有多个有效后继，不能自动判定唯一结果')
        ranked=sorted(nodes.values(),key=lambda note:(-scores[note.id],note.id))
        hits=[]; seen=set()
        for note in ranked:
            if note.is_archived or memory_kind(note) not in request.kinds:
                continue
            if request.purpose=='chat' and note.id in superseded:
                continue
            key=(('episode',note.id) if memory_kind(note)=='episode' else
                 ('fact',note.reality,note.event_time,str(note.content).strip()))
            if key in seen:
                continue
            seen.add(key)
            evidence=[dict(row) for row in self.note_repo.conn.execute(
                'SELECT e.message_id,e.quote FROM note_evidence e JOIN conversation_log m ON m.id=e.message_id '
                'WHERE e.note_id=? AND m.user_id=? AND m.persona_id=? ORDER BY e.message_id DESC LIMIT 3',
                (note.id,user_id,persona_id))]
            hits.append({'note':note,'score':round(scores[note.id],4),'reasons':reasons[note.id],
                         'path':paths[note.id],'superseded':note.id in superseded,'evidence':evidence})
        truncated |= len(hits)>request.limit
        result=MemorySearchResult(hits[:request.limit],list(edges.values()),list(dict.fromkeys(warnings)),truncated)
        trace=current_trace()
        if trace:
            trace.step('retrieval.retrieve',ms=(time.perf_counter()-started)*1000,purpose=request.purpose,
                       query=request.query[:120],candidates=len(nodes),returned=len(result.hits),
                       notes=[{'id':h['note'].id,'score':h['score'],'reasons':h['reasons'],'path':h['path']} for h in result.hits],
                       warnings=result.warnings,truncated=truncated)
        return result

    def _score(
        self,
        note: Note,
        query: str,
        keywords: list[str],
        now: datetime,
    ) -> float:
        """计算单条 note 的 score。

        公式（来自 cfg.weights）：
        score = importance * weights["importance"]
              + recency_bonus * weights["recency"]
              + fts_bonus * weights["fts"]
        """
        w = self.cfg.weights
        score = 0.0

        # 1. importance
        score += note.importance * w["importance"]

        # 2. 时间新度
        score += self._recency_score(note.updated_at, now) * w["recency"]

        # 3. 关键词相关度
        #    曾经的 bug：这里用 query.split() 统计命中词——中文没有空格，
        #    整句话会被当成 1 个"词"，命中率判断几乎永远不成立。
        #    现在改成按抽取出来的 2/3 字关键词统计命中数。
        if keywords:
            content = (
                note.content
                if isinstance(note.content, str)
                else ""
            )
            if content:
                hits = keyword_hits(content.casefold(), [k.casefold() for k in keywords])
                if hits:
                    # 命中越多越相关（封顶 1.0）
                    ratio = min(hits / max(len(keywords), 1) * 2.0, 1.0)
                    score += w["fts"] * ratio
                # 整句原样包含是最强的相关信号
                exact = query.strip().casefold()
                if exact and exact in content.casefold():
                    score += w["fts"]

        return score

    def _recency_score(self, updated_at: str | None, now: datetime) -> float:
        """返回 0~1，新鲜度越高越接近 1。"""
        if not updated_at:
            return 0.3  # 默认中等

        try:
            ts_str = updated_at.split(".")[0]
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return 0.3

        age_days = (now - ts).total_seconds() / 86400.0
        if age_days < 0:
            return 1.0
        if age_days < 1:
            return 1.0
        if age_days < 7:
            return 0.7
        if age_days < 30:
            return 0.5
        if age_days < 90:
            return 0.3
        return 0.1


__all__ = ["RetrievalService"]
