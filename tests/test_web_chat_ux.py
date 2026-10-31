"""Web 端聊天体验回归测试

覆盖这次改的三件事：

1. **WebSocket 单独推「思考过程」事件**
   以前 WS 只推正文，前端拿不到"刚收到这条消息"的 think，
   必须切开关 + 重新拉历史才能看到 —— 这就是要修的 bug。

2. **「回到最新」悬浮按钮（微信式）**
   用户往上翻历史时出现；期间来了新消息显示"N 条新消息"。

3. **思考过程开关要放在抽屉靠上的位置**
   以前它在"沉淀的 wiki"下面，几乎找不到。
"""
from __future__ import annotations

import os
import sys
import tempfile
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))


def _make_ctx(db_path: str):
    from test_chat_service import _make_ctx as build
    # WebSocket 的生产者跑在后台线程里，DB 连接要允许跨线程
    return build(db_path, check_same_thread=False)


# ============ 1. WebSocket 推思考过程 ============

def test_ws_sends_thinking_event():
    from fastapi.testclient import TestClient
    from companion_ever.entrypoints.web.server import create_app
    from companion_ever.config import WebConfig

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        ctx = _make_ctx(db_path)
        ctx.repos.user.create("alice")
        ctx.services.persona.create_persona(
            user_id="alice", persona_id="舒舒",
            definition='{"name": "舒舒"}',
        )

        # 模拟推理模型：正文里带 <think> 块
        def fake_stream(messages, **kw):
            yield "<think>她问我话，我该撒娇一点</think>"
            yield "宝宝好呀～"

        ctx.llm.stream_chat = fake_stream
        ctx.llm.last_reasoning = None

        app = create_app(ctx=ctx, web_cfg=WebConfig())
        url = "/ws/chat?" + urlencode({"user": "alice", "persona": "舒舒"})

        with TestClient(app) as client:
            with client.websocket_connect(url) as ws:
                ready = ws.receive_json()
                assert ready["type"] == "ready", ready
                ws.send_json({"type": "chat", "content": "在干嘛"})
                events = []
                for _ in range(30):
                    m = ws.receive_json()
                    events.append(m)
                    if m["type"] in ("done", "error"):
                        break

        types = [e["type"] for e in events]
        assert "error" not in types, f"WS 报错: {events}"
        assert "thinking" in types, f"没收到 thinking 事件: {types}"

        think = next(e for e in events if e["type"] == "thinking")
        assert "撒娇" in think["content"], think

        # 正文里绝不能混进思考内容
        text = "".join(e["content"] for e in events if e["type"] == "delta")
        assert text == "宝宝好呀～", f"正文被思考内容污染: {text!r}"

        # 数据库里 think 也是独立存的
        recent = ctx.services.conversation.get_recent_messages("alice", "舒舒")
        assistant = [m for m in recent if m.role == "assistant"]
        assert assistant, "assistant 消息没落库"
        assert assistant[0].content == "宝宝好呀～", assistant[0].content

        print("[OK] WebSocket 单独推送思考过程，正文不受污染")
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


# ============ 2 & 3. 前端静态检查 ============

def test_frontend_jump_latest_and_switch_placement():
    from companion_ever.entrypoints.web.server import WEB_DIR

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")

    # --- 悬浮按钮：元素 + 样式 + 逻辑 ---
    assert "id=\"btn-jump-latest\"" in html, "缺少「回到最新」按钮"
    assert "id=\"jump-latest-label\"" in html, "缺少角标文案节点"
    assert ".jump-latest" in css, "缺少 .jump-latest 样式"
    assert "function updateJumpButton" in js, "缺少 updateJumpButton()"
    assert "newCount" in js, "缺少新消息计数"
    # 用户不��底部时不许把视口拽回底部
    assert js.count("if (wasAtBottom) scrollBottom();") >= 3, (
        "流式输出时应该只在用户本来就在底部的情况下自动跟随"
    )
    assert "S.newCount += 1;" in js, "新消息没有累加计数"
    assert "$('btn-jump-latest').onclick" in js, "悬浮按钮没有绑定点击"

    # --- 思考过程开关要放在抽屉靠上（记忆条目/沉淀 wiki 之前） ---
    i_switch = html.find("toggle-thinking")
    i_wiki = html.find("note-list")
    i_session = html.find("btn-close-session")
    assert i_switch > 0 and i_wiki > 0, "找不到开关或记忆条目"
    assert i_session < i_switch < i_wiki, (
        "「显示她的思考过程」应该放在「让她记住」之后、记忆条目之前，"
        f"现在 session={i_session} switch={i_switch} wiki={i_wiki}"
    )

    # --- 思考过程：前端能显示 + 服务端能带 ---
    assert "include_thinking" in js, "拉历史时没有显式指定要不要带 think"
    assert "case 'thinking'" in js, "前端没有处理 thinking 事件"
    assert "buildThinkBlock" in js, "缺少思考过程折叠块"
    assert "S.showThinking" in js, "没有按开关控制思考过程的显示"

    print("[OK] 前端：回到最新按钮 + 开关位置 + 思考过程显示逻辑")
    return 0


def main() -> int:
    test_ws_sends_thinking_event()
    test_frontend_jump_latest_and_switch_placement()
    print("[ALL] Web 聊天体验测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
