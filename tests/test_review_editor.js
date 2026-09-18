/* Offline frontend regressions. Run: node tests/test_review_editor.js
 * No server, provider, real saved run, or browser package is required.
 */
'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'prototypes/review-workbench/index.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\nboot\(\);\n/,'\n');
const nodes=new Map(),events={};
function node(id){if(!nodes.has(id))nodes.set(id,{id,value:'',textContent:'',innerHTML:'',disabled:false,hidden:false,checked:false,dataset:{},classList:{toggle(){}},setAttribute(){},addEventListener(){},scrollIntoView(){},focus(){},querySelectorAll(){return[];},querySelector(){return null;}});return nodes.get(id);}
const document={getElementById:node,addEventListener(k,f){(events[k]??=[]).push(f);}};
const requests=[];let responder=async()=>({});
const ctx=vm.createContext({document,URL,URLSearchParams,window:{innerWidth:1440},location:{origin:'http://localhost',pathname:'/',search:''},history:{replaceState(){}},navigator:{},setTimeout(){return 1;},clearTimeout(){},console,fetch:async(url,options={})=>{requests.push({url,body:options.body?JSON.parse(options.body):null});return {ok:true,status:200,json:async()=>responder(url,options)};}});
vm.runInContext(script,ctx);
const run=code=>vm.runInContext(code,ctx),copy=o=>JSON.parse(JSON.stringify(o));let count=0;
function check(ok,message){assert.ok(ok,message);count++;}
const behavior={schema:'review_behavior/1',horizon:2,step:{value:'1',unit:'s'},variables:[{name:'voltage',type:'Real',role:'state',unit:'V'}],initial:[{op:'=',args:[{var:'voltage'},{value:'27',unit:'V'}]}],transitions:[],properties:[{id:'P',kind:'always',requirement_ids:['R1'],predicate:{op:'<=',args:[{var:'voltage'},{value:'28',unit:'V'}]}}]};
const inspection={schema:'review_candidate/1',behavior_sha256:'candidate-hash',rows:[{pointer:'/properties/0/predicate',category:'property',label:'Voltage limit',expression:'(voltage <= 28[V])',expression_sha256:'property-hash',editable:true,requirement_ids:['R1'],provenance:{origin:'source',requirement_ids:['R1'],rationale:'The source states <script>28 V</script>'}},{pointer:'/variables/0',category:'variable',label:'voltage',expression:'voltage : Real [V]',expression_sha256:'variable-hash',editable:false,provenance:{origin:'llm',requirement_ids:[],rationale:'Proposed state'}}],diagnostics:[{code:'MISSING_STATE_UPDATE',severity:'warning',message:'voltage has no next-state rule',pointers:['/variables/0']}],provenance:{'/properties/0/predicate':{expression_sha256:'property-hash',origin:'source',requirement_ids:['R1'],rationale:'Source bound'}}};
const fixture={id:'run-fixture',name:'Example',engine:'local',status:'completed',analysis_mode:'check_design',source_hash:'source',evidence_hash:'evidence',contract_review_hash:'contracts',assumption_review_hash:'assumptions',architecture_binding_review_hash:'binding-hash',requirements:[{id:'R1',text:'Voltage at most 28 V'}],behavior, candidate_inspection:inspection,candidate_provenance:inspection.provenance,model:{text:'package Demo {}',inspection:{text_sha256:'model-hash',elements:[]}},analysis:{status:'sat'},compilation:{status:'passed'},contracts:{schema:'review_contracts/1',contracts:[]},reviews:[],baseline:{blockers:[]},correction_summary:{sha256:'summary-hash',summary:'Candidate dynamics corrected',changes:[{category:'design',subject:'Voltage update',before:'unconstrained',after:'hold at 27 V',explanation:'Added a design rule <script>safe</script>'}]},parent_run_id:'parent'};
(async()=>{
run(`state.run=${JSON.stringify(fixture)};state.view='run';state.config={engines:[{id:'local',available:true}]};`);
node('input-engine').value='local';node('input-analysis-mode').value='check_design';node('input-behavior').value=JSON.stringify(behavior);node('input-text').value='R1: Voltage at most 28 V';node('input-format').value='text';
check(run("tabLabels.some(t=>t[0]==='bindings')"),'Architecture bindings tab is available');
const rows=run(`candidateRowsView(${JSON.stringify(inspection)},true,'input')`);
check(rows.includes('next-state')===false&&rows.includes('(voltage &lt;= 28[V])'),'Equations render as readable, escaped text');
check(rows.includes('&lt;script&gt;28 V&lt;/script&gt;')&&!rows.includes('<script>28 V'),'Provenance text cannot inject markup');
check(rows.includes('data-candidate-field="origin"')&&rows.includes('data-candidate-field="requirement_ids"')&&rows.includes('data-candidate-field="rationale"'),'Each row has origin, source IDs, and rationale controls');
check(run(`candidateDiagnostics(${JSON.stringify(inspection)})`).includes('voltage has no next-state rule'),'Missing updates remain visible');
check(run('savedCandidateInspection(state.run)').includes('Recorded candidate equations'),'Saved candidates have readable inspection');
check(run('correctionSummaryView(state.run)').includes('Before')&&run('correctionSummaryView(state.run)').includes('After'),'Correction review shows before and after');
check(!run('correctionSummaryView(state.run)').includes('<script>safe'),'Correction explanation cannot inject markup');
check(run('hasReviewChanges(state.run)'),'Design-only corrections require final acknowledgment');
run("candidateDraft('input').dirty=true");assert.throws(()=>run("candidatePayload('input')"),/Apply or inspect/);count++;
run("candidateDraft('input').dirty=false;candidateDraft('input').provenance="+JSON.stringify(inspection.provenance));
check(run("candidatePayload('input').candidate_provenance['/properties/0/predicate'].origin")==='source','Run payload carries explicit candidate provenance');
run('draftFor(state.run).acknowledge=true;state.run.architecture_binding_review_hash="new-binding";draftFor(state.run)');check(!run('draftFor(state.run).acknowledge'),'Changed binding review invalidates final acknowledgment');
run('draftFor(state.run).acknowledge=true;state.run.correction_summary.sha256="new-summary";draftFor(state.run)');check(!run('draftFor(state.run).acknowledge'),'Changed correction summary invalidates final acknowledgment');
run("draftFor(state.run).designAcknowledge=true;candidateWrite('revision',"+JSON.stringify(behavior)+')');check(!run('draftFor(state.run).designAcknowledge'),'Candidate edits clear design acknowledgment');
const bdata={review_hash:'binding-hash',records:[],choices:[{id:'part-1',kind:'part',qualified_name:'Demo::battery',metadata_status:'not_a_value',value_element_ids:['voltage-1']},{id:'voltage-1',kind:'attribute',qualified_name:'Demo::battery::voltage',type:'Real',unit:'V',metadata_status:'recognized'}],status:{model_sha256:'model-hash',targets:[{target_kind:'variable',target_id:'voltage',label:'voltage',type:'Real',unit:'V',required:true,review_status:'pending'}],blockers:[]}};
run('bindingState(state.run).data='+JSON.stringify(bdata));run('bindingDraft(state.run,bindingSelection(state.run)).element_id="part-1"');
const view=run('bindingsView(state.run)');check(view.includes('Contained value attribute')&&view.includes('Demo::battery::voltage'),'Part binding exposes actual contained value choices');check(view.includes('data-binding-element="part-1"'),'Binding can navigate to actual SysML element');check(view.includes('compatibility remains unestablished'),'Unknown compatibility acknowledgment does not claim verification');
const before=copy(fixture);responder=async url=>url.endsWith('/bindings')?bdata:url.endsWith('/reviews')?{id:'saved'}:copy(fixture);
run("const bd=bindingDraft(state.run,bindingSelection(state.run));bd.value_element_id='voltage-1';bd.reviewer='Engineer';bd.rationale='Voltage attribute allocation';bd.decision='accept';state.tab='bindings';");
await run('submitBindingReview()');const bp=requests.find(r=>r.url.endsWith('/bindings/reviews')).body;
check(bp.model_sha256==='model-hash'&&bp.architecture_binding_review_hash==='binding-hash','Binding decision pins model and review hashes');check(bp.target_kind==='variable'&&bp.value_element_id==='voltage-1','Binding records variable and contained value');
run("const fd=draftFor(state.run);fd.reviewer='Engineer';fd.rationale='Reviewed correction';fd.scope='Demo';fd.acknowledge=true;state.tab='review';");await run('submitReview()');
const fp=requests.filter(r=>r.url.endsWith('/reviews')&&!r.url.endsWith('/bindings/reviews')).at(-1).body;
check(fp.architecture_binding_review_hash==='binding-hash'&&fp.correction_summary_sha256==='summary-hash','Final review includes binding and correction hashes');check(fp.acknowledge_contract_changes,'Final acknowledgment includes design-only corrections');
check(!requests.some(r=>r.url==='/api/runs'||r.url.includes('provider')),'Inspection/review tests do not submit a generation or provider request');
check(run("qualityView(state.run)").includes('No quality snapshot'), 'Historical runs do not receive an invented quality assessment');
run('state.run.formalization_quality='+JSON.stringify({dimensions:[{id:'interpretation_coverage',label:'Interpretation coverage',status:'pending_review',summary:'Candidate <script> meaning needs review',counts:{source_requirements:1,pending_review:1},requirement_ids:{pending_review:['R1']},evidence:['formalization_quality.json']}],limitations:['No source-fidelity proof.'],review_scope:'Analysis completion snapshot'}));
run('state.run.execution_config={engine:"local",analysis_mode:"requirements"};state.tab="quality";renderPanel()');
const quality=node('panel').innerHTML;
check(quality.includes('pending review')||quality.includes('Pending review'), 'Quality view preserves pending interpretation status');
check(!quality.includes('<script>')&&quality.includes('&lt;script&gt;'), 'Quality summaries escape generated text');
check(quality.includes('Execution settings shared by CLI and GUI')&&quality.includes('Analysis completion snapshot'), 'Quality view distinguishes execution settings and generation-time scope');
run('state.uploadedSource='+JSON.stringify({raw:'id,text\r\nR1,Voltage <= 28 V\r\n',display:'id,text\nR1,Voltage <= 28 V\n'})+";document.getElementById('input-text').value=state.uploadedSource.display");
check(run('sourceInputText()').includes('\r\n'), 'An unedited upload retains original CRLF source bytes');
run("document.getElementById('input-text').value='R1: changed source'");
check(run('sourceInputText()')==='R1: changed source', 'Changed textarea content replaces uploaded source');
console.log(`${count} frontend editor and review checks passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
