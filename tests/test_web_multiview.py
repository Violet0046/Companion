"""多视图结构回归测试（消息 / 通讯录 / 我 / 会话 / 角色资料 / TA 的记忆）

覆盖页面导航及容易改回去的交互约束：

1. 底部只有 3 个 Tab（消息 / 通讯录 / 我）—— 没有「记忆」Tab
   因为不同 AI 伴侣的记忆是不同的，记忆必须挂在角色资料页下面。
2. 「我」页文案保持多伴侣中立，不能出现"和某某在一起 N 天"。
3. 角色资料页没有「结束这次对话」按钮，只有「上次记录」+「手动记一次」。
4. 后端要提供 last_record_at（上次记录时间）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def main() -> int:
    from companion_ever.entrypoints.web.server import WEB_DIR

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # --- 1. 六个视图都在 ---
    for vid in ("view-messages", "view-contacts", "view-me",
                "view-chat", "view-profile", "view-memory"):
        assert f'id="{vid}"' in html, f"缺少视图 {vid}"
    print("[OK] 六个视图都在 HTML 里")

    # --- 2. 底部 Tab 只有 3 个，且没有「记忆」 ---
    tabs = html.split('<button class="tab')[1:]
    assert len(tabs) == 3, f"底部 Tab 应该是 3 个，实际 {len(tabs)}"
    labels = []
    for tb in tabs:
        seg = tb.split("</button>")[0]
        labels.append(seg.split("<span>")[-1].split("</span>")[0])
    assert labels == ["消息", "通讯录", "我"], f"Tab 顺序/文案不对: {labels}"
    assert "记忆" not in labels, "记忆不该是全局 Tab（各角色记忆是分开的）"
    print("[OK] 底部 Tab = 消息 / 通讯录 / 我，没有记忆 Tab")

    # --- 3. 「我」页不能绑定单个角色 ---
    me_seg = html.split('id="view-me"')[1].split('id="view-chat"')[0]
    for bad in ("在一起", "已经在一起"):
        assert bad not in me_seg, f"「我」页出现了单伴侣文案: {bad}"
    assert "显示思考过程" in me_seg, "开关文案应该是中立的「显示思考过程」"
    assert "自动记录" in me_seg, "缺少「自动记录」状态"
    print("[OK] 「我」页文案多伴侣中立")

    # --- 4. 角色资料页：没有「结束这次对话」，改成「上次记录 + 手动记一次」 ---
    assert "结束这次对话" not in js, "角色资料页不该再有「结束这次对话」"
    assert "上次记录" in js, "缺少「上次记录」"
    assert "手动记一次" in js, "缺少「手动记一次」"
    assert "btn-record-now" in js, "缺少手动记录的按钮"
    assert "结束会话" not in js, "「结束会话」这个说法要去掉"
    print("[OK] 角色资料页用「上次记录 / 手动记一次」代替结束会话")

    # --- 5. 通讯录是干净的联系人列表，不显示最后一条消息 ---
    import re as _re
    tag_fn = _re.search(r"function personaTag\(p\) \{(.*?)\n\}", js, _re.S)
    assert tag_fn, "缺少 personaTag"
    body = tag_fn.group(1)
    assert "c.last" not in body and "S.convs" not in body, (
        "通讯录不该拿最后一条消息当副标题（那是「消息」页的事）"
    )
    # 消息页才显示最后一条
    assert "last.content" in js, "消息页应该显示最后一条消息"
    print("[OK] 通讯录 / 消息页 的副标题逻辑分开了")

    # --- 6. 记忆是角色资料页的子页面（标题带角色名） ---
    assert "memory-title" in html, "记忆页没有标题"
    assert "的记忆" in js, "记忆页标题应该带角色名"
    assert "openMemory" in js, "缺少进入记忆页的入口"
    print("[OK] 记忆是角色的子页面")

    # --- 6. 抽屉拆掉了 ---
    assert 'id="drawer"' not in html, "抽屉还在（内容已拆到各个页面）"
    assert "openDrawer" not in js, "还留着抽屉的代码"
    print("[OK] 旧抽屉已移除")

    # --- 7. 路由 ---
    for fn in ("showView", "openChat", "openProfile", "openMemory",
               "renderConvs", "renderContacts", "renderMe", "switchTarget"):
        assert f"function {fn}" in js, f"缺少函数 {fn}"
    print("[OK] 页面路由和各页渲染函数都在")

    # --- 8. 后端：上次记录时间 ---
    from companion_ever.entrypoints.web.schemas import PersonaSummary
    assert "last_record_at" in PersonaSummary.model_fields, (
        "PersonaSummary 缺 last_record_at（角色资料页要显示「上次记录」）"
    )
    print("[OK] 后端提供 last_record_at")

    print("[ALL] 多视图结构测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
