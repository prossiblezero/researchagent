'use strict';
// This notebook shares the selected workspace; opening it never creates or forks a conversation.
const notebook = {space:'', report:null, ideas:[], experiments:[], experiment:null, anchors:[]};
const recordStatus = {draft:'草稿',reviewed:'已审核',published:'已发布',planned:'计划中',completed:'已录入结果',failed:'失败',SUPPORTED:'原文支持已检查',UNVERIFIED:'待核验',INSUFFICIENT:'依据不足',HYPOTHESIS:'待验证假设',HUMAN_REVIEWED:'人工已审核'};
const entityKinds = {paper:'论文',project:'项目',method:'方法',dataset:'数据集',metric:'指标',conclusion:'结论'};
const recordsBase = () => base(notebook.space)+'/research-records';
const options = (items, label, blank=false) => (blank?'<option value="">未选择</option>':'')+items.map(i=>`<option value="${esc(i.id)}">${esc(label(i))}</option>`).join('');
const recordAction = fn => async event => {event?.preventDefault();$('records-error').textContent='';try {await fn(event);}catch(error){$('records-error').textContent=error.message;}};
const currentReportVersion = () => notebook.report?.versions.find(v=>v.id===$('records-version').value);
function notebookTab(name) {
  for (const tab of ['reports','graph','ideas','experiments']) $('records-'+tab).hidden=tab!==name;
}
$('close-research-records').onclick=()=>$('research-records-dialog').close();
$('research-records-dialog').addEventListener('click',event=>{const tab=event.target.closest('[data-record-tab]');if(tab)notebookTab(tab.dataset.recordTab);});
$('open-research-records').onclick=recordAction(async()=>{
  if(!sid)throw new Error('请先选择研究区');
  notebook.space=sid;notebook.report=null;notebook.experiment=null;
  $('records-execution-list').innerHTML='';
  $('records-context').textContent=(spaces.find(s=>s.id===sid)?.name||'当前研究区')+' · 成果在本区会话间共享';
  $('research-records-dialog').showModal();notebookTab('reports');
  await loadNotebook();
});
async function loadNotebook() {
  const [reports,ideas,experiments]=await Promise.all([api(recordsBase()+'/reports'),api(recordsBase()+'/ideas'),api(recordsBase()+'/experiments')]);
  notebook.ideas=ideas.ideas;notebook.experiments=experiments.experiments;
  $('records-report').innerHTML=options(reports.reports,r=>r.title+' · v'+r.latest_version+' · '+recordStatus[r.status]);
  if(reports.reports.length)await loadNotebookReport();else {
    $('records-report-content').textContent='完成研究后，报告版本会保存在这里。';$('records-evidence').innerHTML='';
    $('records-report-status').innerHTML='';$('records-version').innerHTML='';$('records-compare-version').innerHTML='';
    $('records-draft').value='';$('records-report-events').innerHTML='';$('records-report-diff').textContent='';
    $('records-review').disabled=true;$('records-publish').disabled=true;
  }
  renderIdeas();renderExperiments();await loadNotebookGraph();await loadExecutions();
}
async function loadNotebookReport(selected) {
  if(!$('records-report').value)return;
  notebook.report=await api(recordsBase()+'/reports/'+$('records-report').value);
  const versions=notebook.report.versions;
  $('records-version').innerHTML=$('records-compare-version').innerHTML=options(versions,v=>'v'+v.version+' · '+recordStatus[v.status]+' · '+v.created_at);
  $('records-version').value=selected||versions.at(-1).id;
  $('records-compare-version').value=versions[0].id;renderNotebookReport();
}
function evidenceCard(e, sources) {
  const source=sources.find(s=>s.source_id===e.source_id)||{},p=e.provenance||{};
  return `<details><summary>${esc(e.evidence_id)} · ${esc(source.title)} · ${esc(p.page?'第 '+p.page+' 页':p.section||e.kind)}</summary><p><a target="_blank" rel="noopener" href="${esc(safeLink(source.url))}">${esc(source.url)}</a></p><p class="muted">来源类型 ${esc(e.kind)} · 获取 ${esc(e.retrieved_at)} · ${e.truncated?'截断片段':'所存片段'} · ${esc(p.section||'')}</p><blockquote class="plain">${esc(e.content)}</blockquote><p class="material-path">原文哈希 ${esc(e.content_hash)}</p></details>`;
}
function renderNotebookReport() {
  const v=currentReportVersion();if(!v)return;
  const prefix=recordsBase()+'/reports/'+notebook.report.id+'/versions/'+v.id;
  $('records-report-status').innerHTML=`<p><span class="badge">${recordStatus[v.status]} · v${v.version}</span> 文献原文 ${v.coverage.original_cited} · 历史原话 ${v.coverage.history_cited||0} · 自动核验支持 ${v.coverage.supported_claims}/${v.coverage.claims}</p><p class="muted">${esc(v.coverage.read_boundary)}</p><div class="actions"><a href="${prefix}/report.html" target="_blank" rel="noopener">阅读此版本 ↗</a><a href="${prefix}/report.md" download>导出 Markdown</a></div>`;
  $('records-report-content').innerHTML=markdown(v.content);$('records-draft').value=v.content;$('records-draft-note').value='';
  $('records-review').disabled=v.status!=='draft';$('records-publish').disabled=v.status!=='reviewed';$('records-review-attest').checked=false;
  $('records-report-diff').textContent='';
  $('records-evidence').innerHTML=v.snapshot.claims.map(c=>`<article class="source"><span class="badge">${esc(c.claim_id)} · ${esc(recordStatus[c.status]||c.status)}</span><p class="plain">${esc(c.statement)}</p><p class="muted">${esc(c.reason)}</p>${c.evidence_ids.map(id=>{const e=v.snapshot.evidence.find(e=>e.evidence_id===id);return e?evidenceCard(e,v.snapshot.sources):'<p>引用不存在</p>';}).join('')}</article>`).join('');
  $('records-report-events').innerHTML=notebook.report.events.map(e=>`<p>${esc(e.created_at)} · ${esc(e.action)} · ${esc(JSON.parse(e.detail).note||'')}</p>`).join('');
}
$('records-report').onchange=recordAction(()=>loadNotebookReport());$('records-version').onchange=renderNotebookReport;
$('records-compare-report').onclick=recordAction(async()=>{
  if(!notebook.report)return;
  const value=await api(recordsBase()+'/reports/'+notebook.report.id+'/compare?left='+$('records-compare-version').value+'&right='+$('records-version').value);
  $('records-report-diff').textContent=value.diff||'这两个版本的正文相同。';
});
$('records-draft-form').onsubmit=recordAction(async()=>{
  const latest=notebook.report?.versions.at(-1);if(!latest)throw new Error('请选择报告');
  const result=await api(recordsBase()+'/reports/'+notebook.report.id+'/versions','POST',{base_version_id:latest.id,content:$('records-draft').value,note:$('records-draft-note').value});
  await loadNotebookReport(result.version_id);
});
$('records-review-form').onsubmit=recordAction(async event=>{
  const v=currentReportVersion();if(!v)return;
  await api(recordsBase()+'/reports/'+notebook.report.id+'/versions/'+v.id,'PATCH',{status:event.submitter.value,note:$('records-review-note').value});
  await loadNotebookReport(v.id);
});
async function loadNotebookGraph() {
  const graph=await api(recordsBase()+'/graph'),nodes=new Map(graph.nodes.map(n=>[n.id,n]));
  $('records-graph-view').innerHTML=`<p>${graph.nodes.length} 个研究对象 · ${graph.edges.length} 条关系</p><div class="record-table"><table><thead><tr><th>来源对象</th><th>关系</th><th>目标对象</th><th>状态 / 依据</th></tr></thead><tbody>${graph.edges.map(e=>`<tr><td>${esc(nodes.get(e.source_id)?.title)}</td><td>${esc(e.relation)}</td><td>${esc(nodes.get(e.target_id)?.title)}</td><td>${esc(recordStatus[e.status]||e.status)}${e.anchors.map(a=>`<details><summary>${esc(a.claim_id)} / ${esc(a.evidence_id)}</summary><p class="plain">${esc(a.quote)}</p><p><a target="_blank" rel="noopener" href="${esc(safeLink(a.source?.url||''))}">${esc(a.source?.title||'原文')}</a> · ${esc(a.provenance?.page?'第 '+a.provenance.page+' 页':a.provenance?.section||'')}</p></details>`).join('')}</td></tr>`).join('')}</tbody></table></div><details><summary>全部对象</summary>${graph.nodes.map(n=>`<p><span class="badge">${entityKinds[n.kind]}</span> ${esc(n.title)}</p>`).join('')}</details>`;
  $('records-relation-source').innerHTML=$('records-relation-target').innerHTML=options(graph.nodes,n=>entityKinds[n.kind]+' · '+n.title);
  const materials=await api(base(notebook.space)+'/materials');
  $('records-entity-origin').innerHTML=options(materials.materials,m=>m.title,true);
  notebook.anchors=(graph.available_anchors||[]).map((a,index)=>({id:String(index),label:a.report_title+' · '+a.value.claim_id+' / '+a.value.evidence_id+' · 片段 '+a.value.span+' · '+a.quote,value:a.value}));
  $('records-relation-anchors').innerHTML=options(notebook.anchors,a=>a.label);
}
$('records-entity-form').onsubmit=recordAction(async()=>{await api(recordsBase()+'/entities','POST',{kind:$('records-entity-kind').value,title:$('records-entity-title').value,origin:$('records-entity-origin').value?{artifact_id:$('records-entity-origin').value}:{}});await loadNotebookGraph();});
$('records-relation-form').onsubmit=recordAction(async()=>{await api(recordsBase()+'/relations','POST',{source_id:$('records-relation-source').value,target_id:$('records-relation-target').value,relation:$('records-relation-label').value,status:$('records-relation-status').value,anchors:Array.from($('records-relation-anchors').selectedOptions,o=>notebook.anchors[Number(o.value)].value)});await loadNotebookGraph();});
const ideaFields={gap:'研究缺口',hypothesis:'核心假设',method_change:'方法变化',expected_benefit:'预期收益',risks:'风险',validation_plan:'验证方案'};
function renderIdeas(){
  $('records-idea-list').innerHTML=notebook.ideas.map(i=>`<article class="source"><label class="check-label"><input type="checkbox" data-compare-idea="${i.id}"><strong>${esc(i.content.title)}</strong></label><span class="badge">HYPOTHESIS · 尚未实验验证</span>${Object.entries(ideaFields).map(([k,label])=>`<details><summary>${label}</summary><p class="plain">${esc(i.content[k])}</p></details>`).join('')}<details><summary>论文、结论与原文依据</summary>${i.content.anchors.map(a=>`<p><a target="_blank" rel="noopener" href="${esc(safeLink(a.source.url))}">${esc(a.source.title)}</a> · ${esc(a.claim_id)} / ${esc(a.evidence_id)} · ${esc(a.provenance.page?'第 '+a.provenance.page+' 页':a.provenance.section||'')}</p><blockquote class="plain">${esc(a.quote)}</blockquote>`).join('')}</details><p class="muted">${esc(i.content.verification)}</p></article>`).join('')||'<p>暂无创新候选。先加入论文，然后在聊天中提出创新问题。</p>';
}
$('records-compare-ideas').onclick=recordAction(()=>{
  const selected=Array.from($('records-idea-list').querySelectorAll('input:checked'),e=>notebook.ideas.find(i=>i.id===e.dataset.compareIdea));
  if(selected.length<2||selected.length>4)throw new Error('请选择 2–4 个候选');
  $('records-idea-comparison').innerHTML=`<div class="record-table"><table><thead><tr><th>比较项</th>${selected.map(i=>'<th>'+esc(i.content.title)+' · HYPOTHESIS</th>').join('')}</tr></thead><tbody>${Object.entries(ideaFields).map(([key,label])=>'<tr><th>'+label+'</th>'+selected.map(i=>'<td>'+esc(i.content[key])+'</td>').join('')+'</tr>').join('')}</tbody></table></div>`;
});
function experimentOptions(){return notebook.experiments.flatMap(r=>r.versions.map(v=>({id:v.id,label:r.title+' · v'+v.version+' · '+recordStatus[v.content.status],record:r,version:v})));}
function renderExperiments(){
  const versions=experimentOptions();
  $('records-experiment-left').innerHTML=$('records-experiment-right').innerHTML=options(versions,v=>v.label);
  $('records-baseline-version').innerHTML=options(versions.filter(v=>v.record.kind==='baseline'),v=>v.label,true);
  $('records-experiment-idea').innerHTML=options(notebook.ideas,i=>i.content.title,true);
  $('records-experiment-list').innerHTML=notebook.experiments.map(r=>`<article class="source"><h3>${esc(r.title)} · ${r.kind==='baseline'?'基线':'实验方案'}</h3><div class="actions">${r.versions.map(v=>`<button data-edit-experiment="${r.id}" data-experiment-version="${v.id}">v${v.version} · ${esc(v.content.executed_by_workbench?'工作台实测':recordStatus[v.content.status])}</button><a href="${recordsBase()}/experiment-versions/${v.id}/handoff" download="experiment-handoff.json">导出 v${v.version} 交接</a>${r.kind==='experiment'&&v.content.status==='planned'&&['public-retrieval-v1','public-retrieval-feedback-v2','public-evidence-rerank-v1'].includes(v.content.config.dataset)?`<button data-run-experiment="${v.id}">运行实验 v${v.version}</button>`:''}`).join('')}</div></article>`).join('')||'<p>尚未保存基线或实验配置。</p>';
}
function metricRow(m={name:'',unit:'',direction:'higher',target_mode:'absolute',target:''},result=''){
  return `<tr><td><input data-metric="name" value="${esc(m.name)}" required aria-label="指标名称"></td><td><input data-metric="unit" value="${esc(m.unit)}" required aria-label="指标单位"></td><td><select data-metric="direction" aria-label="指标方向"><option value="higher" ${m.direction==='higher'?'selected':''}>越高越好</option><option value="lower" ${m.direction==='lower'?'selected':''}>越低越好</option></select></td><td><select data-metric="target_mode" aria-label="目标口径">${[['absolute','绝对值'],['delta','改善差值'],['relative_percent','相对改善 %']].map(([v,l])=>`<option value="${v}" ${m.target_mode===v?'selected':''}>${l}</option>`).join('')}</select></td><td><input data-metric="target" type="number" step="any" value="${esc(m.target)}" required aria-label="目标值"></td><td><input data-metric="result" type="number" step="any" value="${esc(result)}" aria-label="测量结果"></td><td><button type="button" data-remove-metric aria-label="删除指标">×</button></td></tr>`;
}
const configInputs={dataset:'records-dataset',dataset_version:'records-dataset-version',split:'records-split',split_hash:'records-split-hash',code_revision:'records-code-revision',command:'records-command',environment:'records-environment'};
function editExperiment(record=null,version=null){
  notebook.experiment=record;
  $('records-experiment-editor').open=true;$('records-experiment-editor-title').textContent=record?'查看 v'+version.version+'，保存将创建新版本':'新建基线 / 实验';
  $('records-experiment-title').value=record?.title||'';$('records-experiment-kind').value=record?.kind||'baseline';$('records-experiment-kind').disabled=!!record;
  $('records-experiment-idea').value=record?.idea_id||'';$('records-experiment-idea').disabled=!!record;
  $('records-baseline-version').value=version?.baseline_version_id||'';
  const c=version?.content;
  for(const [key,id]of Object.entries(configInputs))$(id).value=c?.config[key]||(key==='split'?'validation':'');
  $('records-seeds').value=(c?.config.seeds||[42]).join(',');$('records-parameters').value=JSON.stringify(c?.config.parameters||{},null,2);
  $('records-metric-rows').innerHTML=c?c.metrics.map(m=>metricRow(m,c.results[m.name]??'')).join(''):metricRow();
  $('records-experiment-status').value=c?.status||'planned';$('records-result-source').value=c?.result_source||'';$('records-experiment-note').value='';
}
$('records-new-experiment').onclick=()=>editExperiment();
$('records-experiment-list').onclick=recordAction(async event=>{
  const b=event.target.closest('[data-edit-experiment]');if(b){const r=notebook.experiments.find(r=>r.id===b.dataset.editExperiment);editExperiment(r,r.versions.find(v=>v.id===b.dataset.experimentVersion));return;}
  const run=event.target.closest('[data-run-experiment]');if(!run)return;
  if(!cid||sid!==notebook.space)throw new Error('请先选择当前研究区的会话');
  run.disabled=true;
  try {await api(recordsBase()+'/experiment-versions/'+run.dataset.runExperiment+'/execute','POST',{
    conversation_id:cid,authorize_execution:true,seconds:Number($('records-execution-seconds').value),token_budget:Number($('records-execution-tokens').value),max_iterations:Number($('records-execution-rounds').value||1)});await loadExecutions();}
  finally {run.disabled=false;}
});
$('records-execution-plan').onsubmit=recordAction(async()=>{
  await api(recordsBase()+'/execution-plans','POST',{title:$('records-execution-title').value,hypothesis:$('records-execution-hypothesis').value,suite:$('records-execution-suite').value});
  notebook.experiments=(await api(recordsBase()+'/experiments')).experiments;renderExperiments();
});
function iterationSummary(run,endpoint){
  const it=run.state.iteration;if(!it)return '';
  const reasons={development_target_met:'开发目标已达到',no_improvement:'连续两轮无改善',max_iterations:'已达轮次上限',budget_exhausted:'编码总预算已耗尽',usage_incomplete:'用量不完整，停止追加调用',coding_interrupted:'编码失败，保留最佳版本'};
  const metric=value=>typeof value==='number'?value.toFixed(2)+'%':'—';
  const rounds=it.rounds.map(row=>`<tr><td>第 ${row.index} 轮</td><td>${metric(row.before?.recall_at5)} → ${metric(row.after?.recall_at5)}</td><td>${row.improved?'更新最佳':'保留此前最佳'}</td><td>${esc(row.status)}</td></tr>`).join('');
  const links=it.rounds.map(row=>`<details><summary>第 ${row.index} 轮记录</summary><div class="actions">${Object.values(row.artifacts||{}).map(name=>`<a href="${endpoint}/executions/${run.job_id}/artifacts/${esc(name)}" target="_blank" rel="noopener">${esc(name)}</a>`).join('')}</div></details>`).join('');
  return `<p>编码 ${it.totals?.coding_invocations||0}/${it.max_iterations} 轮 · ${esc(reasons[it.stop_reason]||it.stop_reason||'进行中')} · 已报告 ${esc(it.totals?.tokens??0)} token${it.totals?.complete===false?'（计量不完整）':''}</p><table><thead><tr><th>轮次</th><th>开发 Recall@5</th><th>选优结果</th><th>状态</th></tr></thead><tbody>${rounds}</tbody></table><p>最佳版本：${esc(it.best?.label||'baseline')} · <a href="${endpoint}/executions/${run.job_id}/artifacts/iterations.json" target="_blank" rel="noopener">完整迭代记录</a></p>${links}`;
}
async function loadExecutions(){
  const space=notebook.space,endpoint=recordsBase(),data=await api(endpoint+'/executions');
  if(space!==notebook.space)return;
  $('records-execution-list').innerHTML=data.executions.map(r=>`<article class="source"><h3>${esc(r.job.question)} · ${esc(states[r.job.status])}</h3><p>${esc(r.job.recent_action||r.state.phase)}</p>${r.state.failure?`<p>${esc(r.state.failure.error)}</p>`:''}${iterationSummary(r,endpoint)}<div class="actions">${['contract.json','baseline.json','candidate.json','codex.json','events.jsonl','diff.patch','result.json','failure.json',...(r.contract.suite!=='public-retrieval-v1'?['dev-feedback.json','proposal.md','development-snapshots.json','submitted-ranker.py']:[]),...(r.contract.suite==='public-evidence-rerank-v1'?['evidence-rerank.json']:[])].map(name=>`<a href="${endpoint}/executions/${r.job_id}/artifacts/${name}" target="_blank" rel="noopener">${esc(name)}</a>`).join('')}${['queued','running'].includes(r.job.status)?`<button data-execution-action="cancel" data-execution-job="${r.job_id}">停止实验</button>`:''}${['failed','interrupted'].includes(r.job.status)?`<button data-execution-action="resume" data-execution-job="${r.job_id}">从检查点继续</button>`:''}${['failed','interrupted','cancelled'].includes(r.job.status)?`<button data-execution-action="retry" data-execution-job="${r.job_id}">新尝试（重新计预算）</button>`:''}</div></article>`).join('')||'<p>还没有执行记录。</p>';
}
$('records-execution-refresh').onclick=recordAction(async()=>{await loadExecutions();notebook.experiments=(await api(recordsBase()+'/experiments')).experiments;renderExperiments();});
$('records-execution-list').onclick=recordAction(async event=>{
  const b=event.target.closest('[data-execution-action]');if(!b)return;
  await api(base(notebook.space)+'/jobs/'+b.dataset.executionJob+'/'+b.dataset.executionAction,'POST',{});await loadExecutions();
});
$('records-add-metric').onclick=()=>$('records-metric-rows').insertAdjacentHTML('beforeend',metricRow());
$('records-metric-rows').onclick=event=>{if(event.target.closest('[data-remove-metric]'))event.target.closest('tr').remove();};
$('records-experiment-form').onsubmit=recordAction(async()=>{
  const config=Object.fromEntries(Object.entries(configInputs).map(([k,id])=>[k,$(id).value]));
  const seeds=$('records-seeds').value.split(',').map(s=>s.trim());if(seeds.some(s=>!/^\d+$/.test(s)))throw new Error('随机种子请用逗号分隔的整数');
  config.seeds=seeds.map(Number);config.parameters=JSON.parse($('records-parameters').value);
  const results={},metrics=Array.from($('records-metric-rows').querySelectorAll('tr'),row=>{
    const m=Object.fromEntries(Array.from(row.querySelectorAll('[data-metric]'),e=>[e.dataset.metric,e.value]));
    if(m.result!=='')results[m.name]=Number(m.result);delete m.result;m.target=Number(m.target);return m;
  });
  const r=notebook.experiment,body={kind:$('records-experiment-kind').value,title:$('records-experiment-title').value,idea_id:$('records-experiment-idea').value||null,
    base_version_id:r?.versions.at(-1).id,baseline_version_id:$('records-baseline-version').value||null,config,metrics,results,status:$('records-experiment-status').value,result_source:$('records-result-source').value,note:$('records-experiment-note').value};
  const saved=await api(recordsBase()+'/experiments'+(r?'/'+r.id+'/versions':''),'POST',body);
  notebook.experiments=(await api(recordsBase()+'/experiments')).experiments;renderExperiments();editExperiment(saved,saved.versions.at(-1));
});
$('records-compare-experiments').onclick=recordAction(async()=>{
  const c=await api(recordsBase()+'/experiment-comparison?left='+$('records-experiment-left').value+'&right='+$('records-experiment-right').value);
  const outcomes={incomparable:'口径不一致，不判定达标',failed:'实验失败',pending:'尚无完整测量结果',comparison_only:'仅比较；未选择此实验固定的基线',undetermined:'无法判定（例如基线为零时的相对提升）',met:'达到录入的目标',not_met:'尚未达到全部目标'};
  $('records-experiment-comparison').innerHTML=`<h3>${outcomes[c.outcome]}</h3><p class="muted">${esc(c.result_origin)}</p>${c.mismatches.length?'<p>口径差异：'+esc(c.mismatches.join('、'))+'</p>':''}<div class="record-table"><table><thead><tr><th>指标</th><th>对照值</th><th>目标版本值</th><th>目标要求</th><th>改善差值</th><th>相对改善 %</th><th>达到目标</th></tr></thead><tbody>${c.metrics.map(m=>`<tr><td>${esc(m.name)} (${esc(m.unit)})</td>${[m.baseline,m.value,({absolute:'绝对值',delta:'改善差值',relative_percent:'相对改善 %'}[m.target_mode]||m.target_mode)+' '+m.target,m.improvement,m.relative_percent,m.target_met===null?null:m.target_met?'是':'否'].map(v=>'<td>'+esc(v??'—')+'</td>').join('')}</tr>`).join('')}</tbody></table></div><details><summary>配置与指标目标变化</summary><pre>${esc(JSON.stringify({config:c.config_changes,metrics:c.metric_changes},null,2))}</pre></details>`;
});
