"""Run real foreground lifecycle handlers, including a socket that never reconnects."""
import os
import shutil
import subprocess
import tempfile
from test_web_ws_flow import STUB
from companion_ever.api.server import WEB_DIR


EVENTS=r"""
const lifecycle = {document:{},window:{}};
for (const [name,target] of [['document',document],['window',window]]) {
  target.addEventListener=(type,callback) => (lifecycle[name][type] ||= []).push(callback);
}
"""

HARNESS=r"""
(async () => {
  function check(ok,message) { if (!ok) throw new Error(message); }
  S.user='u'; S.persona='p'; S.viewMode='live'; S.view='chat'; S.connected=false;
  S.messages=[]; writeOutbox({}); loadConvs=()=>{};
  const answer={id:12,role:'assistant',content:'saved while backgrounded',created_at:'2026-10-30T01:00:00Z'};
  let state={id:'g',status:'completed',messages:[answer]};
  api=async path=>path.includes('/history') ? [answer] : state;
  for (const callback of lifecycle.document.visibilitychange) callback();
  await new Promise(setImmediate);
  check(!S.connected && S.generationId==='g' && S.messages.some(m=>m.id===12),
        'foreground restores persisted reply even when websocket never reconnects');
  S.messages=[];
  for (const callback of lifecycle.window.pageshow) callback({persisted:true});
  await new Promise(setImmediate);
  check(S.messages.some(m=>m.id===12),'bfcache pageshow restores replies');

  S.messages=[{id:Date.now(),role:'user',content:'not acknowledged',client_message_id:'lost-ack',pending:true}];
  writeOutbox({'lost-ack':'not acknowledged'});S.latestClientMessageId='lost-ack';
  const requests=[];
  api=async (path,options)=>{
    requests.push({path,options});
    if (path==='/api/messages') return {message_id:11,generation_id:'g',status:'running'};
    return path.includes('/history') ? [{id:11,role:'user',content:'not acknowledged',client_message_id:'lost-ack'},answer] : state;
  };
  await recoverReply(true);
  check(requests[0].path==='/api/messages' && JSON.parse(requests[0].options.body).client_message_id==='lost-ack',
        'lost websocket receipt falls back to HTTP using the original idempotency key');
  check(!Object.keys(readOutbox()).length && S.messages.length===2 && S.messages.some(m=>m.id===12),
        'HTTP receipt reconciles pending input and restores its reply without an open websocket');

  state={id:'failed',status:'failed',last_error:'APIConnectionError: Connection error.',messages:[],can_retry:true};
  await recoverReply();
  check(S.replyStatusEl && S.replyStatusEl.children[0].textContent.includes('回复未完成'),
        'failed background request remains visible after transient toast would disappear');
  check(S.replyStatusEl.children[1].textContent==='重试回复','failure exposes explicit reply retry');

  S.replyState=null; S.generationId='race';
  const user={id:20,role:'user',content:'question',created_at:'2026-10-30T01:00:00Z'};
  S.messages=[user];
  let resolveHistory;
  api=()=>new Promise(resolve=>{resolveHistory=resolve;});
  const pending=loadHistory(true);
  handleWs({type:'message_done',generation_id:'race',message:{...answer,id:21}});
  resolveHistory([user]); await pending;
  check(S.messages.some(m=>m.id===21),'stale history cannot erase a confirmed reply received during fetch');

  const old=loadHistory(true); const releaseOld=resolveHistory;
  const latest=loadHistory(true);
  resolveHistory([user,{...answer,id:22}]); await latest;
  releaseOld([user]); await old;
  check(S.messages.some(m=>m.id===22),'out-of-order history responses cannot replace newer state');

  const draft=renderMsg('assistant','second message in progress',null,null);
  S.streamEl=draft; S.generationId='stream';
  api=async()=>({id:'stream',status:'running',messages:[{...answer,id:22}]});
  await recoverReply();
  check(S.streamEl===draft,'polling an already persisted first reply must not remove the next streamed draft');
  stopReplyRecovery(); resetSending(); clearTimeout(S.reconnectTimer);
  console.log('[OK] mobile lifecycle, HTTP-only recovery, visible failure and history races');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


if __name__=='__main__':
    node=shutil.which('node')
    assert node
    with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:
        f.write(STUB+'\n'+EVENTS+'\n'+(WEB_DIR/'app.js').read_text(encoding='utf-8')+'\n'+HARNESS)
        path=f.name
    try:
        out=subprocess.run([node,path],capture_output=True,text=True,encoding='utf-8',timeout=15)
        assert out.returncode==0,out.stdout+out.stderr
        print(out.stdout)
    finally:
        os.unlink(path)
