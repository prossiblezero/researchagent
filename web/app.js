'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const states = {queued:'排队中',running:'研究中',completed:'已完成',failed:'未完成',cancelled:'已取消',interrupted:'已中断'};
const autoOutcomes = {reported:'报告已交付',goal_met:'数值目标已有实测支持',blocked:'研究受阻，尚未达标',budget_exhausted:'预算耗尽，尚未达标'};
const jobStatus = job => job.kind==='AUTO_RESEARCH' && job.status==='completed' ? '已结束' : states[job.status] || job.status;
const stages = {downloading:'下载资料',extracting:'提取正文',analyzing:'分析资料',indexing:'建立索引',selecting:'选择论文',retrieving:'查找本地证据',answering:'组织本地回答',reading:'阅读资料',queued:'等待开始',planning:'准备简报',thinking:'分析资料',search:'检索来源',read:'阅读资料',research:'整理证据',verifying:'核验结论',reporting:'生成报告',auto_deciding:'选择下一项研究动作',auto_waiting:'等待调研或实验结果'};
const efforts = {quick:['快速核实',8],standard:['标准研究',16],deep:['深入研究',24],legacy:['历史任务',6]};
stages.preparing_project='准备源码、数据与依赖';
let spaces = [], sid = '', cid = '', jid = '', editing = false, editingId = '', sending = false, initialized = false;
let epoch = 0, selection = 0, eventCursor = 0, runLoaded = '', activeTab = 'trace', networkFailed = false;
let models = [];
let conversationEdit = null, branchJobs = [], memoryItems = [];
let reportText = '', composing = false;
const messageText = new Map(), renderedHTML = new WeakMap();
const drafts = new Map();
function rememberDraft() {if(sid && cid)drafts.set(sid+'/'+cid,$('content').value || '');}
function selectBranch(next) {if(next!==cid){rememberDraft();cid=next;$('content').value=drafts.get(sid+'/'+cid) || '';}}
let liveConnection = null, liveKey = '', liveCursor = 0;
const liveJobs = new Map();
const base = (space = sid) => '/api/spaces/' + space;
const jobBase = (id = jid, space = sid) => base(space) + '/jobs/' + id;
const busy = status => ['queued', 'running'].includes(status);
const safeLink = value => {if (/^\/api\/spaces\/[a-f0-9]{32}\/(materials\/[a-f0-9]{32}\/(reader|original)(#local-L\d+|#page=\d+)?|(history|memory)\/[a-f0-9]{32}|jobs\/[a-f0-9]{32}\/report\.(html|md))$/.test(value)) return value;try {const u = new URL(value);return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password ? u.href : '#';} catch {return '#';}};
function updateHTML(el, markup) {
  const previous = renderedHTML.get(el);
  // Native disclosure toggles are reader state, not a changed rendering.
  const stable = html => html.replace(/(<details\b[^>]*?) open=""/g, '$1');
  // Compare source with source: DOM serialization normalizes quotes and entities.
  if (previous?.source === markup && previous.dom === stable(el.innerHTML)) return;
  el.innerHTML = markup; renderedHTML.set(el, {source:markup, dom:stable(el.innerHTML)});
}
function updateText(el, value) {if (el.textContent !== value) el.textContent = value;}
async function copyText(value) {
  if (!value) throw new Error('还没有可以复制的内容。');
  try {await navigator.clipboard.writeText(value);}
  catch {
    const focused = document.activeElement, selection = window.getSelection();
    const ranges = Array.from({length:selection.rangeCount}, (_, i) => selection.getRangeAt(i).cloneRange());
    const field = document.createElement('textarea'); field.value = value; field.className = 'clipboard-fallback';
    document.body.appendChild(field); field.select();
    try {if (!document.execCommand('copy')) throw new Error('复制失败，请选中文本后按 Ctrl+C。');}
    finally {field.remove(); focused?.focus({preventScroll:true});selection.removeAllRanges();ranges.forEach(range => selection.addRange(range));}
  }
  $('copy-status').textContent = '已复制';
  setTimeout(() => {$('copy-status').textContent = '';}, 1800);
}
function renderMessages(messages) {
  const parent = $('history-messages'); messageText.clear();
  if (!messages.length) {updateHTML(parent, '<div class="empty"><h3>你想深入了解什么？</h3><p>聊聊你的想法，或发起一项研究。<br>对话和研究结果都会保存在这里。</p></div>');return;}
  const existing = new Map(Array.from(parent.children, node => [node.dataset.messageId, node]));
  for (const m of messages) {
    const id = String(m.id); messageText.set(id, m.content);
    let article = existing.get(id);
    if (!article) {article = document.createElement('article'); article.dataset.messageId = id;parent.appendChild(article);}
    existing.delete(id);
    article.className = 'message' + (m.role === 'user' ? ' user' : '');
    updateHTML(article, `<div class="message-meta"><span>${m.role === 'user' ? '你' : 'ResearchAgent' + (m.model_name ? ' · ' + esc(m.model_name) : '')}</span><time>${esc(new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}))}</time></div><div class="message-content ${m.role === 'user' ? 'plain' : 'markdown'}" tabindex="0">${m.role === 'user' ? esc(m.content) : markdown(m.content)}</div><div class="message-actions"><button type="button" class="link" data-copy-message="${esc(id)}" aria-label="${m.role === 'user' ? '复制这条提问' : '复制这条回答'}">复制</button>${m.job_id ? `<button class="link" data-job="${esc(m.job_id)}">查看研究任务 ↗</button>` : ''}${m.intent !== 'PENDING' ? `<button class="link" data-fork-message="${esc(id)}">从此处分支</button>` : ''}${m.role === 'user' ? `<button class="link" data-delete-turn="${esc(id)}">删除这一轮</button>` : ''}</div>`);
  }
  existing.forEach(node => node.remove());
}
function showError(error) {$('error').textContent = error.message || String(error);}

const modelActivities = {routing:'理解问题与选择处理方式',answer:'生成下一步动作或回答',answer_verification:'核验原文、引用与回答完整性',answer_repair:'修订未通过的段落',compression:'压缩研究上下文',session_compression:'整理会话记忆',material_analysis:'分析资料'};
function applyLiveEvent(state, event) {
  const p = event.payload || {}, kind = event.kind;
  if (kind === 'model_stream') {
    if (p.phase === 'start') {
      state.request = p.request_id;state.started = Date.parse(event.ts) || Date.now();state.ended = 0;state.purpose = p.purpose;state.characters = 0;
      state.model = p.model;state.modelStatus = (modelActivities[p.purpose] || '处理资料') + ' · 等待模型响应';
      if (['answer','routing'].includes(p.purpose)) state.text = '';
    }
    if (p.request_id !== state.request) return;
    if (p.phase === 'update') {
      state.characters = p.characters || 0;
      state.modelStatus = (modelActivities[p.purpose] || '处理资料') + (p.reasoning_active ? ' · 模型正在推理' : p.streaming ? ' · 正在接收输出' : ' · 服务商返回完整响应');
      if (['answer','routing'].includes(p.purpose)) state.text = p.tool_names?.length ? '' : p.text || '';
      if (p.tool_names?.length) state.modelStatus = '正在准备工具调用：' + p.tool_names.join('、');
    }
    if (p.phase === 'end') {state.ended = Date.parse(event.ts) || Date.now();state.modelStatus = (modelActivities[p.purpose] || '模型处理') + ' · 本次输出完成';}
    if (p.phase === 'retry') {state.text = '';state.modelStatus = '模型连接中断，准备重试';}
    if (p.phase === 'error') {state.text = '';state.modelStatus = '模型请求未完成，正在保存任务状态';}
    state.phase = p.phase;
  } else if (kind === 'answer_draft') {
    state.text = p.text || '';state.action = '草稿已生成，正在核验；正式结果可能调整';
  } else {
    const descriptions = {model_request:'分析已有资料，决定下一步',answer_check_started:'核验已保存草稿与原文证据',answer_repair:'根据核验结果修订回答',answer_checked:'本轮核验结束，检查是否需要补充证据',answer_check_unavailable:'核验暂时不可用，保存待核验草稿',run_finished:'整理引用并保存结果'};
    let action = descriptions[kind];
    if (kind === 'tool_call_requested') action = ({search:'搜索来源',read:'阅读原文',retrieve:'检索本地资料',recall:'检索会话记忆'}[p.name] || '调用工具 '+p.name) + '：' + (p.args?.query || p.args?.url || p.args?.artifact_id || '按当前问题查找证据');
    if (kind === 'tool_result') action = (p.ok ? '工具已返回' : '工具调用失败') + (p.title ? '：'+p.title : '');
    if (kind === 'auto_tool_call') action = ({research:'定向调研',save_plan:'保存研究方案',prepare_project:'准备源码、数据与依赖',coding_agent:'委派编码与实验',run_experiments:'运行已有代码的实验',inspect:'查看原文或实验产物',finish_research:'整理研究报告'}[p.name] || p.name);
    if (kind === 'auto_tool_result') action = '收到'+(p.tool_kind==='coding' ? '编码与实验' : '调研')+'结果，准备判断下一步';
    if (kind === 'auto_error') action = p.error || '自动研究未完成，记录已保留';
    if (kind === 'project_preparation') action = p.message;
    if (kind === 'material_progress') action = p.message;
    if (kind === 'model_error' || kind === 'material_failed') action = p.message || '本次处理未完成';
    if (action) {
      state.action = action;state.steps = [...(state.steps || []), action].slice(-8);
    }
  }
}
function renderLive() {
  $('stop-generation').classList.toggle('hidden', !liveJobs.size && !branchJobs.length);
  document.querySelector('.conversation').classList.toggle('is-streaming',liveJobs.size > 0);
  const pane = $('messages'), nearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 110;
  const parent = $('chat-live');
  for (const node of Array.from(parent.children)) if (!liveJobs.has(node.dataset.liveJob)) node.remove();
  for (const [id,state] of liveJobs) {
    let card = parent.querySelector(`[data-live-job="${id}"]`);
    if (!card) {
      card = document.createElement('article');card.className = 'message live-message';card.dataset.liveJob = id;
      card.innerHTML = `<div class="live-status"><div class="message-meta"><strong>ResearchAgent · 实时进度</strong><span class="live-connection"></span></div><p class="live-agent" role="status"></p><p class="live-model"></p><p class="live-meter muted"></p></div><details class="live-steps"><summary>最近的执行过程</summary><ol></ol></details><div class="live-draft"><p class="draft-label">回答草稿 · 尚未核验，可能修订</p><div class="live-text markdown" tabindex="0"></div></div><div class="message-actions"><button class="link" data-copy-stream="${esc(id)}">复制当前草稿</button><button class="link" data-action="cancel" data-id="${esc(id)}">取消任务</button></div>`;
      parent.appendChild(card);
    }
    updateText(card.querySelector('.live-connection'), state.connection || '实时连接中');
    updateText(card.querySelector('.live-agent'), 'Agent：' + (state.action || '消息已收到，等待后台开始'));
    updateText(card.querySelector('.live-model'), 'LLM：' + (state.modelStatus || '尚未开始模型调用'));
    const elapsed = state.started ? Math.max(0,Math.floor(((state.ended || Date.now())-state.started)/1000)) : 0;
    updateText(card.querySelector('.live-meter'), (state.model || state.model_name || '') + (state.started ? ` · 本次请求 ${elapsed} 秒 · 已收到 ${state.characters || 0} 个字符` : ''));
    updateHTML(card.querySelector('ol'), (state.steps || []).map(s=>'<li>'+esc(s)+'</li>').join(''));
    card.querySelector('.live-draft').classList.toggle('hidden', !state.text);
    updateHTML(card.querySelector('.live-text'), markdown(state.text || ''));
  }
  if (nearBottom) pane.scrollTop = pane.scrollHeight;
}
function connectLive() {
  const key = sid && cid ? sid+'/'+cid : '';
  if (key === liveKey) return;
  liveConnection?.close();liveConnection = null;liveKey = key;liveCursor = 0;liveJobs.clear();$('chat-live').replaceChildren();
  if (!key || typeof EventSource === 'undefined') return;
  const source = new EventSource(base()+'/conversations/'+cid+'/events');liveConnection = source;
  source.addEventListener('jobs', event => {
    if (liveKey !== key || liveConnection !== source) return;
    const jobs = JSON.parse(event.data).jobs, active = new Set(jobs.map(j=>j.id));
    for (const id of liveJobs.keys()) if (!active.has(id)) liveJobs.delete(id);
    for (const job of jobs) {
      const state = liveJobs.get(job.id) || {steps:[],text:''};
      Object.assign(state,job);state.action ||= job.recent_action || '等待后台处理';state.connection = '● 实时连接';liveJobs.set(job.id,state);
    }
    renderLive();
    refresh().catch(showError);
  });
  source.addEventListener('progress', event => {
    if (liveKey !== key || liveConnection !== source || Number(event.lastEventId) <= liveCursor) return;
    liveCursor = Number(event.lastEventId);
    const data = JSON.parse(event.data), state = liveJobs.get(data.job_id);
    if (state) {applyLiveEvent(state,data);renderLive();}
  });
  source.addEventListener('cursor', event => {if (liveKey === key && liveConnection === source) liveCursor = Math.max(liveCursor,Number(event.lastEventId));});
  source.onopen = () => {if(liveKey !== key || liveConnection !== source)return;for (const s of liveJobs.values()) s.connection = '● 实时连接';renderLive();};
  source.onerror = () => {if(liveKey !== key || liveConnection !== source)return;for (const s of liveJobs.values()) s.connection = '连接中断，正在恢复进度…';renderLive();};
}
function connected(ok) {
  $('connection').textContent = ok ? '服务已连接' : '正在重连';
  $('connection').className = 'badge ' + (ok ? 'live' : 'warning');
  if (ok && networkFailed) {$('error').textContent = ''; networkFailed = false;}
}
async function api(path, method = 'GET', body) {
  let response;
  try {response = await fetch(path, {method, headers: {'Content-Type':'application/json'}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});}
  catch {
    networkFailed = true; connected(false);
    throw new Error(method === 'GET' ? '暂时无法连接研究服务，正在自动重连。请确认本机服务仍在运行。' : '与研究服务的连接中断。请求可能已被接收，请恢复连接后先查看记录，再决定是否重试。');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败，请稍后重试');
  return data;
}

function updateModelChoice() {
  const model = models.find(item => item.id === $('model-select').value);
  $('model-select').disabled = sending || !models.length;
  $('send').disabled = sending || !model?.configured;
  $('model-hint').textContent = model?.configured ? '用于下一条消息和由它启动的研究' : '当前模型未配置，请填写 .env 后重启服务并刷新页面，或选择已配置的模型。';
}
async function loadModels() {
  const data = await api('/api/models'); models = data.models;
  const saved = localStorage.getItem('research-model');
  const selected = models.find(model => model.id === saved)?.id || data.default_model_id;
  $('model-select').innerHTML = models.map(model => `<option value="${esc(model.id)}" ${model.configured ? '' : 'disabled'}>${esc(model.label)}${model.configured ? '' : '（未配置）'}</option>`).join('');
  $('model-select').value = selected; updateModelChoice();
}
$('model-select').onchange = () => {localStorage.setItem('research-model', $('model-select').value);updateModelChoice();};

// Small, escaped Markdown subset. Raw HTML, images and executable links never render.
function inline(value) {
  const tokens = [];
  let text = String(value).replace(/\0/g, '').replace(/`([^`]+)`|\[([^\]]+)\]\(((?:https?:\/\/|\/api\/spaces\/)[^\s)]+)\)/g, (_, code, label, url) => {
    const markup = code !== undefined ? '<code>' + esc(code) + '</code>' : '<a href="' + esc(safeLink(url)) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + '</a>';
    return '\0' + (tokens.push(markup) - 1) + '\0';
  });
  return esc(text).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/\0(\d+)\0/g, (_, i) => tokens[Number(i)]);
}
function codeBlock(code, language = '') {
  let content = esc(code), label = language || '代码';
  // Large or unsupported snippets remain readable plain text; no dynamic loading.
  if (typeof hljs !== 'undefined' && code.length <= 20000) {
    try {
      const known = language && hljs.getLanguage(language);
      const result = known ? hljs.highlight(code, {language, ignoreIllegals:true}) : !language ? hljs.highlightAuto(code, ['python','javascript','json','bash','sql']) : null;
      if (result) {content = result.value; label = hljs.getLanguage(result.language)?.name || label;}
    } catch { /* A highlighting failure must not hide code or break the conversation. */ }
  }
  return `<div class="code-block"><div class="code-label">${esc(label)}</div><pre tabindex="0" aria-label="${esc(label)}代码"><code>${content}</code></pre></div>`;
}
function markdown(value, depth = 0, softBreaks = false) {
  if (depth > 12) return '<pre>' + esc(value) + '</pre>';
  const lines = String(value || '').replace(/\r/g, '').split('\n'), out = [];
  let evidenceLevel = 0;
  const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(x => x.trim());
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) {i++;continue;}
    const fence = line.match(/^\s*(`{3,}|~{3,})([^\s]*)/);
    if (fence) {
      const block = []; i++;
      const closing = new RegExp('^\\s*' + fence[1][0] + '{' + fence[1].length + ',}\\s*$');
      while (i < lines.length && !closing.test(lines[i])) block.push(lines[i++]);
      if (i < lines.length) i++;
      out.push(codeBlock(block.join('\n'), fence[2].toLowerCase())); continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)/);
    if (heading) {
      const level = heading[1].length;
      if (evidenceLevel && level <= evidenceLevel) evidenceLevel = 0;
      if (heading[2].trim() === '本地证据') evidenceLevel = level;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);i++;continue;
    }
    const location = evidenceLevel && line.match(/^\s*文件[：:]\s*(.+)$/);
    if (location) {
      out.push('<details class="evidence-location"><summary>文件位置</summary><p class="evidence-path">' + esc(location[1]) + '</p></details>');i++;continue;
    }
    if (i + 1 < lines.length && line.includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1]) && cells(lines[i+1]).every(c => /^:?-{3,}:?$/.test(c))) {
      let table = '<div class="table-wrap"><table><thead><tr>' + cells(line).map(c => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>';i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) table += '<tr>' + cells(lines[i++]).map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>';
      out.push(table + '</tbody></table></div>');continue;
    }
    const list = line.match(/^\s*([-*+] |\d+[.)] )(.+)/);
    if (list) {
      const ordered = /^\d/.test(list[1]), tag = ordered ? 'ol' : 'ul'; let block = '';
      while (i < lines.length) {const m = lines[i].match(/^\s*([-*+] |\d+[.)] )(.+)/);if (!m || /^\d/.test(m[1]) !== ordered) break;block += '<li>' + inline(m[2]) + '</li>';i++;}
      out.push(`<${tag}>${block}</${tag}>`);continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^>/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ''));
      // PDF line wraps are soft breaks; quoted blank lines still separate paragraphs.
      const block = '<blockquote>' + markdown(quote.join('\n'), depth + 1, true) + '</blockquote>';
      out.push(evidenceLevel ? '<details class="evidence-excerpt"><summary>查看原文摘录</summary>' + block + '</details>' : block);continue;
    }
    if (/^\s*---+\s*$/.test(line)) {out.push('<hr>');i++;continue;}
    const paragraph = [line];i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*(?:`{3,}|~{3,})|\s*[-*+] |\s*\d+[.)] |>|\s*---)/.test(lines[i]) && !(lines[i].includes('|') && i+1<lines.length && /-{3}/.test(lines[i+1]))) paragraph.push(lines[i++]);
    out.push('<p>' + paragraph.map(inline).join(softBreaks ? ' ' : '<br>') + '</p>');
  }
  return out.join('');
}

function selectTab(tab) {
  activeTab = tab;
  for (const name of ['report', 'sources', 'trace']) {
    $('tab-' + name).setAttribute('aria-selected', String(name === tab));
    $('tab-' + name).tabIndex = name === tab ? 0 : -1;
    $('pane-' + name).classList.toggle('hidden', name !== tab);
  }
}
function resetJob(id = '', tab = 'trace') {
  jid = id; selection++; eventCursor = 0; runLoaded = ''; reportText = '';
  $('events').replaceChildren(); $('event-count').textContent = '0';
  $('detail').classList.add('hidden'); $('no-job').classList.remove('hidden');
  $('sources').innerHTML = '<p class="empty">研究结束后展示来源与证据；当前调用请查看实时过程。</p>';
  $('trace-empty').classList.remove('hidden'); selectTab(tab);
}
async function loadSpaces(preferred) {
  rememberDraft();
  const token = ++epoch;
  const data = await api('/api/spaces'); if (token !== epoch) return;
  spaces = data.spaces;
  const selected = spaces.find(s => s.id === preferred) || spaces.find(s => s.id === localStorage.getItem('research-space')) || spaces[0];
  sid = selected?.id || ''; cid = ''; branchJobs=[]; resetJob();
  $('spaces').innerHTML = spaces.map(s => `<button class="space ${s.id === sid ? 'active' : ''}" data-space="${esc(s.id)}"><strong>${esc(s.name)}</strong><span class="muted">${esc(s.description.slice(0, 42) || '持续探索，积累证据')}</span></button>`).join('');
  $('open-library').disabled = !sid; $('work').classList.toggle('hidden', !sid); $('welcome').classList.toggle('hidden', !!sid); $('edit-space').classList.toggle('hidden', !sid);
  $('space-title').textContent = selected?.name || '开始一段有据可循的探索';
  $('space-description').textContent = selected?.description || '围绕一个方向，保存对话、研究过程与结论。';
  $('jobs').replaceChildren(); $('history-messages').replaceChildren(); connectLive(); $('job-count').textContent = '0';
  if (sid) {localStorage.setItem('research-space', sid); await loadConversations(); if (token === epoch) {await refresh();await loadLibrary();}}
}
async function loadConversations(preferred) {
  const space = sid, token = epoch, data = await api(base(space) + '/conversations');
  const available=data.conversations.filter(c=>!c.archived_at);
  const main = spaces.find(s=>s.id===space)?.main_branch_id || data.conversations.find(c=>!c.parent_id)?.id;
  if (space !== sid || token !== epoch) return;
  selectBranch((available.find(c => c.id === preferred) || available.find(c => c.id === localStorage.getItem('conversation-' + space)) || available.find(c=>c.id===main) || available[0])?.id || '');
  $('conversations').innerHTML = available.map(c => `<option value="${esc(c.id)}">${c.parent_id ? '分支 · ' : ''}${esc(c.title)}</option>`).join('');
  $('conversations').value = cid; if (cid) localStorage.setItem('conversation-' + space, cid);
  $('branch-picker').classList.toggle('hidden',available.length<2);
  $('delete-branch').disabled=!cid;
  $('archive-conversation').disabled=!cid;
  const current=available.find(c=>c.id===cid), parent=data.conversations.find(c=>c.id===current?.parent_id);
  $('conversation-origin').textContent=current?.parent_id ? `从「${parent?.title || '历史会话'}」继承到所选消息，后续讨论独立保存。` : '独立会话 · 共享本区资料与 Notes，其他会话的历史按需查阅。';
  branchJobs=[];connectLive();
}
async function refresh() {
  if (!sid) {await api('/health');return;}
  const space = sid, conversation = cid, token = epoch;
  let history,jobs;
  try { [history,jobs] = await Promise.all([conversation ? api(base(space) + '/conversations/' + conversation + '/messages') : {messages:[]}, api(base(space) + '/jobs')]); }
  catch(error) {if(token!==epoch || space!==sid || conversation!==cid)return;throw error;}
  if (token !== epoch || space !== sid || conversation !== cid) return;
  const el = $('messages'), atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
  branchJobs=jobs.jobs.filter(j=>j.conversation_id===conversation && busy(j.status));
  renderMessages(history.messages);
  connectLive();renderLive();
  if (atBottom) el.scrollTop = el.scrollHeight;
  if (jid && !jobs.jobs.some(j=>j.id===jid)) resetJob();
  if (!jid && jobs.jobs.length) resetJob(jobs.jobs[0].id, busy(jobs.jobs[0].status) ? 'trace' : 'report');
  $('job-count').textContent = jobs.jobs.length;
  updateHTML($('jobs'), jobs.jobs.map(j => `<button class="job ${j.id === jid ? 'active' : ''}" data-job="${esc(j.id)}"><span class="badge ${j.status === 'running' ? 'live' : ['failed','interrupted'].includes(j.status) ? 'warning' : ''}">${esc(jobStatus(j))}</span><strong>${esc(j.question)}</strong><span class="muted">${esc(j.search_scope === 'arxiv' ? 'arXiv · ' : '')}${esc(stages[j.stage] || jobStatus(j))}</span></button>`).join('') || '<p class="empty">还没有研究任务</p>');
  if (jid) await loadJob(jid, space);
  if ($('library-dialog').open) await loadLibrary();
}
async function loadJob(id, space) {
  const version = selection, job = await api(jobBase(id, space));
  if (space !== sid || id !== jid || version !== selection) return;
  $('no-job').classList.add('hidden'); $('detail').classList.remove('hidden');
  $('job-title').textContent = job.question; $('job-state').textContent = jobStatus(job);
  $('job-state').className = 'badge ' + (job.status === 'running' ? 'live' : ['failed','interrupted'].includes(job.status) ? 'warning' : '');
  const effort = efforts[job.research_effort] || efforts.legacy; const local = job.kind && job.kind !== 'RESEARCH';
  $('job-meta').textContent = (job.search_scope === 'arxiv' ? 'arXiv 论文' : job.search_scope === 'auto' ? '自动选择来源' : '综合网络检索') + ' · ' + effort[0] + ' · ' + new Date(job.created_at).toLocaleString();
  if (job.model_name) $('job-meta').textContent += ' · ' + job.model_name;
  if (local) $('job-meta').textContent = ({MATERIAL:'资料阅读与分析',DOWNLOAD:'资料下载',LOCAL_QA:'本地资料问答',AUTO_RESEARCH:'自动研究'}[job.kind] || job.kind) + ' · ' + new Date(job.created_at).toLocaleString() + ' · ' + (job.model_name || '');
  $('job-budget').textContent = effort[0] + ' · 最多 ' + effort[1] + ' 次搜索/阅读，证据充分即可提前结束。';
  $('job-budget').closest('details').classList.toggle('hidden',local);
  $('job-progress').textContent = busy(job.status) ? (job.recent_action || '正在等待后台处理…') : (job.error || '研究记录已保存，可随时回看');
  updateText($('job-brief'), job.brief);
  updateText($('job-assumptions'), JSON.parse(job.assumptions).join('；'));
  reportText = job.summary;
  $('job-error').textContent = job.error;
  updateHTML($('job-summary'), job.summary ? markdown(job.summary) : `<p class="empty">${busy(job.status) ? '研究正在进行，切换到「实时过程」查看进展。' : '这次任务未生成报告，可以重试。'}</p>`);
  const prefix = jobBase(id, space);
  updateHTML($('job-actions'), `${busy(job.status) ? `<button data-action="cancel" data-id="${esc(id)}">取消任务</button>` : ''}${['failed','cancelled','interrupted'].includes(job.status) ? `<button data-action="retry" data-id="${esc(id)}">重新研究</button>` : ''}${["failed","interrupted"].includes(job.status) && ["RESEARCH","LOCAL_QA","BRAINSTORM","AUTO_RESEARCH"].includes(job.kind) ? `<button data-action="resume" data-id="${esc(id)}">${job.error?.includes('answer_verification_unavailable') ? '继续核验草稿' : '从检查点继续'}</button>` : ""}${job.summary ? '<button type="button" data-copy-report>复制研究结果</button>' : ''}${job.report || (local && job.summary) ? `<a href="${prefix}/report.md" download>下载 Markdown</a><a href="${prefix}/report.html" target="_blank" rel="noopener">打开完整报告 ↗</a>` : ''}`);
  $('trace-download').classList.toggle('hidden', !job.trace_path); $('trace-download').href = prefix + '/trace';
  $('trace-status').textContent = busy(job.status) ? '● 每秒自动更新' : '过程已保存 · ' + jobStatus(job);
  await Promise.all([loadEvents(id, space, version), job.kind==='AUTO_RESEARCH' ? loadAutoResearch(job,space,version) : job.run_id ? job.run_id !== runLoaded ? loadSources(job, space, version) : Promise.resolve() : local ? loadLocalSources(job,space,version) : Promise.resolve()]);
}
function autoResearchMarkup(data, space) {
  const budget=data.config.budget;
  const executionCalls=data.coding.filter(t=>t.operation==='run_experiments').length;
  const codingCalls=data.coding.length-executionCalls;
  const remaining=data.coding[data.coding.length-1]?.remaining_execution;
  const remainingText=remaining ? `<p class="muted">编码剩余 ${esc(remaining.coding_calls)} 次 / ${esc(remaining.coding_seconds)} 秒 / ${esc(remaining.coding_tokens)} token；实验剩余 ${esc(remaining.experiment_seconds)} 秒。</p>` : '';
  const plans=data.plans.map(p=>`<details><summary>方案 ${esc(p.version)} · ${esc(p.title)} · 待验证假设</summary><p>基线：${esc(p.baseline)}</p><p>假设：${esc(p.hypothesis)}</p><p>方法：${esc(p.method)}</p><p>验证：${esc(p.validation)}</p><p>风险：${esc(p.risks)}</p></details>`).join('');
  const research=data.research.map(r=>`<p><a href="/api/spaces/${esc(space)}/jobs/${esc(r.id)}/report.html" target="_blank" rel="noopener">调研记录 ${esc(r.id.slice(0,8))}</a> · ${esc(states[r.status] || r.status)}${r.error ? ' · '+esc(r.error) : ''}</p>`).join('');
  const preparations=(data.preparations || []).map(p=>`<details><summary>项目准备 · ${p.ok ? '已完成' : '未完成'}</summary><p>${esc(p.error)}</p>${p.sources.map(s=>`<p><a href="${esc(safeLink(s.url))}" target="_blank" rel="noopener">${esc(s.destination)}</a> · ${esc(s.sha256)}</p>`).join('')}<p>固定依赖：${esc(p.packages.join(', ') || '未新增')}</p></details>`).join('');
  const coding=data.coding.map(t=>`<details><summary>${t.operation==='run_experiments' ? '已有代码实验' : '编码与实验'} ${esc(t.id.slice(0,8))} · ${esc(states[t.status] || t.status)}</summary><p>${esc(t.summary)}</p><p>${esc(t.error)}</p>${t.measurements.map(m=>`<p><strong>${esc(m.name)} · ${esc(m.role)}</strong> · ${m.valid ? '已取得执行结果' : '执行失败'}</p><pre>${esc(JSON.stringify(m.metrics,null,2))}</pre><p>${esc(m.error || '')}</p><p class="muted">由项目脚本实际执行得到，评分方法及实验可比性需结合配置复核。</p><details><summary>配置与执行记录</summary><pre>${esc(JSON.stringify({config:m.result?.config,diagnostics:m.result?.diagnostics,execution:m.execution,script_sha256:m.script_sha256},null,2))}</pre>${m.log_tail ? `<p>标准输出</p><pre>${esc(m.log_tail)}</pre>` : ''}${m.stderr_tail ? `<p>错误输出</p><pre>${esc(m.stderr_tail)}</pre>` : ''}</details>`).join('')}</details>`).join('');
  return `<section><p><strong>${esc(autoOutcomes[data.outcome] || '自动研究进行中')}</strong></p><p>研究决策 ${esc(data.decisions)}/${esc(budget.decisions)} · 调研 ${esc(data.research.length)}/${esc(budget.research_calls)} · 编码 ${esc(codingCalls)}/${esc(budget.coding_calls)} · 独立实验执行 ${esc(executionCalls)}</p><p class="muted">编码累计上限 ${esc(budget.coding_seconds)} 秒 / ${esc(budget.coding_tokens)} token；实验累计上限 ${esc(budget.experiment_seconds)} 秒。</p>${remainingText}${plans}${preparations}${research}${coding}</section>`;
}
async function loadAutoResearch(job,space,version) {
  const data=await api(base(space)+'/auto-research/'+job.id);
  if (space!==sid || job.id!==jid || version!==selection) return;
  $('job-state').textContent = autoOutcomes[data.outcome] || jobStatus(job);
  if (['blocked','budget_exhausted'].includes(data.outcome)) $('job-state').className = 'badge warning';
  updateHTML($('job-summary'),(job.summary ? markdown(job.summary) : '')+autoResearchMarkup(data,space));
  updateHTML($('sources'),data.research.map(r=>`<article class="source"><h3><a href="/api/spaces/${esc(space)}/jobs/${esc(r.id)}/report.html" target="_blank" rel="noopener">原文与报告 ${esc(r.id.slice(0,8))}</a></h3>${(r.sources || []).map(s=>`<p><a href="${esc(safeLink(s.url))}" target="_blank" rel="noopener">${esc(s.title)}</a></p>`).join('')}</article>`).join('') || '<p class="empty">定向调研完成后在这里查看来源与报告。</p>');
}
async function loadLocalSources(job,space,version) {
  const data=await api(jobBase(job.id,space)+'/local-evidence');
  if (space!==sid || job.id!==jid || version!==selection) return;
  const notes=data.citations.map(c=>`<article class="source"><span class="badge">L${c.chunk_id} · 本地证据</span><h3><a href="${base(c.space_id || space)}/materials/${c.artifact_id}/reader#local-L${c.chunk_id}" target="_blank" rel="noopener">${esc(c.title)}</a></h3><p class="muted">${esc(c.source_space_name || '')} · ${esc(c.page ? '第 '+c.page+' 页' : '文本章节')} · ${esc(c.section)}</p><p class="plain">${esc(c.quote)}</p><p class="muted">${esc(c.original_path || '资料库只读文本快照')}</p></article>`).join('');
  updateHTML($('sources'),notes || data.materials.map(m=>`<article class="source"><h3><a href="${base(space)}/materials/${m.id}/reader" target="_blank" rel="noopener">${esc(m.title)}</a></h3><p class="plain">${esc(m.boundary)}</p><p class="muted">${m.chunk_count} 段已索引 · ${esc(m.original_path || '只读文本快照')}</p></article>`).join('') || '<p class="empty">任务完成后在这里查看本地资料与引用。也可以从左侧资料库打开原文。</p>');
}
const eventNames = {material_progress:'资料处理',material_failed:'资料未完成',run_started:'开始研究',model_request:'分析与规划',model_decision:'确定下一步',model_response:'收到模型回复',model_usage:'模型用量',tool_call_requested:'调用工具',tool_result:'收到工具结果',tool_retry:'重试工具',tool_recovery:'调整工具调用',audit:'安全检查',claims_verified:'核验结论',final_validated:'检查最终回答',run_summary:'汇总证据',run_finished:'研究结束',stop_decision:'停止决策',model_error:'模型请求失败',validation_error:'校验提示',context_budget:'整理上下文',context_built:'整理研究上下文'};
Object.assign(eventNames,{answer_draft:'生成回答草稿',answer_check_started:'开始核验草稿',answer_checked:'核验回答',answer_repair:'修订回答',answer_check_unavailable:'核验暂不可用',answer_patch_rejected:'修订格式检查'});
Object.assign(eventNames,{auto_tool_call:'安排研究动作',auto_tool_result:'收到研究结果'});
const researchActions = {research:'定向调研',save_plan:'保存方法方案',prepare_project:'准备实验项目',coding_agent:'委派编码与实验',run_experiments:'运行已有代码的实验',inspect:'查看资料与结果',finish_research:'交付研究结论'};
function eventDescription(event) {
  if (event.event === 'auto_tool_call') return (researchActions[event.name] || '研究动作') + (event.args?.question ? '：'+event.args.question : event.args?.title ? '：'+event.args.title : event.args?.task ? '：'+event.args.task : '');
  if (event.event === 'auto_tool_result') return (event.tool_kind==='coding' ? '编码与实验' : '定向调研') + ' · ' + (states[event.result?.status] || '已返回') + (event.result?.error ? '：'+event.result.error : '');
  if (event.event === 'tool_call_requested') return (event.name === 'read' ? '阅读：' : '搜索：') + (event.args?.query || event.args?.url || JSON.stringify(event.args || {}));
  if (event.event === 'tool_result') return (event.ok ? '调用成功' : '调用失败') + (event.name === 'search' ? ' · ' + (event.results?.length || 0) + ' 条结果' : '') + (event.name === 'read' && event.ok ? ' · ' + (event.title || '已读取页面') : '') + (event.error ? ' · ' + (event.error.message || event.error.code || JSON.stringify(event.error)) : '');
  if (event.event === 'model_request') return '根据现有资料安排下一步研究。';
  if (event.event === 'audit') return event.decision === 'allow' ? '检查通过' : '已拦截或请求修正，详见事件记录。';
  if (event.event === 'context_built') return event.reason === 'within_budget' ? '当前资料在上下文预算内。' : '整理已有资料与研究进度。';
  if (event.event === 'claims_verified') return `已核验 ${event.claims?.length || 0} 条结论。`;
  if (event.event === 'run_finished') return `${event.termination || ''} · ${event.tool_calls ?? 0} 次工具调用`;
  return event.reason || event.message || event.decision || event.status || '';
}
async function loadEvents(id, space, version) {
  const after = eventCursor, data = await api(jobBase(id, space) + '/events?after=' + after);
  if (space !== sid || id !== jid || version !== selection || after !== eventCursor) return;
  const pane = $('pane-trace'), atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 90;
  for (const e of data.events) {
    const article = document.createElement('article');
    article.className = 'event ' + (['tool_call_requested','run_finished'].includes(e.event) ? 'important' : e.event.includes('error') || e.ok === false ? 'problem' : '');
    article.innerHTML = `<time>${esc(new Date(e.ts).toLocaleTimeString())}</time><span class="event-title">${esc(eventNames[e.event] || e.event)}</span><p>${esc(eventDescription(e))}</p><details><summary>事件详情 · #${esc(e.seq)}</summary><pre>${esc(JSON.stringify(e, null, 2))}</pre></details>`;
    $('events').appendChild(article);
  }
  eventCursor = data.next_seq; $('event-count').textContent = $('events').childElementCount;
  $('trace-empty').classList.toggle('hidden', eventCursor > 0);
  if (atBottom && activeTab === 'trace') pane.scrollTop = pane.scrollHeight;
}
async function loadSources(job, space, version) {
  const run = await api(jobBase(job.id, space) + '/run');
  if (space !== sid || job.id !== jid || version !== selection) return;
  runLoaded = job.run_id;
  const evidence = run.evidence || [], sources = run.sources || [], pages = evidence.filter(e => e.kind !== 'snippet');
  $('sources').innerHTML = `<div class="evidence-stat">${sources.length} 个来源 · ${pages.length} 条页面阅读证据 · ${evidence.length - pages.length} 条搜索摘要</div><p class="muted" style="margin-top:10px">摘要页仅提供论文摘要；预印本不代表正式发表，未读取的正文不会被算作已阅读。</p>` + sources.map(s => {
    const items = evidence.filter(e => e.source_id === s.source_id);
    return `<article class="source"><span class="badge">${esc(s.source_id)} · ${items.some(e => e.kind !== 'snippet') ? '已读取页面' : '仅搜索摘要'}</span><h3><a href="${esc(safeLink(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></h3><p class="muted">${esc(s.url)}</p>${items.map(e => `<details><summary>${esc(e.evidence_id)} · ${e.kind === 'snippet' ? '搜索摘要' : '页面内容'}</summary><p class="plain">${esc(e.content)}</p></details>`).join('')}</article>`;
  }).join('');
}
function openSpace(edit) {
  editing = edit; editingId = edit ? sid : '';
  const space = edit ? spaces.find(s => s.id === sid) : null;
  $('dialog-title').textContent = edit ? '研究区设置' : '新建研究区';
  $('name').value = space?.name || ''; $('description').value = space?.description || ''; $('download-root').value = space?.download_root || '';
  $('auto-download').checked = !!space?.auto_download; $('download-count').value=space?.download_count || 3; $('download-mb').value=space?.download_mb || 50;
  $('dialog-error').textContent = ''; $('delete-space').classList.toggle('hidden', !edit); $('space-dialog').showModal();
}
$('new-space').onclick = $('start-space').onclick = () => openSpace(false);
$('edit-space').onclick = () => openSpace(true);
$('close-dialog').onclick = () => $('space-dialog').close();
$('space-form').onsubmit = async event => {
  event.preventDefault(); $('save-space').disabled = true;
  const body = {name:$('name').value, description:$('description').value,auto_download:$('auto-download').checked,download_count:Number($('download-count').value),download_mb:Number($('download-mb').value)};
  if ($('download-root').value.trim()) body.download_root = $('download-root').value.trim();
  try {
    const space = await api(editing ? base(editingId) : '/api/spaces', editing ? 'PATCH' : 'POST', body);
    if (!editing) await api(base(space.id) + '/conversations', 'POST', {title:'主线'});
    $('space-dialog').close(); await loadSpaces(space.id);
  } catch (e) {$('dialog-error').textContent = e.message;} finally {$('save-space').disabled = false;}
};
$('pick-folder').onclick = async () => {
  $('pick-folder').disabled = true; $('pick-folder').textContent = '请在系统窗口中选择…'; $('dialog-error').textContent = '';
  try {const result = await api('/api/folders/pick', 'POST', {initial:$('download-root').value.trim()});if (!result.cancelled) $('download-root').value = result.path;}
  catch (e) {$('dialog-error').textContent = e.message;}
  finally {$('pick-folder').disabled = false; $('pick-folder').textContent = '选择文件夹…';}
};
$('delete-space').onclick = async () => {
  if (!confirm('将整个研究区移入回收站？会话、Notes 和资料将保留，可随时恢复。')) return;
  try {await api(base(editingId), 'DELETE', {});$('space-dialog').close();await loadSpaces();} catch (e) {$('dialog-error').textContent = e.message;}
};
$('new-conversation').onclick = () => {
  conversationEdit={space:sid};
  $('conversation-dialog-title').textContent='在本研究区新建会话';$('create-conversation').textContent='创建会话';
  $('conversation-name').value='';$('conversation-error').textContent='';
  $('conversation-dialog').showModal();$('conversation-name').focus();
};
$('close-conversation').onclick = () => $('conversation-dialog').close();
$('conversation-form').onsubmit = async event => {
  event.preventDefault();const title = $('conversation-name').value.trim();if (!title) return;
  $('create-conversation').disabled = true;
  const space = conversationEdit?.space || sid, conversation = conversationEdit?.id;
  try {const result = await api(base(space) + '/conversations' + (conversation ? '/'+conversation : ''), conversation ? 'PATCH' : 'POST', {title});$('conversation-dialog').close();if (space !== sid) return;await loadConversations(result.id);$('history-messages').replaceChildren();resetJob();await refresh();}
  catch (e) {$('conversation-error').textContent = e.message;}
  finally {$('create-conversation').disabled = false;}
};
$('conversations').onchange = async () => {
  const selected=$('conversations').value;
  try {await loadConversations(selected);$('history-messages').replaceChildren();resetJob();await refresh();} catch (e) {showError(e);}
};
$('send-form').onsubmit = async event => {
  event.preventDefault(); if (sending || !sid || !$('content').value.trim() || !models.find(model => model.id === $('model-select').value)?.configured) return;
  sending = true; updateModelChoice(); $('send-status').textContent = '正在理解你的消息…'; $('error').textContent = '';
  const space = sid, content = $('content').value, model_id = $('model-select').value;
  let conversation = cid;
  try {
    if (!conversation) {const result = await api(base(space) + '/conversations', 'POST', {title:'研究会话'});conversation = result.id;if (space !== sid) return;await loadConversations(conversation);}
    const result = await api(base(space) + '/conversations/' + conversation + '/messages', 'POST', {content, model_id, background:true});
    if (space === sid && conversation === cid) {if ($('content').value === content) $('content').value = '';if (result.task_id) resetJob(result.task_id);await refresh();$('messages').scrollTop = $('messages').scrollHeight;}
  } catch (e) {showError(e);} finally {sending = false; updateModelChoice(); $('send-status').textContent = '普通聊天直接回答 · 研究任务在后台继续';}
};
$('content').addEventListener('compositionstart', () => {composing = true;});
$('content').addEventListener('compositionend', () => {composing = false;});
$('content').onkeydown = event => {
  if (event.key !== 'Enter' || event.shiftKey || event.altKey || event.isComposing || composing || event.keyCode === 229) return;
  event.preventDefault(); if (!event.repeat) $('send-form').requestSubmit();
};
document.addEventListener('click', async event => {
  const button = event.target.closest('button');if (!button) return;
  try {
    if (button.dataset.copyMessage !== undefined) await copyText(messageText.get(button.dataset.copyMessage));
    if (button.dataset.copyStream !== undefined) await copyText(liveJobs.get(button.dataset.copyStream)?.text || '');
    if (button.hasAttribute('data-copy-report')) await copyText(reportText);
    if (button.id === 'copy-draft') await copyText($('content').value);
    if (button.dataset.space) await loadSpaces(button.dataset.space);
    if (button.dataset.forkMessage) await forkBranch(Number(button.dataset.forkMessage));
    if (button.dataset.deleteTurn) {
      if (!confirm('删除这一轮提问及其回答？可以从“恢复已删除内容”找回，其他分支不受影响。')) return;
      await api(base()+'/conversations/'+cid+'/messages/'+button.dataset.deleteTurn,'DELETE',{});resetJob();await refresh();
    }
    if (button.dataset.restorePath) {
      await api(button.dataset.restorePath,'POST',{});$('recycle-dialog').close();await loadSpaces(sid);
    }
    if (button.dataset.materialRetry) {
      button.disabled=true;
      const conversation=await materialConversation();
      const job=await api(base()+'/materials','POST',{conversation_id:conversation,artifact_id:button.dataset.materialRetry,model_id:$('model-select').value});
      $('library-dialog').close();resetJob(job.id);await refresh();
    }
    if (button.dataset.materialAsk) {
      const material=libraryItems.find(m=>m.id===button.dataset.materialAsk);
      if (material) {$('content').value='根据本地资料《'+material.title+'》，';$('library-dialog').close();$('content').focus();}
    }
    if (button.dataset.job) {resetJob(button.dataset.job);await refresh();}
    if (button.dataset.tab) selectTab(button.dataset.tab);
    if (button.dataset.action) {
      button.disabled = true; const space = sid, result = await api(jobBase(button.dataset.id, space) + '/' + button.dataset.action, 'POST', {});
      if (space === sid) {if (result.id !== jid) resetJob(result.id);await refresh();}
    }
  } catch (e) {showError(e);} finally {if (button.dataset.action) button.disabled = false;}
});

