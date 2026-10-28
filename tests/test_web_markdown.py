r"""Markdown 渲染测试

把 app.js 里的 renderMarkdown / renderInlineMd 抽出来，用 node 跑真实逻辑：

- 粗体 / 斜体 / 删除线 / 行内代码 / 有序无序列表 / 换行
- XSS：模型输出里的 <script> 必须被转义（模型输出是不可信输入）
- 中文里的 *斜体* 也要能匹配（中文不是 \w，用 \w 做边界会漏）
- 行内代码不能再转义一次（否则 &lt; 变成 &amp;lt;）

环境里没有 node 时跳过功能部分，只做静态检查。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

HARNESS = r"""
const cases = [
  // 基本格式
  ["**\u7c97\u4f53**", "<strong>\u7c97\u4f53</strong>"],
  ["\u4f60\u597d*\u659c\u4f53*\u597d", "\u4f60\u597d<em>\u659c\u4f53</em>\u597d"],
  ["*\u5f00\u5934\u659c\u4f53*", "<em>\u5f00\u5934\u659c\u4f53</em>"],
  ["~~\u5220\u6389~~", "<del>\u5220\u6389</del>"],
  ["`code`", "<code>code</code>"],
  ["**\u7c97**\u548c`x`", "<strong>\u7c97</strong>\u548c<code>x</code>"],
  // 列表
  ["- \u7532\n- \u4e59", "<ul><li>\u7532</li><li>\u4e59</li></ul>"],
  ["1. \u7532\n2. \u4e59", "<ol><li>\u7532</li><li>\u4e59</li></ol>"],
  // 换行 / 普通文本
  ["a\nb", "a<br>b"],
  ["\u666e\u901a\u4e00\u53e5\u8bdd", "\u666e\u901a\u4e00\u53e5\u8bdd"],
  ["a * b", "a * b"],
  ["", ""],
  // XSS
  ["<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"],
  ["**<script>x</script>**", "<strong>&lt;script&gt;x&lt;/script&gt;</strong>"],
  ["`<b>`", "<code>&lt;b&gt;</code>"],
  ["`**\u4e0d\u662f\u7c97\u4f53**`", "<code>**\u4e0d\u662f\u7c97\u4f53**</code>"],
];

let fail = 0;
for (const [input, expect] of cases) {
  const got = renderMarkdown(input);
  if (got !== expect) {
    console.error("FAIL " + JSON.stringify(input));
    console.error("  got     " + JSON.stringify(got));
    console.error("  expect  " + JSON.stringify(expect));
    fail++;
  }
}
if (fail) { console.error(fail + " \u4e2a\u7528\u4f8b\u5931\u8d25"); process.exit(1); }
console.log("[OK] " + cases.length + " \u4e2a markdown \u7528\u4f8b\u5168\u8fc7");
"""


def extract_fn(src: str, name: str) -> str:
    """从 JS 源码里切出一个函数（按花括号配平）。"""
    idx = src.find("function " + name + "(")
    assert idx >= 0, f"app.js 里找不到函数 {name}"
    i = src.index("{", idx)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[idx:j + 1]
    raise AssertionError(f"{name} 的花括号没配平")


def test_markdown_functional() -> bool:
    """用 node 跑真实的渲染逻辑。"""
    node = shutil.which("node")
    if not node:
        print("[SKIP] 环境里没有 node，跳过 markdown 功能测试")
        return False

    from companion_ever.api.server import WEB_DIR
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    snippet = "\n\n".join([
        extract_fn(js, "esc"),
        extract_fn(js, "renderInlineMd"),
        extract_fn(js, "renderMarkdown"),
        HARNESS,
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(snippet)
        path = f.name
    try:
        r = subprocess.run(
            [node, path], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if r.returncode != 0:
        raise AssertionError(f"markdown 用例失败:\n{r.stdout}\n{r.stderr}")
    print(r.stdout.strip())
    return True


def test_markdown_wired_in():
    """前端确实用上了渲染器。"""
    from companion_ever.api.server import WEB_DIR
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")

    # 历史消息渲染
    assert "text.innerHTML = renderMarkdown(content);" in js, (
        "renderMsg 没有用 renderMarkdown —— 历史消息里的 ** 不会被渲染"
    )
    # 流式结束后升级成 markdown
    assert "S.streamEl.firstChild.innerHTML = renderMarkdown(rawText);" in js, (
        "流式结束后没有升级成 markdown"
    )
    # 入库/进内存的必须是原始文本，不能是渲染后的
    assert "content: rawText," in js, "存进 S.messages 的必须是原始文本"
    assert "content: S.streamEl.firstChild.textContent" not in js, (
        "存进 S.messages 的不能是渲染后的文本（会丢 ** 标记，入库就变形了）"
    )
    # 样式
    for sel in (".msg .body strong", ".msg .body code", ".msg .body ul"):
        assert sel in css, f"缺少 markdown 样式 {sel}"
    print("[OK] markdown 渲染器已接进前端")
    return True


def main() -> int:
    test_markdown_functional()
    test_markdown_wired_in()
    print("[ALL] markdown 渲染测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
