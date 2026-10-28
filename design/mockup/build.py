# -*- coding: utf-8 -*-
"""设计稿生成器（唯一真源）。

改设计 -> 改这个文件 -> 跑 `python design/mockup/build.py` -> 跑 shoot.py 出图。
所有界面都由这里拼出来，避免手改 HTML 改出结构错乱。
"""
import pathlib

D = pathlib.Path(__file__).resolve().parent

# ---------- 状态栏 ----------
SB = """<div class="statusbar"><span>9:41</span>
<span class="sb-right">
<svg width="17" height="11" viewBox="0 0 17 11" fill="currentColor"><rect x="0" y="7" width="3" height="4" rx="1"/><rect x="4.5" y="5" width="3" height="6" rx="1"/><rect x="9" y="2.5" width="3" height="8.5" rx="1"/><rect x="13.5" y="0" width="3" height="11" rx="1"/></svg>
<svg width="16" height="12" viewBox="0 0 16 12" fill="currentColor"><path d="M8 10.5 6.2 8.6a2.6 2.6 0 0 1 3.6 0L8 10.5zM8 6.6c-1.3 0-2.5.5-3.4 1.4L3.2 6.6A6.4 6.4 0 0 1 8 4.4c1.7 0 3.3.7 4.5 1.8l-1.4 1.4A4.5 4.5 0 0 0 8 6.6zm0-4.4a10 10 0 0 0-7 2.8L-.2 3.6A12 12 0 0 1 8 .2c3.2 0 6.2 1.2 8.4 3.4L15 5A10 10 0 0 0 8 2.2z"/></svg>
<svg width="25" height="12" viewBox="0 0 25 12" fill="none"><rect x=".5" y=".5" width="21" height="11" rx="3" stroke="currentColor" opacity=".45"/><rect x="2" y="2" width="16" height="8" rx="1.8" fill="currentColor"/><path d="M23 4v4a2.2 2.2 0 0 0 0-4z" fill="currentColor" opacity=".45"/></svg>
</span></div>"""

# ---------- tab 图标 ----------
TAB_ICONS = {
 "msg":  '<path d="M12 3.2c-5 0-9 3.4-9 7.6 0 2.4 1.3 4.5 3.4 5.9l-.9 3.6 3.9-2.1c.8.2 1.7.3 2.6.3 5 0 9-3.4 9-7.7s-4-7.6-9-7.6z"/>',
 "book": '<path d="M4 3.6h6.2c1 0 1.8.8 1.8 1.8v13c0-.9-.7-1.6-1.6-1.6H4V3.6zm16 0h-6.2c-1 0-1.8.8-1.8 1.8v13c0-.9.7-1.6 1.6-1.6H20V3.6z"/>',
 "me":  '<path d="M12 12.2c-4.6 0-8 2.4-8 5.4 0 .5.4.9.9.9h14.2c.5 0 .9-.4.9-.9 0-3-3.4-5.4-8-5.4zM12 11c2.3 0 4.2-1.7 4.2-3.8S14.3 3.4 12 3.4 7.8 5.1 7.8 7.2 9.7 11 12 11z"/>',
}
TAB_NAMES = [("msg", "消息"), ("book", "通讯录"), ("me", "我")]

def tabs(active):
    out = ['<nav class="tabbar">']
    for k, lb in TAB_NAMES:
        out.append(f'<div class="tab{" active" if k == active else ""}">'
                   f'<svg viewBox="0 0 24 24" fill="currentColor">{TAB_ICONS[k]}</svg>'
                   f'<span>{lb}</span></div>')
    out.append("</nav>")
    return "".join(out)

SEARCH = ('<div class="searchbar"><svg viewBox="0 0 24 24" fill="currentColor">'
          '<path d="M15.5 14h-.79l-.28-.27a6.5 6.5 0 1 0-.7.7l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0A4.5 4.5 0 1 1 14 9.5 4.5 4.5 0 0 1 9.5 14z"/>'
          '</svg>搜索</div>')

def cell(ch, cls, name, sub, time="", badge="", pin=False):
    pin_tag = '<span class="pin">置顶</span>' if pin else ""
    badge_html = f'<span class="badge">{badge}</span>' if badge else '<span style="height:18px"></span>'
    return (f'<div class="cell"><div class="avatar {cls}">{ch}</div>'
            f'<div class="cell-body"><div class="cell-title">{name}{pin_tag}</div>'
            f'<div class="cell-sub">{sub}</div></div>'
            f'<div class="cell-meta"><span class="cell-time">{time}</span>{badge_html}</div></div>')

