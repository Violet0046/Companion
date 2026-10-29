"""Kinds inferred from existing note types until the two-layer writer is migrated."""
EPISODE_TYPES = {'event', 'milestone', 'inside_joke', 'callback', 'persona_memory', 'persona_arc'}


def memory_kind(note):
    return 'episode' if note.type.value in EPISODE_TYPES else 'fact'


def valid_relation(source, target, relation):
    expected = {'supports': ('episode', 'fact'), 'continues': ('episode', 'episode'),
                'supersedes': ('fact', 'fact')}
    return (memory_kind(source), memory_kind(target)) == expected.get(relation)
