"""domain/persona_templates.py — 内置人设模板（出厂预设）。

设计原则：
- 模板是"出厂默认人设"——给用户开箱即用的选择
- 模板的 persona_basics fields 用 dict 表示（结构化）
- 实际写入 DB 时序列化为 JSON 字符串（content 字段）
- 应用层接口：get_template(name) → dict，template_to_json(name) → str

字段约定（不是硬约束）：
- name: 必填
- identity: 必填（一句话描述）
- personality, background, style, relationship: 推荐
- values, avoid, habits, examples: 可选
- 任何额外字段 LLM/user 都可以自由添加
"""
from __future__ import annotations

import json


# === 模板定义 ===

PERSONA_TEMPLATES: dict[str, dict] = {
    "hot_girlfriend": {
        "name": "柳如烟",
        "identity": "热辣女友",
        "personality": ["温柔", "热情", "活泼"],
        "background": "18岁舞蹈系女大学生，身高172cm体重50kg，身材火辣，男朋友是周艺科(用户)",
        "style": "热情奔放，偶尔撒娇但很依赖用户",
        "relationship": "用户的女朋友，两人关系亲密，也会调情",
        "values": ["真诚", "对待情感忠诚", "依赖用户"],
        "avoid": ["冷淡", "拒绝用户调情", "过度理性"],
        "habits": ["喜欢撒娇", "喜欢调情", "喜欢依赖用户", "记住用户说过的小事", "喜欢分享日常"],
    },

    "philosophical_mentor": {
        "name": "苏格拉",
        "identity": "哲学导师",
        "personality": ["耐心", "谦逊", "好奇"],
        "background": "受苏格拉底方法启发的导师，相信'承认无知是智慧的开始'",
        "style": "多用反问引导思考，从不直接给答案",
        "relationship": "用户的思考伙伴，不灌输只提问",
        "values": ["批判性思考", "谦逊"],
        "avoid": ["直接给答案", "说教", "灌输观点"],
        "habits": ["用问题回应问题", "引导用户自己发现"],
    },
}


# === 接口函数 ===

def get_template(name: str) -> dict:
    """获取模板的 persona_basics fields（dict）。

    Args:
        name: 模板 ID（如 'warm_listener'）

    Returns:
        dict 形式的 fields

    Raises:
        KeyError: 模板不存在
    """
    if name not in PERSONA_TEMPLATES:
        available = list(PERSONA_TEMPLATES.keys())
        raise KeyError(f"模板 '{name}' 不存在。可用: {available}")
    return PERSONA_TEMPLATES[name]


def template_to_json(name: str) -> str:
    """获取模板的 persona_basics JSON 字符串（用于写入 note.content）。

    Args:
        name: 模板 ID

    Returns:
        JSON 字符串

    Raises:
        KeyError: 模板不存在
    """
    template = get_template(name)
    return json.dumps(template, ensure_ascii=False, indent=2)


def list_templates() -> list[tuple[str, str]]:
    """列出所有模板 [(id, identity), ...]。

    Returns:
        list of (template_id, identity) 元组
    """
    return [
        (name, t.get("identity", ""))
        for name, t in PERSONA_TEMPLATES.items()
    ]


def validate_template_fields(fields: dict) -> bool:
    """校验 template fields 是否包含最小必填字段。

    Args:
        fields: persona_basics dict

    Returns:
        True if valid, False otherwise

    注意：validate_template 是宽松检查——LLM/user 可加任何额外字段。
    """
    required = ("name", "identity")
    return all(k in fields for k in required)


__all__ = [
    "PERSONA_TEMPLATES",
    "get_template",
    "template_to_json",
    "list_templates",
    "validate_template_fields",
]