# ---------- 1. 消息 ----------
S1 = (f'<div class="nav"><div class="nav-title">消息</div><div class="spacer"></div>'
      f'<button class="nav-btn">+</button></div>{SEARCH}<div class="list">'
      + cell("柳", "", "柳如烟", "刚在回味你那句“好的呢”，现在正窝在你怀里～", "刚刚", "2", True)
      + cell("小", "a2", "小满", "你今天吃饭了吗？我给你留了排骨汤。", "昨天 21:30")
      + cell("阿", "a3", "阿澈", "我把项目部署好了，你看看有没有问题", "昨天 14:12")
      + cell("姜", "a4", "姜黎", "周末那个展你去不去？票我买好了", "周三")
      + "</div>" + tabs("msg"))

# ---------- 2. 通讯录 ----------
def ccell(ch, cls, name, sub):
    return (f'<div class="cell"><div class="avatar {cls}">{ch}</div>'
            f'<div class="cell-body"><div class="cell-title">{name}</div>'
            f'<div class="cell-sub">{sub}</div></div></div>')
S2 = (f'<div class="nav"><div class="nav-title">通讯录</div></div>{SEARCH}<div class="list">'
      f'<div class="group-label">角色</div>'
      + ccell("柳", "", "柳如烟", "温柔、黏人、宠溺的女朋友")
      + ccell("小", "a2", "小满", "元气满满的同桌")
      + ccell("阿", "a3", "阿澈", "话不多但很靠谱的学长")
      + ccell("姜", "a4", "姜黎", "有点毒舌的闺蜜")
      + f'<div class="group-label">功能</div>'
      + ccell("+", "add", "新建角色", "创建一个新的 AI 伴侣")
      + ccell("群", "add", "群聊", "多个角色在同一个房间里聊天")
      + "</div>" + tabs("book"))

# ---------- 3. 记忆 ----------
NOTES = [("用户偏好", "0.97", "用户希望助手以伴侣而非 AI 话术助手的身份陪伴和回应自己。", 97),
         ("里程碑", "0.96", "用户于 2026 年 10 月 23 日收到了广联达的录用意向。", 96),
         ("画像", "0.95", "用户名叫周艺科，正在求职并参加面试。", 95),
         ("事件", "0.88", "用户昨天提到那家牛骨汤面馆在地铁口左手边。", 88)]
note_html = "".join(
    f'<div class="note"><div class="note-top"><span class="note-type">{t}</span>'
    f'<span class="note-imp">重要度 {imp}</span></div>'
    f'<div class="note-text">{txt}</div>'
    f'<div class="note-bar"><i style="width:{w}%"></i></div></div>'
    for t, imp, txt, w in NOTES)
S3 = ('<div class="nav"><span class="back">‹</span>'
      '<div class="nav-center nav-title">柳如烟的记忆</div><div class="spacer"></div>'
      '<button class="nav-btn">⌕</button></div><div class="scroll">'
      f'<div class="stats"><div><div class="stat-num">42</div><div class="stat-label">条记忆</div></div>'
      f'<div><div class="stat-num">30</div><div class="stat-label">天认识</div></div>'
      f'<div><div class="stat-num">12</div><div class="stat-label">段对话</div></div></div>'
      f'<div class="chips"><span class="chip active">全部</span><span class="chip">画像</span>'
      f'<span class="chip">里程碑</span><span class="chip">偏好</span><span class="chip">事件</span></div>'
      f'{note_html}</div>')

# ---------- 4. 我 ----------
S4 = ('<div class="nav"><div class="nav-title">我</div></div><div class="scroll">'
      '<div class="me-card"><div class="avatar a4">周</div><div>'
      '<div class="me-name">周艺科</div>'
      '<div class="me-sub">4 个角色 · 共 128 条记忆 · 37 段对话</div></div></div>'
      '<div class="group" style="margin-top:12px">'
      '<div class="row"><span>显示思考过程</span><span class="switch on"><i></i></span></div>'
      '<div class="row"><span>自动记录间隔</span><span class="row-val">每 2 小时 <span class="chev">›</span></span></div>'
      '<div class="row"><span>服务器地址</span><span class="row-val">192.168.137.1:8000 <span class="chev">›</span></span></div>'
      '</div><div class="group">'
      '<div class="row"><span>关于</span><span class="row-val">v0.3.0 <span class="chev">›</span></span></div>'
      '</div></div>' + tabs("me"))
