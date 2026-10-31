"""Stored memory nature and the single relationship direction contract."""
from companion_ever.domain.enums import LinkType

RELATION_KINDS = {
    LinkType.SUPPORTS: ('episode','fact'),
    LinkType.CONTINUES: ('episode','episode'),
    LinkType.SUPERSEDES: ('fact','fact'),
}

def memory_kind(note):
    return note.kind.value

def valid_relation(source, target, relation):
    kinds_match = (memory_kind(source), memory_kind(target)) == RELATION_KINDS.get(relation)
    reality_matches = source.reality == target.reality or (relation == LinkType.SUPERSEDES and target.reality == 'uncertain')
    return kinds_match and reality_matches
