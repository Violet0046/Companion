"""Durable user receipt and cancellable, independently persisted assistant messages."""
from companion_ever.infrastructure.tracing import call_context
import logging
import uuid
from companion_ever.conversations.reply import split_assistant_reply, split_messages
from companion_ever.conversations.stream_filter import visible_prefix
from companion_ever.conversations.generation_repository import GenerationRepo
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.utils.text import strip_invisible
from companion_ever.runtime.errors import describe_error

MESSAGE_BREAK = "<message_break>"


class ChatService:
    def __init__(self, prompt_service, conversation_service, llm_client,
                 session_close_service=None, idle_session_minutes=120):
        self.prompt_service = prompt_service
        self.conversation_service = conversation_service
        self.llm_client = llm_client
        self.session_close_service = session_close_service
        self.idle_session_minutes = idle_session_minutes
        self.generations = GenerationRepo(conversation_service.conv_repo.conn)

    def receive(self, user_id, persona_id, user_input, client_message_id=None):
        self.conversation_service.authorizer.require_persona(user_id,persona_id)
        self._maybe_close_stale(user_id,persona_id)
        with transaction(self.generations.conn):
            self.conversation_service.authorizer.require_persona(user_id,persona_id)
            return self.generations.receive(user_id,persona_id,strip_invisible(user_input),client_message_id or uuid.uuid4().hex)

    def _maybe_close_stale(self, user_id, persona_id):
        if self.session_close_service:
            try:
                self.session_close_service.close_if_stale(user_id,persona_id,self.idle_session_minutes)
            except Exception:
                logging.getLogger(__name__).exception('自动整理触发失败')

    def reply_events(self, generation, *, stream=True):
        gid=generation['id']
        if not self.generations.claim(gid):
            for saved in self.generations.outputs(gid):
                yield {'type':'message_done','message':saved,'generation_id':gid,'replayed':True}
            status=self.generations.get(gid)['status']
            yield {'type':'done' if status=='completed' else status,'generation_id':gid}
            return
        completed=False
        chunks=None
        with call_context('chat',generation['user_id'],generation['persona_id'],generation_id=gid,purpose='reply'):
            try:
                # The host can prime this event under its execution lock,
                # fencing claim against shutdown before any model request.
                yield {'type':'start','generation_id':gid}
                trigger=self.generations.trigger_content(generation)
                result=self.prompt_service.build(generation['user_id'],generation['persona_id'],query=trigger,
                                                 current_message_id=generation['trigger_message_id'])
                messages=list(result['messages'])
                if not self.generations.active(gid):
                    yield {'type':'cancelled','generation_id':gid}
                    return
                chunks=self.llm_client.stream_chat(messages) if stream else iter([self.llm_client.chat(messages)])
                pending=''
                body_parts=[]
                index=0
                for chunk in chunks:
                    if not self.generations.active(gid):
                        yield {'type':'cancelled','generation_id':gid}
                        return
                    pending+=chunk
                    while MESSAGE_BREAK in pending:
                        part,pending=pending.split(MESSAGE_BREAK,1)
                        if not visible_prefix(''.join(body_parts)+part+MESSAGE_BREAK).endswith(MESSAGE_BREAK):
                            body_parts.append(part+MESSAGE_BREAK)
                            yield {'type':'delta','content':part+MESSAGE_BREAK,'generation_id':gid}
                            continue
                        body_parts.append(part)
                        if part:
                            yield {'type':'delta','content':part,'generation_id':gid}
                        raw=''.join(body_parts)
                        body,thinking=split_assistant_reply(raw,getattr(self.llm_client,'last_reasoning',None))
                        if not body.strip():
                            continue
                        saved=self.generations.save_message(gid,index,body,thinking)
                        if saved is None:
                            yield {'type':'cancelled','generation_id':gid}
                            return
                        yield {'type':'message_done','message':saved,'raw':raw,'generation_id':gid}
                        index+=1
                        body_parts=[]
                        if index>=3:
                            pending=''
                            break
                    if index>=3:
                        break
                    # Keep only a possible separator prefix; do not display protocol tokens.
                    keep=0
                    for n in range(1,min(len(pending),len(MESSAGE_BREAK)-1)+1):
                        if pending.endswith(MESSAGE_BREAK[:n]):
                            keep=n
                    emit=pending[:-keep] if keep else pending
                    pending=pending[-keep:] if keep else ''
                    if emit:
                        body_parts.append(emit)
                        yield {'type':'delta','content':emit,'generation_id':gid}
                raw=''.join(body_parts)+pending
                if raw.strip() and index<3:
                    if not self.generations.active(gid):
                        yield {'type':'cancelled','generation_id':gid}
                        return
                    if pending:
                        yield {'type':'delta','content':pending,'generation_id':gid}
                    body,thinking=split_assistant_reply(raw,getattr(self.llm_client,'last_reasoning',None))
                    parts = split_messages(body) if index==0 else [body]
                    if not parts:
                        raise ValueError('模型未生成可保存的正文')
                    for offset,part in enumerate(parts):
                        saved=self.generations.save_message(gid,index+offset,part,thinking if offset==0 else None)
                        if saved is None:
                            yield {'type':'cancelled','generation_id':gid}
                            return
                        yield {'type':'message_done','message':saved,'raw':raw if len(parts)==1 else part,'generation_id':gid}
                elif index==0:
                    raise ValueError('模型返回了空回复')
                self.generations.finish(gid)
                completed=True
                yield {'type':'done','generation_id':gid}
            except GeneratorExit:
                raise
            except Exception as exc:
                self.generations.finish(gid,'failed',describe_error(exc))
                raise
            finally:
                if chunks is not None and hasattr(chunks,'close'):
                    chunks.close()
                if not completed:
                    self.generations.finish(gid,'cancelled')

    def chat(self,user_id,persona_id,user_input,client_message_id=None):
        generation,_=self.receive(user_id,persona_id,user_input,client_message_id)
        replies=[]
        for event in self.reply_events(generation,stream=False):
            if event['type']=='message_done':
                replies.append(event.get('raw',event['message']['content']))
        return '\n\n'.join(replies)

    def stream_chat(self,user_id,persona_id,user_input,client_message_id=None):
        generation,_=self.receive(user_id,persona_id,user_input,client_message_id)
        for event in self.reply_events(generation):
            if event['type']=='delta':
                yield event['content']
