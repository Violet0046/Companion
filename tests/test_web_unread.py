"""Unread counts follow displayed replies, visibility and persistent read cursors."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from test_web_ws_flow import STUB
from test_durable_flow import context
from companion_ever.entrypoints.web.server import WEB_DIR,create_app
from companion_ever.config import WebConfig


HARNESS=r"""
(async () => {
  function check(ok,message) { if (!ok) throw new Error(message); }
  global.requestAnimationFrame=callback=>callback();
  S.user='u';S.persona='p';S.view='chat';S.viewMode='live';S.messages=[];S.generationId='g';
  S.convs=[{user:'u',persona:'p',last:{id:103,role:'assistant'},unread:3,unreadThrough:103}];
  document.visibilityState='visible';wasAtBottom=true;setReadMark('u','p',100);
  const reply=id=>({id,role:'assistant',content:'reply '+id,created_at:'2026-10-30T01:00:00Z'});
  for (const id of [101,102,103]) handleWs({type:'message_done',generation_id:'g',message:reply(id)});
  check(readMark('u','p')===103 && S.convs[0].unread===0,'three replies displayed in the open conversation are already read');
  showView('messages');
  check(S.convs[0].unread===0,'leaving the conversation does not resurrect read badges');

  S.view='chat';document.visibilityState='hidden';
  handleWs({type:'message_done',generation_id:'g',message:reply(104)});
  check(readMark('u','p')===103,'a background page must not mark replies as read');
  document.visibilityState='visible';S.view='profile';
  handleWs({type:'message_done',generation_id:'g',message:reply(105)});
  check(readMark('u','p')===103,'viewing a profile must not consume chat unread replies');
  S.convs[0].unread=2;S.convs[0].unreadThrough=105;
  showView('chat');
  check(readMark('u','p')===105 && S.convs[0].unread===0,'returning to the visible conversation reads the restored replies');

  wasAtBottom=false;
  handleWs({type:'message_done',generation_id:'g',message:reply(106)});
  check(readMark('u','p')===105,'a reply below the viewport remains unread while reading older messages');
  const box=$('chat');box.scrollHeight=1200;box.clientHeight=800;box.scrollTop=400;
  listeners.chat.scroll();
  check(readMark('u','p')===106,'scrolling back to the latest replies marks them read');
  setReadMark('u','p',99);
  check(readMark('u','p')===106,'late responses cannot move the read cursor backwards');

  S.convs[0].unread=3;S.convs[0].unreadThrough=106;renderConvs();
  check(S.convs[0].unread===0,'a delayed conversation-list response cannot resurrect already read badges');
  let resolve;
  api=()=>new Promise(done=>{resolve=done;});
  const pending=fetchUnread('u','p');
  setReadMark('u','p',108);
  resolve({count:2,latest_id:108});
  check((await pending).count===0,'a count arriving after its replies were read is discarded');

  setReadMark('u','other',0);
  api=async path=>path.includes('/history') ? Array.from({length:20},(_,i)=>reply(150+i))
    : path.includes('/unread-count') ? {count:128,latest_id:200} : {};
  const other=await fetchConv({user:'u',persona:'other'});
  check(other.unread===128 && readMark('u','p')===108,'unread counts use full server history and remain isolated by persona');

  for (const status of ['queued','running']) {
    S.replyState={scope:base(),status};setTyping(true);renderReplyStatus();
    check(S.replyStatusEl===null && S.typing,'pending replies use only the typing indicator');
  }
  S.replyState={scope:base(),status:'failed',last_error:'Connection error',can_retry:true};renderReplyStatus();
  check(S.replyStatusEl && S.replyStatusEl.children[1].textContent==='重试回复','actual failures retain their recovery action');
  S.relationPersona={user:'u',persona:'p'};S.view='relation';
  const meta={job_status:'failed',last_error:'租约到期',pending_changes:74};
  S.debug=false;renderRelationPage('u','p',{summary_meta:meta});
  const normal=$('relation-body').innerHTML;
  check(!normal.includes('btn-record-now') && !normal.includes('btn-summary-refresh') && !normal.includes('租约到期'),
        'normal relationship view has no maintenance buttons or task failure details');
  S.debug=true;renderRelationPage('u','p',{summary_meta:meta});
  check($('relation-body').innerHTML.includes('btn-task-attempts') && $('relation-body').innerHTML.includes('btn-record-now'),
        'debug mode retains task inspection and manual recovery');
  stopReplyRecovery();resetSending();clearTimeout(S.reconnectTimer);
  console.log('[OK] visible read cursors, background unread, count races, full counts and unobtrusive typing');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


def backend(path):
    ctx=context(path)
    try:
        conv=ctx.services.conversation
        ids=[]
        for i in range(125):
            conv.log_turn(user_id='alice',persona_id='one',turn_index=i+1,role='user',content='question')
            ids.append(conv.log_turn(user_id='alice',persona_id='one',turn_index=i+1,role='assistant',content='reply'))
        conv.log_session_close('alice','one')
        ctx.services.persona.create_persona('alice','two','{}')
        conv.log_turn(user_id='alice',persona_id='two',turn_index=1,role='assistant',content='another persona')
        with TestClient(create_app(ctx,WebConfig(pin='1234'))) as client:
            endpoint='/api/persona/alice/one/unread-count'
            assert client.get(endpoint).status_code==401
            client.headers['X-Pin']='1234'
            assert client.get(endpoint).json()=={'count':125,'latest_id':ids[-1]}
            assert client.get(endpoint,params={'after_id':ids[19]}).json()['count']==105
            assert client.get(endpoint,params={'after_id':ids[-1]}).json()['count']==0
            assert client.get(endpoint,params={'after_id':-1}).status_code==422
            assert client.get('/api/persona/alice/two/unread-count').json()['count']==1
            assert client.get('/api/persona/foreign/one/unread-count').status_code==403
    finally:
        ctx.conn.close()


if __name__=='__main__':
    node=shutil.which('node');assert node
    with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:
        f.write(STUB+'\n'+(WEB_DIR/'app.js').read_text(encoding='utf-8')+'\n'+HARNESS);path=f.name
    try:
        out=subprocess.run([node,path],capture_output=True,text=True,encoding='utf-8',timeout=15)
        assert out.returncode==0,out.stdout+out.stderr
        print(out.stdout)
    finally:
        os.unlink(path)
    with tempfile.TemporaryDirectory() as tmp:
        backend(Path(tmp)/'unread.db')
