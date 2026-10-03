'use strict';
const policyDialog=document.createElement('dialog');
policyDialog.id='strategy-dialog';policyDialog.className='memory-dialog';
policyDialog.setAttribute('aria-labelledby','strategy-title');
policyDialog.innerHTML=`<div class="row"><h2 id="strategy-title">策略改进与反馈</h2><button id="strategy-close" aria-label="关闭策略">✕</button></div>
<p class="muted">记录失败、生成候选，再用固定任务比较效果。启用只影响之后开始的任务，正在运行或恢复的任务保留原版本。</p>
<p id="strategy-error" class="inline-error" role="alert"></p><div id="strategy-active"></div>
<div class="actions"><button id="strategy-refresh">刷新</button><button id="strategy-rollback">回退到上个版本</button></div>
<h3>反馈与诊断</h3><form id="strategy-feedback"><label>本区任务<select id="strategy-job" required></select></label>
<label>问题类型<select id="strategy-kind"><option value="retrieval_miss">遗漏了相关资料</option><option value="evidence_miss">原文证据选择不足</option><option value="missed_original">没有充分阅读原文</option><option value="unsupported_claim">结论缺少依据</option><option value="cost">成本或延迟偏高</option><option value="positive">结果有效</option></select></label>
<label>具体问题与应核对的原文<textarea id="strategy-note" required maxlength="2000" rows="3"></textarea></label><button>保存反馈</button></form>
<div id="strategy-feedback-list"></div><h3>策略版本与验证</h3><div id="strategy-versions"></div>
<h3>最近运行与回退记录</h3><div id="strategy-runs"></div><div id="strategy-events"></div>`;
document.body.append(policyDialog);
let policySpace='';
const policyBase=()=>base(policySpace)+'/strategies';
const policyAction=fn=>async e=>{e?.preventDefault();$('strategy-error').textContent='';try{await fn(e);}catch(error){$('strategy-error').textContent=error.message;}};
$('strategy-close').onclick=()=>policyDialog.close();
$('open-strategies').onclick=policyAction(async()=>{if(!sid)throw new Error('请先选择研究区');policySpace=sid;policyDialog.showModal();await loadStrategies();});
$('strategy-refresh').onclick=policyAction(loadStrategies);
$('strategy-rollback').onclick=policyAction(async()=>{await api(policyBase()+'/rollback','POST',{reason:'用户在策略面板回退'});await loadStrategies();});
$('strategy-feedback').onsubmit=policyAction(async()=>{await api(policyBase()+'/feedback','POST',{job_id:$('strategy-job').value,kind:$('strategy-kind').value,note:$('strategy-note').value});$('strategy-note').value='';await loadStrategies();});
async function loadStrategies(){
  const p=await api(policyBase());
  const active=p.versions.find(v=>v.id===p.active_id);
  $('strategy-active').textContent='当前策略：'+(active?.name||p.active_id);
  $('strategy-job').innerHTML=p.jobs.map(r=>`<option value="${esc(r.id)}">${esc(r.question.slice(0,65))} · ${esc(r.status)}</option>`).join('');
  $('strategy-feedback-list').innerHTML=p.feedback.map(f=>`<article class="memory-item"><p>${esc(f.note)}</p><small>${esc(f.kind)} · ${f.origin==='user'?'用户反馈':'运行信号'}</small>${['retrieval_miss','missed_original','unsupported_claim','evidence_miss'].includes(f.kind)?` <button data-propose="${esc(f.id)}">生成待验证候选</button>`:''}</article>`).join('');
  $('strategy-versions').innerHTML=p.versions.map(v=>{
    const evaluations=p.evaluations.filter(e=>e.version_id===v.id);
    return `<article class="memory-item"><strong>${esc(v.name)} ${v.id===p.active_id?'（当前）':''}</strong><p>${esc(v.diagnosis)}</p><p>查询规划：${v.config.query_plan?'开':'关'} · 覆盖重排：${v.config.coverage_rerank?'开':'关'} · 原文提示：${v.config.reading_guide?'开':'关'} · 原文重排：${v.config.evidence_rerank?'开':'关'}</p>${evaluations.map(e=>`<p>${e.metrics.n} 组配对：成功率 ${(100*e.metrics.baseline.success).toFixed(1)}% → ${(100*e.metrics.candidate.success).toFixed(1)}%；恢复 ${e.metrics.recovered}，退化 ${e.metrics.regressed}。${e.passed?'通过启用门槛':'未通过，保留失败'} ${e.passed&&p.active_id===e.baseline_id?`<button data-activate="${esc(v.id)}" data-evaluation="${esc(e.id)}">启用已验证版本</button>`:''}</p>`).join('')||'<p class="muted">尚无可信配对重放。候选不会自动启用。</p>'}</article>`;
  }).join('');
  $('strategy-runs').innerHTML=p.runs.slice(0,20).map(r=>`<p>${esc(r.job_id.slice(0,8))} · 策略 ${esc(r.version_id.slice(0,12))} · ${esc(r.model)} · ${r.outcome?(r.outcome.success?'通过':'失败'):'尚无终态'}${r.outcome?' · 工具 '+esc(r.outcome.tool_calls)+' 次':''}</p>`).join('')||'升级后开始的研究任务会记录策略归因。';
  $('strategy-events').innerHTML=p.events.map(e=>`<p>${esc(e.created_at)} · ${esc(e.kind)} · ${esc(e.reason)}</p>`).join('');
}
policyDialog.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.propose)await policyAction(async()=>{await api(policyBase()+'/propose','POST',{feedback_id:b.dataset.propose});await loadStrategies();})(e);
  if(b.dataset.activate)await policyAction(async()=>{await api(policyBase()+'/activate','POST',{version_id:b.dataset.activate,evaluation_id:b.dataset.evaluation});await loadStrategies();})(e);
});
