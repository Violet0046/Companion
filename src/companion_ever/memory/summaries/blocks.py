"""Read the two summaries; SummaryService owns their atomic replacement."""
from __future__ import annotations

from companion_ever.characters.access import Authorizer
from companion_ever.domain.entities import Block
from companion_ever.memory.summaries.block_repository import BlockRepo


class BlockService:
    def __init__(self, authorizer: Authorizer, block_repo: BlockRepo):
        self.authorizer = authorizer
        self.block_repo = block_repo

    def get_blocks(self, user_id: str, persona_id: str) -> list[Block]:
        self.authorizer.require_persona(user_id, persona_id)
        return self.block_repo.get_all(user_id=user_id, persona_id=persona_id)
