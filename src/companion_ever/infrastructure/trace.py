"""infrastructure/trace.py — 薄版 trace（可观测性）。

回答一个问题：**这一轮模型究竟看到了什么、为什么这么答、花了多少。**

设计取舍：
1. **文件存储，不动 DB schema** —— trace 是诊断数据，业务数据在 SQLite，边界清晰
2. **产物在发送前捕获** —— 存的是实际发给 LLM 的 payload，不是事后重建
   （重建会因为 think 剥离、时间戳转换等处理而失真，那就失去意义了）
3. **零侵入** —— thread-local 传递当前 trace，业务代码签名不用改
4. **默认开启** —— COMPANION_TRACE=0 关闭。调试期开着最省事

存储布局：
    memory/traces/2026-10-27/<trace_id>.json
    memory/traces/2026-10-27/<trace_id>.request.txt
    memory/traces/2026-10-27/<trace_id>.response.txt

典型用法：
    with trace_scope("chat", user_id, persona_id) as tr:
        tr.step("prompt.build", ms=12, total_chars=20834)
        tr.artifact("request", payload)
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional


# ============ 全局开关 ============

def trace_enabled() -> bool:
    """trace 是否开启。COMPANION_TRACE=0 关闭（默认开启）。"""
    return os.environ.get("COMPANION_TRACE", "1").strip().lower() not in ("0", "false", "off", "no")


def default_trace_dir(db_path: Path | str) -> Path:
    """trace 目录：默认放在 DB 同级的 traces/ 下（DB 换位置，trace 跟着走）。"""
    return Path(db_path).parent / "traces"


# ============ 数据模型 ============

@dataclass
class TraceStep:
    """链路里的一步。"""

    name: str
    ms: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "ms": round(self.ms, 2)}
        if self.detail:
            d["detail"] = self.detail
        return d


class Trace:
    """一次调用链的完整记录。"""

    def __init__(
        self,
        kind: str,
        user_id: str,
        persona_id: str,
        trace_id: str | None = None,
    ) -> None:
        self.trace_id = trace_id or _new_trace_id()
        self.kind = kind
        self.user_id = user_id
        self.persona_id = persona_id
        self.started_at = datetime.now().astimezone()
        self.steps: list[TraceStep] = []
        self.artifacts: dict[str, str] = {}
        self.status: str = "running"
        self.error: str | None = None
        self.duration_ms: float = 0.0
        self._t0 = time.perf_counter()

    # --- 记录 ---

    def step(self, name: str, ms: float = 0.0, **detail: Any) -> None:
        """记录一个步骤。detail 是关键字参数，会被原样存进 JSON。"""
        self.steps.append(TraceStep(name=name, ms=ms, detail=detail))

    def artifact(self, name: str, content: str) -> None:
        """记录一个产物（实际发送/收到的内容）。"""
        self.artifacts[name] = content

    def finish(self, status: str = "ok", error: str | None = None) -> None:
        """结束 trace（幂等）。"""
        self.duration_ms = (time.perf_counter() - self._t0) * 1000
        self.status = status
        self.error = error

    # --- 序列化 ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "kind": self.kind,
            "user_id": self.user_id,
            "persona_id": self.persona_id,
            "started_at": self.started_at.isoformat(),
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "error": self.error,
            "steps": [s.to_dict() for s in self.steps],
            "artifacts": sorted(self.artifacts.keys()),
        }


def _new_trace_id() -> str:
    """trace_id 格式：20261027-143022-a3f9（时间 + 随机后缀，便于按时间排序）。"""
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


# ============ thread-local 上下文 ============

_local = threading.local()


def current_trace() -> Optional[Trace]:
    """当前线程正在进行的 trace（没有则 None）。"""
    return getattr(_local, "trace", None)


def record_step(name: str, ms: float = 0.0, **detail: Any) -> None:
    """给当前 trace 记一步（无 trace 时静默跳过）。"""
    tr = current_trace()
    if tr is not None:
        tr.step(name, ms=ms, **detail)


def record_artifact(name: str, content: str) -> None:
    """给当前 trace 记一个产物（无 trace 时静默跳过）。"""
    tr = current_trace()
    if tr is not None:
        tr.artifact(name, content)


# ============ 默认 store ============

_default_store: "TraceStore | None" = None


def set_default_store(store: "TraceStore | None") -> None:
    """设置进程级默认 store（build_app 时调用一次）。"""
    global _default_store
    _default_store = store


def get_default_store() -> "TraceStore | None":
    """取默认 store。"""
    return _default_store


# ============ trace 作用域 ============

@contextmanager
def trace_scope(
    kind: str,
    user_id: str,
    persona_id: str,
    store: "TraceStore | None" = None,
) -> Iterator[Trace]:
    """开启一个 trace 作用域。

    退出时自动落盘。异常时 status=error 并记录错误信息。

    用法：
        with trace_scope("chat", user_id, persona_id) as tr:
            ...
            tr.step("xxx", ms=1)
    """
    if not trace_enabled():
        # 关闭时给一个"哑" trace，调用方不用改代码
        yield _NullTrace()
        return

    tr = Trace(kind=kind, user_id=user_id, persona_id=persona_id)
    if store is None:
        store = get_default_store()
    prev = getattr(_local, "trace", None)
    _local.trace = tr
    try:
        yield tr
    except GeneratorExit:
        tr.finish("aborted")
        raise
    except Exception as e:
        tr.finish("error", f"{type(e).__name__}: {e}")
        raise
    else:
        tr.finish("ok")
    finally:
        _local.trace = prev
        if store is not None:
            store.save(tr)


class _NullTrace:
    """trace 关闭时的哑对象，方法都是 no-op。"""

    trace_id = "-"
    steps: list = []
    artifacts: dict = {}

    def step(self, *a, **k) -> None:
        return None

    def artifact(self, *a, **k) -> None:
        return None

    def finish(self, *a, **k) -> None:
        return None


# ============ 存储 ============

class TraceStore:
    """文件存储。目录结构：<base>/YYYY-MM-DD/<trace_id>.{json,request.txt,response.txt}"""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    # --- 写 ---

    def save(self, trace: Trace) -> Path | None:
        """落盘。失败不抛异常（诊断功能不该拖垮主流程）。"""
        try:
            day_dir = self.base_dir / trace.started_at.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)

            meta_path = day_dir / f"{trace.trace_id}.json"
            meta_path.write_text(
                json.dumps(trace.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            for name, content in trace.artifacts.items():
                (day_dir / f"{trace.trace_id}.{name}.txt").write_text(
                    content, encoding="utf-8"
                )
            return meta_path
        except Exception:
            return None

    # --- 读 ---

    def iter_meta(self) -> Iterator[tuple[Path, dict[str, Any]]]:
        """遍历所有 trace 元数据（按文件修改时间倒序 = 最新在前）。"""
        if not self.base_dir.is_dir():
            return
        files = sorted(
            self.base_dir.glob("*/*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            yield f, data

    def list_traces(
        self,
        user_id: str | None = None,
        persona_id: str | None = None,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[tuple[Path, dict[str, Any]]]:
        """列出 trace（过滤 user / persona / kind）。"""
        out: list[tuple[Path, dict[str, Any]]] = []
        for path, data in self.iter_meta():
            if user_id and data.get("user_id") != user_id:
                continue
            if persona_id and data.get("persona_id") != persona_id:
                continue
            if kind and data.get("kind") != kind:
                continue
            out.append((path, data))
            if len(out) >= limit:
                break
        return out

    def find(self, trace_id: str) -> tuple[Path, dict[str, Any]] | None:
        """按 trace_id 查找（支持只给前缀）。"""
        for path, data in self.iter_meta():
            if data.get("trace_id") == trace_id:
                return path, data
            if data.get("trace_id", "").startswith(trace_id):
                return path, data
        return None

    def artifact_path(self, meta_path: Path, name: str) -> Path | None:
        """找某个产物的文件路径。"""
        candidate = meta_path.with_suffix(f".{name}.txt")
        if candidate.exists():
            return candidate
        # trace_id 里带点号的兜底
        stem = meta_path.stem
        candidate = meta_path.parent / f"{stem}.{name}.txt"
        return candidate if candidate.exists() else None

    def prune(self, keep_days: int = 7) -> int:
        """删除 N 天前的 trace。返回删除的文件数。"""
        if not self.base_dir.is_dir():
            return 0
        cutoff = time.time() - keep_days * 86400
        removed = 0
        for f in self.base_dir.glob("*/*"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
        # 清理空目录
        for d in self.base_dir.glob("*"):
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass
        return removed


__all__ = [
    "Trace",
    "TraceStep",
    "TraceStore",
    "trace_scope",
    "current_trace",
    "record_step",
    "record_artifact",
    "trace_enabled",
    "default_trace_dir",
    "set_default_store",
    "get_default_store",
]
