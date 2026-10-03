// Run the actual page script with a small DOM boundary; browser checks cover real selection/clipboard.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function page(overrides={}) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      listeners:{}, html:'', writes:0, submissions:0,
      get innerHTML() {return this.html;},
      set innerHTML(value) {this.writes++;this.html=value.replace(/&#39;/g,"'").replace(/&quot;/g,'"');},
      addEventListener(name, listener) {this.listeners[name]=listener;},
      attributes:{}, setAttribute(name,value) {this.attributes[name]=value;}, classList:{toggle(){}},
      requestSubmit() {this.submissions++;},
    });
    return elements.get(id);
  }
  const context = vm.createContext({document:{getElementById:element,querySelector:element,addEventListener(){},createElement(){return element('dynamic-dialog');},body:{append(){}}},
    fetch:()=>new Promise(()=>{}),setTimeout(){},...overrides});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/highlight.min.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/research-records.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/strategies.js'),'utf8'),context);
  return {element,context};
}
function press(input, overrides={}) {
  const event = {key:'Enter',prevented:false,preventDefault(){this.prevented=true;},...overrides};
  input.onkeydown(event);return event;
}

test('auto research distinguishes unfinished goals and escapes plans, metrics and logs',()=>{
  const {context}=page();
  context.autoData={config:{budget:{decisions:40,research_calls:8,coding_calls:4,coding_seconds:2400,coding_tokens:1200000,experiment_seconds:7200}},
    outcome:'budget_exhausted',decisions:40,research:[],plans:[{version:1,title:'<img onerror=bad>',baseline:'base',hypothesis:'unverified',method:'method',validation:'test',risks:'unknown'}],
    coding:[{id:'task',status:'failed',summary:'<script>bad</script>',error:'timeout',measurements:[{name:'<img>',role:'candidate',valid:false,metrics:{score:0},log_tail:'<script>bad</script>',result:{config:{split:'test'}}}]}]};
  const html=vm.runInContext("autoResearchMarkup(autoData,'workspace')",context);
  assert.match(html,/预算耗尽，尚未达标/);assert.match(html,/待验证假设/);assert.match(html,/执行失败/);
  assert.equal(vm.runInContext("jobStatus({kind:'AUTO_RESEARCH',status:'completed'})",context),'已结束');
  assert.equal(vm.runInContext("jobStatus({kind:'RESEARCH',status:'completed'})",context),'已完成');
  assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<script>|<img/);
});

test('auto research separates coding calls from measurement and exposes escaped stderr',()=>{
  const {context}=page();
  context.autoData={config:{budget:{decisions:40,research_calls:3,coding_calls:4,coding_seconds:1200,coding_tokens:1600000,experiment_seconds:1800}},
    decisions:4,research:[],plans:[],coding:[{id:'code',operation:'coding_agent',status:'failed',measurements:[]},
      {id:'measure',operation:'run_experiments',status:'failed',measurements:[{name:'baseline',role:'baseline',valid:false,metrics:{},
        stderr_tail:'ModuleNotFoundError <img onerror=bad>',error:'Malformed metrics <script>bad</script>'}],
        remaining_execution:{coding_calls:3,coding_seconds:1100,coding_tokens:1500000,experiment_seconds:1795}}]};
  const html=vm.runInContext("autoResearchMarkup(autoData,'workspace')",context);
  assert.ok(html.includes('编码 1/4'));assert.match(html,/独立实验执行 1/);
  assert.match(html,/编码剩余 3 次/);assert.match(html,/1795 秒/);
  assert.match(html,/ModuleNotFoundError &lt;img/);assert.match(html,/Malformed metrics &lt;script&gt;/);
  assert.doesNotMatch(html,/<script>|<img/);
});

test('strategy feedback retains form submission and never changes conversation',async()=>{
  const {element,context}=page();let opened=0;
  element('dynamic-dialog').showModal=()=>opened++;
  vm.runInContext("sid='study';cid='chat';loadStrategies=async()=>{}",context);
  await element('open-strategies').onclick();assert.equal(opened,1);
  assert.equal(vm.runInContext('cid',context),'chat');
  let prevented=false;
  await element('dynamic-dialog').listeners.click({target:{closest:()=>({dataset:{}})},preventDefault(){prevented=true;}});
  assert.equal(prevented,false);
});

