"""提示词模板的结构约束：引用必须指向真实存在的小节。

背景：
- 聊天行为指引曾引用【当前状态】与【最近对话】，而实际渲染的小节叫
  「## 相关记忆」与「## 用户画像与关系概况」，历史又是原生 role 消息——
  模板指向了从未渲染过的章节，模型只能自己猜。
- 现在章节标题与行为指引同在 chat_system.txt 里，引用不可能再走偏。
  本文件用结构检查把这条约束固定下来。

验证：
1. 行为指引里的每个【X】都能在同一个模板里找到对应的 "## X" 小节
2. 模板里的每个 {占位符} 都是渲染方会提供的名字
3. compose() 丢弃空小节时连标题一起丢弃
4. 所有模板都能加载，且不含遗留的英文占位章节名
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion_ever.prompts import CHAT_SYSTEM, compose, load  # noqa: E402

# 渲染方（conversations.context.PromptService）会填的小节
CHAT_SECTIONS = {"time_section", "persona_section", "notes_section", "summary_section"}

# chat_system.txt 之外的其他模板
OTHER_TEMPLATES = (
    "memory_draft_system.txt",
    "summary_system.txt",
    "repair_evidence.txt",
    "repair_citation.txt",
)


def template_sections(template: str) -> set[str]:
    """模板里定义的小节标题（不含 "## " 前缀）。"""
    return {
        line[3:].strip()
        for line in template.splitlines()
        if line.startswith("## ")
    }


def template_references(template: str) -> set[str]:
    """模板里【X】形式的小节引用。"""
    return set(re.findall(r"【([^】]+)】", template))


def template_placeholders(template: str) -> set[str]:
    """模板里的 {name} 占位符。"""
    return set(re.findall(r"\{(\w+)\}", template))


def references_resolve():
    """行为指引引用的每个小节，都必须在同一个模板里真实存在。"""
    sections = template_sections(CHAT_SYSTEM)
    references = template_references(CHAT_SYSTEM)
    missing = references - sections
    assert not missing, (
        f"chat_system.txt 引用了不存在的小节 {sorted(missing)}；"
        f"模板里的小节只有 {sorted(sections)}"
    )
    # 反向也要成立：数据型小节若没人引用，说明指引漏了说明。
    # 行为指引与回复格式本身是指令，不需要被引用。
    standalone = {"行为指引", "回复格式"}
    unreferenced = sections - references - standalone
    assert not unreferenced, (
        f"chat_system.txt 定义了小节 {sorted(unreferenced)}，"
        f"但行为指引没有提到它——模型不知道该拿它做什么"
    )


def placeholders_are_known():
    """模板里的占位符必须都是渲染方提供的。"""
    unknown = template_placeholders(CHAT_SYSTEM) - CHAT_SECTIONS
    assert not unknown, f"chat_system.txt 出现未知占位符 {sorted(unknown)}"


def compose_drops_empty_sections():
    """正文为空的小节，连标题一起消失。"""
    rendered = compose(CHAT_SYSTEM, {
        "time_section": "- **现在**：2026-01-01",
        "persona_section": "```json\n{}\n```",
        "notes_section": "",
        "summary_section": "",
    })
    assert "## 相关记忆" not in rendered, "空小节的标题应一并消失"
    assert "## 用户画像与关系概况" not in rendered, "空小节的标题应一并消失"
    assert "## 当前这一轮" in rendered
    assert "## 人设基础" in rendered
    assert "## 行为指引" in rendered
    # 正文内容确实进去了
    assert "- **现在**：2026-01-01" in rendered


def compose_keeps_filled_sections():
    """有正文的小节正常渲染。"""
    rendered = compose(CHAT_SYSTEM, {
        "time_section": "T",
        "persona_section": "P",
        "notes_section": "- (fact) 喜欢茶",
        "summary_section": "用户画像（上次成功整理）：\n- 喜欢茶",
    })
    assert "## 相关记忆\n- (fact) 喜欢茶" in rendered
    assert "## 用户画像与关系概况" in rendered
    assert "用户画像（上次成功整理）" in rendered


def templates_load():
    """所有模板可加载，且不再引用旧的英文占位章节名。"""
    for name in OTHER_TEMPLATES:
        text = load(name)
        assert text.strip(), f"{name} 内容为空"
    # 旧章节名不应再出现在任何模板里
    stale = ("【当前状态】", "【最近对话】", "persona_basics")
    for name in ("chat_system.txt",) + OTHER_TEMPLATES:
        text = load(name)
        for phrase in stale:
            assert phrase not in text, f"{name} 仍含旧表述 {phrase}"


def closing_sections_come_last():
    """最后两节是「历史会话」和「回复格式」。

    顺序有讲究：先给数据（当前这一轮、人设、记忆、摘要），
    最后才讲"历史在哪、怎么读"和"怎么回"。
    """
    order = [line[3:].strip() for line in CHAT_SYSTEM.splitlines() if line.startswith("## ")]
    assert order[-2:] == ["历史会话", "回复格式"], f"结尾小节顺序不对: {order}"
    assert order[0] == "当前这一轮", f"当前这一轮应在最前，实际顺序: {order}"


def chat_template_explains_the_vocabulary():
    """聊天模型看到的笔记元数据，模板必须解释词汇。

    相关记忆渲染成 (kind, reality=..., event_time=..., importance=...)，
    摘要渲染成 [reality] 文本——不解释的话模型只能猜。
    """
    text = load("chat_system.txt")
    for word in ("fact", "episode", "user_report", "shared_fiction", "uncertain",
                 "event_time", "importance"):
        assert word in text, f"聊天模板没有解释 {word}"


def protocol_text_lives_in_templates():
    """行为规则不能藏在 Python 里。"""
    chat = load("chat_system.txt")
    assert "不要用旧摘要否定用户的更正" in chat, (
        "『旧摘要不能否定用户最新陈述』这条规则应在聊天模板里"
    )
    source = (ROOT / "src" / "companion_ever" / "memory" / "summaries"
              / "service.py").read_text(encoding="utf-8")
    assert "不要用旧摘要否定用户的更正" not in source, (
        "这条规则还在 summaries/service.py 里，应移到聊天模板"
    )


def templates_share_one_style():
    """规则型模板统一用 '## ' 小节 + '- ' 列表，不再堆无标题的密集中文段落。"""
    for name in ("chat_system.txt", "memory_draft_system.txt", "summary_system.txt"):
        lines = [l for l in load(name).splitlines() if l.strip()]
        heads = [l for l in lines if l.startswith("## ")]
        assert heads, f"{name} 没有任何 '## ' 小节"
        assert len(heads) >= 2, f"{name} 只有 {len(heads)} 个小节，结构不足以分块"
        # 非小节行必须是列表项（小节正文占位符除外），不能是裸段落
        body = [l for l in lines if not l.startswith("## ")]
        naked = [l for l in body
                 if not l.startswith("- ") and not re.fullmatch(r"\{\w+\}", l)]
        assert not naked, f"{name} 仍有无列表符号的裸段落: {naked[:2]}"


if __name__ == "__main__":
    references_resolve()
    placeholders_are_known()
    compose_drops_empty_sections()
    compose_keeps_filled_sections()
    templates_load()
    templates_share_one_style()
    closing_sections_come_last()
    chat_template_explains_the_vocabulary()
    protocol_text_lives_in_templates()
    print("[OK] 提示词模板结构：引用指向真实小节、占位符已知、空小节连标题消失、"
          "风格统一、小节顺序固定、词汇有解释、规则在模板里、无旧表述")
