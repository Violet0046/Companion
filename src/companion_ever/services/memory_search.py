"""Public search contract; scope is supplied by the authorized caller."""
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class MemorySearch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(default='', max_length=2000)
    context_query: str = Field(default='', max_length=1000)
    purpose: Literal['chat', 'consolidation'] = 'chat'
    kinds: list[Literal['episode', 'fact']] = Field(default_factory=lambda: ['episode', 'fact'])
    limit: int = Field(default=10, ge=1, le=50)
    max_hops: int = Field(default=1, ge=0, le=2)
    seed_ids: list[str] = Field(default_factory=list, max_length=10)
    recent_limit: int = Field(default=0, ge=0, le=10)


class MemorySearchResult:
    def __init__(self, hits, relations, warnings, truncated):
        self.hits = hits
        self.relations = relations
        self.warnings = warnings
        self.truncated = truncated

    @property
    def notes(self):
        return [hit['note'] for hit in self.hits]

    def to_dict(self):
        from companion_ever.domain.memory import memory_kind
        hits = []
        for hit in self.hits:
            note = hit['note']
            hits.append({**{k:v for k,v in hit.items() if k != 'note'},
                         'note': {**note.to_llm_dict(), 'kind': memory_kind(note),
                                  'created_at': note.created_at, 'updated_at': note.updated_at}})
        return {'hits': hits, 'relations': self.relations, 'warnings': self.warnings,
                'truncated': self.truncated, 'backend': 'lexical+graph'}
