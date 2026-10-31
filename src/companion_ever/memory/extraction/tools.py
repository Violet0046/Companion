"""Two bounded, read-only memory tools bound to an authorized task scope."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from companion_ever.memory.relations import valid_relation
from companion_ever.memory.retrieval.query import MemorySearch
from companion_ever.memory.notes.links import NoteLinkRepo


class SearchNotesArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=1000)
    kinds: list[Literal['episode', 'fact']] = Field(default_factory=lambda: ['episode', 'fact'], min_length=1, max_length=2)
    limit: StrictInt = Field(default=6, ge=1, le=8)


class NoteContextArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    note_id: str = Field(min_length=1, max_length=128)


class SearchNotesCall(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Literal['search_notes']
    arguments: SearchNotesArgs


class NoteContextCall(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Literal['get_note_context']
    arguments: NoteContextArgs


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tool_calls: list[Annotated[SearchNotesCall | NoteContextCall, Field(discriminator='name')]] = Field(min_length=1, max_length=4)


def note_payload(note):
    return {'id': note.id, 'kind': note.kind.value, 'content': note.content,
            'reality': note.reality, 'event_time': note.event_time,
            'time_basis': note.time_basis, 'revision': note.revision,
            'is_archived': note.is_archived}


def tool_specs():
    return [
        {'name': 'search_notes', 'description': '查找可能重复、同一经历或有明确关联的旧笔记；返回原文、证据及检索状态。',
         'arguments_schema': SearchNotesArgs.model_json_schema()},
        {'name': 'get_note_context', 'description': '读取一个笔记的完整正文、用户证据和一跳关系；未知/其他角色 ID 不返回内容。',
         'arguments_schema': NoteContextArgs.model_json_schema()},
    ]


class MemoryTools:
    def __init__(self, note_service, retrieval, user_id, persona_id):
        self.authorizer = note_service.authorizer
        self.notes = note_service.note_repo
        self.links = NoteLinkRepo(self.notes.conn)
        self.retrieval = retrieval
        self.user_id = user_id
        self.persona_id = persona_id

    def _owned(self, note):
        return note is not None and (note.user_id, note.persona_id) == (self.user_id, self.persona_id)

    def _evidence(self, note_id):
        rows = self.notes.evidence(note_id, self.user_id, self.persona_id, limit=4, user_only=True)
        return rows[:3], len(rows) > 3

    def search_notes(self, query, kinds=None, limit=6):
        self.authorizer.require_persona(self.user_id, self.persona_id)
        args = SearchNotesArgs(query=query, kinds=kinds if kinds is not None else ['episode', 'fact'], limit=limit)
        if not args.query.strip():
            raise ValueError('检索 query 不能为空白')
        result = self.retrieval.search(self.user_id, self.persona_id, MemorySearch(
            query=args.query, kinds=args.kinds, limit=args.limit, purpose='consolidation', max_hops=1))
        notes = {hit['note'].id: hit['note'] for hit in result.hits}
        return {'notes': [{**note_payload(hit['note']), 'superseded': hit['superseded'],
                           'evidence': hit['evidence']} for hit in result.hits],
                'warnings': result.warnings, 'truncated': result.truncated}, notes

    def get_note_context(self, note_id):
        self.authorizer.require_persona(self.user_id, self.persona_id)
        args = NoteContextArgs(note_id=note_id)
        note = self.notes.get(args.note_id)
        if not self._owned(note):
            return {'error': 'not_found', 'message': '笔记不存在或不在当前可读范围。'}, {}
        evidence, evidence_truncated = self._evidence(note.id)
        edges = self.links.neighbours([note.id], self.user_id, self.persona_id, limit=9)
        relations = []
        for edge in edges[:8]:
            source = self.notes.get(edge['from_id'])
            target = self.notes.get(edge['to_id'])
            if self._owned(source) and self._owned(target) and valid_relation(source, target, edge['link_type']):
                relations.append({k: edge[k] for k in ('id', 'from_id', 'to_id', 'link_type', 'reason')})
        return {'note': note_payload(note), 'evidence': evidence,
                'evidence_truncated': evidence_truncated, 'relations': relations,
                'relations_truncated': len(edges) > 8}, {note.id: note} if not note.is_archived else {}

    def execute(self, call):
        if isinstance(call, SearchNotesCall):
            return self.search_notes(**call.arguments.model_dump())
        if isinstance(call, NoteContextCall):
            return self.get_note_context(**call.arguments.model_dump())
        raise ValueError('未知只读工具')
