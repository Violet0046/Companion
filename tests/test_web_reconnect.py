"""Execute the real frontend recovery code with delayed responses and stale sockets."""
import os
import shutil
import subprocess
import tempfile
from test_web_ws_flow import STUB
from companion_ever.api.server import WEB_DIR

HARNESS = r"""
(async () => {
  function check(ok, message) { if (!ok) throw new Error(message); }
  S.user = 'u'; S.persona = 'one'; S.viewMode = 'live'; S.connected = true;
  S.messages = []; S.latestClientMessageId = 'request'; writeOutbox({});
  loadConvs = () => {};
  let response = {id:'g', status:'running', messages:[{id:12, role:'assistant', content:'saved', created_at:'2026-10-30T01:00:00Z'}]};
  api = async () => response;
  await recoverReply();
  check(S.generationId === 'g' && S.typing, 'running generation resumes typing');
  check(S.messages.length === 1, 'persisted output is restored');
  await recoverReply();
  check(S.messages.length === 1, 'polling must not duplicate output');
  response = {...response, status:'completed', messages:[...response.messages, {id:13, role:'assistant', content:'second', created_at:'2026-10-30T01:00:01Z'}]};
  await recoverReply();
  check(!S.typing && S.messages.length === 2 && replyRecoveryTimer === null, 'completion restores the second output and stops polling');

  let resolve;
  api = () => new Promise(r => { resolve = r; });
  const pending = recoverReply();
  S.latestClientMessageId = 'new-request'; S.generationId = 'new-generation';
  resolve({id:'old',status:'running',messages:[]});
  await pending;
  check(S.generationId === 'new-generation' && replyRecoveryTimer === null, 'late recovery cannot replace a newer input');

  const switched = recoverReply();
  S.persona = 'two';
  resolve({id:'foreign',status:'running',messages:[]});
  await switched;
  check(S.generationId === 'new-generation', 'late recovery cannot cross personas');

  const oldHistory = loadHistory(true);
  S.persona = 'three';
  resolve([{id:99,role:'assistant',content:'wrong chat'}]);
  await oldHistory;
  check(!S.messages.some(m => m.id === 99), 'late history cannot cross personas');

  connect(); const oldSocket = S.ws;
  connect(); const newSocket = S.ws;
  newSocket.onopen(); oldSocket.onclose();
  check(S.connected, 'obsolete socket close cannot disconnect the replacement');
  oldSocket.onmessage({data:JSON.stringify({type:'received',generation_id:'wrong',client_message_id:'new-request'})});
  check(S.generationId === 'new-generation', 'obsolete socket events are ignored');

  S.recordPending = {key:'u/three'};
  handleWs({type:'record_done',user_id:'u',persona_id:'three',more_pending:true});
  check(S.recordPending && S.recording['u/three'], 'intermediate batches do not announce completion');
  const notices = [];
  toast = text => notices.push(text);
  api = async path => path.endsWith('/memory-jobs')
    ? [{status:'failed',last_error:'timeout'}]
    : {last_record_at:'2026-10-30T01:00:00Z',recording:false};
  await pollRecordDone('u', 'three', null, 1);
  check(S.recordPending === null && notices.some(t => t.includes('未完成')) && !notices.includes('记好了'),
        'offline polling must not announce success when a later batch failed');
  const memoryBox = document.getElementById('memory-list');
  const pinButton = document.createElement('button');
  pinButton.dataset.id = 'n1';
  memoryBox.querySelectorAll = selector => selector === '.note-pin' ? [pinButton] : [];
  S.memoryPersona = {user:'u',persona:'three'};
  S.memoryNotes = [{id:'n1',kind:'fact',content:'user fact',reality:'shared_fiction',pinned:false}];
  S.memoryFilter = 'all';
  let pinRequest;
  api = async (path, opts) => { pinRequest = {path, ...JSON.parse(opts.body)}; return {pinned:true}; };
  renderMemory();
  check(memoryBox.innerHTML.includes('事实') && memoryBox.innerHTML.includes('共同想象'), 'memory view renders kind and reality');
  await pinButton.onclick();
  check(pinRequest.path.endsWith('/notes/n1/pin') && pinRequest.pinned && S.memoryNotes[0].pinned,
        'note pin button calls scoped API and updates its canonical state');
  const derived = {blocks:[{block_type:'relationship_state',fields:{overview:['明确的关系事实'],agreements:['约定'],key_moments:[]}},
                          {block_type:'user_profile',fields:{identity:['用户信息'],preferences:['偏好']}}]};
  const derivedHtml = relationHtml(derived);
  check(derivedHtml.includes('明确的关系事实') && derivedHtml.includes('用户信息') && !derivedHtml.includes('0%'),
        'derived summaries render both blocks without inventing a progress percentage');
  const sourceHtml = summaryEvidenceHtml({document:{user_profile:{identity:[{text:'claim',citations:[{note_id:'n1',quote:'fact quote'}]}]},relationship_state:{}}});
  check(sourceHtml.includes('fact quote') && sourceHtml.includes('查看事实依据'), 'summary evidence remains inspectable');
  api = async () => derived;
  handleWs({type:'summary_updated',user_id:'u',persona_id:'three'});
  await Promise.resolve(); await Promise.resolve();
  check(S.summaries['u/three'] === derived, 'background summary notification reloads the correct scoped cache');
  stopReplyRecovery(); resetSending(); clearTimeout(S.reconnectTimer);
  console.log('[OK] restored outputs, polling, request fencing, scope and stale socket handling');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""


if __name__ == '__main__':
    node = shutil.which('node')
    assert node, 'Node is required to validate frontend recovery'
    with tempfile.NamedTemporaryFile('w', suffix='.js', encoding='utf-8', delete=False) as f:
        f.write(STUB + '\n' + (WEB_DIR / 'app.js').read_text(encoding='utf-8') + '\n' + HARNESS)
        path = f.name
    try:
        result = subprocess.run([node, path], capture_output=True, text=True, encoding='utf-8', timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout)
    finally:
        os.unlink(path)