let libraryItems = [];
async function loadLibrary() {
  if (!sid) return;
  const space=sid, token=epoch, data=await api(base(space)+'/materials');
  if (sid!==space || token!==epoch) return;
  libraryItems=data.materials; $('library-count').textContent=libraryItems.length;
  renderLibrary();
}
function renderLibrary() {
  const query=$('library-search').value?.trim().toLowerCase() || '';
  const kinds={paper:'论文',document:'技术文档',github:'GitHub',report:'研究报告'};
  const statuses={ready:'已索引',partial:'部分解析',unreadable:'待解析 / 无可读文字'};
  const categories={classic:'经典',representative:'代表性',recent:'最新'};
  updateHTML($('material-list'),libraryItems.filter(m=>m.title.toLowerCase().includes(query)).map(m=>{
    const prefix=base()+'/materials/'+m.id,meta=m.metadata || {};
    return `<article class="material-card"><div class="row"><span class="badge">${esc(kinds[m.kind] || m.kind)}</span><span class="muted">${esc(statuses[m.status] || m.status)} · ${m.chunk_count} 段</span></div><h3><a href="${prefix}/reader" target="_blank" rel="noopener">${esc(m.title)}</a></h3><p class="muted">${esc([meta.year,meta.venue,meta.publication_status,categories[meta.selection_category]].filter(Boolean).join(' · ') || '元信息待核验')}</p>${meta.selection_reason ? `<p>${esc(meta.selection_reason)}</p>` : ''}<p class="material-boundary">${esc(m.boundary)}</p>${m.warnings.length ? `<details><summary>${m.warnings.length} 项阅读边界</summary><ul>${m.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></details>` : ''}${m.original_path ? `<p class="material-path">${esc(m.original_path)}</p>` : '<p class="material-path">只读文本索引 · 未保存原始文件</p>'}<div class="actions"><a href="${prefix}/reader" target="_blank" rel="noopener">阅读分析 ↗</a>${m.original_path ? `<a href="${prefix}/original" target="_blank" rel="noopener">打开原文</a>` : ''}${m.analysis_status==='complete' ? `<a href="${prefix}/analysis.md" download>分析 Markdown</a>` : ''}${m.kind!=='report' && m.analysis_status!=='complete' ? `<button data-material-retry="${m.id}">${m.analysis_status==='failed' ? '重试分析' : '解析与分析'}</button>` : ''}<button data-material-ask="${m.id}">询问这份资料</button></div></article>`;
  }).join('') || '<div class="library-empty"><span aria-hidden="true">▤</span><h3>让资料留在书架上</h3><p>添加论文或导入文档后，便能结合原文追问。<br>当前研究区的历史报告也会出现在这里。</p></div>');
}
async function materialConversation() {
  if (!sid) throw new Error('请先选择研究区');
  if (cid) return cid;
  const result=await api(base()+'/conversations','POST',{title:'资料阅读'});
  await loadConversations(result.id); return result.id;
}
$('open-library').onclick = async () => {
  $('material-error').textContent='';
  $('library-context').textContent=(spaces.find(s=>s.id===sid)?.name || '')+' · 本研究区的论文、文档与历史报告';
  $('library-dialog').showModal();
  try {await loadLibrary();} catch(e) {$('material-error').textContent=e.message;}
};
$('close-library').onclick=()=>$('library-dialog').close();
$('refresh-library').onclick=()=>loadLibrary().catch(e=>{$('material-error').textContent=e.message;});
$('library-search').oninput=renderLibrary;
$('material-form').onsubmit=async event=>{
  event.preventDefault();$('add-material').disabled=true;$('material-error').textContent='';
  const space=sid;
  try {
    if (!$('material-url').value.trim()) throw new Error('请填写资料链接，或选择下方的本地文件');
    const conversation=await materialConversation();
    const job=await api(base(space)+'/materials','POST',{conversation_id:conversation,url:$('material-url').value.trim(),topic:$('material-topic').value.trim(),download:$('material-download').checked,model_id:$('model-select').value});
    if (space!==sid) return;
    $('material-url').value='';$('library-dialog').close();resetJob(job.id);await refresh();
  } catch(e) {$('material-error').textContent=e.message;} finally {$('add-material').disabled=false;}
};
$('material-file').onchange=async ()=>{
  const file=$('material-file').files[0];if (!file) return;
  $('material-file').disabled=true;$('material-error').textContent='正在导入文件…';
  const space=sid;
  try {
    if (file.size>25*1024*1024) throw new Error('请选择 25 MB 以内的文件');
    const conversation=await materialConversation();
    const query=new URLSearchParams({filename:file.name,conversation_id:conversation,model_id:$('model-select').value});
    const response=await fetch(base(space)+'/materials/upload?'+query,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
    const data=await response.json();if (!response.ok) throw new Error(data.error || '文件导入失败');
    if (space!==sid) return;
    $('library-dialog').close();resetJob(data.job.id);await refresh();
  } catch(e) {$('material-error').textContent=e.message;} finally {$('material-file').disabled=false;$('material-file').value='';}
};
document.querySelector('.tabs').onkeydown = event => {
  const tabs = ['report','sources','trace'];if (!['ArrowLeft','ArrowRight'].includes(event.key)) return;
  event.preventDefault();selectTab(tabs[(tabs.indexOf(activeTab) + (event.key === 'ArrowRight' ? 1 : 2)) % 3]);$('tab-' + activeTab).focus();
};
$('legacy').onclick = async () => {
  try {const data = await api('/api/runs');$('legacy-list').innerHTML = data.runs.map(r => `<p><a href="/api/runs/${encodeURIComponent(r.id)}" target="_blank" rel="noopener">${esc(r.question.slice(0, 120))}</a> <span class="muted">${esc(r.status)}</span></p>`).join('') || '<p>暂无运行记录</p>';$('legacy-dialog').showModal();} catch (e) {showError(e);}
};
$('close-legacy').onclick = () => $('legacy-dialog').close();
async function poll() {
  try {
    if (!initialized) {
      const health = await api('/health');
      $('mode').textContent = `${(health.version || 'v4').toUpperCase()} · ${health.mode === 'offline-demo' ? '离线演示' : '真实模型'}`;
      $('pick-folder').disabled = !health.folder_picker;
      if (!health.folder_picker) $('folder-hint').textContent = '当前服务不支持本机窗口，请填写服务器上的绝对路径。资料会保存到这个服务器目录。';
      await loadModels(); await loadSpaces(); initialized = true;
    } else await refresh();
    connected(true);
  } catch (e) {showError(e);} finally {setTimeout(poll, 1200);}
}
selectTab('trace');poll();

// A local, seamless soundscape: low fire rumble, soft air and irregular wood crackles.
// Generated only on the first play gesture; no downloads or audio processing on the UI timer.
function createFireBuffer(context, random = Math.random) {
  const rate = 24000, length = rate * 24, blend = rate / 4;
  const buffer = context.createBuffer(2, length, rate);
  for (let channel = 0; channel < 2; channel++) {
    const samples = new Float32Array(length + blend);
    let low = 0, air = 0;
    for (let i = 0; i < samples.length; i++) {
      const white = random() * 2 - 1;
      low = .985 * low + .015 * white;
      air = .82 * air + .18 * white;
      const swell = .82 + .12 * Math.sin(i / length * Math.PI * 6) + .06 * Math.sin(i / length * Math.PI * 14);
      samples[i] = (1.8 * low + .14 * air) * swell;
    }
    for (let start = 0; start < samples.length;) {
      start += Math.floor(rate * (.055 + random() ** 2 * .85));
      const duration = Math.floor(rate * (.008 + random() ** 2 * .09));
      const strength = .04 + random() ** 3 * .45;
      const pitch = 650 + random() * 1700;
      for (let j = 0; j < duration && start + j < samples.length; j++) {
        const envelope = Math.min(1, j / (rate * .0015)) * Math.exp(-6 * j / duration);
        samples[start + j] += strength * envelope * ((random() * 2 - 1) * .8 + Math.sin(2 * Math.PI * pitch * j / rate) * .2);
      }
    }
    const output = buffer.getChannelData(channel);
    // The last sample continues into the extra tail at frame zero, then blends into the start.
    for (let i = 0; i < length; i++) {
      const value = i < blend ? samples[length + i] * (1 - i / blend) + samples[i] * (i / blend) : samples[i];
      output[i] = Math.tanh(value); // Keep even overlapping crackles below clipping.
    }
  }
  return buffer;
}

function setupFireplace() {
  const button = $('fire-sound'), slider = $('fire-volume'), output = $('fire-volume-value');
  const controls = $('hearth-audio'), status = $('fire-sound-status');
  if (!button || !slider || !output || !controls || !status) return;
  let context, master, source, changing = false;
  let volume = 25;
  try {
    const saved = localStorage.getItem('research-fire-volume');
    if (saved !== null && saved.trim() !== '' && Number.isFinite(Number(saved))) volume = Math.max(0, Math.min(100, Number(saved)));
  } catch { /* Audio remains usable when preference storage is unavailable. */ }
  slider.value = String(volume);
  function showVolume() {
    volume = Math.max(0, Math.min(100, Number(slider.value) || 0));
    output.textContent = volume + '%';
    slider.setAttribute('aria-valuetext', volume + '%');
  }
  function updateSound() {
    const playing = context?.state === 'running';
    button.setAttribute('aria-pressed', String(playing));
    $('fire-sound-label').textContent = playing ? '关闭炉火声' : '开启炉火声';
    controls.setAttribute('data-playing', String(playing && volume > 0));
  }
  showVolume(); updateSound();
  slider.oninput = () => {
    showVolume();
    if (master) master.gain.setTargetAtTime(volume / 100, context.currentTime, .08);
    try {localStorage.setItem('research-fire-volume', String(volume));} catch {}
    updateSound();
  };
  button.onclick = async () => {
    if (changing) return;
    changing = true; button.disabled = true; status.textContent = '';
    try {
      if (!context || context.state === 'closed') {
        const Audio = globalThis.AudioContext || globalThis.webkitAudioContext;
        if (!Audio) throw new Error('audio unavailable');
        context = new Audio();
        master = context.createGain(); master.gain.value = 0; master.connect(context.destination);
        source = context.createBufferSource(); source.buffer = createFireBuffer(context); source.loop = true;
        source.connect(master); source.start();
        context.onstatechange = updateSound;
        await context.resume();
        master.gain.setTargetAtTime(volume / 100, context.currentTime, .3);
      } else if (context.state === 'running') {
        master.gain.setTargetAtTime(0, context.currentTime, .03);
        await new Promise(resolve => setTimeout(resolve, 160));
        await context.suspend();
      } else {
        await context.resume();
        master.gain.setTargetAtTime(volume / 100, context.currentTime, .3);
      }
      if (context.state !== 'running' && context.state !== 'suspended') throw new Error('audio interrupted');
    } catch {
      status.textContent = '炉火声未能开启，请再试一次。';
      try {await context?.close();} catch {}
      context = master = source = undefined;
    } finally {
      changing = false; button.disabled = false; updateSound();
    }
  };
  // Leaving the page stops playback; restoring from the back/forward cache stays quiet.
  globalThis.addEventListener?.('pagehide', () => {context?.suspend().catch(() => {});});
}
setupFireplace();


async function showMemory() {
  if (!sid || !cid) return;
  $('memory-error').textContent='';
  const space=sid, conversation=cid;
  const [memory,context,retrieval]=await Promise.all([api(base(space)+'/memories'),api(base(space)+'/conversations/'+conversation+'/context'),api(base(space)+'/retrieval')]);
  if (space!==sid || conversation!==cid) return;
  const usage=context.usage, pct=v=>v===null || v===undefined ? '未知' : (v*100).toFixed(1)+'%';
  $('context-status').textContent=`检索：${retrieval.mode==='hybrid' ? '关键词 + BGE-M3 语义' : '仅关键词'} · 向量覆盖 ${pct(retrieval.coverage)}。当前会话 ${context.event_count} 条事件，${context.checkpoints.length} 个近期检查点。模型请求 ${usage.requests} 次，已知 token ${usage.total_tokens ?? '未知'}（用量覆盖 ${pct(usage.total_tokens_coverage)}），缓存命中 ${pct(usage.cache_hit_rate_on_reported_input)}（字段覆盖 ${pct(usage.cache_read_tokens_coverage)}）。`;
  if(context.coding_usage?.requests)$('context-status').textContent+=` Codex 编码 ${context.coding_usage.requests} 轮，已报告 token ${context.coding_usage.total_tokens??'未知'}；CLI 只提供整轮汇总，不等同于模型请求数。`;
  memoryItems=memory.memories;
  const labels={confirmed:'用户确认',evidence_checked:'原文支持已检查',candidate:'待确认',stale:'已过时',conflicting:'有冲突',rejected:'已拒绝',superseded:'已被替代'};
  $('memory-items').innerHTML=memory.memories.map(m=>`<article class="memory-card"><div class="row"><span class="badge">${esc(labels[m.status] || m.status)}</span><span class="muted">${m.kind==='preference' ? '用户偏好' : '研究结论'}</span></div><p class="muted">来源研究区：${esc(m.source_space_name)} · ${m.scope==='global' ? '全局偏好' : m.scope==='workspace' ? '研究区共享记忆' : '来源会话记忆'}</p>${m.kind==='preference' ? `<label>应用范围<select data-memory-scope="${m.id}" aria-label="偏好应用范围"><option value="session" ${m.scope==='session' ? 'selected' : ''}>来源会话与继承分支</option><option value="workspace" ${m.scope==='workspace' ? 'selected' : ''}>来源研究区所有会话</option><option value="global" ${m.scope==='global' ? 'selected' : ''}>所有研究区</option></select></label>` : ''}<textarea data-memory-content="${m.id}" aria-label="记忆内容" maxlength="2000">${esc(m.content)}</textarea><details><summary>来源与时间</summary><p>${esc(m.updated_at)}</p>${m.source_refs.map(r=>`<p class="plain">${esc(r.ref)}<br>${esc(r.quote || '')}</p>`).join('')}</details><div class="actions"><button data-memory-id="${m.id}" data-memory-action="save">保存修改</button><button data-memory-id="${m.id}" data-memory-action="confirm">确认</button><button data-memory-id="${m.id}" data-memory-action="reject">拒绝</button><button data-memory-id="${m.id}" data-memory-action="forget">忘记</button></div></article>`).join('') || '<p class="empty">尚无长期记忆。说“记住：……”保存会话偏好；“研究区记住：……”在本区共享；“全局记住：……”应用于所有研究区。</p>';
  $('memory-dialog').showModal();
}
$('open-memory').onclick=()=>showMemory().catch(e=>{$('send-status').textContent=e.message;});
$('close-memory').onclick=()=>$('memory-dialog').close();
$('memory-items').onclick=async event=>{
  const button=event.target.closest('[data-memory-action]'); if (!button) return;
  const id=button.dataset.memoryId, action=button.dataset.memoryAction;
  button.disabled=true;
  try {
    const path=base(memoryItems.find(m=>m.id===id)?.space_id || sid)+'/memories/'+id;
    if (action==='forget') await api(path,'DELETE',{});
    else if (action==='save') await api(path,'PATCH',{content:document.querySelector(`[data-memory-content="${id}"]`).value,status:'confirmed',scope:document.querySelector(`[data-memory-scope="${id}"]`)?.value || 'session'});
    else await api(path,'PATCH',{status:action==='confirm' ? 'confirmed' : 'rejected'});
    $('memory-dialog').close(); await showMemory();
  } catch(error) {$('memory-error').textContent=error.message;button.disabled=false;}
};
$('rename-conversation').onclick=async()=>{
  if (!cid) return;
  conversationEdit={space:sid,id:cid};
  $('conversation-dialog-title').textContent='重命名会话';$('create-conversation').textContent='保存名称';
  $('conversation-name').value=($('conversations').selectedOptions?.[0]?.textContent || '').replace(/^分支 · /,'');
  $('conversation-error').textContent='';$('conversation-dialog').showModal();$('conversation-name').select();
};
async function forkBranch(messageId) {
  if (!cid) return;
  const space=sid, source=cid;
  const branch=await api(base(space)+'/conversations/'+source+'/fork','POST',messageId ? {through_message_id:messageId} : {});
  if (sid!==space || cid!==source) return;
  $('session-menu').open=false;
  await loadConversations(branch.id);$('history-messages').replaceChildren();resetJob();await refresh();
  if (branch.draft) {$('content').value=branch.draft;$('content').focus();}
  $('send-status').textContent=branch.draft ? '已从这条提问建立分支。可修改后发送，原分支保留。' : '已建立分支，后续讨论在这里继续。';
}
$('fork-conversation').onclick=()=>forkBranch().catch(showError);
$('archive-conversation').onclick=async()=>{
  if (!cid) return;
  const space=sid,conversation=cid;
  try {await api(base(space)+'/conversations/'+conversation,'PATCH',{archived:true});if(space!==sid || conversation!==cid)return;$('session-menu').open=false;await loadConversations();resetJob();await refresh();} catch(e){showError(e);}
};
$('archive-space').onclick=async()=>{
  if (!sid) return;
  try {await api(base()+'/archive','POST',{});$('session-menu').open=false;await loadSpaces();} catch(e){showError(e);}
};
$('delete-branch').onclick=async()=>{
  if (!cid || !confirm('将当前会话移入回收站？可恢复，其他会话和已建立的分支副本保留。')) return;
  const space=sid,branch=cid;
  try {await api(base(space)+'/conversations/'+branch,'DELETE',{});if(space!==sid || branch!==cid)return;epoch++;cid='';branchJobs=[];connectLive();$('content').value='';$('error').textContent='';$('session-menu').open=false;await loadConversations();resetJob();await refresh();} catch(e){showError(e);}
};
$('stop-generation').onclick=async()=>{
  const space=sid, branch=cid;
  $('stop-generation').disabled=true;
  try {
    await api(base(space)+'/conversations/'+branch+'/stop','POST',{});
    if(sid===space && cid===branch){branchJobs=[];liveJobs.clear();renderLive();await refresh();$('send-status').textContent='已停止当前会话的生成与排队任务，可以继续提问。';}
  } catch(e){showError(e);} finally {$('stop-generation').disabled=false;}
};
async function showRecycle(branches=false) {
  $('recycle-error').textContent='';let markup='';
  if(branches) {
    const space=sid,data=await api(base(space)+'/recycle');if(space!==sid)return;
    $('recycle-title').textContent='会话归档与回收站';
    markup=[...(data.archived || []),...data.branches].map(b=>`<article class="memory-card"><p>${esc(b.title)} · ${b.deleted_at ? '已删除' : '已归档'}</p><button data-restore-path="${base(space)}/conversations/${b.id}/restore">恢复会话</button></article>`).join('')+
      data.turns.map(t=>`<article class="memory-card"><p>${esc(t.title)} · 第 ${t.turn_seq} 轮</p><p>${esc(t.content.slice(0,160))}</p><button data-restore-path="${base(space)}/conversations/${t.conversation_id}/turns/${t.turn_seq}/restore">恢复对话</button></article>`).join('');
  } else {
    const [archived,trash]=await Promise.all([api('/api/spaces?state=archived'),api('/api/spaces?state=trash')]);
    $('recycle-title').textContent='研究区归档与回收站';
    markup=[...archived.spaces,...trash.spaces].map(s=>`<article class="memory-card"><p>${esc(s.name)} · ${s.deleted_at ? '已删除' : '已归档'}</p><button data-restore-path="${base(s.id)}/restore">恢复研究区</button></article>`).join('');
  }
  $('recycle-items').innerHTML=markup || '<p class="empty">暂无需要恢复的内容。</p>';
  $('session-menu').open=false;$('recycle-dialog').showModal();
}
$('open-session-trash').onclick=()=>showRecycle().catch(showError);
$('open-branch-trash').onclick=()=>showRecycle(true).catch(showError);
$('close-recycle').onclick=()=>$('recycle-dialog').close();
