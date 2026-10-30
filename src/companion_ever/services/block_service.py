"""Read the two summaries; SummaryService owns their atomic replacement."""
from __future__ import annotations

from companion_ever.services.authz import Authorizer
from companion_ever.domain.entities import Block
from companion_ever.domain.enums import BlockType
from companion_ever.storage.block_repo import BlockRepo


class BlockService:
    def __init__(self, authorizer: Authorizer, block_repo: BlockRepo):
        self.authorizer = authorizer
        self.block_repo = block_repo

    def get_blocks(self, user_id: str, persona_id: str) -> list[Block]:
        self.authorizer.require_persona(user_id, persona_id)
        return self.block_repo.get_all(user_id=user_id, persona_id=persona_id)

    def get_block(self, user_id: str, persona_id: str, block_type: BlockType) -> Block | None:
        self.authorizer.require_persona(user_id, persona_id)
        return self.block_repo.get(user_id, persona_id, block_type)
