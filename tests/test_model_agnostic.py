"""换模型也不怕 —— 思考处理的模型无关性测试。

场景：以后从 MiniMax-M3 换成别的模型，可能出现三种情况：

1. **模型没有思考功能**（比如普通 GPT）—— 正文里根本没有 think 块
2. **模型用不同的标签**（<thinking> / <reasoning> 而不是 <think>）
3. **模型把思考放在独立字段**（reasoning_content），不在正文里

这三种都必须正常工作：
- 正文不能被污染（think 绝不能进 prompt）
- 思考过程仍然能被用户看到（存进 thinking_log）
- 没有思考的模型不能产生任何副作用

另外还测了零宽字符（BOM）的清理。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class BaseLLM:
    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0
        self.last_reasoning = None

    def chat(self, messages, **kwargs) -> str:
        self.calls += 1
        return self.reply()

    def stream_chat(self, messages, **kwargs):
        self.calls += 1
        yield self.reply()

    def reply(self) -> str:  # pragma: no cover - 子类覆盖
        return "普通回复"


class NoThinkLLM(BaseLLM):
    """模型完全没有思考功能。"""

    def reply(self) -> str:
        return "今天天气不错呀，你还好吗～"


class ThinkingTagLLM(BaseLLM):
    """模型用 <thinking> 标签（不是 <think>）。"""

    def reply(self) -> str:
        return "<thinking>用户问天气了，关心一下</thinking>今天有点凉，记得加衣服哦～"


class ReasoningFieldLLM(BaseLLM):
    """模型把思考放在 reasoning_content 字段，正文里没有 think 标签。"""

    def reply(self) -> str:
        self.last_reasoning = "The user asks about weather. I should be caring."
        return "今天有点凉，记得加衣服哦～"


def _build(tmpdir: str, llm):
    from companion_ever.config import AppConfig
    from companion_ever.app import build_app
    import companion_ever.app as app_mod
    from companion_ever.llm.traced_client import TracedLLMClient

    app_mod.create_llm_client = lambda cfg: TracedLLMClient(llm)

    cfg = AppConfig(
        db_path=os.path.join(tmpdir, "test.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "fake-model"},
        retrieval={"always_include_types": ["profile_fact"], "top_k": 10},
    )
    ctx = build_app(cfg)
    ctx.repos.user.create("alice")
    ctx.services.persona.create_persona(
        "alice", "苏苏",
        json.dumps({"name": "苏苏", "personality": ["温柔"]}, ensure_ascii=False),
    )
    return ctx


def _assistant_body(ctx):
    msgs = ctx.services.conversation.get_recent_messages("alice", "苏苏", limit=10)
    return [m for m in msgs if m.role == "assistant"][-1].content


def _thinking_of(ctx):
    msgs = ctx.services.conversation.get_recent_messages("alice", "苏苏", limit=10)
    tmap = ctx.services.conversation.get_thinking_map(
        user_id="alice", persona_id="苏苏", message_ids=[m.id for m in msgs],
    )
    return list(tmap.values())[0] if tmap else None


def _case(tmpdir, name, llm):
    d = tempfile.mkdtemp(prefix=f"model_{name}_", dir=tmpdir)
    ctx = _build(d, llm)
    ctx.services.chat.chat("alice", "苏苏", "今天天气怎么样")
    return ctx


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="model_agnostic_")

    # ---------- 1. 没有思考功能的模型 ----------
    ctx = _case(tmpdir, "nothink", NoThinkLLM())
    body = _assistant_body(ctx)
    assert body == "今天天气不错呀，你还好吗～", f"正文不对: {body!r}"
    assert _thinking_of(ctx) is None, "没有思考的模型不该产生 thinking_log 记录"
    r = ctx.services.prompt.build("alice", "苏苏", query="天气")
    whole = "".join(m["content"] for m in r["messages"])
    assert "<think>" not in whole and "<thinking>" not in whole
    print("[OK] 无思考功能的模型：正文完整、无 thinking_log、prompt 干净")
    ctx.conn.close()

    # ---------- 2. 不同标签的思考 ----------
    ctx = _case(tmpdir, "thinking", ThinkingTagLLM())
    body = _assistant_body(ctx)
    assert "<thinking>" not in body, f"<thinking> 没被剥掉: {body!r}"
    assert "今天有点凉" in body, "正文丢了"
    think = _thinking_of(ctx)
    assert think and "关心一下" in think, f"思考记录不对: {think!r}"
    r = ctx.services.prompt.build("alice", "苏苏", query="天气")
    whole = "".join(m["content"] for m in r["messages"])
    assert "<thinking>" not in whole and "关心一下" not in whole, (
        "思考内容漏进 prompt 了"
    )
    print("[OK] <thinking> 标签：正文剥干净、思考另存、prompt 无污染")
    ctx.conn.close()

    # ---------- 3. 思考在独立字段 ----------
    ctx = _case(tmpdir, "reasoning", ReasoningFieldLLM())
    body = _assistant_body(ctx)
    assert body == "今天有点凉，记得加衣服哦～", f"正文不对: {body!r}"
    think = _thinking_of(ctx)
    assert think and "I should be caring" in think, (
        f"reasoning_content 没被当成思考记录: {think!r}"
    )
    r = ctx.services.prompt.build("alice", "苏苏", query="天气")
    whole = "".join(m["content"] for m in r["messages"])
    assert "I should be caring" not in whole, "reasoning_content 漏进 prompt 了"
    print("[OK] reasoning_content 字段：思考被捕获并存进 thinking_log、prompt 无污染")
    ctx.conn.close()

    # ---------- 4. 零宽字符（BOM）清理 ----------
    ctx = _build(tempfile.mkdtemp(prefix="model_bom_", dir=tmpdir), NoThinkLLM())
    ctx.services.chat.chat("alice", "苏苏", "﻿我最近面试怎么样了\u200b")
    msgs = ctx.services.conversation.get_recent_messages("alice", "苏苏", limit=10)
    user_msgs = [m for m in msgs if m.role == "user"]
    content = user_msgs[-1].content
    assert "\ufeff" not in content, f"BOM 没被清理: {content!r}"
    assert "\u200b" not in content, f"零宽空格没被清理: {content!r}"
    assert content == "我最近面试怎么样了", f"清理过头了: {content!r}"
    print(f"[OK] 零宽字符清理: {content!r}")
    ctx.conn.close()

    print("[ALL] 换模型也不怕 —— 思考处理的模型无关性测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
