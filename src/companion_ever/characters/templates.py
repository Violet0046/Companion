"""characters/templates.py — 内置人设模板（出厂预设）。

设计原则：
- 模板是"出厂默认人设"——给用户开箱即用的选择
- 模板内容用 dict 表示（结构化）
- 实际写入 DB 时序列化为 JSON 字符串（persona_definition.content）
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

# 表单字段描述：前端按这个渲染「自定义角色」表单。
# 与 PERSONA_TEMPLATES 的结构同源——type=text 是字符串，type=list 是字符串数组。
TEMPLATE_FIELDS: list[dict] = [
    {"key": "name", "label": "名字", "type": "text", "required": True},
    {"key": "identity", "label": "一句话身份", "type": "text", "required": True},
    {"key": "personality", "label": "性格", "type": "list"},
    {"key": "background", "label": "背景", "type": "text"},
    {"key": "style", "label": "说话风格", "type": "text"},
    {"key": "relationship", "label": "与用户的关系", "type": "text"},
    {"key": "values", "label": "价值观", "type": "list"},
    {"key": "avoid", "label": "不会做的事", "type": "list"},
    {"key": "habits", "label": "习惯", "type": "list"},
]

# 模板里写死的用户名占位符 → 实际 user_id。
# "周艺科(用户)" 保留"(用户)"后缀，读起来仍是"某某（作为用户）"。
_USER_PLACEHOLDERS = (("周艺科(用户)", "{user}(用户)"), ("周艺科", "{user}"))


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


def list_templates() -> list[str]:
    """所有模板 ID。"""
    return list(PERSONA_TEMPLATES)


def _substitute_user(fields: dict, user_id: str) -> dict:
    """把模板里写死的"周艺科"等用户名占位符替换成实际 user_id。

    递归扫描所有字符串字段。"我"/"自己"属于角色视角，不需要替换。
    返回新 dict，不修改原模板。
    """
    def _walk(obj):
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        if isinstance(obj, str):
            for old, new in _USER_PLACEHOLDERS:
                obj = obj.replace(old, new.format(user=user_id))
            return obj
        return obj

    return _walk(fields)


def build_definition(name: str, user_id: str) -> str:
    """用模板生成角色的 JSON 设定（用户占位符已替换成实际 user_id）。

    Args:
        name: 模板 ID
        user_id: 实际用户 ID

    Returns:
        JSON 字符串，写入 persona_definition.content

    Raises:
        KeyError: 模板不存在
    """
    fields = _substitute_user(get_template(name), user_id)
    return json.dumps(fields, ensure_ascii=False, indent=2)


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


def template_cards() -> list[dict]:
    """给前端的模板卡片数据。

    fields 是完整的 persona_basics——前端选模板后要拿它预填表单。
    """
    cards = []
    for template_id, fields in PERSONA_TEMPLATES.items():
        cards.append({
            "id": template_id,
            "name": fields.get("name", template_id),
            "identity": fields.get("identity", ""),
            "fields": dict(fields),
        })
    return cards


__all__ = [
    "PERSONA_TEMPLATES",
    "TEMPLATE_FIELDS",
    "get_template",
    "list_templates",
    "build_definition",
    "template_cards",
    "template_to_json",
]