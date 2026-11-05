"""Account-owned group endpoints; all execution delegates to the shared generation service."""
from fastapi import APIRouter, Request, Query, HTTPException
from pydantic import BaseModel, Field, StrictInt

from companion_ever.entrypoints.web.routes.chat import _present_snapshot, _present_message
from companion_ever.entrypoints.web.schemas import ChatIn
from companion_ever.accounts.usage import LimitExceeded


def checked(action,*args,**kwargs):
    try:return action(*args,**kwargs)
    except LimitExceeded:raise
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc

router=APIRouter()


class GroupIn(BaseModel):
    name:str=Field(min_length=1,max_length=60)
    persona_ids:list[str]=Field(min_length=2,max_length=8)


class GroupChatIn(ChatIn):
    mentions:list[dict]=Field(default_factory=list,max_length=8)


class MembersIn(BaseModel):
    persona_ids:list[str]=Field(min_length=1,max_length=8)


class ReadIn(BaseModel):
    message_id:StrictInt=Field(gt=0)


class ProactiveIn(BaseModel):
    enabled:bool


class VisibleIn(BaseModel):
    visible:bool


class ContinueIn(BaseModel):
    request_key:str=Field(min_length=1,max_length=128)
    after_message_id:StrictInt|None=Field(default=None,gt=0)


def target(request,cid):
    ctx=request.app.state.ctx;user=request.state.account['user_id']
    ctx.services.groups.require(user,cid)
    return ctx,user


@router.get('/api/groups')
def list_groups(request:Request):
    return request.app.state.ctx.services.groups.list(request.state.account['user_id'])


@router.post('/api/groups')
def create_group(request:Request,payload:GroupIn):
    return checked(request.app.state.ctx.services.groups.create,request.state.account['user_id'],payload.name,payload.persona_ids)


@router.get('/api/groups/{cid}')
def get_group(request:Request,cid:int):
    ctx,user=target(request,cid);return ctx.services.groups.info(user,cid)


@router.get('/api/groups/{cid}/history')
def history(request:Request,cid:int,limit:int=Query(50,ge=1,le=500),before_id:int|None=None,after_id:int|None=None):
    ctx,user=target(request,cid)
    return [_present_message(m) for m in ctx.services.groups.history(user,cid,limit=limit,before_id=before_id,after_id=after_id)]


@router.get('/api/groups/{cid}/unread-count')
def unread(request:Request,cid:int,after_id:int=Query(0,ge=0)):
    ctx,user=target(request,cid);return ctx.services.groups.unread(user,cid,after_id)


@router.get('/api/groups/{cid}/proactive')
def proactive(request:Request,cid:int):
    ctx,user=target(request,cid)
    return ctx.services.proactive.group_setting(user,cid)


@router.put('/api/groups/{cid}/proactive')
def set_proactive(request:Request,cid:int,payload:ProactiveIn):
    ctx,user=target(request,cid)
    return ctx.services.proactive.group_setting(user,cid,payload.enabled)


@router.post('/api/groups/{cid}/presence')
def presence(request:Request,cid:int,payload:VisibleIn):
    ctx,user=target(request,cid);ctx.services.groups.visible(user,cid,payload.visible);return {'ok':True}


@router.post('/api/groups/{cid}/read')
def read(request:Request,cid:int,payload:ReadIn):
    ctx,user=target(request,cid);ctx.services.groups.mark_read(user,cid,payload.message_id);return {'ok':True}


@router.post('/api/groups/{cid}/messages')
def receive(request:Request,cid:int,payload:GroupChatIn):
    ctx,user=target(request,cid)
    g,created=checked(ctx.services.generation.submit_group,user,cid,payload.content,payload.client_message_id,
        quoted_message_id=payload.quoted_message_id,mentions=payload.mentions)
    return {'message_id':g['trigger_message_id'],'generation_id':g['id'],'status':g['status'],'duplicate':not created}


@router.get('/api/groups/{cid}/generation')
def latest(request:Request,cid:int):
    ctx,user=target(request,cid);return _present_snapshot(ctx.services.generation.group_snapshot(user,cid))


@router.post('/api/groups/{cid}/continue')
def continue_group(request:Request,cid:int,payload:ContinueIn):
    ctx,user=target(request,cid)
    g,created=checked(ctx.services.generation.continue_group,user,cid,payload.request_key,payload.after_message_id)
    return {'generation_id':g['id'],'status':g['status'],'duplicate':not created}


@router.get('/api/groups/{cid}/generations/{gid}')
def generation(request:Request,cid:int,gid:str):
    ctx,user=target(request,cid)
    try:return _present_snapshot(ctx.services.generation.group_snapshot(user,cid,gid))
    except LookupError as exc:raise HTTPException(404,str(exc)) from exc


@router.post('/api/groups/{cid}/generations/{gid}/retry')
def retry(request:Request,cid:int,gid:str):
    ctx,user=target(request,cid)
    try:return _present_snapshot(checked(ctx.services.generation.retry_group,user,cid,gid))
    except LookupError as exc:raise HTTPException(404,str(exc)) from exc


@router.delete('/api/groups/{cid}/members/{persona}')
def leave(request:Request,cid:int,persona:str):
    ctx,user=target(request,cid);return checked(ctx.services.groups.leave,user,cid,persona)


@router.post('/api/groups/{cid}/members')
def add_members(request:Request,cid:int,payload:MembersIn):
    ctx,user=target(request,cid);return checked(ctx.services.groups.add_members,user,cid,payload.persona_ids)
