"""Self-contained UI for bounded, on-demand acquisition browsing."""
PAGE = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EPU Mapper · Unified review</title><style>
[hidden]{display:none!important}:root{font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#203047;background:#f3f6fa}*{box-sizing:border-box}body{margin:0}button,select,input,textarea{font:inherit;border:1px solid #ccd7e4;border-radius:7px;background:white;color:inherit;padding:7px}button{cursor:pointer}button:hover,button.active{background:#e2f3f0;border-color:#309b90}button:disabled{opacity:.4;cursor:default}header{display:flex;justify-content:space-between;gap:15px;padding:18px 22px;background:white;border-bottom:1px solid #dbe3ed;align-items:center}h1{font-size:21px;margin:0}h2{font-size:14px;margin:0}small,.muted{color:#6a7a90;font-size:12px}.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}#status{padding:12px 22px;min-height:45px}#error{color:#a5223b;background:#fff0f2;padding:12px;display:none;white-space:pre-wrap}.layout{display:grid;grid-template-columns:190px minmax(0,1fr) 220px;gap:12px;padding:0 18px 18px}aside,.card{background:#fff;border:1px solid #dbe3ed;border-radius:12px;min-width:0}aside{padding:12px;align-self:start;position:sticky;top:12px;max-height:92vh;overflow:auto}aside h2{margin:7px 0 10px}.list{max-height:32vh;overflow:auto;display:grid;gap:5px;margin:8px 0}.list button{text-align:left;font-size:12px;overflow-wrap:anywhere}.list small{display:block}.images{display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:start}.card{overflow:hidden}.heading{height:62px;padding:12px;display:flex;justify-content:space-between;align-items:center;gap:8px}.heading small{display:block;max-width:30vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.viewport{height:clamp(250px,32vw,480px);position:relative;overflow:hidden;background:#101824;touch-action:none;cursor:grab}.scene{position:absolute;transform-origin:center}.scene img{width:100%;height:100%;display:block}.scene svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}.scene circle{cursor:pointer;stroke:white;stroke-width:.003;fill:#248f84;fill-opacity:.7}.scene circle.selected{fill:#f2c450;stroke:#f2c450;stroke-width:.007}.message{position:absolute;bottom:8px;left:8px;right:8px;background:#101824dc;padding:7px;color:#dde6ef;font-size:12px;pointer-events:none;border-radius:5px}.controls{padding:9px;display:flex;gap:5px;align-items:center;flex-wrap:wrap;font-size:11px;min-height:45px}.controls button{font-size:11px;padding:5px 8px}.nav{padding:8px;display:flex;align-items:center;justify-content:space-between;gap:5px;min-height:46px;border-top:1px solid #e8edf3}.nav small{text-align:center}.contrast{padding:9px;font-size:12px}.contrast label{display:inline-flex;align-items:center;gap:4px;margin:4px}.contrast input{width:62px;padding:4px}.contrast select{font-size:12px}textarea{width:100%;height:120px;resize:vertical}aside label{display:block;margin:10px 0 5px}aside select{width:100%}#strips{display:flex;gap:5px;overflow:auto;padding:8px}#strips button{font-size:11px;min-width:48px}#strips img{width:60px;height:50px;object-fit:contain;display:block}#legend{font-size:11px;color:#6a7a90;padding:0 18px 10px}.enlarged{position:fixed;inset:12px;z-index:20;display:flex;flex-direction:column;box-shadow:0 0 0 30px #0009}.enlarged .viewport{height:auto;flex:1;min-height:0}.enlarged .heading small{max-width:75vw}.foil-only #data{display:none}.foil-only #foil{grid-column:1/-1}.foil-only #foil .viewport{height:420px}a{color:#147d76}.badge{font-size:11px;border-radius:12px;background:#dff3ee;color:#237867;padding:5px 9px}#cacheStatus{font-size:12px;white-space:pre-wrap;margin-top:8px}@media(max-width:1050px){.layout{grid-template-columns:155px minmax(0,1fr)}aside.review{grid-column:1/-1;position:static;max-height:none}.review textarea{height:70px}.viewport{height:300px}}@media(max-width:700px){header{display:block}.layout{display:block}aside{position:static;max-height:none;margin-bottom:10px}.images{grid-template-columns:1fr}.heading small{max-width:70vw}.viewport{height:350px}}
</style><header><div><h1>EPU Mapper <span id="mode" class="badge"></span></h1><small id="title"></small></div><div class="actions"><button id="refresh">Refresh index</button><button id="cache">Prepare local previews</button><a href="/api/annotations.json" download>Export annotations</a></div></header>
<div id="status" role="status">Opening local index…</div><div id="error" role="alert"></div><div id="legend">Atlas: green = available · amber = no Data preview · suitable = green ring · unsuitable = red ring · selected hole = thicker group outline. Scroll to zoom · drag to pan.</div>
<main class="layout"><aside><h2>GridSquares</h2><input id="search" placeholder="Find GridSquare" aria-label="Find GridSquare" style="width:100%"><div id="grids" class="list"></div><h2>FoilHoles</h2><div id="holes" class="list"></div><div class="actions"><button id="pagePrev">← Page</button><button id="pageNext">Page →</button></div><small id="page"></small></aside>
<section class="images" id="images"></section><aside class="review"><h2>Review annotation</h2><label for="scope">Annotate</label><select id="scope"><option value="grid">GridSquare</option><option value="hole">FoilHole</option><option value="exposure">Exposure</option></select><small id="target"></small><label for="rating">Rating</label><select id="rating"><option value="0">Unrated</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select><label for="suitability">Collection suitability</label><select id="suitability"><option value="">Unmarked</option><option value="suitable">Suitable</option><option value="unsuitable">Not suitable</option></select><label><input type="checkbox" id="flag"> Flag for follow-up</label><label for="comment">Comment</label><textarea id="comment" placeholder="Notes for this selection"></textarea><button id="save">Save annotation</button><p class="muted" id="saved">Saved locally, not written to the network share. Cmd/Ctrl+Enter saves and advances a square.</p><div id="cacheStatus"></div></aside></main>
<script>
const config=__CONFIG__, $=id=>document.getElementById(id);let grids=[],grid=null,hole='',holeRows=[],holeOffset=0,holeTotal=0,pairs=[],shot=0,selection=0,atlas=null,annotationKey='',annotationToken=0,pendingRefresh=false,navigationIntent=0;
$('mode').textContent='Unified review';$('title').textContent=config.label;
if(config.mode==='foilhole'){$('scope').querySelector('[value=exposure]').disabled=true;$('legend').textContent='Data loading is off (change Ignore Data images in the launcher). FoilHole overlays remain available. Scroll to zoom · drag to pan · click to select.';}
const fail=e=>{$('error').textContent=e.message||String(e);$('error').style.display='block'};
// Activity is independent of viewer messages: loading never masquerades as
// missing data, and concurrent operations cannot clear each other's indicator.
const activityStyle=document.createElement('style');activityStyle.textContent=`
.activity{display:flex;align-items:center;gap:8px;color:#164f69;background:#eaf6fc;border:1px solid #afd7e9;border-radius:7px;padding:8px 11px;font-size:12px}
.activity::before{content:'';width:13px;height:13px;flex-shrink:0;border:2px solid #a9ccdc;border-top-color:#176986;border-radius:50%;animation:activity-spin .8s linear infinite}
#activitySummary{margin:8px 18px}.viewer-activity{position:absolute;top:8px;left:8px;right:8px;z-index:3;pointer-events:none;background:#eaf6fcf2}
@keyframes activity-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.activity::before{animation:none}}
`;document.head.append(activityStyle);
const activitySummary=document.createElement('div');activitySummary.id='activitySummary';activitySummary.className='activity';activitySummary.hidden=true;activitySummary.setAttribute('role','status');activitySummary.setAttribute('aria-live','polite');document.querySelector('header').after(activitySummary);
const activities=new Map(),jobActivities=new Map();let activitySerial=0;
function paintActivity(){
 const current=[...activities.values()].filter(a=>a.valid());
 const describe=a=>{const seconds=Math.floor((Date.now()-a.started)/1000);return a.label+(seconds>=2?' · '+seconds+'s':'')+(seconds>=12?' · still working; network reads may take longer':'')};
 activitySummary.hidden=!current.length;
 activitySummary.textContent=current.map(describe).join(' · ');
 for(const viewport of document.querySelectorAll('.viewport')){
  const id=viewport.parentElement.id,matching=current.filter(a=>a.viewer===id);
  let badge=viewport.querySelector('.viewer-activity');
  if(!badge){badge=document.createElement('div');badge.className='activity viewer-activity';badge.setAttribute('role','status');viewport.append(badge)}
  badge.hidden=!matching.length;badge.textContent=matching.map(describe).join(' · ');viewport.setAttribute('aria-busy',matching.length?'true':'false');
 }
}
function beginActivity(label,viewer='',valid=()=>true){
 const id=++activitySerial;activities.set(id,{label,viewer,valid,started:Date.now()});paintActivity();
 return {update(text){const a=activities.get(id);if(a){a.label=text;paintActivity()}},finish(){activities.delete(id);paintActivity()}};
}
setInterval(paintActivity,1000);
function describeRequest(path,data){
 if(path.startsWith('/api/prepare/'))return ['Preparing image from cache or source',data?.viewer||''];
 if(path.startsWith('/api/geometry/'))return ['Mapping FoilHole positions','grid'];
 if(path.startsWith('/api/areas/'))return ['Mapping planned acquisition areas','grid'];
 if(path.startsWith('/api/density/grid/'))return ['Mapping particle density','grid'];
 if(path.startsWith('/api/holes/'))return ['Loading FoilHole list','grid'];
 if(path.startsWith('/api/exposures/'))return ['Finding matching Data previews','data'];
 if(path==='/api/cache-previews')return ['Preparing local previews',''];
 if(path==='/api/refresh')return ['Starting index refresh',''];
 if(path.startsWith('/api/annotation/'))return [data?'Saving annotation':'Loading annotation',''];
 return null;
}
async function api(path,data){
 const description=describeRequest(path,data),activity=description?beginActivity(...description):null;
 try{const r=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok){let t=await r.text();try{t=JSON.parse(t).detail||t}catch{}throw Error(t)}const result=await r.json();if(result.job&&description)jobActivities.set(result.job,description);return result}
 finally{activity?.finish()}
}
async function waitJob(id,valid=()=>true){
 const description=jobActivities.get(id)||['Processing data',''];jobActivities.delete(id);
 const activity=beginActivity(description[0],description[1],valid);
 try{for(let n=0;n<1800&&valid();n++){const j=await api('/api/jobs/'+id);if(j.status==='done')return j.result;if(j.status==='error'||j.status==='cancelled')throw Error(j.message);activity.update((j.status==='queued'?'Queued: ':'')+description[0]);await new Promise(r=>setTimeout(r,350))}if(valid())throw Error('Still waiting for the network. Retry when the share is available.');return null}
 finally{activity.finish()}
}
function button(text,fn){const b=document.createElement('button');b.textContent=text;b.onclick=()=>Promise.resolve(fn()).catch(fail);return b}
// Overlay radius is measured in image coordinates, so it follows zoom and pan.
function normalizeOverlay(value={}){const radius=Number(value.radius);return {style:value.style==='filled'?'filled':'outline',radius:Number.isFinite(radius)?Math.max(.2,Math.min(4,radius)):1.2}}
let foilOverlay=normalizeOverlay();
try{foilOverlay=normalizeOverlay(JSON.parse(localStorage.getItem('epu-foil-overlay'))||{})}catch{}
function groupColor(anchor){if(!anchor)return '#9ba9bc';let h=0;for(const c of String(anchor))h=(h*31+c.charCodeAt(0))>>>0;return 'hsl('+((h*137.508)%360)+' 75% 65%)'}
function styleFoilCircle(circle,selected,marker={}){
 const color=marker.anchor?groupColor(marker.anchor):'#58cbb7';
 circle.setAttribute('r',foilOverlay.radius/100);
 circle.style.fill=selected||foilOverlay.style==='outline'?'none':marker.anchor?color:'#248f84';
 circle.style.stroke=marker.anchor?color:selected?'#f2c450':color;
 circle.style.strokeDasharray=marker.role==='shifted'?'.006 .004':marker.role==='unknown'?'.002 .004':'';
 circle.style.filter=selected?'drop-shadow(0px 0px 2px white)':'none';
 circle.style.strokeWidth=selected?'.003':'.0015';
 circle.style.pointerEvents='all';
}
class Viewer{
 constructor(id,title){this.id=id;this.zoom=1;this.x=0;this.y=0;this.token=0;this.key='';this.markers=[];this.card=document.createElement('article');this.card.className='card';this.card.id=id;this.card.innerHTML=`<div class="heading"><div><h2>${title}</h2><small class="filename"></small></div></div><div class="viewport"><div class="scene"><img alt="${title}" hidden><svg viewBox="0 0 1 1" preserveAspectRatio="none"></svg></div><div class="message">No image selected</div></div><div class="controls"></div><details class="contrast"><summary>Contrast & low-pass</summary><label>Black % <input class="low" type="number" min="0" max="99" value="1"></label><label>White % <input class="high" type="number" min="1" max="100" value="99"></label><label>Gamma <input class="gamma" type="number" min="0.2" max="3" step="0.1" value="1"></label><label>Low-pass σ <input class="sigma" type="number" min="0" max="4" step="0.5" value="0"></label><label>Auto <select class="routine"><option value="percentile">Percentile stretch</option><option value="equalize">Equalize</option></select></label><button class="apply">Apply</button></details>`;$('images').append(this.card);this.viewport=this.card.querySelector('.viewport');this.scene=this.card.querySelector('.scene');this.img=this.card.querySelector('img');this.svg=this.card.querySelector('svg');this.message=this.card.querySelector('.message');const controls=this.card.querySelector('.controls');controls.append(button('−',()=>{this.zoom=Math.max(.25,this.zoom/1.2);this.fit()}),button('+',()=>{this.zoom=Math.min(15,this.zoom*1.2);this.fit()}),button('Reset',()=>this.reset()),button('Preview',()=>this.load(this.previewKey,this.previewName,this.mrcKey)),this.mrcButton=button('Load MRC',()=>this.load(this.mrcKey,this.name+' · MRC',this.mrcKey,true)));this.mrcButton.hidden=true;this.card.querySelector('.heading').append(button('Enlarge',()=>{const enlarged=this.card.classList.toggle('enlarged');this.card.querySelector('.heading button').textContent=enlarged?'Close':'Enlarge';this.fit()}));this.card.querySelector('.apply').onclick=()=>this.load(this.key,this.name,this.mrcKey,this.isMrc,true);this.viewport.onwheel=e=>{e.preventDefault();this.zoom=Math.max(.25,Math.min(15,this.zoom*Math.exp(-e.deltaY*.002)));this.fit()};this.viewport.ondblclick=()=>this.reset();let drag=null;this.viewport.onpointerdown=e=>{if(['circle','polygon'].includes(e.target.tagName.toLowerCase()))return;drag=[e.clientX,e.clientY,this.x,this.y];this.viewport.setPointerCapture(e.pointerId)};this.viewport.onpointermove=e=>{if(drag){this.x=drag[2]+e.clientX-drag[0];this.y=drag[3]+e.clientY-drag[1];this.fit()}};this.viewport.onpointerup=this.viewport.onpointercancel=()=>drag=null;new ResizeObserver(()=>this.fit()).observe(this.viewport);
 }
 reset(){this.zoom=1;this.x=this.y=0;this.fit()}
 fit(){if(!this.img.naturalWidth)return;const r=this.viewport.getBoundingClientRect(),s=Math.min(r.width/this.img.naturalWidth,r.height/this.img.naturalHeight),w=this.img.naturalWidth*s,h=this.img.naturalHeight*s;Object.assign(this.scene.style,{width:w+'px',height:h+'px',left:(r.width-w)/2+'px',top:(r.height-h)/2+'px',transform:`translate(${this.x}px,${this.y}px) scale(${this.zoom})`})}
 mark(markers,click){this.markers=markers;this.svg.replaceChildren();for(const m of markers){const c=document.createElementNS('http://www.w3.org/2000/svg','circle');c.setAttribute('cx',m.x);c.setAttribute('cy',m.y);c.setAttribute('r',this.id==='atlas'?.018:.012);if(m.selected)c.classList.add('selected');if(m.color)c.style.fill=m.color;if(m.stroke)c.style.stroke=m.stroke;if(this.id==='grid')styleFoilCircle(c,m.selected,m);const t=document.createElementNS(c.namespaceURI,'title');t.textContent=m.label||('FoilHole '+m.hole+(m.anchor?' · '+(m.role==='anchor'?'centering hole':'beam-shifted from '+m.anchor):' · centering group unknown'));c.append(t);c.onclick=()=>Promise.resolve(click(m)).catch(fail);this.svg.append(c)}}
 clear(message='No matching image'){if(this.id==='data'&&config.mode==='foilhole')message='Data loading is off. Uncheck Ignore Data images in the launcher to include exposures.';this.token++;this.key='';this.img.hidden=true;this.img.removeAttribute('src');this.svg.replaceChildren();this.message.textContent=message;this.message.hidden=false;this.card.querySelector('.filename').textContent='';this.mrcButton.hidden=true}
 async load(key,name='',mrc='',isMrc=false,adjust=false){if(!key){this.clear('No matching '+this.id+' preview');return}const changed=key!==this.key,token=++this.token;this.key=key;this.name=name;this.mrcKey=mrc;this.isMrc=isMrc;if(isMrc)this.card.querySelector('details').open=true;if(!isMrc){this.previewKey=key;this.previewName=name;}this.img.hidden=true;this.message.hidden=false;this.message.textContent='Loading '+name+'…';this.card.querySelector('.filename').textContent=name;this.mrcButton.hidden=!mrc;try{const prepared=await api('/api/prepare/'+key,{viewer:this.id});if(prepared.job)await waitJob(prepared.job,()=>token===this.token);if(token!==this.token)return;const params=new URLSearchParams({adjust:adjust||isMrc?'true':'false',v:Date.now()});if(adjust){for(const n of ['low','high','gamma','sigma','routine'])params.set(n,this.card.querySelector('.'+n).value)}const image=new Image();image.src='/api/image/'+key+'?'+params;await image.decode();if(token!==this.token)return;this.img.src=image.src;this.img.hidden=false;this.message.hidden=true;if(changed)this.reset();else this.fit()}catch(e){if(token===this.token){this.message.textContent='Unavailable: '+e.message;this.img.hidden=true}}}
}
// Keep annotation context above the images, without a competing right sidebar.
const reviewBar=document.querySelector('aside.review');
for(const id of ['scope','rating','suitability','comment']){
 const field=document.getElementById(id),label=reviewBar.querySelector(`label[for="${id}"]`);
 const group=document.createElement('div');group.className='review-field review-'+id;
 label.before(group);group.append(label,field);
 if(id==='scope')group.append($('target'));
}
reviewBar.parentElement.insertBefore(reviewBar,document.getElementById('images'));
const compactStyle=document.createElement('style');compactStyle.textContent=`
.layout{grid-template-columns:170px minmax(0,1fr);gap:10px}
.layout>aside:not(.review){grid-column:1;grid-row:1 / span 2}
.layout>aside.review{grid-column:2;grid-row:1;position:static;max-height:none;overflow:visible;display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;padding:10px 12px}
.review h2{font-size:12px;margin:0}.review-field{min-width:95px}.review-field label{font-size:11px;margin:0 0 4px}.review-scope{max-width:230px}.review #target{display:block;overflow-wrap:anywhere;font-size:10px;margin-top:3px}.review-comment{flex:1;min-width:180px}.review textarea{height:42px;min-height:42px}.review>label{font-size:11px;margin:0}.review #saved{flex-basis:100%;font-size:10px;margin:0}.review #cacheStatus:empty{display:none}
.images{grid-column:2;grid-row:2;gap:8px;align-items:start;min-width:0}
.images>.card:not(.enlarged){display:grid;grid-template-columns:minmax(0,1fr) 88px;align-content:start}
.images>.card:not(.enlarged)>*{grid-column:1/-1}
.images>.card:not(.enlarged)>.viewport{grid-column:1;grid-row:2;height:clamp(230px,29vh,420px)}
.images>.card:not(.enlarged)>.viewer-tools{grid-column:2;grid-row:2;display:flex;flex-direction:column;align-items:stretch;justify-content:center;flex-wrap:nowrap;padding:6px;gap:6px;background:#f7f9fc;overflow:auto}
.viewer-tools button{white-space:normal;line-height:1.25}.heading{height:50px;padding:8px}.heading>div{min-width:0}.heading small{max-width:100%}.heading h2{font-size:12px}.nav{min-height:38px;padding:5px}.controls{padding:6px;min-height:32px}.controls:empty{display:none}
.viewer-options{display:flex;flex-wrap:wrap;align-items:start;background:#f7f9fc}.viewer-options>details{flex:1;min-width:120px;padding:8px!important;border-top:0;font-size:11px}.viewer-options>details[open]{flex-basis:100%;order:1}.viewer-options .adjustment-badge{display:none}
@media(min-width:1800px){.layout{grid-template-columns:195px minmax(0,1fr)}.images>.card:not(.enlarged)>.viewport{height:clamp(300px,34vh,560px)}}
@media(max-width:1150px){.layout{grid-template-columns:145px minmax(0,1fr);padding:0 10px 10px}.review h2{display:none}.images>.card:not(.enlarged){grid-template-columns:minmax(0,1fr)}.images>.card:not(.enlarged)>.viewport{grid-column:1;grid-row:2}.images>.card:not(.enlarged)>.viewer-tools{grid-column:1;grid-row:3;flex-direction:row;flex-wrap:wrap;justify-content:flex-start;overflow:visible}.heading button{font-size:11px;padding:5px}.review #target{max-width:160px}}
@media(max-width:800px){.layout{display:grid;grid-template-columns:120px minmax(0,1fr)}.images{grid-template-columns:1fr 1fr}.layout>aside:not(.review){position:static}.review-field{min-width:80px}.heading{height:58px}}
`;document.head.append(compactStyle);
const views={atlas:new Viewer('atlas','Atlas'),grid:new Viewer('grid','GridSquare · click a hole to select'),foil:new Viewer('foil','FoilHole'),data:new Viewer('data','Data exposure')};
Object.values(views).forEach(v=>v.card.querySelector('.controls').classList.add('viewer-tools'));
function adjustmentPreset(name){return {low:name==='strong'?2:name==='full'?0:1,high:name==='strong'?98:name==='full'?100:99,gamma:1,sigma:name==='reset'?0:null,routine:name==='equalize'?'equalize':'percentile',enabled:name!=='reset'}}
function setupImageAdjustments(view){
 const panel=view.card.querySelector('details.contrast');panel.classList.add('image-adjustments');
 panel.innerHTML=`<summary>Adjust image <span class="adjustment-badge">Original preview</span></summary>
 <div class="adjustment-presets"><button data-preset="robust">Auto · robust</button><button data-preset="strong">Auto · strong</button><button data-preset="full">Full range</button><button data-preset="equalize">Equalize histogram</button><button data-preset="reset">Reset image</button></div>
 <div class="adjustment-fields"><label>Black percentile<input class="low" aria-label="Black percentile" type="number" min="0" max="99.9" step="0.1" value="1"></label><label>White percentile<input class="high" aria-label="White percentile" type="number" min="0.1" max="100" step="0.1" value="99"></label>
 <label>Gamma <output class="gamma-value">1.00</output><input class="gamma" aria-label="Gamma" type="range" min="0.2" max="3" step="0.05" value="1"></label>
 <label>Low-pass σ <output class="sigma-value">Off</output><input class="sigma" aria-label="Low-pass filter sigma" type="range" min="0" max="4" step="0.1" value="0"></label></div>
 <select class="routine" hidden><option value="percentile">Percentile</option><option value="equalize">Equalize</option></select>
 <p class="adjustment-note">Display only; original files and exports stay unchanged. Low-pass σ is in preview pixels (maximum 2048 px image). Filtering can conceal fine detail.</p>
 <div class="adjustment-status" role="status"></div>`;
 const field=name=>panel.querySelector('.'+name),status=field('adjustment-status');
 view.updateAdjustmentLabels=()=>{field('gamma-value').textContent=Number(field('gamma').value).toFixed(2);field('sigma-value').textContent=Number(field('sigma').value)?Number(field('sigma').value).toFixed(1):'Off';field('adjustment-badge').textContent=view.adjustmentEnabled?`${field('low').value}–${field('high').value}% · γ ${Number(field('gamma').value).toFixed(2)}`:'Original preview'};
 async function render(){
  if(!view.key){status.textContent='Select an image first.';return}
  const low=Number(field('low').value),high=Number(field('high').value);
  if(!Number.isFinite(low)||!Number.isFinite(high)||low<0||high>100||low>=high){status.textContent='Black percentile must be below white percentile (0–100).';return}
  status.textContent='Adjusting preview…';
  await view.load(view.key,view.name,view.mrcKey,view.isMrc,!!view.adjustmentEnabled);
  status.textContent=view.img.hidden?'Preview unavailable; try Reset image or check the connection.':view.adjustmentEnabled?'Preview adjusted; original unchanged.':'Original preview restored.';
 }
 for(const input of panel.querySelectorAll('input'))input.oninput=()=>{clearTimeout(view.adjustmentTimer);view.adjustmentEnabled=true;view.updateAdjustmentLabels();view.adjustmentTimer=setTimeout(render,200)};
 for(const button of panel.querySelectorAll('[data-preset]'))button.onclick=()=>{
  clearTimeout(view.adjustmentTimer);const preset=adjustmentPreset(button.dataset.preset);view.adjustmentEnabled=preset.enabled;
  for(const name of ['low','high','gamma','sigma','routine'])if(preset[name]!==null)field(name).value=preset[name];
  view.updateAdjustmentLabels();render();
 };
}
const adjustmentStyle=document.createElement('style');adjustmentStyle.textContent=`
.image-adjustments{background:#f7f9fc;border-top:1px solid #dbe3ed;padding:12px!important}.image-adjustments summary{font-weight:650;cursor:pointer}.adjustment-badge{font-weight:400;color:#64748b;margin-left:10px}.adjustment-presets{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}.adjustment-presets button{font-size:12px;padding:6px 9px}.adjustment-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}.image-adjustments .adjustment-fields label{display:block;margin:0}.image-adjustments .adjustment-fields input{display:block;width:100%;margin-top:5px}.adjustment-note,.adjustment-status{color:#64748b;font-size:11px;line-height:1.5;margin-top:10px}.adjustment-status{min-height:17px}`;
document.head.append(adjustmentStyle);Object.values(views).forEach(setupImageAdjustments);
Object.values(views).forEach(v=>{const options=document.createElement('div');options.className='viewer-options';const adjustments=v.card.querySelector('details.contrast');adjustments.before(options);options.append(adjustments)});
const originalViewerLoad=Viewer.prototype.load;
Viewer.prototype.load=async function(key,name='',mrc='',isMrc=false,adjust=false){
 clearTimeout(this.adjustmentTimer);adjust=adjust||!!this.adjustmentEnabled;
 if(!key)return originalViewerLoad.call(this,key,name,mrc,isMrc,adjust);
 const token=this.token+1,activity=beginActivity(adjust?'Applying contrast / low-pass':isMrc?'Loading MRC image':'Loading image',this.id,()=>this.token===token);
 try{return await originalViewerLoad.call(this,key,name,mrc,isMrc,adjust)}finally{activity.finish()}
};
// Keep indexing visible throughout asynchronous scans, including cached Atlas loading.
let indexActivity=null;
const activityApi=api;
api=async function(path,data){
 const result=await activityApi(path,data);
 if(path==='/api/status'){
  const busy=['new','indexing'].includes(result.index.status);
  if(busy&&!indexActivity)indexActivity=beginActivity(result.index.message||'Indexing session');
  if(busy)indexActivity.update(result.index.message||'Indexing session');
  else if(indexActivity){indexActivity.finish();indexActivity=null}
  $('refresh').disabled=busy;
 }
 return result;
};
const atlasLegend=document.createElement('div');atlasLegend.className='controls';atlasLegend.id='atlasLegend';
atlasLegend.innerHTML=(config.mode==='acquisition'?'<span><span style="color:#248f84">●</span> Data previews indexed</span><span><span style="color:#d8a731">●</span> No Data previews indexed</span>':'<span><span style="color:#248f84">●</span> Mapped GridSquare · Data ignored</span>')+'<span><span style="color:#36c792">◯</span> Suitable</span><span><span style="color:#ef5963">◯</span> Unsuitable</span><span>White outer ring = displayed GridSquare</span>';
atlasLegend.title='Atlas fill describes indexed preview availability, not particle density or ice quality. Orange may mean missing previews, incomplete copying, or an index that needs Refresh.';
views.atlas.card.append(atlasLegend);
const atlasFrameNotice=document.createElement('small');atlasFrameNotice.className='controls';atlasFrameNotice.setAttribute('role','status');views.atlas.card.append(atlasFrameNotice);
const atlasOverlayControl=document.createElement('label');atlasOverlayControl.className='controls';
atlasOverlayControl.innerHTML='Overlay opacity <input type="range" aria-label="Atlas overlay opacity" min="0" max="100" step="1" value="100" style="width:110px"><output>100%</output>';
views.atlas.card.append(atlasOverlayControl);
function setAtlasOverlayOpacity(value){
 const parsed=Number(value),opacity=Number.isFinite(parsed)?Math.max(0,Math.min(100,parsed)):100;
 views.atlas.svg.style.opacity=String(opacity/100);
 views.atlas.svg.style.pointerEvents=opacity===0?'none':'';
 atlasOverlayControl.querySelector('input').value=opacity;
 atlasOverlayControl.querySelector('output').textContent=opacity+'%';
 try{localStorage.setItem('epu-atlas-overlay-opacity',String(opacity))}catch{}
}
try{setAtlasOverlayOpacity(localStorage.getItem('epu-atlas-overlay-opacity')??100)}catch{setAtlasOverlayOpacity(100)}
atlasOverlayControl.querySelector('input').oninput=e=>setAtlasOverlayOpacity(e.target.value);
const overlayMenu=document.createElement('details');
overlayMenu.className='contrast foil-overlay';
overlayMenu.innerHTML='<summary>FoilHole overlay</summary><label>Circle style <select id="foilStyle"><option value="outline">Outline only</option><option value="filled">Filled</option></select></label><label>Radius <input id="foilRadiusSlider" aria-label="Circle radius slider" type="range" min="0.2" max="4" step="0.1" style="width:110px"><input id="foilRadius" aria-label="Circle radius (% of image width)" type="number" min="0.2" max="4" step="0.1"> % of image width</label>';
views.grid.card.querySelector('.viewer-options').append(overlayMenu);
const groupLegend=document.createElement('div');groupLegend.className='controls';
groupLegend.textContent='Solid = centering hole · dashed = beam-shifted hole · same color = same centering group · white outer ring = displayed hole';
overlayMenu.append(groupLegend);
const areaControl=document.createElement('label');areaControl.className='controls';
areaControl.innerHTML='<input type="checkbox" id="showAreas"> Show planned Data acquisition areas';
overlayMenu.append(areaControl);
const focusHole=button('Zoom to selected hole',()=>{
 const m=grid?.markers?.find(m=>String(m.hole)===String(hole)),v=views.grid;
 if(!m||!v.img.naturalWidth)return;
 const r=v.viewport.getBoundingClientRect(),s=Math.min(r.width/v.img.naturalWidth,r.height/v.img.naturalHeight);
 v.zoom=6;v.x=(.5-m.x)*v.img.naturalWidth*s*v.zoom;v.y=(.5-m.y)*v.img.naturalHeight*s*v.zoom;v.fit();
});
focusHole.title='Center and magnify the selected hole on the GridSquare. Reset restores the full square.';
views.grid.card.querySelector('.controls').append(focusHole);
const areaNote=document.createElement('small');areaNote.id='areaNote';areaNote.className='controls';areaNote.hidden=true;views.grid.card.append(areaNote);
let areaToken=0;
async function loadAreas(){
 const token=++areaToken,id=grid?.id;
 if(!$('showAreas').checked||!id){areaNote.hidden=true;drawHoles();return}
 areaNote.hidden=false;areaNote.textContent='Loading planned Data footprints…';
 try{
  const j=await api('/api/areas/'+id,{});
  const result=await waitJob(j.job,()=>token===areaToken&&grid?.id===id);
  if(!result||token!==areaToken||grid?.id!==id)return;
  grid.areas=result.areas;areaNote.textContent=(result.areas.length?result.areas.length+' Data areas shown. Zoom in to distinguish the individual exposures. ':'No Data areas could be placed. ')+result.note;drawHoles();
 }catch(e){if(token===areaToken)areaNote.textContent='Data areas unavailable: '+e.message}
}
$('showAreas').onchange=loadAreas;
function drawAreas(){
 if(!$('showAreas').checked)return;
 for(const area of grid?.areas||[]){
  const p=document.createElementNS('http://www.w3.org/2000/svg','polygon');
  p.setAttribute('points',area.points.map(x=>x.join(',')).join(' '));
  p.setAttribute('vector-effect','non-scaling-stroke');
  p.style.fill='none';p.style.stroke=area.id===pairs[shot]?.id?'#ffffff':groupColor(area.anchor);p.style.strokeWidth=area.id===pairs[shot]?.id?'2.5px':'1.25px';
  p.style.paintOrder='stroke';
  p.style.pointerEvents='all';p.style.cursor='pointer';
  const title=document.createElementNS(p.namespaceURI,'title');title.textContent=area.name+' · planned acquisition area';p.append(title);
  p.onclick=async()=>{try{if(!await saveIfDirty())return;if(hole!==area.hole)await selectHole(area.hole);if(hole!==area.hole)return;const i=pairs.findIndex(x=>x.id===area.id);if(i>=0){shot=i;await showShot()}}catch(e){fail(e)}};
  views.grid.svg.append(p);
 }
}
function updateOverlayControls(){
 $('foilStyle').value=foilOverlay.style;
 $('foilRadius').value=foilOverlay.radius;
 $('foilRadiusSlider').value=foilOverlay.radius;
}
function changeOverlay(radius,sync=true){
 foilOverlay=normalizeOverlay({style:$('foilStyle').value,radius});
 if(sync)updateOverlayControls();else $('foilRadiusSlider').value=foilOverlay.radius;
 drawHoles();
 try{localStorage.setItem('epu-foil-overlay',JSON.stringify(foilOverlay))}catch{}
}
updateOverlayControls();
$('foilStyle').onchange=()=>changeOverlay(foilOverlay.radius);
$('foilRadiusSlider').oninput=()=>changeOverlay($('foilRadiusSlider').value);
$('foilRadius').oninput=()=>{const r=Number($('foilRadius').value);if(r>=.2&&r<=4)changeOverlay(r,false)};
$('foilRadius').onchange=()=>changeOverlay($('foilRadius').value);
function nav(view,prev,next,label){const n=document.createElement('div');n.className='nav';const l=document.createElement('small');l.id=label;const subject=label==='gridNav'?'square':label==='holeNav'?'hole':'exposure';n.append(button('← Previous '+subject,prev),l,button('Next '+subject+' →',next));view.card.append(n)}
nav(views.grid,()=>stepGrid(-1),()=>stepGrid(1),'gridNav');nav(views.data,()=>stepHole(-1),()=>stepHole(1),'holeNav');nav(views.data,()=>stepShot(-1),()=>stepShot(1),'shotNav');const strip=document.createElement('div');strip.id='strips';views.data.card.append(strip);
if(config.mode==='foilhole'){
 views.data.card.querySelectorAll('.viewer-tools button').forEach(b=>b.disabled=true);
 $('shotNav').parentElement.querySelectorAll('button').forEach(b=>b.disabled=true);
 views.data.clear();
}
function drawActiveGridOutline(){
 const m=views.atlas.markers.find(m=>m.selected);
 if(!m)return;
 // Two non-scaling strokes keep the active square visible on bright/dark ice.
 // Leave availability and suitability colors intact, and never obscure the interior.
 for(const [color,width] of [['#101824','5px'],['#ffffff','2.5px']]){
  const ring=document.createElementNS('http://www.w3.org/2000/svg','circle');
  ring.setAttribute('cx',m.x);ring.setAttribute('cy',m.y);ring.setAttribute('r',.022);
  ring.setAttribute('vector-effect','non-scaling-stroke');ring.setAttribute('aria-hidden','true');
  ring.setAttribute('class','active-grid-outline');
  ring.style.fill='none';ring.style.stroke=color;ring.style.strokeWidth=width;ring.style.pointerEvents='none';
  views.atlas.svg.append(ring);
 }
}
function renderAtlas(){if(!atlas)return;atlasFrameNotice.textContent=atlas.note||'';const markers=[];for(const g of grids){const p=atlas.nodes[g.name.replace('GridSquare_','')]?.center;if(!p||!atlas.width||!atlas.height)continue;markers.push({x:p[0]/atlas.width,y:p[1]/atlas.height,id:g.id,label:g.name,selected:grid?.id===g.id,color:config.mode!=='foilhole'&&!g.exposures?'#d8a731':'#248f84',stroke:g.annotation.status==='suitable'?'#36c792':g.annotation.status==='unsuitable'?'#ef5963':null})}views.atlas.mark(markers,m=>selectGrid(m.id));drawActiveGridOutline()}
function renderGrids(){const q=$('search').value.toLowerCase();$('grids').replaceChildren();for(const g of grids.filter(g=>g.name.toLowerCase().includes(q))){const b=button(g.name,()=>selectGrid(g.id));b.classList.toggle('active',grid?.id===g.id);const s=document.createElement('small');s.textContent=g.holes+' holes'+(g.exposures===null?' · Data ignored':' · '+g.exposures+' previews')+(g.missing?' · '+g.missing+' missing':'');b.append(s);$('grids').append(b)}}
async function loadHoles(){const id=grid.id;const r=await api('/api/holes/'+id+'?offset='+holeOffset);if(grid?.id!==id)return;holeRows=r.rows;holeTotal=r.total;$('holes').replaceChildren();for(const h of holeRows){const b=button(h.hole+(config.mode==='foilhole'?'':' · '+h.exposures+' exposures'),()=>selectHole(h.hole));b.dataset.hole=h.hole;$('holes').append(b)}$('page').textContent=holeTotal?(holeOffset+1)+'–'+Math.min(holeOffset+100,holeTotal)+' of '+holeTotal:'No FoilHole previews found';$('pagePrev').disabled=!holeOffset;$('pageNext').disabled=holeOffset+100>=holeTotal}
async function selectGrid(id){const intent=++navigationIntent;if(!await saveIfDirty()||intent!==navigationIntent)return;const g=grids.find(g=>g.id===id);if(!g)return;$('error').style.display='none';areaToken++;grid=g;hole='';pairs=[];areaNote.hidden=true;invalidateAnnotation();const seq=++selection;holeOffset=0;views.grid.mark([],()=>{});views.foil.clear('Select a FoilHole');views.data.clear('Select a FoilHole');views.grid.load(g.image,g.name,g.mrc);renderGrids();renderAtlas();$('gridNav').textContent=(grids.indexOf(g)+1)+' / '+grids.length;$('holeNav').textContent='Loading holes…';if($('shotNav'))$('shotNav').textContent='';if($('strips'))$('strips').replaceChildren();await loadHoles();if(seq!==selection)return;if(holeRows.length)await selectHole(holeRows[0].hole);else{$('holeNav').textContent='No holes available';await loadAnnotation()}const j=await api('/api/geometry/'+id,{});const result=await waitJob(j.job,()=>grid?.id===id);if(grid?.id!==id||!result)return;g.markers=result.markers;drawHoles();if(pairs[shot]&&!pairs[shot].foil)showFoilForExposure(pairs[shot]);if($('showAreas').checked)loadAreas();if(result.note){views.grid.message.textContent=result.note;views.grid.message.hidden=false}}
function drawActiveHoleOutline(){
 const m=grid?.markers?.find(m=>String(m.hole)===String(hole));
 if(!m)return;
 // Keep selection visible at every zoom without obscuring the hole interior
 // or replacing the acquisition-group color and dash pattern underneath.
 for(const [color,width] of [['#101824','5px'],['#ffffff','2.5px']]){
  const ring=document.createElementNS('http://www.w3.org/2000/svg','circle');
  ring.setAttribute('cx',m.x);ring.setAttribute('cy',m.y);ring.setAttribute('r',foilOverlay.radius/100+.004);
  ring.setAttribute('vector-effect','non-scaling-stroke');ring.setAttribute('aria-hidden','true');
  ring.style.fill='none';ring.style.stroke=color;ring.style.strokeWidth=width;ring.style.pointerEvents='none';
  ring.setAttribute('class','active-hole-outline');views.grid.svg.append(ring);
 }
}
function drawHoles(){views.grid.mark((grid?.markers||[]).map(m=>({...m,selected:String(m.hole)===String(hole)})),m=>selectHole(m.hole));drawAreas();drawActiveHoleOutline()}
function syncActiveHole(id){
 if(id!==undefined&&id!==null&&String(id)!=='')hole=String(id);
 drawHoles();
 for(const b of $('holes').children)b.classList.toggle('active',String(b.dataset.hole)===String(hole));
 $('holeNav').textContent='FoilHole '+hole;
}
async function selectHole(id){const intent=++navigationIntent;if(!await saveIfDirty()||intent!==navigationIntent)return;hole=String(id);invalidateAnnotation();const gid=grid.id,seq=++selection;pairs=[];shot=0;syncActiveHole(hole);views.foil.clear('Loading selected FoilHole…');views.data.clear('Loading matching Data previews…');if($('strips'))$('strips').replaceChildren();const r=await api('/api/exposures/'+gid+'/'+id);if(seq!==selection)return;pairs=r.exposures;shot=0;if(pairs.length)await showShot();else{views.foil.load(r.foil,r.foil_name);views.data.clear(config.mode==='foilhole'?'Data images are ignored':r.missing.length?'No Data JPEG/PNG: '+r.missing.length+' XML records have missing previews':'No matching Data preview');if($('strips'))$('strips').replaceChildren();if($('shotNav'))$('shotNav').textContent='No Data previews';await loadAnnotation()}}
function showFoilForExposure(p){
 if(p.foil){views.foil.load(p.foil,p.foil_name);return}
 const m=grid?.markers?.find(m=>m.hole===hole);
 views.foil.clear(m?.role==='shifted'?
  'Beam-shift collection: FoilHole '+hole+' was targeted from centering hole '+m.anchor+'. No separate FoilHole image is expected for this target. Its Data images are available in the Data viewer.':
  'Data images are available for FoilHole '+hole+', but no matching FoilHole preview is indexed. Its centering relationship is not confirmed; the preview may be unavailable.');
}
async function showShot(){const p=pairs[shot];if(!p)return;syncActiveHole(p.hole);views.data.load(p.id,p.name);showFoilForExposure(p);$('shotNav').textContent='Exposure '+(shot+1)+' / '+pairs.length;const strip=$('strips');strip.replaceChildren();pairs.forEach((p,i)=>{const b=button(String(i+1),async()=>{if(!await saveIfDirty())return;shot=i;await showShot()});b.classList.toggle('active',i===shot);b.title=p.name;strip.append(b)});await loadAnnotation()}
async function stepGrid(delta){if(!grid)return;const i=grids.findIndex(g=>g.id===grid.id)+delta;if(i>=0&&i<grids.length)await selectGrid(grids[i].id)}
async function stepHole(delta){let i=holeRows.findIndex(h=>h.hole===hole)+delta;if(i>=0&&i<holeRows.length)return selectHole(holeRows[i].hole);const offset=holeOffset+(delta>0?100:-100);if(offset<0||offset>=holeTotal)return;holeOffset=offset;await loadHoles();if(holeRows.length)await selectHole(holeRows[delta>0?0:holeRows.length-1].hole)}
async function stepShot(delta){if(!await saveIfDirty())return;const i=shot+delta;if(i>=0&&i<pairs.length){shot=i;await showShot()}}
function invalidateAnnotation(){annotationToken++;annotationKey='';dirty=false;formDisabled(true)}function formDisabled(value){for(const id of ['rating','suitability','flag','comment','save'])$(id).disabled=value}formDisabled(true);let dirty=false;function key(){const scope=$('scope').value;return scope==='grid'&&grid?'grid:'+grid.id:scope==='hole'&&grid&&hole?'hole:'+grid.id+':'+hole:scope==='exposure'&&pairs[shot]?'exposure:'+pairs[shot].id:''}
async function loadAnnotation(){formDisabled(true);const target=key(),token=++annotationToken;annotationKey='';dirty=false;const a=target?await api('/api/annotation/'+encodeURIComponent(target)):{};if(token!==annotationToken)return;annotationKey=target;$('target').textContent=target?($('scope').value==='grid'?grid.name:$('scope').value==='hole'?'FoilHole '+hole:pairs[shot].name):'Select an image first';$('rating').value=a.rating||0;$('suitability').value=a.status||'';$('flag').checked=!!a.flag;$('comment').value=a.comment||'';formDisabled(!target);$('saved').textContent='Annotations are stored locally. Cmd/Ctrl+Enter saves and advances.'}
async function saveIfDirty(){if(!dirty||!annotationKey)return true;const savedKey=annotationKey,token=annotationToken;formDisabled(true);try{const a=await api('/api/annotation/'+encodeURIComponent(savedKey),{rating:Number($('rating').value),status:$('suitability').value,flag:$('flag').checked,comment:$('comment').value});if(token===annotationToken){dirty=false;$('saved').textContent='Saved locally'}if(savedKey.startsWith('grid:')){const g=grids.find(g=>'grid:'+g.id===savedKey);if(g)g.annotation=a;renderAtlas()}return true}catch(e){fail(e);return false}finally{if(token===annotationToken)formDisabled(!annotationKey)}}
for(const id of ['rating','suitability','flag','comment'])$(id).oninput=()=>{dirty=true;$('saved').textContent='Unsaved changes'};
$('save').onclick=()=>saveIfDirty();$('scope').onchange=async()=>{if(await saveIfDirty())loadAnnotation().catch(fail)};$('comment').onkeydown=async e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();if(await saveIfDirty())stepGrid(1).catch(fail)}};window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue=''}});
$('search').oninput=renderGrids;$('pagePrev').onclick=()=>{holeOffset=Math.max(0,holeOffset-100);loadHoles().catch(fail)};$('pageNext').onclick=()=>{holeOffset+=100;loadHoles().catch(fail)};$('refresh').onclick=async()=>{if(!await saveIfDirty())return;try{pendingRefresh=(await api('/api/refresh',{})).started}catch(e){fail(e)}};
$('cache').onclick=async()=>{if(!confirm('Copy all indexed JPEG/PNG previews into the local cache? MRCs are excluded. This may take time and disk space; repeating resumes cached files.'))return;$('cache').disabled=true;try{const j=await api('/api/cache-previews',{});$('cacheStatus').textContent='Preparing previews in background…';const r=await waitJob(j.job);$('cacheStatus').textContent=r.missing.length?'Finished with '+r.missing.length+' missing files. Reconnect and retry.':'All '+r.total+' previews cached locally.'}catch(e){fail(e)}finally{$('cache').disabled=false}};
async function poll(){try{const s=await api('/api/status');grids=s.grids;const total=grids.reduce((n,g)=>n+(g.exposures||0),0);$('status').textContent=s.index.message+' · '+grids.length+' squares · '+(config.mode==='foilhole'?'Data ignored':total.toLocaleString()+' Data previews')+' · '+s.cache.files+' cached files';if(s.index.status==='error')$('status').style.color='#a5223b';else $('status').style.color='';renderGrids();if(s.atlas&&(!atlas||atlas.id!==s.atlas.id)){atlas=s.atlas;views.atlas.load(atlas.id,atlas.name,atlas.mrc)}renderAtlas();if(pendingRefresh&&s.index.status==='ready'){pendingRefresh=false;atlas=s.atlas;if(atlas)views.atlas.load(atlas.id,atlas.name,atlas.mrc);if(grid)await selectGrid(grid.id)}if(s.index.status==='error')pendingRefresh=false;if(!grid&&grids.length&&s.index.status!=='indexing')await selectGrid(grids[0].id)}catch(e){fail(e)}setTimeout(poll,2000)}poll();
document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.querySelectorAll('.enlarged').forEach(c=>{c.classList.remove('enlarged');c.querySelector('.heading button').textContent='Enlarge'});Object.values(views).forEach(v=>v.fit())}});
</script><script src="/position_corrections.js"></script><script src="/review-tools.js"></script></html>'''
