"""application/setting_service.py — 用户设置的用例封装。

目前只有一个设置：**show_thinking**（界面上是否展示模型的思考过程）。

设计要点：
- **数据永远两边都存**（conversation_log 干净正文 + thinking_log 思考过程）
  设置只决定"显示哪个"，不影响存储 —— 这样用户随时能切换，历史不会丢。
- 设置是 **per (user, persona)** 的：同一个用户对不同角色可以有不同偏好。
"""
from __future__ import annotations

from typing import Any

from companion_ever.application.authz import Authorizer
from companion_ever.infrastructure.persistence.setting_repo import SettingRepo


class SettingService:
    """用户设置的用例封装。"""

    def __init__(self, authorizer: Authorizer, setting_repo: SettingRepo):
        self.authorizer = authorizer
        self.setting_repo = setting_repo

    def get_settings(self, user_id: str, persona_id: str) -> dict[str, Any]:
        """读该 (user, persona) 的全部设置。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.setting_repo.get(user_id=user_id, persona_id=persona_id)

    def get_show_thinking(self, user_id: str, persona_id: str) -> bool:
        """是否展示思考过程（默认 True —— 多数人觉得看思考过程挺有意思）。"""
        return bool(self.get_settings(user_id, persona_id)["show_thinking"])

    def set_show_thinking(
        self, user_id: str, persona_id: str, show: bool,
    ) -> dict[str, Any]:
        """设置是否展示思考过程。"""
        self.authorizer.require_persona(user_id, persona_id)
        return self.setting_repo.set_show_thinking(
            user_id=user_id, persona_id=persona_id, show=show,
        )


__all__ = ["SettingService"]