# ---------- 5. 会话 ----------
def bubble(side, text, time, think=None):
    cls = "me" if side == "me" else "them"
    th = ""
    if think:
        th = ('<details class="think"><summary>她当时在想什么</summary>'
              f'<div class="think-body">{think}</div></details>')
    return (f'<div class="msg {cls}"><div class="body">{text}</div>{th}'
            f'<span class="stamp">{time}</span></div>')
S5 = ('<div class="nav"><span class="back">‹</span>'
      '<div class="nav-center"><span class="nav-title">柳如烟</span><span class="nav-sub">在线</span></div>'
      '<div class="spacer"></div>'
      '<div class="avatar" style="width:32px;height:32px;border-radius:9px;font-size:14px">柳</div></div>'
      '<div class="chat">'
      + bubble("them", "早呀～今天醒得挺早的嘛 ☀️", "09:12")
      + bubble("me", "被你猜中了，刚醒", "09:14")
      + bubble("them", "那我去给你热杯牛奶，等你哦～", "09:15",
               "We need answer in Chinese, persona as girlfriend... 用户刚醒，语气应该是慵懒但亲昵的。")
      + bubble("me", "等着我，马上来", "09:16")
      + bubble("them", "好呀～不过你要是又赖床，我可就要<b>生气</b>了哦 😤", "09:16")
      + '</div><div class="composer"><span class="jump">⌄ 2 条新消息</span>'
        '<div class="composer-row"><div id="input">说点什么…</div>'
        '<button class="send"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M3.4 20.4 21 12 3.4 3.6 3.4 10.2 15 12 3.4 13.8z"/></svg></button></div></div>')

# ---------- 6. 角色资料 ----------
S6 = ('<div class="nav"><span class="back">‹</span><div class="nav-center nav-title">资料</div></div>'
      '<div class="scroll"><div class="profile-hero">'
      '<div class="avatar xl">柳</div><div class="profile-name">柳如烟</div>'
      '<div class="profile-sub">温柔、黏人、宠溺的女朋友</div></div>'
      '<div class="stats" style="margin:0 14px 12px">'
      '<div><div class="stat-num">30</div><div class="stat-label">天认识</div></div>'
      '<div><div class="stat-num">42</div><div class="stat-label">条记忆</div></div>'
      '<div><div class="stat-num">12</div><div class="stat-label">段对话</div></div></div>'
      '<div class="group">'
      '<div class="row"><span>她的记忆</span><span class="row-val">42 条 <span class="chev">›</span></span></div>'
      '<div class="row"><span>聊天记录</span><span class="row-val">按日期回溯 <span class="chev">›</span></span></div>'
      '<div class="row"><span>关系状态</span><span class="row-val">恋人 <span class="chev">›</span></span></div>'
      '</div><div class="group" style="margin-top:12px">'
      '<div class="row"><span>上次记录</span><span class="row-val">3 天前</span></div>'
      '<div class="row"><span>手动记一次</span>'
      '<span class="row-val"><span class="btn-mini">记一次</span></span></div>'
      '</div>'
      '<div class="hint">对话每 2 小时自动记录一次，记录后她会记住聊过的事。<br/>想现在记也可以手动触发。</div>'
      '</div>')

SCREENS = [("01-messages", "消息", S1), ("02-contacts", "通讯录", S2),
           ("03-memory", "她的记忆", S3), ("04-me", "我", S4),
           ("05-conversation", "会话", S5), ("06-profile", "角色资料", S6)]

def write(stem, title, body, single=True):
    style = ("<style>body{padding:0;gap:0;background:#14100f}.phone{box-shadow:none;border-radius:0}</style>"
             if single else "<style>body{padding:30px 26px;align-items:flex-start}</style>")
    html = (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f'<title>{title}</title><link rel="stylesheet" href="base.css">{style}</head>\n'
            f'<body><div class="phone"><div class="notch"></div>{SB}{body}</div></body></html>')
    (D / f"{stem}.html").write_text(html, encoding="utf-8")

for stem, title, body in SCREENS:
    write(stem, title, body)
    print("[OK]", stem)

# 总览：从单屏 body 拼，保证同步
phones = "".join(f'<div class="phone"><div class="notch"></div>{SB}{b}</div>' for _, _, b in SCREENS)
(D / "00-overview.html").write_text(
    f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">\n'
    f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
    f'<title>Companion Ever Memory · App 设计稿</title><link rel="stylesheet" href="base.css">\n'
    f'<style>body{{padding:30px 26px;align-items:flex-start}}</style></head>\n'
    f'<body>{phones}</body></html>', encoding="utf-8")
print("[OK] 00-overview.html")