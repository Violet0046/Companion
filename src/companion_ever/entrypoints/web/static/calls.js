/* Read-only diagnostics. Rows, totals and exports always share the same filters. */
const $=id=>document.getElementById(id);
const names={chat:'聊天',extraction:'记忆整理',summary:'摘要更新',coordination:'群聊协调'};
const callName=r=>r.layer==='chat'&&r.purpose==='proactive'?'主动聊天':names[r.layer];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>n===null||n===undefined?'未知':n.toLocaleString('zh-CN');
const seconds=n=>(n/1000).toFixed(n>=10000?1:2)+' s';
const initial=new URLSearchParams(location.search);
const state={date:initial.get('date')||'',user_id:initial.get('user')||localStorage.getItem('ce_user')||'',
  persona_id:initial.get('persona')||'',layer:'all',status:'all',job_id:null};
let records=[],selected=null,requestNumber=0,controller=null;
function query(){
  const params=new URLSearchParams();
  for(const [key,value] of Object.entries(state))if(value&&value!=='all')params.set(key,value);
  return params.toString();
}
function headers(){return {};}
function badge(r){return `<span class="badge ${r.status==='error'?'error':''}">${{ok:'成功',error:'失败',aborted:'中断'}[r.status]||'未知'}</span>`;}
function kv(label,value,mono=false){return `<div class="kv"><span>${label}</span><span class="${mono?'mono':''}">${esc(value)}</span></div>`;}
function renderSummary(summary){
  $('calls-total').textContent=summary.calls+' 次';
  $('calls-caption').textContent=`${summary.ok} 次成功 · ${summary.errors} 次失败 · ${summary.aborted} 次中断`;
  $('token-total').textContent=fmt(summary.input_tokens)+' / '+fmt(summary.output_tokens);
  $('token-caption').textContent=`输入 ${summary.input_tokens_reported_calls}/${summary.calls} 次、输出 ${summary.output_tokens_reported_calls}/${summary.calls} 次已报告；合计仅含已知用量`;
  $('time-total').textContent=seconds(summary.elapsed_ms);
}
function renderDetail(r){
  if(!r){$('detail').innerHTML='<div class="empty">选择一条调用查看详情</div>';return;}
  const metadata=[kv('开始时间',r.started_at.replace('T',' ')),kv('调用模型',r.model,true),kv('耗时',seconds(r.duration_ms)),
    kv('输入 token',fmt(r.input_tokens)),kv('输出 token',fmt(r.output_tokens)),kv('缓存命中输入 token',fmt(r.cached_input_tokens))].join('');
  const error=r.error?`<div class="notice"><strong>${esc(r.error.message)}</strong><div class="tiny">分类 ${esc(r.error.kind)} · 阶段 ${esc(r.error.stage)}${r.error.http_status?' · HTTP '+esc(r.error.http_status):''}</div></div>`:'';
  $('detail').innerHTML=`<div class="detail-head"><div><h2>${esc(callName(r))} · 调用详情</h2><div class="tiny mono">${esc(r.id)}</div></div>${badge(r)}</div><div class="detail-body">${metadata}${error}<hr>${kv('用户 / 角色',r.user_id+' / '+r.persona_id,true)}${kv(r.job_id?'关联任务':'关联回复',r.job_id||r.generation_id||'—',true)}${r.attempt_id?kv('执行 ID',r.attempt_id,true):''}${kv('执行位置','第 '+r.attempt+' 次尝试 · 第 '+r.call_index+' 次模型调用')}${r.job_id?'<button class="plain" id="same-task">只看这个任务的调用</button>':''}<p class="hint">${r.input_tokens===null?'服务商没有返回完整用量，缺失值保留为未知。':'记录服务商返回的实际计数，不使用字符数估算。'}</p><details><summary>查看这条原始记录</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details></div><div class="sidenote hint">模型请求结果与任务最终写库结果分别记录。</div>`;
  if($('same-task'))$('same-task').onclick=()=>{state.job_id=r.job_id;state.layer='all';state.status='all';$('status-filter').value='all';loadCalls();};
}
function renderRows(){
  if(!records.some(r=>r.id===selected))selected=records[0]?.id||null;
  $('rows').innerHTML=records.length?records.map(r=>`<tr data-id="${esc(r.id)}" tabindex="0" class="${r.id===selected?'selected':''}"><td class="mono">${esc(r.started_at)}</td><td>${esc(callName(r))}<div class="tiny">第 ${r.attempt} 次尝试 / 调用 ${r.call_index}</div></td><td>${badge(r)}</td><td class="mono">${seconds(r.duration_ms)}</td><td class="mono pair">${fmt(r.input_tokens)} / ${fmt(r.output_tokens)}</td></tr>`).join(''):'<tr><td colspan="5"><div class="empty">没有符合条件的调用</div></td></tr>';
  document.querySelectorAll('tr[data-id]').forEach(row=>{const select=()=>{selected=row.dataset.id;renderRows();};row.onclick=select;row.onkeydown=e=>{if(e.key==='Enter'){select();}};});
  $('file-lines').innerHTML=records.map((r,i)=>`<div class="file-line"><span>${i+1}</span><code>${esc(JSON.stringify(r))}</code></div>`).join('');
  renderDetail(records.find(r=>r.id===selected));
}
function filters(){
  document.querySelectorAll('[data-layer]').forEach(b=>b.classList.toggle('active',b.dataset.layer===state.layer));
  $('scope').hidden=!state.job_id;$('scope-text').textContent=state.job_id?'关联任务：'+state.job_id:'';
}
async function loadCalls(){
  const number=++requestNumber;
  if(controller)controller.abort();const activeController=new AbortController();controller=activeController;
  filters();$('message').hidden=true;
  for(const id of ['calls-total','token-total','time-total'])$(id).textContent='统计中…';
  records=[];renderRows();$('table-count').textContent='正在加载…';$('download').disabled=true;
  const timeout=setTimeout(()=>activeController.abort(),15000);
  try{
    const response=await fetch('/api/calls?'+query(),{headers:headers(),cache:'no-store',signal:activeController.signal});
    if(!response.ok)throw new Error(response.status===401?'登录已失效，请返回应用重新登录。':'无法读取调用记录（HTTP '+response.status+'）');
    const result=await response.json();if(number!==requestNumber)return;
    state.date=result.date;$('date-filter').value=result.date;
    state.user_id=result.user_id;state.persona_id=result.persona_id;
    records=result.records;renderSummary(result.summary);renderRows();
    $('table-count').textContent=`当前筛选共 ${result.summary.calls} 次调用，展示 ${records.length} 次${result.truncated?'；顶部统计和下载包含全部匹配调用':''}`;
    $('file-name').textContent=result.date+'.jsonl';$('download').disabled=false;
    $('current-scope').textContent=result.user_id+' / '+result.persona_id;
    if(result.write_error){$('message').textContent='调用记录曾写入失败，统计可能不完整。';$('message').hidden=false;}
  }catch(error){
    if(number!==requestNumber)return;
    records=[];renderRows();
    for(const id of ['calls-total','token-total','time-total'])$(id).textContent='—';
    $('calls-caption').textContent='';$('token-caption').textContent='';$('table-count').textContent='未加载';
    $('message').textContent=error.name==='AbortError'?'读取超时，请重试。':error.message;$('message').hidden=false;
  }finally{clearTimeout(timeout);}
}
document.querySelectorAll('[data-layer]').forEach(b=>b.onclick=()=>{state.layer=b.dataset.layer;loadCalls();});
$('status-filter').onchange=e=>{state.status=e.target.value;loadCalls();};
$('date-filter').onchange=e=>{state.date=e.target.value;loadCalls();};
$('clear-scope').onclick=()=>{state.job_id=null;loadCalls();};
$('refresh').onclick=loadCalls;
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));$('list-view').hidden=b.dataset.tab!=='list';$('file-view').hidden=b.dataset.tab!=='file';});
$('download').onclick=async()=>{
  $('download').disabled=true;
  try{
    const response=await fetch('/api/calls/export?'+query(),{headers:headers(),cache:'no-store'});
    if(!response.ok)throw new Error('下载失败（HTTP '+response.status+'）');
    const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=state.date+'.jsonl';
    document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(error){$('message').textContent=error.message;$('message').hidden=false;}
  finally{$('download').disabled=false;}
};
loadCalls();