test('unapproved strategy shows no activation button and escapes feedback',async()=>{
  const {element,context}=page();
  const data={active_id:'base',jobs:[],runs:[],events:[],feedback:[{id:'f',kind:'cost',note:'<script>bad</script>',origin:'user'}],
    versions:[{id:'v',name:'candidate',diagnosis:'unproven',config:{}}],evaluations:[{version_id:'v',passed:0,metrics:{n:8,baseline:{success:.5},candidate:{success:.25},recovered:0,regressed:2}}]};
  context.fetch=async()=>({ok:true,json:async()=>data});
  await vm.runInContext('loadStrategies()',context);
  assert.doesNotMatch(element('strategy-versions').innerHTML,/data-activate/);
  assert.match(element('strategy-feedback-list').innerHTML,/&lt;script&gt;/);
});
test('research notebook uses the workspace without creating or forking a conversation',async()=>{
  const {element,context}=page();let opened=0;
  element('research-records-dialog').showModal=()=>opened++;
  vm.runInContext("sid='workspace';cid='independent';spaces=[{id:'workspace',name:'Study'}];loadNotebook=async()=>{}",context);
  await element('open-research-records').onclick();
  assert.equal(opened,1);assert.equal(vm.runInContext('notebook.space',context),'workspace');
  assert.equal(vm.runInContext('cid',context),'independent');assert.match(element('records-context').textContent,/Study/);
});
test('report snapshot displays separate automatic status and disables invalid review transitions',()=>{
  const {element,context}=page();element('records-version').value='version';
  context.snapshot={id:'version',version:1,status:'published',content:'<img src=x onerror=alert(1)> [E1]',
    coverage:{original_cited:1,supported_claims:1,claims:1,read_boundary:'limited excerpt'},snapshot:{claims:[],sources:[],evidence:[]}};
  vm.runInContext("notebook.space='space';notebook.report={id:'report',versions:[snapshot],events:[]};renderNotebookReport()",context);
  assert.equal(element('records-review').disabled,true);assert.equal(element('records-publish').disabled,true);
  assert.match(element('records-report-content').innerHTML,/&lt;img/);assert.doesNotMatch(element('records-report-content').innerHTML,/<img src=x/);
  assert.match(element('records-report-status').innerHTML,/自动核验支持/);
});
test('experiment comparison keeps incompatible results ungraded and escapes imported labels',async()=>{
  const {element,context}=page();element('records-experiment-left').value='a';element('records-experiment-right').value='b';
  context.fetch=async()=>({ok:true,json:async()=>({outcome:'incomparable',result_origin:'externally recorded',mismatches:['dataset_version'],config_changes:{},metrics:[{name:'<script>',unit:'ms',baseline:0,value:1,improvement:null,relative_percent:null,target_met:null}]})});
  await element('records-compare-experiments').onclick();
  assert.match(element('records-experiment-comparison').innerHTML,/不判定达标/);
  assert.match(element('records-experiment-comparison').innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(element('records-experiment-comparison').innerHTML,/<script>/);
});
test('branch switching retains independent unsent drafts',()=>{
  const {element,context}=page();vm.runInContext("sid='space';cid='main'",context);
  element('content').value='main draft';vm.runInContext("selectBranch('fork')",context);
  assert.equal(element('content').value,'');element('content').value='fork draft';
  vm.runInContext("selectBranch('main')",context);assert.equal(element('content').value,'main draft');
  vm.runInContext("selectBranch('fork')",context);assert.equal(element('content').value,'fork draft');
});
test('only inherited conversations are labelled branches; any conversation can be deleted',async()=>{
  const {element,context}=page({localStorage:{getItem(){return null;},setItem(){}}});
  vm.runInContext("sid='a';spaces=[{id:'a',main_branch_id:'main'}];connectLive=()=>{}",context);
  let hidden;
  element('branch-picker').classList.toggle=(name,value)=>hidden=value;
  context.fetch=async()=>({ok:true,json:async()=>({conversations:[{id:'main',title:'主线'}]})});
  await vm.runInContext('loadConversations()',context);
  assert.equal(hidden,true);assert.equal(element('delete-branch').disabled,false);
  context.fetch=async()=>({ok:true,json:async()=>({conversations:[{id:'main',title:'主线'},{id:'fork',title:'旧聊天'}]})});
  await vm.runInContext("loadConversations('fork')",context);
  assert.equal(hidden,false);assert.equal(element('delete-branch').disabled,false);
  assert.equal(vm.runInContext('cid',context),'fork');
  assert.doesNotMatch(element('conversations').innerHTML,/分支/);
  context.fetch=async()=>({ok:true,json:async()=>({conversations:[{id:'main',title:'来源'},{id:'fork',title:'实验',parent_id:'main'},{id:'old',title:'归档',archived_at:'now'}]})});
  await vm.runInContext("loadConversations('fork')",context);
  assert.match(element('conversations').innerHTML,/分支 · 实验/);
  assert.doesNotMatch(element('conversations').innerHTML,/归档/);
  assert.match(element('conversation-origin').textContent,/来源/);
});

test('new conversation clears rename state and opens within the current workspace',()=>{
  const {element,context}=page();let opened=0;
  element('conversation-dialog').showModal=()=>opened++;
  element('conversation-name').focus=()=>{};
  vm.runInContext("sid='workspace';conversationEdit={space:'old',id:'old-chat'}",context);
  element('new-conversation').onclick();
  assert.equal(opened,1);assert.equal(vm.runInContext('conversationEdit.space',context),'workspace');
  assert.equal(vm.runInContext('conversationEdit.id',context),undefined);
  assert.equal(element('create-conversation').textContent,'创建会话');
});
test('historical prompt fork submits a boundary and returns editable draft',async()=>{
  const {element,context}=page();const calls=[];
  context.fetch=async(path,options)=>{calls.push({path,...options});return {ok:true,json:async()=>({id:'branch',draft:'修改此问题'})};};
  vm.runInContext("sid='space';cid='main';loadConversations=async id=>{cid=id};resetJob=()=>{};refresh=async()=>{}",context);
  element('history-messages').replaceChildren=()=>{};element('content').focus=()=>{};
  await vm.runInContext('forkBranch(42)',context);
  assert.deepEqual(JSON.parse(calls[0].body),{through_message_id:42});
  assert.equal(element('content').value,'修改此问题');assert.equal(vm.runInContext('cid',context),'branch');
});
test('stop targets selected branch and stale selection keeps its new live state',async()=>{
  const {element,context}=page();const calls=[];
  vm.runInContext("sid='space';cid='main';renderLive=()=>{};refresh=async()=>{};liveJobs.set('new',{text:'keep'})",context);
  context.fetch=async(path,options)=>{calls.push({path,...options});vm.runInContext("cid='other'",context);return {ok:true,json:async()=>({stopped:['job']})};};
  await element('stop-generation').onclick();
  assert.equal(calls[0].path,'/api/spaces/space/conversations/main/stop');
  assert.equal(vm.runInContext("liveJobs.get('new').text",context),'keep');
  assert.equal(element('stop-generation').disabled,false);
});
test('forget sends a JSON body accepted by the server; rename uses the existing dialog', async () => {
  const calls=[];
  const {element,context}=page();
  context.fetch=async(path,options)=>{calls.push({path,...options});return {ok:true,json:async()=>({})};};
  element('memory-dialog').close=()=>{};
  const button={dataset:{memoryId:'abc',memoryAction:'forget'}};
  await element('memory-items').onclick({target:{closest:()=>button}});
  assert.equal(calls[0].method,'DELETE');assert.deepEqual(JSON.parse(calls[0].body),{});
  vm.runInContext("sid='space';cid='conversation'",context);
  let opened=0;element('conversation-dialog').showModal=()=>opened++;
  element('conversation-name').select=()=>{};
  element('conversations').selectedOptions=[{textContent:'Existing title'}];
  await element('rename-conversation').onclick();
  assert.equal(opened,1);assert.equal(element('conversation-name').value,'Existing title');
  assert.equal(element('create-conversation').textContent,'保存名称');
});
test('Enter submits once, Shift+Enter remains a newline, held Enter cannot resubmit', () => {
  const {element} = page(), input=element('content'), form=element('send-form');
  assert.equal(press(input).prevented,true);assert.equal(form.submissions,1);
  assert.equal(press(input,{shiftKey:true}).prevented,false);assert.equal(form.submissions,1);
  assert.equal(press(input,{repeat:true}).prevented,true);assert.equal(form.submissions,1);
  press(input,{ctrlKey:true});assert.equal(form.submissions,2);
});
test('Chinese IME confirmation does not send; Enter works after composition', () => {
  const {element} = page(), input=element('content'), form=element('send-form');
  input.listeners.compositionstart();assert.equal(press(input).prevented,false);
  input.listeners.compositionend();
  assert.equal(press(input,{isComposing:true}).prevented,false);
  assert.equal(press(input,{keyCode:229}).prevented,false);assert.equal(form.submissions,0);
  press(input);assert.equal(form.submissions,1);
});
test('polling unchanged quoted HTML does not replace selectable nodes; changed content still updates', () => {
  const {element,context} = page(), pane=element('job-summary');
  const render = () => vm.runInContext("updateHTML(document.getElementById('job-summary'), '<p>&quot;quoted&quot; and &#39;quoted&#39;</p>')",context);
  render();render();render();assert.equal(pane.writes,1);
  vm.runInContext("updateHTML(document.getElementById('job-summary'), '<p>New answer</p>')",context);
  assert.equal(pane.writes,2);assert.equal(pane.innerHTML,'<p>New answer</p>');
  pane.innerHTML='';render();assert.equal(pane.writes,4);
});

test('local evidence links render only scoped reader paths; executable and arbitrary local paths are rejected', () => {
  const {context}=page();
  const safe='/api/spaces/'+'a'.repeat(32)+'/materials/'+'b'.repeat(32)+'/reader#local-L42';
  context.testLocalUrl=safe;
  assert.equal(vm.runInContext('safeLink(testLocalUrl)',context),safe);
  const rendered=vm.runInContext("inline('[原文页码]('+testLocalUrl+')')",context);
  assert.ok(rendered.includes('href="'+safe+'"'));
  for (const value of ['javascript:alert(1)','file:///etc/passwd','/api/secrets','//evil.example/a','/api/spaces/../../x']) {
    context.testLocalUrl=value;assert.equal(vm.runInContext('safeLink(testLocalUrl)',context),'#');
  }
});

test('Python fences highlight syntax and preserve Chinese comments, indentation and text', () => {
  const {context}=page();
  context.sample='```python\nimport numpy as np\n\ndef cross_entropy(logits, labels):\n    # 保留原文和缩进\n    return np.log(1.0)\n```';
  const markup=vm.runInContext('markdown(sample)',context);
  assert.match(markup,/code-label">Python/);
  assert.match(markup,/hljs-keyword/);assert.match(markup,/hljs-comment/);assert.match(markup,/hljs-number/);
  assert.ok(markup.includes('    <span class="hljs-comment"># 保留原文和缩进'));
  assert.match(markup,/tabindex="0"/);
});

test('PDF quote lines form one readable block with separate paragraphs and escaped text', () => {
  const {context}=page();
  context.sample='> answers from questions (Wei et al.,\n> 2022). The model uses\n> its own representations.\n>\n> 中文原文 <script>alert(1)</script>\n\nAfter the quote.';
  const markup=vm.runInContext('markdown(sample)',context);
  assert.equal((markup.match(/<blockquote>/g)||[]).length,1);
  assert.ok(markup.includes('<p>answers from questions (Wei et al., 2022). The model uses its own representations.</p>'));
  assert.ok(markup.includes('<p>中文原文 &lt;script&gt;alert(1)&lt;/script&gt;</p>'));
  assert.ok(markup.endsWith('</blockquote><p>After the quote.</p>'));
  assert.ok(!markup.includes('<details'));
});

test('existing local evidence keeps links visible and folds only its excerpts and file locations', () => {
  const {context}=page({URL});
  const url='/api/spaces/'+'a'.repeat(32)+'/materials/'+'b'.repeat(32)+'/reader#local-L1155';
  context.sample='Answer.\n\n## 本地证据\n\n- [L1155] [ReAct.pdf · 第 2 页]('+url+')\n\n  文件：D:\\paper\\原文.pdf\n> First passage\n> continues here.\n\n> Separate excerpt.\n\n## 补充说明\n文件：D:\\other.pdf\n\n> Ordinary quote.';
  const markup=vm.runInContext('markdown(sample)',context);
  assert.ok(markup.includes('href="'+url+'"'));
  assert.ok(markup.indexOf('ReAct.pdf')<markup.indexOf('<details'));
  assert.equal((markup.match(/class="evidence-excerpt"/g)||[]).length,2);
  assert.equal((markup.match(/class="evidence-location"/g)||[]).length,1);
  assert.ok(markup.includes('<p>First passage continues here.</p>'));
  assert.ok(markup.includes('D:\\paper\\原文.pdf'));
  assert.ok(!/<details[^>]*\bopen\b/.test(markup));
  assert.ok(markup.endsWith('<blockquote><p>Ordinary quote.</p></blockquote>'));
});

test('background refresh preserves a reader opening and closing a source excerpt', () => {
  const {element,context}=page(), pane=element('job-summary');
  context.sample='<details class="evidence-excerpt"><summary>查看原文摘录</summary><blockquote>原文</blockquote></details>';
  const render=()=>vm.runInContext("updateHTML(document.getElementById('job-summary'),sample)",context);
  render();
  pane.html=pane.html.replace('<details ', '<details open="" ');
  render();assert.equal(pane.writes,1);assert.ok(pane.html.includes('open=""'));
  pane.html=pane.html.replace(' open=""','');
  render();assert.equal(pane.writes,1);
  context.sample='<p>Updated answer</p>';
  render();assert.equal(pane.writes,2);assert.equal(pane.html,context.sample);
});

test('unfinished streamed fences render as code and unknown languages or HTML remain escaped', () => {
  const {context}=page();
  context.sample='说明\n~~~python\nprint("你好")';
  assert.match(vm.runInContext('markdown(sample)',context),/hljs-built_in/);
  context.sample='```unknown\n<img src=x onerror=alert(1)>\n```';
  const markup=vm.runInContext('markdown(sample)',context);
  assert.ok(markup.includes('&lt;img'));assert.ok(!markup.includes('<img'));
  context.sample='````python\ntext = "```"\nprint(text)\n````';
  assert.ok(vm.runInContext('markdown(sample)',context).includes('print'));
  vm.runInContext('hljs=undefined',context);
  assert.match(vm.runInContext('markdown(sample)',context),/code-block/);
});

test('streamed drafts stay separate from verification and retry discards interrupted output', () => {
  const {context}=page();
  vm.runInContext("state={steps:[]}; applyLiveEvent(state,{kind:'model_stream',payload:{phase:'start',request_id:'one',purpose:'answer',model:'Luna'}}); applyLiveEvent(state,{kind:'model_stream',payload:{phase:'update',request_id:'one',purpose:'answer',text:'草稿第一句。',characters:6,streaming:true}})",context);
  assert.equal(vm.runInContext('state.text',context),'草稿第一句。');
  vm.runInContext("applyLiveEvent(state,{kind:'model_stream',payload:{phase:'start',request_id:'check',purpose:'answer_verification'}}); applyLiveEvent(state,{kind:'model_stream',payload:{phase:'update',request_id:'check',purpose:'answer_verification',text:'private JSON',characters:30}})",context);
  assert.equal(vm.runInContext('state.text',context),'草稿第一句。');
  assert.match(vm.runInContext('state.modelStatus',context),/核验/);
  vm.runInContext("applyLiveEvent(state,{kind:'answer_draft',payload:{text:'修订后的草稿。'}}); applyLiveEvent(state,{kind:'model_stream',payload:{phase:'update',request_id:'one',purpose:'answer',text:'过期内容'}})",context);
  assert.equal(vm.runInContext('state.text',context),'修订后的草稿。');
  vm.runInContext("applyLiveEvent(state,{kind:'model_stream',payload:{phase:'retry',request_id:'check'}})",context);
  assert.equal(vm.runInContext('state.text',context),'');
});

test('verification recovery is distinct from starting research again', async()=>{
  const {context,element}=page();
  for (const id of ['no-job','detail','trace-download']) element(id).classList={add(){},remove(){},toggle(){}};
  element('job-budget').closest=()=>({classList:{toggle(){}}});
  const job={question:'q',status:'failed',kind:'RESEARCH',research_effort:'quick',created_at:'2026-09-23',
    assumptions:'[]',brief:'',summary:'',error:'研究未完成有效核验：answer_verification_unavailable'};
  context.fetch=async()=>({ok:true,json:async()=>job});
  vm.runInContext("sid='space';jid='job';loadEvents=async()=>{}",context);
  await vm.runInContext("loadJob('job','space')",context);
  assert.match(element('job-actions').innerHTML,/继续核验草稿/);
  assert.match(element('job-actions').innerHTML,/重新研究/);
  job.error='model_error';await vm.runInContext("loadJob('job','space')",context);
  assert.match(element('job-actions').innerHTML,/从检查点继续/);
  vm.runInContext("state={steps:[]};applyLiveEvent(state,{kind:'answer_check_started',payload:{}})",context);
  assert.match(vm.runInContext('state.action',context),/核验已保存草稿/);
});

test('tool activity is readable and never inserted as unescaped HTML', () => {
  const {context}=page();
  vm.runInContext("state={steps:[]};applyLiveEvent(state,{kind:'tool_call_requested',payload:{name:'read',args:{url:'https://example.com/<script>'}}})",context);
  assert.match(vm.runInContext('state.action',context),/^阅读原文/);
  assert.match(vm.runInContext('esc(state.steps[0])',context),/&lt;script&gt;/);
  assert.match(vm.runInContext("eventDescription({event:'auto_tool_call',name:'coding_agent',args:{task:'实现候选'}})",context),/委派编码与实验：实现候选/);
  assert.match(vm.runInContext("eventDescription({event:'auto_tool_call',name:'run_experiments',args:{}})",context),/运行已有代码的实验/);
  assert.match(vm.runInContext("eventDescription({event:'auto_tool_result',tool_kind:'coding',result:{status:'failed',error:'timeout'}})",context),/编码与实验 · 未完成：timeout/);
});

test('switching conversations closes the stream and rejects stale or duplicate events', () => {
  const streams=[];
  class Stream {
    constructor(url){this.url=url;this.events={};streams.push(this);}
    addEventListener(name,fn){this.events[name]=fn;}
    close(){this.closed=true;}
  }
  const {context,element}=page({EventSource:Stream});
  element('chat-live').replaceChildren=()=>{};
  vm.runInContext("renderLive=()=>{};sid='space';cid='one';connectLive();liveJobs.set('job',{steps:[]})",context);
  streams[0].events.progress({lastEventId:'7',data:JSON.stringify({job_id:'job',kind:'answer_draft',payload:{text:'first'}})});
  streams[0].events.progress({lastEventId:'7',data:JSON.stringify({job_id:'job',kind:'answer_draft',payload:{text:'duplicate'}})});
  assert.equal(vm.runInContext("liveJobs.get('job').text",context),'first');
  vm.runInContext("cid='two';connectLive();liveJobs.set('job',{text:'new'})",context);
  assert.equal(streams[0].closed,true);
  streams[0].events.progress({lastEventId:'8',data:JSON.stringify({job_id:'job',kind:'answer_draft',payload:{text:'old conversation'}})});
  assert.equal(vm.runInContext("liveJobs.get('job').text",context),'new');
  assert.match(streams[1].url,/conversations\/two\/events$/);
});

function audioHarness() {
  const instances=[];
  class Audio {
    constructor() {this.state='suspended';this.currentTime=0;this.destination={};this.sources=[];instances.push(this);}
    createBuffer(channels,length,sampleRate) {
      const data=Array.from({length:channels},()=>new Float32Array(length));
      return {length,sampleRate,numberOfChannels:channels,getChannelData:i=>data[i]};
    }
    createGain() {return {connect(){},gain:{value:0,setTargetAtTime(value){this.value=value;}}};}
    createBufferSource() {const source={connect(){},start(){this.started=true;}};this.sources.push(source);return source;}
    async resume() {this.state='running';this.onstatechange?.();}
    async suspend() {this.state='suspended';this.onstatechange?.();}
    async close() {this.state='closed';this.onstatechange?.();}
  }
  return {Audio,instances};
}

test('fire sound is a finite stereo loop with audible energy, bounded peaks and no boundary pop', () => {
  const {Audio}=audioHarness(), {context}=page();
  let seed=42;
  const random=()=>((seed=(1664525*seed+1013904223)>>>0)/4294967296);
  const buffer=vm.runInContext('createFireBuffer',context)(new Audio(),random);
  assert.equal(buffer.numberOfChannels,2);assert.equal(buffer.length/buffer.sampleRate,24);
  for (let channel=0;channel<2;channel++) {
    const data=buffer.getChannelData(channel);let energy=0,peak=0;
    for (const value of data) {assert.ok(Number.isFinite(value));energy+=value*value;peak=Math.max(peak,Math.abs(value));}
    const rms=Math.sqrt(energy/data.length);
    assert.ok(rms>.025 && rms<.25,`Unexpected sound energy: ${rms}`);
    assert.ok(peak<1);assert.ok(Math.abs(data[0]-data.at(-1))<.1,'Loop boundary clicks');
  }
  assert.notEqual(buffer.getChannelData(0)[100],buffer.getChannelData(1)[100]);
});

test('sound starts only on click, reuses one loop after pause, and zero volume survives reload', async () => {
  const {Audio,instances}=audioHarness(), saved=new Map([['research-fire-volume','0']]);
  const {element}=page({AudioContext:Audio,localStorage:{getItem:k=>saved.get(k)??null,setItem:(k,v)=>saved.set(k,v)},setTimeout:fn=>fn()});
  const button=element('fire-sound'), slider=element('fire-volume');
  assert.equal(instances.length,0);assert.equal(slider.value,'0');
  const starting=button.onclick();await button.onclick();await starting;
  assert.equal(instances.length,1);assert.equal(instances[0].sources.length,1);
  assert.equal(instances[0].sources[0].loop,true);assert.equal(button.attributes['aria-pressed'],'true');
  assert.equal(element('hearth-audio').attributes['data-playing'],'false');
  slider.value='35';slider.oninput();assert.equal(saved.get('research-fire-volume'),'35');
  assert.equal(element('fire-volume-value').textContent,'35%');
  await button.onclick();assert.equal(instances[0].state,'suspended');assert.equal(button.attributes['aria-pressed'],'false');
  await button.onclick();assert.equal(instances.length,1);assert.equal(instances[0].sources.length,1);
  const next=page({localStorage:{getItem:k=>saved.get(k)??null}});
  assert.equal(next.element('fire-volume').value,'35');
  assert.equal(next.element('fire-sound').attributes['aria-pressed'],'false');
});

test('unavailable audio or blocked preferences leave the workbench usable and allow a retry', async () => {
  const {element,context}=page({localStorage:{getItem(){throw new Error('blocked');}}});
  const button=element('fire-sound');await button.onclick();
  assert.equal(button.disabled,false);assert.equal(button.attributes['aria-pressed'],'false');
  assert.match(element('fire-sound-status').textContent,/再试一次/);
  const {Audio}=audioHarness();context.AudioContext=Audio;
  await button.onclick();assert.equal(button.attributes['aria-pressed'],'true');
  assert.equal(element('fire-sound-status').textContent,'');
});


test('V4 submits an explicit bounded experiment in the selected conversation',async()=>{
  const {element,context}=page();let sent;
  vm.runInContext("sid='space';cid='chat';notebook.space='space';loadExecutions=async()=>{}",context);
  element('records-execution-seconds').value='240';element('records-execution-tokens').value='200000';element('records-execution-rounds').value='3';
  context.fetch=async(url,request)=>{sent={url,body:JSON.parse(request.body)};return {ok:true,json:async()=>({})};};
  const button={dataset:{runExperiment:'version'},disabled:false};
  await element('records-experiment-list').onclick({preventDefault(){},target:{closest:selector=>selector==='[data-run-experiment]'?button:null}});
  assert.equal(sent.body.authorize_execution,true);assert.equal(sent.body.conversation_id,'chat');
  assert.equal(sent.body.seconds,240);assert.equal(sent.body.token_budget,200000);assert.equal(sent.body.max_iterations,3);
  assert.equal(vm.runInContext('cid',context),'chat');assert.equal(button.disabled,false);
});

test('V4 execution results arriving after workspace switch are discarded',async()=>{
  const {element,context}=page();let finish;
  vm.runInContext("notebook.space='old'",context);context.fetch=()=>new Promise(resolve=>finish=resolve);
  const loading=vm.runInContext('loadExecutions()',context);
  vm.runInContext("notebook.space='new'",context);element('records-execution-list').innerHTML='new area';
  finish({ok:true,json:async()=>({executions:[]})});await loading;
  assert.equal(element('records-execution-list').innerHTML,'new area');
});

test('V4 evidence suite selection exposes run and audit artifacts',async()=>{
  const {element,context}=page();let submitted;
  vm.runInContext("sid='space';notebook.space='space'",context);
  element('records-execution-title').value='content ranking';
  element('records-execution-hypothesis').value='hypothesis only';
  element('records-execution-suite').value='public-evidence-rerank-v1';
  const record={id:'exp',title:'content ranking',kind:'experiment',versions:[{id:'version',version:1,content:{status:'planned',config:{dataset:'public-evidence-rerank-v1'}}}]};
  context.fetch=async(url,request)=>{
    if(url.endsWith('/execution-plans')){submitted=JSON.parse(request.body);return {ok:true,json:async()=>({})};}
    if(url.endsWith('/executions'))return {ok:true,json:async()=>({executions:[{job_id:'run',job:{question:'content',status:'completed'},state:{phase:'completed'},contract:{suite:'public-evidence-rerank-v1'}}]})};
    return {ok:true,json:async()=>({experiments:[record]})};
  };
  await element('records-execution-plan').onsubmit({preventDefault(){}});
  assert.equal(submitted.suite,'public-evidence-rerank-v1');
  assert.match(element('records-experiment-list').innerHTML,/data-run-experiment="version"/);
  await vm.runInContext('loadExecutions()',context);
  for(const file of ['evidence-rerank.json','development-snapshots.json','submitted-ranker.py'])assert.ok(element('records-execution-list').innerHTML.includes(file));
});

test('bounded iteration view shows actual percentage units and escaped round artifacts',()=>{
  const {context}=page();
  const run={job_id:'run',state:{iteration:{max_iterations:3,totals:{coding_invocations:2,tokens:42,complete:true},
    stop_reason:'no_improvement',best:{label:'round-1-candidate'},rounds:[{index:1,before:{recall_at5:64.92},
    after:{recall_at5:80},improved:true,status:'completed',artifacts:{receipt:'round-one-codex.json'}}]}}};
  context.iterationRun=run;
  const result=vm.runInContext("iterationSummary(iterationRun,'/api')",context);
  assert.match(result,/64.92% → 80.00%/);assert.match(result,/连续两轮无改善/);
  assert.match(result,/round-one-codex.json/);assert.match(result,/iterations.json/);
});
