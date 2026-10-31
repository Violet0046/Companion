"""模型自编 <tool_call> 块的清洗。

背景：聊天没有工具，但 MiniMax-M3 偶尔会自编一个 tool_call（工具名还是乱的，
标签里还夹零宽字符），原样显示给用户就是一段垃圾。

两个约定：
1. 入库正文里不能有 tool_call 块
2. 模型只吐了 tool_call 时，块里 arguments.text 才是真正的回复——要兜底出来
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from companion_ever.conversations.reply import split_assistant_reply  # noqa: E402
from companion_ever.conversations.stream_filter import ThinkFilter  # noqa: E402

ZWSP = ""

NORMAL = "你好呀，今天过得怎么样？"

MIXED = (
    "你好，很高兴认识你。  \n"
    "今天有什么想一起 exploring 的吗？\n"
    f"<{ZWSP}tool_call>\n"
    '{"name":"电池","arguments":{"reasoning":"向用户表达问候并开启对话。",'
    '"text":"你好，很高兴认识你。\\n今天有什么想一起探索的吗？"}}\n'
    f"</{ZWSP}tool_call>"
)

ONLY_CALL = (
    f"<{ZWSP}tool_call>\n"
    '{"name":"电池","arguments":{"text":"其实我更想先听你说。"}}\n'
    f"</{ZWSP}tool_call>"
)


def main() -> int:
    # 1. 普通回复不受影响
    body, think = split_assistant_reply(NORMAL)
    assert body == NORMAL, body
    assert think is None, think

    # 2. 混着 tool_call 的回复：正文留下，tool_call 块被剔除
    body, _ = split_assistant_reply(MIXED)
    assert "tool_call" not in body, body
    assert "电池" not in body, body
    assert "很高兴认识你" in body, body

    # 3. 只有 tool_call：正文从 arguments.text 兜底
    body, _ = split_assistant_reply(ONLY_CALL)
    assert body == "其实我更想先听你说。", body

    # 4. 流式：tool_call 块不能漏给前端
    filt = ThinkFilter()
    out = "".join(filt.feed(chunk) for chunk in MIXED)
    out += filt.flush()
    assert "tool_call" not in out, out
    assert "电池" not in out, out
    assert "很高兴认识你" in out, out

    # 5. 流式：只吐 tool_call 时，兜底把真正的回复显示出来
    filt = ThinkFilter()
    out = "".join(filt.feed(chunk) for chunk in ONLY_CALL)
    out += filt.flush()
    assert "tool_call" not in out, out
    assert "其实我更想先听你说" in out, out

    print("[OK] tool_call 噪声清洗：正文剔除、arguments.text 兜底、流式不漏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
