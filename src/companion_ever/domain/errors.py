"""domain/errors.py — 域异常。

策略：复用内建异常（PermissionError、ValueError），不引入新的异常类型。
这样可以保证现有调用方代码无需 try/except 改造。

PermissionError 用于：user 不存在 / 不拥有 persona / note 不属于 user。
ValueError 用于：业务输入不合法（content 空、importance 越界、persona 重复等）。
"""
from __future__ import annotations


# 仅作占位符；当前没有自定义异常类型需要导出。
__all__: list[str] = []