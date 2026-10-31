"""utils/text.py — 文本处理工具（横切）。

关键词抽取
----------
中文没有空格分词，而 SQLite FTS5 的 trigram tokenizer 只能做**字面子串匹配**
（3 字以上才有效）。两者叠加导致：

    用户输入 "我最近面试怎么样了"
      → FTS5 要求整句原样出现 → 0 命中
      → LIKE  要求整句原样出现 → 0 命中

结果就是"模型看到的相关记忆"其实和当前这句话毫无关系。

解决思路（**不引入分词器依赖**）：
1. 去掉标点
2. 生成所有 2 字 / 3 字滑窗
3. 停用字过滤
   - 2 字：两个字都不能是停用字（"面试" ✓ / "怎么" ✗）
   - 3 字：最多允许 1 个停用字
4. 检索时多个关键词用 **OR** 组合 → 召回变宽
5. 打分阶段用"命中关键词数"排序 → 精度回来

中文的有效检索单位往往就是 2 字（面试 / 秋招 / 研三 / 承诺），
所以 2 字词走 LIKE、3 字词走 FTS5，是这个场景下性价比最高的组合。
"""
from __future__ import annotations

import json
import re
from typing import Iterable


# 标点（中英文）
_PUNCT = set(
    "，。！？、；：""''（）()【】[]{}《》〈〉…—－-～~"
    "`@#$%^&*_+=|<>.,!?;:'\"\\/"
)

# 零宽 / 不可见字符（含 UTF-8 BOM）——管道输入时经常混进来
# 零宽 / 不可见字符。不含 ZWJ/ZWNJ（U+200D/U+200C），
# 它们在 emoji 序列里是有意义的，删了会破坏表情。
_INVISIBLE = set("﻿​⁠ ")

# 停用字：常见虚词/代词/语气词。2 字关键词要求每个字都不是停用字。
_STOP_CHARS = set(
    "我你他她它们的了是不在有和就都也很太还要会能可以能跟把被给让从到为与及"
    "并且或者但而虽若如使做说道说讲问答叫想看听吃喝睡玩买给用时候时间现在今天"
    "明天昨天以前以后之前之后这个那个什么怎样么呢吧啊呀哦嗯唉嘿嘞呗嘛啦哦"
    "请麻烦谢谢对不起不好意思应该觉得认为知道感觉因为所以如果虽然然后而且"
    "已经正在刚刚一直比较非常真的确实其实一下一样一些个些"
)


# 模型模仿旧版 prompt 历史格式时吐出的前缀：
#   [2026-09-29 11:14:12] assistant: \n\n...
#   [2026-09-29 21:56:00]\n\n...
#   [2026-10-27 20:48] \n\n...
# 有时还会叠加多层。
#
# 为什么会这样：prompt 里历史消息的格式是 "[时间戳] 内容"，
# 模型看了几百轮之后学会了这个格式，开始在自己的回复里也加上前缀。
# 所以**写入时就要剥掉**，否则每次对话都会重新制造这种污染。
_FORMAT_PREFIX_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2}(?::\d{2})?)\]"
    r"\s*(?:(?:assistant|user)\s*:\s*)?"
    r"[\s\u200b\u3000]*",
    re.IGNORECASE,
)


def strip_format_prefix(content: str) -> tuple[str, int]:
    """剥掉开头模仿的时间戳/role 前缀（可能叠加多层）。

    Returns:
        (新内容, 剥了几层)
    """
    layers = 0
    text = content
    while True:
        m = _FORMAT_PREFIX_RE.match(text)
        if not m:
            break
        text = text[m.end():]
        layers += 1
    return text, layers


# 不同厂商的推理模型用的思考标签不一样：
#   MiniMax-M3 / DeepSeek-R1 / Qwen  -> <think>
#   Anthropic 风格                   -> <think>
#   一些自建/代理网关                 -> <thinking> / <reasoning>
# 换模型时不用改代码，这里统一识别。
THINK_TAG_NAMES = ("think", "thinking", "reasoning")

