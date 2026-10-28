"""历史浏览接口的契约测试（前端依赖这些行为）。

覆盖微信式历史浏览需要的三件事：
1. 无限上翻 —— before_id 游标分页，必须无缝接上、不重不漏
2. 按日期   —— /dates 列出日期，/history?date= 精确取当天
3. 搜索     —— /search 能同时搜到聊天记录和记忆条目
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class FakeLLM:
    model = "fake"

    def chat(self, messages, **kwargs) -> str:
        return json.dumps({"facts": [], "blocks": [], "reasoning": "t"}, ensure_ascii=False)

    def stream_chat(self, messages, **kwargs):
        yield "ok"


def main() -> int:
    import uvicorn
    from companion_ever.api.server import create_app
    from companion_ever.app import build_app
    from companion_ever.config import AppConfig, WebConfig
    import companion_ever.app as app_mod
    from companion_ever.infrastructure.ai.traced_client import TracedLLMClient

    tmpdir = tempfile.mkdtemp(prefix="history_test_")
    app_mod.create_llm_client = lambda cfg: TracedLLMClient(FakeLLM())
    cfg = AppConfig(
        db_path=os.path.join(tmpdir, "test.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "fake"},
        retrieval={"always_include_types": ["profile_fact"], "top_k": 10},
    )
    ctx = build_app(cfg)
    ctx.repos.user.create("alice")
    ctx.services.persona.create_persona("alice", "苏苏", '{"name": "苏苏"}')

    # 造 120 条历史消息（跨越两天）
    conn = ctx.conn
    for i in range(60):
        conn.execute(
            "INSERT INTO conversation_log (user_id, persona_id, turn_index, role, content, created_at) "
            "VALUES ('alice','苏苏',?,?,?,?)",
            (i + 1, "user" if i % 2 == 0 else "assistant",
             f"消息内容 第{i}条 广联达 面试", "2026-10-27 10:00:00"),
        )
    for i in range(60):
        conn.execute(
            "INSERT INTO conversation_log (user_id, persona_id, turn_index, role, content, created_at) "
            "VALUES ('alice','苏苏',?,?,?,?)",
            (100 + i, "user" if i % 2 == 0 else "assistant",
             f"第二天消息 第{i}条 姐姐", "2026-10-28 10:00:00"),
        )
    conn.execute(
        "INSERT INTO note (id, user_id, persona_id, type, content, importance) "
        "VALUES ('n1','alice','苏苏','event','用户收到了广联达录用意向', 0.9)"
    )

    # 起服务
    app = create_app(ctx, WebConfig())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8795, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2.5)
    B = "http://127.0.0.1:8795"
    BASE = "/api/persona/alice/" + urllib.parse.quote("苏苏")

    def get(path):
        with urllib.request.urlopen(B + path, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---------- 1. 无限上翻 ----------
    page = get(BASE + "/history?limit=50")
    assert len(page) == 50, f"首页应 50 条，实际 {len(page)}"
    assert page[0]["id"] < page[-1]["id"], "应按时间正序"
    ids_seen = [m["id"] for m in page]

    # 往前翻，直到翻完
    guard = 0
    while len(ids_seen) < 120 and guard < 10:
        guard += 1
        older = get(BASE + f"/history?limit=50&before_id={ids_seen[0]}")
        if not older:
            break
        ids_seen = [m["id"] for m in older] + ids_seen
    assert len(ids_seen) == 120, f"翻完应拿到 120 条，实际 {len(ids_seen)}"
    assert len(set(ids_seen)) == 120, "分页有重复"
    assert ids_seen == sorted(ids_seen), "分页顺序乱了"
    print("[OK] 无限上翻：120 条分页无重无漏、顺序正确")

    # ---------- 2. after_id 往后 ----------
    mid = ids_seen[60]
    fwd = get(BASE + f"/history?limit=500&after_id={mid}")
    assert all(m["id"] > mid for m in fwd), "after_id 之后应都是更大的 id"
    assert len(fwd) == 59, f"应返回 59 条，实际 {len(fwd)}"
    print("[OK] after_id 往后：定位跳转用")

    # ---------- 3. 按日期 ----------
    dates = get(BASE + "/dates")
    assert len(dates) == 2, f"应有 2 天，实际 {dates}"
    assert dates[0]["date"] == "2026-10-28", f"应按日期倒序，实际 {dates[0]}"
    day = get(BASE + "/history?date=2026-10-27&limit=500")
    assert len(day) == 60, f"10-27 应 60 条，实际 {len(day)}"
    assert all(m["created_at"].startswith("2026-10-27") for m in day)
    print("[OK] 按日期：/dates 列表 + /history?date= 精确取当天")

    # ---------- 4. 搜索 ----------
    hits = get(BASE + "/search?q=" + urllib.parse.quote("广联达"))
    kinds = {h["kind"] for h in hits}
    assert "message" in kinds, "应搜到聊天记录"
    assert "memory" in kinds, "应搜到记忆条目"
    hits2 = get(BASE + "/search?q=" + urllib.parse.quote("广联达") + "&scope=messages")
    assert all(h["kind"] == "message" for h in hits2), "scope=messages 应只返回聊天记录"
    print(f"[OK] 搜索：聊天记录 + 记忆条目（{len(hits)} 条），scope 过滤正常")

    # ---------- 5. 2 字中文关键词（trigram 覆盖不到的）----------
    hits3 = get(BASE + "/search?q=" + urllib.parse.quote("姐姐"))
    assert len(hits3) > 0, "2 字中文关键词应该能搜到"
    print(f"[OK] 2 字中文关键词能搜到（{len(hits3)} 条）")

    server.should_exit = True
    time.sleep(0.4)
    ctx.conn.close()
    print("[ALL] 历史浏览接口契约测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
