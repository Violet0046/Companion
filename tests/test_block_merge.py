"""Block 合并策略与注入范围的测试。

三个约定：
1. `persona_runtime` **不进对话 prompt**（它是会话级状态，只在 /quit 刷新，必然滞后）
2. 抽取 LLM **必须看到现有 Block**（否则它每次从零重写，画像会不断丢失）
3. 应用层合并策略兜底：
   - key_moments 取并集（里程碑不会因时间过去而变假）
   - stage_progress 只能递增
   - list 字段去重 + 限长
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class CapturingLLM:
    """会记录自己收到的 prompt 的假 LLM。"""

    model = "fake-model"

    def __init__(self, blocks_payload: dict | None = None) -> None:
        self.seen: list = []
        self.blocks_payload = blocks_payload or {}

    def chat(self, messages, **kwargs) -> str:
        self.seen.append(messages)
        return json.dumps({
            "facts": [],
            "blocks": self.blocks_payload.get("blocks", []),
            "reasoning": "test",
        }, ensure_ascii=False)

    def stream_chat(self, messages, **kwargs):
        yield self.chat(messages)


def main() -> int:
    from companion_ever.config import AppConfig
    from companion_ever.app import build_app
    import companion_ever.app as app_mod
    from companion_ever.domain.enums import BlockType
    from companion_ever.infrastructure.ai.traced_client import TracedLLMClient

    tmpdir = tempfile.mkdtemp(prefix="blockmerge_test_")
    llm = CapturingLLM()
    app_mod.create_llm_client = lambda cfg: TracedLLMClient(llm)

    cfg = AppConfig(
        db_path=os.path.join(tmpdir, "test.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "fake-model"},
        retrieval={"always_include_types": ["profile_fact"], "top_k": 10},
    )
    ctx = build_app(cfg)
    ctx.repos.user.create("alice")
    ctx.services.persona.create_persona(
        "alice", "苏苏", '{"name": "苏苏", "personality": ["温柔"]}',
    )

    # ---------- 1. 预置一个画像 + 关系状态 ----------
    ctx.services.block.update_block_fields(
        "alice", "苏苏", BlockType.USER_PROFILE,
        {"personality": ["喜欢互称梗"], "preferences": ["喜欢被叫姐姐"]},
    )
    ctx.services.block.update_block_fields(
        "alice", "苏苏", BlockType.RELATIONSHIP_STATE,
        {"stage": "close", "stage_progress": 0.8,
         "key_moments": ["用户第一次叫我姐姐"]},
    )
    ctx.services.block.update_block_fields(
        "alice", "苏苏", BlockType.PERSONA_RUNTIME,
        {"current_topic": "旧话题", "speech_style_reminder": "多用表情"},
    )

    # ---------- 2. persona_runtime 不进 prompt ----------
    r = ctx.services.prompt.build("alice", "苏苏", query="在吗")
    sp = r["messages"][0]["content"]
    assert "speech_style_reminder" not in sp, "persona_runtime 不该进 prompt"
    assert "current_topic" not in sp, "persona_runtime 不该进 prompt"
    assert "## 你对用户的了解" in sp
    assert "用户画像" in sp and "关系进展" in sp
    print("[OK] persona_runtime 不进对话 prompt")

    # ---------- 3. 抽取时 LLM 能看到现有 Block ----------
    ctx.services.conversation.log_turn(
        user_id="alice", persona_id="苏苏", turn_index=1,
        role="user", content="今天心情不错",
    )
    ctx.services.session_close.close_session("alice", "苏苏")

    assert llm.seen, "抽取没有调用 LLM"
    extract_prompt = llm.seen[-1][-1]["content"]
    assert "现有状态" in extract_prompt, "抽取 prompt 里没有【现有状态】"
    assert "喜欢互称梗" in extract_prompt, (
        "抽取 LLM 看不到已有的画像 —— 它会从零重写，把之前学到的全冲掉"
    )
    assert "用户第一次叫我姐姐" in extract_prompt, "抽取 LLM 看不到已有的里程碑"
    # persona_runtime 不该给 LLM 看（里面是自我强化的产物）
    assert "多用表情" not in extract_prompt, (
        "persona_runtime 不该喂给抽取 LLM（speech_style_reminder 是自我强化产物）"
    )
    print("[OK] 抽取 LLM 能看到现有画像和里程碑（读-改-写的前提）")

    # ---------- 4. 合并策略：里程碑取并集、进度不倒退、list 限长 ----------
    ctx.services.block.update_block_fields(
        "alice", "苏苏", BlockType.RELATIONSHIP_STATE,
        {"key_moments": ["用户第一次叫我姐姐", "用户第一次说晚安"],
         "stage_progress": 0.5},
    )
    b = ctx.services.block.get_block("alice", "苏苏", BlockType.RELATIONSHIP_STATE)
    moments = b.fields["key_moments"]
    assert "用户第一次叫我姐姐" in moments, "旧里程碑被冲掉了"
    assert "用户第一次说晚安" in moments
    assert b.fields["stage_progress"] == 0.8, (
        f"stage_progress 倒退了: {b.fields['stage_progress']}"
    )
    print(f"[OK] 合并策略：里程碑取并集（{len(moments)} 条）、进度不倒退")

    # list 限长
    many = [f"偏好{i}" for i in range(20)]
    ctx.services.block.update_block_fields(
        "alice", "苏苏", BlockType.USER_PROFILE, {"preferences": many},
    )
    b2 = ctx.services.block.get_block("alice", "苏苏", BlockType.USER_PROFILE)
    assert len(b2.fields["preferences"]) <= 8, (
        f"preferences 没限长: {len(b2.fields['preferences'])}"
    )
    print(f"[OK] list 字段限长（20 -> {len(b2.fields['preferences'])}）")

    # ---------- 5. persona_runtime 只保留 current_topic ----------
    llm2 = CapturingLLM({
        "blocks": [{
            "block_type": "persona_runtime",
            "fields": {
                "current_topic": "新话题",
                "speech_style_reminder": "多使用颜文字",
                "stable_mood_baseline": "更甜",
            },
        }],
    })
    app_mod.create_llm_client = lambda c: TracedLLMClient(llm2)
    ctx2 = build_app(AppConfig(
        db_path=os.path.join(tmpdir, "test2.db"),
        llm={"api_key": "x", "base_url": "https://x", "model": "fake-model"},
        retrieval={"always_include_types": ["profile_fact"], "top_k": 10},
    ))
    ctx2.repos.user.create("bob")
    ctx2.services.persona.create_persona("bob", "苏苏", '{"name": "苏苏"}')
    ctx2.services.conversation.log_turn(
        user_id="bob", persona_id="苏苏", turn_index=1,
        role="user", content="在干嘛",
    )
    ctx2.services.session_close.close_session("bob", "苏苏")
    rt = ctx2.services.block.get_block("bob", "苏苏", BlockType.PERSONA_RUNTIME)
    assert rt is not None and rt.fields.get("current_topic") == "新话题", (
        f"current_topic 没写进去: {rt}"
    )
    assert "speech_style_reminder" not in rt.fields, (
        "speech_style_reminder 应该被过滤掉（自我强化产物）"
    )
    assert "stable_mood_baseline" not in rt.fields
    print("[OK] persona_runtime 只保留 current_topic（自我强化回环被切断）")

    ctx.conn.close()
    ctx2.conn.close()
    print("[ALL] Block 合并策略与注入范围测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