THINK_PATTERN = re.compile(
    r"<(" + "|".join(THINK_TAG_NAMES) + r")>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)

# 兼容旧名字
_THINK_BLOCK_RE = THINK_PATTERN


def extract_thinking(text: str) -> tuple[str, str | None]:
    """从 LLM 回复里拆出 <think> 块。

    Returns:
        (去掉 think 的正文, think 原文 or None)

    用途：思考过程要保存到 thinking_log（给用户看），
    正文要进 conversation_log（给模型看）。
    """
    blocks = THINK_PATTERN.findall(text)
    if not blocks:
        return text, None
    body = THINK_PATTERN.sub("", text).strip()
    think = "\n\n".join(b.strip() for _tag, b in blocks if b.strip())
    return body, (think or None)


def strip_thinking(text: str) -> str:
    """移除思考块（包含标签本身）。

    不限于某一种模型：识别 think / thinking / reasoning 三种标签。
    """
    return THINK_PATTERN.sub("", text).strip()


# 聊天没有工具，但模型偶尔会自编一个 <tool_call> 块（工具名还是乱的）。
# 标签里常夹零宽字符，匹配时要能容忍。
TOOL_CALL_PATTERN = re.compile(
    r"<[\s]*tool_call[\s]*>(.*?)</[\s]*tool_call[\s]*>",
    re.IGNORECASE | re.DOTALL,
)


def _tool_call_text(block: str) -> str | None:
    """从 tool_call 块里取出 arguments.text——有的模型把回复放在那儿。"""
    try:
        data = json.loads(block.strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    args = data.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return None
    if isinstance(args, dict):
        text = args.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def strip_tool_calls(text: str) -> tuple[str, str | None]:
    """剔除模型误吐的 <tool_call> 块。

    聊天没有工具，这种块对用户是垃圾。它里面的 arguments.text 往往才是
    真正的回复——正文被清空时用它兜底。

    Returns:
        (清理后的正文, 兜底回复 or None)
    """
    blocks = TOOL_CALL_PATTERN.findall(text)
    if not blocks:
        return text, None
    body = TOOL_CALL_PATTERN.sub("", text).strip()
    for block in blocks:
        fallback = _tool_call_text(block)
        if fallback:
            return body, fallback
    return body, None


def strip_invisible(text: str) -> str:
    """去掉零宽 / 不可见字符（含 UTF-8 BOM）。

    这些字符看不出来，但会进数据库、会破坏检索、会让模型
    的分词失效。例如 PowerShell 管道输入带来的 BOM。
    """
    return "".join(ch for ch in text if ch not in _INVISIBLE)


def normalize(text: str) -> str:
    """去掉标点与空白，只保留实义字符。"""
    return "".join(
        ch for ch in text
        if ch not in _PUNCT and ch not in _INVISIBLE and not ch.isspace()
    )


def _is_stop(ch: str) -> bool:
    return ch in _STOP_CHARS


def extract_keywords(
    query: str,
    max_short: int = 8,
    max_long: int = 8,
) -> tuple[list[str], list[str]]:
    """从自然语言查询里抽取检索关键词。

    Args:
        query: 用户原始输入（可以是整句话）
        max_short: 最多几个 2 字词
        max_long: 最多几个 3 字词

    Returns:
        (short_keywords, long_keywords)
        - short：2 字词，走 LIKE 兜底（中文主力）
        - long ：3 字词，走 FTS5 字面匹配（精度高）
    """
    text = normalize(query)
    if len(text) < 2:
        return [], []

    short: list[str] = []
    long: list[str] = []
    seen: set[str] = set()

    # 3 字词：最多允许 1 个停用字
    for i in range(len(text) - 2):
        g = text[i : i + 3]
        if g in seen:
            continue
        if sum(1 for ch in g if _is_stop(ch)) > 1:
            continue
        seen.add(g)
        long.append(g)
        if len(long) >= max_long:
            break

    # 2 字词：每个字都不能是停用字
    for i in range(len(text) - 1):
        g = text[i : i + 2]
        if g in seen:
            continue
        if any(_is_stop(ch) for ch in g):
            continue
        seen.add(g)
        short.append(g)
        if len(short) >= max_short:
            break

    return short, long


def all_keywords(query: str, **kw) -> list[str]:
    """短词 + 长词合并（打分阶段用）。"""
    short, long = extract_keywords(query, **kw)
    # 长词优先（更具体）
    return long + short


def keyword_hits(content: str, keywords: Iterable[str]) -> int:
    """content 命中了多少个不同关键词。"""
    if not content:
        return 0
    return sum(1 for kw in keywords if kw and kw in content)


def keyword_hit_ratio(content: str, keywords: Iterable[str]) -> float:
    """命中率 = 命中关键词数 / 关键词总数（0~1）。"""
    kws = list(keywords)
    if not kws or not content:
        return 0.0
    return keyword_hits(content, kws) / len(kws)


__all__ = [
    "normalize",
    "extract_thinking",
    "strip_thinking",
    "strip_invisible",
    "THINK_PATTERN",
    "THINK_TAG_NAMES",
    "strip_format_prefix",
    "extract_keywords",
    "all_keywords",
    "keyword_hits",
    "keyword_hit_ratio",
]
