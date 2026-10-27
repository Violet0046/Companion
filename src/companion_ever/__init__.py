"""Companion Wiki - v2.0

基于 SQLite Wiki 的对话记忆系统（重构版）。

新架构：
- domain/        — 纯数据 / 类型 / 异常 / 模板
- infrastructure/ — 持久化 + AI 客户端
- application/   — 按子域划分的用例服务
- prompts/       — 外部化的 LLM 提示词
- utils/         — 横切工具（时间等）
- app.py         — 组合根（build_app）

使用：
    from companion_ever.config import AppConfig
    from companion_ever.app import build_app

    ctx = build_app(AppConfig.from_env())
    ctx.services.persona.create_persona("alice", "舒舒", "...")
    response = ctx.services.chat.chat("alice", "舒舒", "你好")
"""
from __future__ import annotations

__version__ = "0.2.0"