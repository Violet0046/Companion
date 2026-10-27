"""application/chat/retrieval_service.py — Wiki 召回 + 排序。

职责：
- 决定每轮对话要送哪些 notes 进 prompt
- 输入：user_id, persona_id, query（用户当前输入）
- 输出：排序后的 notes 列表

算法：
1. 永远包含（high priority 类型）—— 由 cfg.always_include_types 配置
2. 按 query 检索：FTS5 召回
3. 合并去重
4. 按 importance × 时间新度 × FTS rank 排序（权重由 cfg.weights 配置）
5. 截取 top_k（由 cfg.top_k 配置）

变化：
- 权重 / 类型加分 / always-include-types 由 RetrievalConfig 注入
- 使用 Authorizer 替代内联 _check_permission
"""
from __future__ import annotations

from datetime import datetime, timezone

from companion_ever.application.authz import Authorizer
from companion_ever.config import RetrievalConfig
from companion_ever.domain.entities import Note
from companion_ever.domain.enums import NoteType
from companion_ever.infrastructure.persistence.note_repo import NoteRepo
from companion_ever.utils.text import all_keywords, keyword_hits
from companion_ever.infrastructure.trace import current_trace


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

    def retrieve(
        self,
        user_id: str,
        persona_id: str,
        query: str,
        top_k: int | None = None,
    ) -> list[Note]:
        """召回与 query 相关的 notes（按 score 排序）。

        Args:
            user_id, persona_id: 限定范围
            query: 当前用户输入（用于 FTS5 检索）
            top_k: 最多返回多少条（默认用 cfg.top_k）

        Returns:
            按 score 降序排列的 notes 列表
        """
        import time as _time
        _t0 = _time.perf_counter()

        # 权限校验
        self.authorizer.require_persona(user_id, persona_id)

        if top_k is None:
            top_k = self.cfg.top_k

        # 1. 永远包含的核心类型
        always_notes = self.note_repo.find(
            user_id=user_id,
            persona_id=persona_id,
            types=list(self.cfg.always_include_types),
            is_archived=False,
            limit=100,
        )

        # 2. 关键词召回（FTS5 + LIKE 兜底）
        fts_hits: list[Note] = []
        keywords: list[str] = []
        if query and query.strip():
            keywords = all_keywords(query)
            fts_hits = self.note_repo.search(
                query=query,
                user_id=user_id,
                persona_id=persona_id,
                limit=max(self.cfg.top_k * 3, 20),
            )

        # 3. 合并去重（保持顺序：先 always，再 fts）
        seen_ids: set[str] = set()
        candidates: list[Note] = []
        for note in always_notes + fts_hits:
            if note.id in seen_ids:
                continue
            seen_ids.add(note.id)
            candidates.append(note)

        # 4. 评分排序
        now = datetime.now(timezone.utc)
        scored = [(self._score(n, query, keywords, now), n) for n in candidates]
        scored.sort(key=lambda x: -x[0])  # 降序

        # 5. 取 top_k
        result = [n for _, n in scored[:top_k]]

        # --- trace：记下召回了什么、为什么 ---
        tr = current_trace()
        if tr is not None:
            tr.step(
                "retrieval.retrieve",
                ms=(_time.perf_counter() - _t0) * 1000,
                query=query[:120],
                top_k=top_k,
                always_included=len(always_notes),
                fts_hits=len(fts_hits),
                candidates=len(candidates),
                returned=len(result),
                notes=[
                    {
                        "id": n.id,
                        "type": n.type.value,
                        "score": round(sc, 3),
                        "importance": n.importance,
                        "content": (
                            n.content if isinstance(n.content, str)
                            else str(n.content)
                        )[:80],
                    }
                    for sc, n in scored[:top_k]
                ],
            )

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
              + type_bonus * weights["type"]
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
                hits = keyword_hits(content, keywords)
                if hits:
                    # 命中越多越相关（封顶 1.0）
                    ratio = min(hits / max(len(keywords), 1) * 2.0, 1.0)
                    score += w["fts"] * ratio
                # 整句原样包含是最强的相关信号
                exact = query.strip()
                if exact and exact in content:
                    score += w["fts"]

        # 4. 类型加权
        type_str = (
            note.type.value
            if isinstance(note.type, NoteType)
            else str(note.type)
        )
        type_bonus = self.cfg.type_bonus.get(type_str, 0.03)
        score += type_bonus * w["type"]

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