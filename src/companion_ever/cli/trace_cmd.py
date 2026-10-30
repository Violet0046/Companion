"""cli/trace_cmd.py — trace 查看命令。

子命令：
- trace list    列出最近的 trace（看链路概览、耗时、token）
- trace show    看某一轮的完整链路 + 产物
- trace prune   清理 N 天前的 trace

这些命令只读文件，不需要连数据库，也不需要 API Key。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from companion_ever.trace import TraceStore, default_trace_dir


def _store(args) -> TraceStore:
    if getattr(args,'trace_dir',None):
        return TraceStore(args.trace_dir)
    db_path = getattr(args, "db", "./memory/companion.db")
    return TraceStore(default_trace_dir(db_path))


def _fmt_detail(detail: dict[str, Any]) -> str:
    """把 detail 压成一行 key=value。"""
    parts: list[str] = []
    for k, v in detail.items():
        if k in ("notes", "violations"):
            continue
        if isinstance(v, str) and len(v) > 40:
            v = v[:40] + "…"
        parts.append(f"{k}={v}")
    return "  ".join(parts)


def cmd_trace_list(args) -> int:
    """trace list：列出最近的 trace。"""
    store = _store(args)
    if not store.base_dir.is_dir():
        print(f"[!] 还没有任何 trace：{store.base_dir}")
        print("    先跑一次 companion-cli chat 或打开 Web 端聊一句。")
        return 0

    rows = store.list_traces(
        user_id=args.user,
        persona_id=args.persona,
        kind=args.kind,
        limit=args.last,
    )
    if not rows:
        print("[!] 没有匹配的 trace")
        return 0

    print()
    print(f"最近 {len(rows)} 条 trace（{store.base_dir}）")
    print()
    print(f"{'时间':<20}{'类型':<11}{'状态':<8}{'耗时':>9}  {'输入字符':>9}  trace_id")
    print("-" * 78)
    for _path, d in rows:
        started = str(d.get("started_at", ""))[:19].replace("T", " ")
        status = d.get("status", "?")
        if status == "error":
            status = "ERROR"
        dur = d.get("duration_ms", 0)
        total = _total_chars(d)
        print(
            f"{started:<20}{d.get('kind','?'):<11}{status:<8}"
            f"{dur:>8.0f}ms  {total:>9,}  {d.get('trace_id','?')}"
        )
    print()
    print("看某一条：companion-cli trace show <trace_id>")
    return 0


def _total_chars(d: dict[str, Any]) -> int:
    """从 steps 里找出 prompt 总字符数。"""
    for s in d.get("steps", []):
        if s.get("name") == "prompt.build":
            return int(s.get("detail", {}).get("total_chars", 0))
    return sum(int(s.get('detail',{}).get('request_chars',0)) for s in d.get('steps',[])
               if s.get('name') in ('llm.chat','llm.stream'))


def cmd_trace_show(args) -> int:
    """trace show：看某一轮的链路 + 产物。"""
    store = _store(args)
    found = store.find(args.trace_id)
    if found is None:
        print(f"[!] 找不到 trace: {args.trace_id}")
        return 1

    path, d = found
    tid = d.get("trace_id", "?")

    print()
    print(f"=== trace {tid} ===")
    print(f"  类型      : {d.get('kind')}")
    print(f"  用户/角色 : {d.get('user_id')} / {d.get('persona_id')}")
    print(f"  开始      : {str(d.get('started_at'))[:19].replace('T',' ')}")
    print(f"  耗时      : {d.get('duration_ms', 0):.0f} ms")
    print(f"  状态      : {d.get('status')}")
    if d.get("error"):
        print(f"  错误      : {d['error']}")
    for key,value in (d.get('context') or {}).items():
        if value is not None:
            print(f'  {key}: {value}')
    children=[item for _,item in store.iter_meta()
              if item.get('context',{}).get('parent_trace_id')==tid]
    if children:
        print('\n--- 执行记录 ---')
        for child in children:
            print(f"  {child['trace_id']}  {child['kind']}  {child['status']}")
    if d.get('llm_calls'):
        print('\n--- 模型调用（每次纠错独立保留）---')
        for call in d['llm_calls']:
            print(f"  {call['id']}  {call['status']}  response={call.get('response_state','?')}  "
                  f"{call.get('elapsed_ms',0):.0f}ms  {call.get('response_chars',0)} 字符")

    steps = d.get("steps", [])
    if steps:
        print()
        print("--- 链路 ---")
        for i, s in enumerate(steps, 1):
            ms = s.get("ms", 0)
            print(f"  {i:>2}. {s.get('name'):<26} {ms:>7.0f} ms   {_fmt_detail(s.get('detail', {}))}")

    # 不变量违规单独标出来
    for s in steps:
        if s.get("name") == "invariants.check":
            viol = s.get("detail", {}).get("violations") or []
            if viol:
                print()
                print("--- ⚠ 不变量违规 ---")
                for v in viol:
                    print(f"  [{v.get('rule')}] {v.get('message')}")

    # 产物
    arts = d.get("artifacts") or []
    if arts:
        print()
        print("--- 产物（模型实际看到的 / 实际返回的）---")
        for a in arts:
            ap = store.artifact_path(path, a)
            exists = "✓" if ap and ap.exists() else "✗"
            size = f"{ap.stat().st_size:,} 字节" if ap and ap.exists() else "缺失"
            print(f"  {exists} {a:<10} {size}")
            if ap:
                print(f'      {ap.resolve()}')
            if a == "request":
                print(f"      → companion-cli trace show {tid} --request")
            elif a == "response":
                print(f"      → companion-cli trace show {tid} --response")

    print()
    return 0


def cmd_trace_show_raw(args) -> int:
    """trace show --request/--response：直接打印产物内容。"""
    store = _store(args)
    found = store.find(args.trace_id)
    if found is None:
        print(f"[!] 找不到 trace: {args.trace_id}")
        return 1

    path, d = found
    name = getattr(args,'artifact',None) or ("request" if args.request else "response")
    if name not in (d.get('artifacts') or []):
        print(f'[!] 该 trace 没有 {name} 产物')
        return 1
    ap = store.artifact_path(path, name)
    if ap is None or not ap.exists():
        print(f"[!] 该 trace 没有 {name} 产物")
        return 1

    content = ap.read_text(encoding="utf-8")
    if args.max_chars and len(content) > args.max_chars:
        content = content[: args.max_chars] + f"\n\n... (截断，全文 {len(content):,} 字符)"
    print(content)
    return 0


def cmd_trace_prune(args) -> int:
    """trace prune：清理 N 天前的 trace。"""
    store = _store(args)
    removed = store.prune(keep_days=args.days)
    print(f"[OK] 清理了 {removed} 个文件（保留最近 {args.days} 天）")
    return 0


__all__ = [
    "cmd_trace_list",
    "cmd_trace_show",
    "cmd_trace_show_raw",
    "cmd_trace_prune",
]
