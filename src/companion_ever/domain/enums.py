"""Memory nature and relationship enums; persona configuration is not a memory kind."""
from enum import Enum

class MemoryKind(str, Enum):
    EPISODE = 'episode'
    FACT = 'fact'

class LinkType(str, Enum):
    SUPPORTS = 'supports'
    CONTINUES = 'continues'
    SUPERSEDES = 'supersedes'

class BlockType(str, Enum):
    """用户画像与关系进展摘要，未来从有效 fact 独立演化。"""

    USER_PROFILE = "user_profile"
    RELATIONSHIP_STATE = "relationship_state"


__all__ = ["MemoryKind", "LinkType", "BlockType"]
