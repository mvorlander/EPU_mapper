// Reversible EPU intensity filtering and GridSquare selection brush.
const holeSelectionPanel=document.createElement('details');
holeSelectionPanel.id='holeSelectionPanel';
holeSelectionPanel.style.cssText='margin:12px 18px;padding:12px;background:white;border:1px solid #a9c8c2;border-radius:10px';
holeSelectionPanel.innerHTML=`<summary style="cursor:pointer;font-weight:650">FoilHole selection & CryoSPARC export</summary>
 <p class="muted">Set the session-wide recorded-intensity range, then refine individual GridSquares with reversible brush overrides. EPU's recorded selection remains visible in the histogram for comparison.</p>
<div class="hole-select-controls">
 <label>Selection basis <select id="holeSelectMode"><option value="intensity">Recorded intensity range</option><option value="all">All acquired holes</option></select></label>
 <label><input id="holeSelectVisible" type="checkbox" checked> Show selection layer</label>
 <button id="holeBrushToggle">Selection brush</button>
 <label>Brush radius <input id="holeBrushRadius" type="range" min="0.5" max="6" step="0.25" value="1.5"> <output id="holeBrushRadiusValue">1.5 hole spacings</output></label>
 <button id="holeBrushUndo" disabled>Undo</button><button id="holeBrushRedo" disabled>Redo</button><button id="holeBrushClear">Clear brush edits</button>
</div>
<div id="intensityEditor" hidden>
 <div class="intensity-heading"><strong>Recorded hole intensity</strong><span id="intensityRangeSummary" class="muted"></span></div>
 <svg id="intensityHistogram" viewBox="0 0 640 150" preserveAspectRatio="none" aria-label="Histogram of recorded EPU hole intensities"></svg>
 <div class="intensity-values"><label>Include from <input id="holeSelectLow" type="number" step="1"></label><label>to <input id="holeSelectHigh" type="number" step="1"></label><span class="muted">Drag either edge of the highlighted histogram range.</span></div>
</div>
<p id="holeBrushHelp" class="muted"><strong>Brush:</strong> swipe to remove · Control + swipe to add · Control + click adds one · Shift + click removes one · Shift + wheel changes radius · middle/right-drag pans.</p>
<p id="holeSelectLegend"><span class="hole-legend-in">● included</span> · <span class="hole-legend-out">⊘ excluded</span> · <span class="hole-legend-manual">bold ring = manual override</span></p>
<p id="holeSelectStatus" role="status" class="muted">Select a GridSquare to load its recorded EPU intensities.</p>
<div class="hole-filter-source"><strong>Apply this selection to a CryoSPARC dataset</strong><button id="holeChooseParticles">Choose particle .cs…</button><span id="holeParticlesChoice" class="density-choice">Uses mapped dataset if available</span><button id="holeChoosePassthrough">Add passthrough…</button><span id="holePassthroughChoice" class="density-choice"></span><a href="/api/hole-selection/file" download>Download portable selection file</a></div>
<div class="hole-export-controls"><label><input id="holeKeepUnmatched" type="checkbox"> Keep unmatched or ambiguous particles</label> <label><input id="holeIncludeExcluded" type="checkbox"> Also write excluded tables (slower, larger)</label> <button id="holeChooseOutput">Choose direct output folder…</button><span id="holeOutputChoice" class="density-choice">ZIP download</span><button id="holeClearOutput" hidden>Use ZIP instead</button> <button id="holeExport">Filter and export .cs files</button> <a id="holeExportDownload" hidden>Download filtered particle tables</a></div>`;
(document.getElementById('densityPanel')||document.querySelector('header')).after(holeSelectionPanel);
const holeSelectionStyle=document.createElement('style');holeSelectionStyle.textContent=`
.hole-select-controls,.hole-export-controls,.hole-filter-source{display:flex;align-items:end;gap:8px;flex-wrap:wrap;margin:9px 0}.hole-filter-source{padding:9px;background:#f7fafc;border:1px solid #dbe4ec;border-radius:8px;align-items:center}.hole-filter-source strong{margin-right:4px}.hole-select-controls label{display:grid;gap:3px;font-size:11px}.hole-select-controls button.active,#gridBrushToggle.active{background:#d5eee9;border-color:#20877c}.hole-legend-in{color:#17896f;font-weight:700}.hole-legend-out{color:#8a5260;font-weight:700}.hole-legend-manual{color:#7658ac;font-weight:700}#holeSelectLegend,#holeBrushHelp{font-size:11px;margin:7px 0}.selection-brush-active #grid .viewport{cursor:none}.selection-brush-panning #grid .viewport{cursor:grabbing}.selection-brush-active #grid .scene circle{pointer-events:none!important}#selectionBrushCursor circle{fill:#10182455;stroke:#b24d5e;stroke-width:2px;vector-effect:non-scaling-stroke;stroke-dasharray:4 3;filter:drop-shadow(0 0 2px #111)}#selectionBrushCursor text{fill:white;stroke:#101824;stroke-width:.003px;paint-order:stroke;font-weight:800;text-anchor:middle;dominant-baseline:central;pointer-events:none}
#intensityEditor{max-width:900px;padding:9px 10px;border:1px solid #dbe4ec;border-radius:8px;background:#f7fafc}.intensity-heading,.intensity-values{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:11px}.intensity-values{justify-content:flex-start}.intensity-values label{display:flex;align-items:center;gap:5px}.intensity-values input{width:100px;padding:4px}#intensityHistogram{display:block;width:100%;height:150px;margin:4px 0;touch-action:none;cursor:ew-resize}.histogram-bar{fill:#aebdca}.histogram-bar.in-range{fill:#43a99c}.histogram-selected{fill:#7658ac;opacity:.65}.histogram-excluded{fill:#f2d9de;opacity:.55}.histogram-handle{stroke:#175d58;stroke-width:3;vector-effect:non-scaling-stroke}.histogram-label{font:10px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#526579}
#gridSelectionToolbar{display:none}.enlarged#grid #gridSelectionToolbar{display:flex;position:absolute;z-index:8;top:60px;left:50%;transform:translateX(-50%);align-items:center;gap:6px;max-width:calc(100% - 30px);padding:7px 9px;border-radius:9px;background:#f8fbfdf2;border:1px solid #a9c8c2;box-shadow:0 3px 14px #0b152c44;font-size:11px;white-space:nowrap}.enlarged#grid #gridSelectionToolbar button{padding:5px 8px}.enlarged#grid #gridSelectionToolbar .brush-tip{overflow:hidden;text-overflow:ellipsis;color:#53677c}
@media(max-width:900px){#holeSelectionPanel{margin:8px 10px}.hole-select-controls{align-items:center}}
`;document.head.append(holeSelectionStyle);

let holeSelectionData=null,holeSelectionGrid='',holeBrushMode='',holeUndo=[],holeRedo=[],holeStroke=null,holeSelectionToken=0;
let holeCursorPoint=null,holeCursorKeep=false,holePanPointer=null;
let holeHistogram=null,histogramLoading=false,histogramHandle='';
let holeOutputPath='';
let holeFilterPaths={particles:'',passthrough:''},holeFilterInspection=null;
const gridSelectionToolbar=document.createElement('div');gridSelectionToolbar.id='gridSelectionToolbar';gridSelectionToolbar.innerHTML=`<button id="gridBrushToggle">Selection brush</button><button id="gridBrushSmaller" title="Smaller brush (Shift + wheel)">−</button><strong id="gridBrushRadiusValue">1.5×</strong><button id="gridBrushLarger" title="Larger brush (Shift + wheel)">+</button><button id="gridBrushUndo" disabled>Undo</button><button id="gridBrushRedo" disabled>Redo</button><span class="brush-tip">Swipe removes · Control adds · middle/right-drag pans</span>`;views.grid.card.append(gridSelectionToolbar);
function median(values){const a=[...values].sort((x,y)=>x-y);return a.length?a[Math.floor(a.length/2)]:.025}
function holeSpacing(targets){const shown=targets.filter(t=>t.exposures);if(shown.length<2)return .025;const d=[];for(const a of shown){let best=Infinity;for(const b of shown){if(a===b)continue;best=Math.min(best,Math.hypot(a.x-b.x,a.y-b.y))}if(Number.isFinite(best))d.push(best)}return median(d)}
function brushRadius(){return Number($('holeBrushRadius').value)*(holeSelectionData?.spacing||.025)}
function selectionAt(target){return !!holeSelectionData?.resolved?.[String(target.hole)]}
function drawHoleSelection(){
 document.querySelector('#holeSelectionLayer')?.remove();document.querySelector('#selectionBrushCursor')?.remove();
 if(!$('holeSelectVisible').checked||!holeSelectionData||holeSelectionGrid!==grid?.id)return;
 const layer=document.createElementNS('http://www.w3.org/2000/svg','g');layer.id='holeSelectionLayer';layer.style.pointerEvents='none';
 for(const target of holeSelectionData.targets.filter(t=>t.exposures)){
  const include=selectionAt(target),manual=Object.hasOwn(holeSelectionData.state.overrides,String(target.hole));
  const circle=document.createElementNS(layer.namespaceURI,'circle');circle.setAttribute('cx',target.x);circle.setAttribute('cy',target.y);circle.setAttribute('r',foilOverlay.radius/100+.006);
  circle.style.cssText=`fill:none;stroke:${include?'#21b58d':'#9b5363'};stroke-width:${manual?4:2}px;vector-effect:non-scaling-stroke;stroke-dasharray:${include?'':'3 3'}`;
  const title=document.createElementNS(layer.namespaceURI,'title');title.textContent=`FoilHole ${target.hole} · ${include?'included':'excluded'} · intensity ${target.intensity==null?'not recorded':target.intensity.toFixed(1)}${manual?' · manual override':''}`;circle.append(title);layer.append(circle);
  if(!include){const r=foilOverlay.radius/100+.002;for(const sign of [-1,1]){const line=document.createElementNS(layer.namespaceURI,'line');line.setAttribute('x1',target.x-r);line.setAttribute('y1',target.y-sign*r);line.setAttribute('x2',target.x+r);line.setAttribute('y2',target.y+sign*r);line.style.cssText='stroke:#9b5363;stroke-width:2px;vector-effect:non-scaling-stroke';layer.append(line)}}
 }
 views.grid.svg.append(layer);drawBrushCursor();
}
function drawBrushCursor(point=holeCursorPoint,keep=holeCursorKeep){
 document.querySelector('#selectionBrushCursor')?.remove();if(!holeBrushMode||!holeSelectionData)return;
 if(!point||holePanPointer!==null)return;holeCursorPoint=point;holeCursorKeep=keep;
 const ns='http://www.w3.org/2000/svg',cursor=document.createElementNS(ns,'g'),circle=document.createElementNS(ns,'circle'),label=document.createElementNS(ns,'text'),x=point.x,y=point.y;cursor.id='selectionBrushCursor';circle.setAttribute('r',brushRadius());circle.setAttribute('cx',x);circle.setAttribute('cy',y);circle.style.stroke=keep?'#20b48b':'#b24d5e';label.setAttribute('x',x);label.setAttribute('y',y);label.setAttribute('font-size',Math.max(.014,brushRadius()*.65));label.textContent=keep?'+':'−';cursor.append(circle,label);views.grid.svg.append(cursor);
}
function histogramValue(clientX){const r=$('intensityHistogram').getBoundingClientRect(),edges=holeHistogram?.edges||[];if(edges.length<2)return 0;return edges[0]+Math.max(0,Math.min(1,(clientX-r.left)/r.width))*(edges.at(-1)-edges[0])}
function drawIntensityHistogram(){
 const svg=$('intensityHistogram');svg.replaceChildren();if(!holeHistogram?.counts?.length)return;
 const {counts,selected,edges}=holeHistogram,max=Math.max(1,...counts),low=holeSelectionData?.state.low??edges[0],high=holeSelectionData?.state.high??edges.at(-1),x=v=>20+(v-edges[0])/(edges.at(-1)-edges[0])*600;
 const ns='http://www.w3.org/2000/svg',left=x(low),right=x(high);
 for(const [x0,w] of [[20,Math.max(0,left-20)],[right,Math.max(0,620-right)]]){const shade=document.createElementNS(ns,'rect');shade.setAttribute('x',x0);shade.setAttribute('y',8);shade.setAttribute('width',w);shade.setAttribute('height',118);shade.setAttribute('class','histogram-excluded');svg.append(shade)}
 counts.forEach((count,i)=>{const bx=20+i*600/counts.length,bw=Math.max(1,600/counts.length-1),h=Math.log1p(count)/Math.log1p(max)*105;const bar=document.createElementNS(ns,'rect');bar.setAttribute('x',bx);bar.setAttribute('y',123-h);bar.setAttribute('width',bw);bar.setAttribute('height',h);bar.setAttribute('class','histogram-bar '+(edges[i+1]>=low&&edges[i]<=high?'in-range':''));svg.append(bar);if(selected?.[i]){const sh=Math.log1p(selected[i])/Math.log1p(max)*105,s=document.createElementNS(ns,'rect');s.setAttribute('x',bx);s.setAttribute('y',123-sh);s.setAttribute('width',bw);s.setAttribute('height',sh);s.setAttribute('class','histogram-selected');svg.append(s)}});
 for(const [value,label,anchor] of [[low,Number(low).toFixed(1),'start'],[high,Number(high).toFixed(1),'end']]){const line=document.createElementNS(ns,'line');line.setAttribute('x1',x(value));line.setAttribute('x2',x(value));line.setAttribute('y1',5);line.setAttribute('y2',128);line.setAttribute('class','histogram-handle');svg.append(line);const text=document.createElementNS(ns,'text');text.setAttribute('x',x(value)+(anchor==='start'?4:-4));text.setAttribute('y',145);text.setAttribute('text-anchor',anchor);text.setAttribute('class','histogram-label');text.textContent=label;svg.append(text)}
 const represented=counts.reduce((n,count,i)=>n+(edges[i+1]>=low&&edges[i]<=high?count:0),0);$('intensityRangeSummary').textContent=`Approximately ${represented.toLocaleString()} of ${holeHistogram.total.toLocaleString()} recorded targets are in range · purple = ${holeHistogram.epu_selected.toLocaleString()} recorded EPU-selected targets`;
}
async function loadIntensityHistogram(){
 if(holeHistogram||histogramLoading)return;histogramLoading=true;$('intensityRangeSummary').textContent='Building histogram from recorded EPU metadata…';
 try{const started=await api('/api/hole-selection/histogram',{});holeHistogram=await waitJob(started.job);if(holeSelectionData&&holeSelectionData.state.low==null)holeSelectionData.state.low=holeHistogram.minimum;if(holeSelectionData&&holeSelectionData.state.high==null)holeSelectionData.state.high=holeHistogram.maximum;drawIntensityHistogram()}
 catch(e){$('intensityRangeSummary').textContent='Histogram unavailable: '+e.message}finally{histogramLoading=false}
}
function updateHoleSelectionControls(){
 if(!holeSelectionData)return;const state=holeSelectionData.state;
 $('holeSelectMode').value=state.mode;$('holeSelectLow').value=state.low==null?'':Number(state.low).toFixed(1);$('holeSelectHigh').value=state.high==null?'':Number(state.high).toFixed(1);
 const intensity=state.mode==='intensity';$('intensityEditor').hidden=!intensity;if(intensity){loadIntensityHistogram();drawIntensityHistogram()}
 const c=holeSelectionData.counts;$('holeSelectStatus').textContent=`${c.acquired_holes.toLocaleString()} acquired holes selected · ${c.exposures.toLocaleString()} exposures${densityLoaded?' · '+c.particles.toLocaleString()+' mapped particles':''}. ${holeSelectionData.note||holeSelectionData.session.note||''}`;
 $('holeBrushUndo').disabled=$('gridBrushUndo').disabled=!holeUndo.length;$('holeBrushRedo').disabled=$('gridBrushRedo').disabled=!holeRedo.length;
 drawHoleSelection();
}
async function loadHoleSelection(){
 const gid=grid?.id,token=++holeSelectionToken;holeSelectionData=null;holeSelectionGrid='';drawHoleSelection();if(!gid)return;
 $('holeSelectStatus').textContent='Loading recorded FoilHole intensities and EPU selections…';
 try{const result=await api('/api/hole-selection/grid/'+gid);if(token!==holeSelectionToken||gid!==grid?.id)return;holeSelectionData=result;holeSelectionGrid=gid;holeSelectionData.spacing=holeSpacing(result.targets);holeUndo=[];holeRedo=[];updateHoleSelectionControls()}
 catch(e){if(token===holeSelectionToken)$('holeSelectStatus').textContent='Hole selection unavailable: '+e.message}
}
async function saveHoleSelection(){
 if(!grid||!holeSelectionData)return;const gid=grid.id;
 $('holeSelectStatus').textContent='Saving selection…';
 const result=await api('/api/hole-selection/grid/'+gid,holeSelectionData.state);if(gid!==grid?.id)return;
 const spacing=holeSelectionData.spacing;holeSelectionData=result;holeSelectionData.spacing=spacing;updateHoleSelectionControls();
}
function recomputeHoleSelection(){
 const s=holeSelectionData.state;
 for(const t of holeSelectionData.targets){let keep=t.metadata_missing||s.mode==='all';if(!t.metadata_missing&&s.mode==='epu'){const anchor=t.primary&&holeSelectionData.targets.find(x=>String(x.hole)===String(t.primary));keep=!!(anchor||t).selected}else if(!t.metadata_missing&&s.mode==='intensity')keep=t.intensity!=null&&(s.low==null||t.intensity>=s.low)&&(s.high==null||t.intensity<=s.high);if(Object.hasOwn(s.overrides,String(t.hole)))keep=s.overrides[String(t.hole)];holeSelectionData.resolved[String(t.hole)]=keep}
 const chosen=holeSelectionData.targets.filter(t=>t.exposures&&selectionAt(t));holeSelectionData.counts.acquired_holes=chosen.length;holeSelectionData.counts.exposures=chosen.reduce((n,t)=>n+t.exposures,0);holeSelectionData.counts.particles=chosen.reduce((n,t)=>n+(holeSelectionData.particles?.[t.hole]||0),0);updateHoleSelectionControls();
}
function setBrushMode(active){holeBrushMode=(active===undefined?!holeBrushMode:active)?'brush':'';if(!holeBrushMode){holeCursorPoint=null;holePanPointer=null;document.body.classList.remove('selection-brush-panning')}document.body.classList.toggle('selection-brush-active',!!holeBrushMode);$('holeBrushToggle').classList.toggle('active',!!holeBrushMode);$('gridBrushToggle').classList.toggle('active',!!holeBrushMode);drawHoleSelection()}
function svgPoint(event){const r=views.grid.svg.getBoundingClientRect();return {x:(event.clientX-r.left)/r.width,y:(event.clientY-r.top)/r.height}}
function brushAt(point,keep,individual=false){
 if(!holeStroke||!holeSelectionData)return;let targets=holeSelectionData.targets.filter(t=>t.exposures),radius=brushRadius();
 if(individual){let nearest=null,distance=Infinity;for(const t of targets){const d=Math.hypot(t.x-point.x,t.y-point.y);if(d<distance){nearest=t;distance=d}}targets=nearest&&distance<=Math.max(radius,holeSelectionData.spacing*.8)?[nearest]:[]}
 else targets=targets.filter(t=>Math.hypot(t.x-point.x,t.y-point.y)<=radius);
 for(const t of targets){const key=String(t.hole);if(!Object.hasOwn(holeStroke.before,key))holeStroke.before[key]=Object.hasOwn(holeSelectionData.state.overrides,key)?holeSelectionData.state.overrides[key]:null;holeSelectionData.state.overrides[key]=keep;holeStroke.after[key]=keep}recomputeHoleSelection();drawBrushCursor(point,keep)
}
views.grid.viewport.addEventListener('pointerdown',e=>{if(!holeBrushMode||!holeSelectionData)return;if(e.button===1||e.button===2){e.preventDefault();holePanPointer=e.pointerId;document.body.classList.add('selection-brush-panning');document.querySelector('#selectionBrushCursor')?.remove();return}if(e.button!==0)return;e.preventDefault();e.stopImmediatePropagation();views.grid.viewport.setPointerCapture(e.pointerId);holeStroke={before:{},after:{},keep:!!(e.ctrlKey||e.metaKey),individual:!!e.shiftKey,start:[e.clientX,e.clientY],point:svgPoint(e),moved:false};if(e.shiftKey)brushAt(holeStroke.point,false,true)},true);
views.grid.viewport.addEventListener('pointermove',e=>{if(!holeBrushMode)return;if(holePanPointer===e.pointerId){document.querySelector('#selectionBrushCursor')?.remove();return}const p=svgPoint(e),keep=!!(e.ctrlKey||e.metaKey);drawBrushCursor(p,holeStroke?.keep??keep);if(holeStroke){e.preventDefault();e.stopImmediatePropagation();holeStroke.point=p;if(Math.hypot(e.clientX-holeStroke.start[0],e.clientY-holeStroke.start[1])>3)holeStroke.moved=true;if(holeStroke.moved)brushAt(p,holeStroke.keep,false)}},true);
views.grid.viewport.addEventListener('pointerleave',()=>{if(holeBrushMode&&!holeStroke&&holePanPointer===null){holeCursorPoint=null;document.querySelector('#selectionBrushCursor')?.remove()}},true);
async function finishBrush(e){if(holePanPointer===e?.pointerId){holePanPointer=null;document.body.classList.remove('selection-brush-panning');return}if(!holeStroke)return;e?.preventDefault();e?.stopImmediatePropagation();const stroke=holeStroke;if(!stroke.moved&&!stroke.individual)brushAt(stroke.point,stroke.keep,stroke.keep);holeStroke=null;if(Object.keys(stroke.after).length){holeUndo.push(stroke);holeRedo=[];await saveHoleSelection()}updateHoleSelectionControls()}
views.grid.viewport.addEventListener('pointerup',finishBrush,true);views.grid.viewport.addEventListener('pointercancel',finishBrush,true);
views.grid.viewport.addEventListener('contextmenu',e=>{if(holeBrushMode)e.preventDefault()});
views.grid.viewport.addEventListener('wheel',e=>{if(!holeBrushMode||!e.shiftKey)return;e.preventDefault();e.stopImmediatePropagation();holeCursorPoint=svgPoint(e);holeCursorKeep=!!(e.ctrlKey||e.metaKey);adjustBrushRadius(e.deltaY>0?-0.25:0.25)},true);
function applyStroke(stroke,undo){for(const key of Object.keys(stroke.after)){const value=undo?stroke.before[key]:stroke.after[key];if(value===null)delete holeSelectionData.state.overrides[key];else holeSelectionData.state.overrides[key]=value}recomputeHoleSelection()}
function moveHistogramHandle(clientX){
 if(!histogramHandle||!holeSelectionData||!holeHistogram)return;const value=histogramValue(clientX),s=holeSelectionData.state;
 if(histogramHandle==='low')s.low=Math.min(value,s.high??holeHistogram.maximum);else s.high=Math.max(value,s.low??holeHistogram.minimum);
 $('holeSelectLow').value=Number(s.low).toFixed(1);$('holeSelectHigh').value=Number(s.high).toFixed(1);recomputeHoleSelection();drawIntensityHistogram();
}
$('intensityHistogram').addEventListener('pointerdown',e=>{if(!holeHistogram||!holeSelectionData)return;e.preventDefault();const value=histogramValue(e.clientX),s=holeSelectionData.state,low=s.low??holeHistogram.minimum,high=s.high??holeHistogram.maximum;histogramHandle=Math.abs(value-low)<=Math.abs(value-high)?'low':'high';$('intensityHistogram').setPointerCapture(e.pointerId);moveHistogramHandle(e.clientX)});
$('intensityHistogram').addEventListener('pointermove',e=>{if(histogramHandle)moveHistogramHandle(e.clientX)});
async function finishHistogram(){if(!histogramHandle)return;histogramHandle='';await saveHoleSelection()}
$('intensityHistogram').addEventListener('pointerup',finishHistogram);$('intensityHistogram').addEventListener('pointercancel',finishHistogram);
function adjustBrushRadius(delta=0){const input=$('holeBrushRadius');input.value=Math.max(Number(input.min),Math.min(Number(input.max),Number(input.value)+delta));const value=Number(input.value).toFixed(2).replace(/\.00$/,'').replace(/0$/,'');$('holeBrushRadiusValue').textContent=value+' hole spacings';$('gridBrushRadiusValue').textContent=value+'×';drawHoleSelection()}
$('holeBrushToggle').onclick=()=>setBrushMode();$('gridBrushToggle').onclick=()=>setBrushMode();$('gridBrushSmaller').onclick=()=>adjustBrushRadius(-.25);$('gridBrushLarger').onclick=()=>adjustBrushRadius(.25);
$('holeBrushRadius').oninput=()=>adjustBrushRadius();
$('holeSelectVisible').onchange=drawHoleSelection;
$('holeSelectMode').onchange=async()=>{if(!holeSelectionData)return;holeSelectionData.state.mode=$('holeSelectMode').value;if(holeSelectionData.state.mode==='intensity')await loadIntensityHistogram();recomputeHoleSelection();await saveHoleSelection()};
for(const id of ['holeSelectLow','holeSelectHigh'])$(id).onchange=async()=>{if(!holeSelectionData)return;holeSelectionData.state[id.endsWith('Low')?'low':'high']=$ (id).value===''?null:Number($(id).value);recomputeHoleSelection();await saveHoleSelection()};
$('holeBrushClear').onclick=async()=>{if(!holeSelectionData||!Object.keys(holeSelectionData.state.overrides).length)return;holeUndo.push({before:{...holeSelectionData.state.overrides},after:Object.fromEntries(Object.keys(holeSelectionData.state.overrides).map(k=>[k,null])),clear:true});holeRedo=[];holeSelectionData.state.overrides={};recomputeHoleSelection();await saveHoleSelection()};
$('holeBrushUndo').onclick=async()=>{const stroke=holeUndo.pop();if(!stroke)return;if(stroke.clear)holeSelectionData.state.overrides={...stroke.before};else applyStroke(stroke,true);holeRedo.push(stroke);recomputeHoleSelection();await saveHoleSelection()};
$('holeBrushRedo').onclick=async()=>{const stroke=holeRedo.pop();if(!stroke)return;if(stroke.clear)holeSelectionData.state.overrides={};else applyStroke(stroke,false);holeUndo.push(stroke);recomputeHoleSelection();await saveHoleSelection()};
$('gridBrushUndo').onclick=()=>$('holeBrushUndo').click();$('gridBrushRedo').onclick=()=>$('holeBrushRedo').click();
const holeSelectionDrawHoles=drawHoles;drawHoles=function(){holeSelectionDrawHoles();drawHoleSelection()};
const holeSelectionSelectGrid=selectGrid;selectGrid=async function(id){await holeSelectionSelectGrid(id);await loadHoleSelection()};
function updateHoleFilterSource(){
 const main=$('holeParticlesChoice'),pass=$('holePassthroughChoice');
 if(holeFilterInspection){main.textContent=`${holeFilterInspection.name} · ${holeFilterInspection.particles.toLocaleString()} particles · ${densityFileSize(holeFilterInspection.size)}`;main.title=holeFilterPaths.particles}else{main.textContent='Uses mapped dataset if available';main.title=''}
 pass.textContent=holeFilterPaths.passthrough?holeFilterPaths.passthrough.split(/[\\/]/).pop():holeFilterInspection?.needs_passthrough?'Matching passthrough required':'';pass.title=holeFilterPaths.passthrough;
 pass.classList.toggle('required',!!holeFilterInspection?.needs_passthrough&&!holeFilterPaths.passthrough);$('holeChoosePassthrough').textContent=holeFilterPaths.passthrough?'Replace passthrough…':holeFilterInspection?.needs_passthrough?'Choose passthrough…':'Add passthrough…';
 $('holeExport').disabled=!!holeFilterInspection?.needs_passthrough&&!holeFilterPaths.passthrough;
}
async function chooseHoleFilterFile(role){
 try{const main=role==='particles',picked=await api('/api/native-dialog',{kind:'cs',prompt:main?'Choose CryoSPARC particle table to filter':'Choose matching CryoSPARC passthrough',initial:holeFilterPaths[role]||holeFilterPaths.particles||densityPaths.particles});if(picked.cancelled)return false;
  const info=await api('/api/density/inspect-path',{path:picked.path,suggest_passthrough:main,purpose:'filter'});
  if(main){holeFilterPaths={particles:info.path,passthrough:info.suggestion||''};holeFilterInspection=info;$('holeSelectStatus').textContent=info.needs_passthrough?(info.suggestion?'Matching passthrough found automatically. Ready to filter.':'This table needs its matching passthrough for the original micrograph path.'):'Particle table ready for filtering. Density mapping is not required.'}
  else{if(!holeFilterInspection)throw Error('Choose the particle table first.');if(!info.fields.includes('location/micrograph_path'))throw Error('This passthrough has no location/micrograph_path field.');holeFilterPaths.passthrough=info.path;$('holeSelectStatus').textContent='Particle and passthrough tables are ready for filtering.'}
  updateHoleFilterSource();return true
 }catch(e){$('holeSelectStatus').textContent='Could not use this particle table: '+e.message;return false}
}
$('holeChooseParticles').onclick=()=>chooseHoleFilterFile('particles');$('holeChoosePassthrough').onclick=()=>chooseHoleFilterFile('passthrough');updateHoleFilterSource();
$('holeChooseOutput').onclick=async()=>{try{const picked=await api('/api/native-dialog',{kind:'folder',prompt:'Choose folder for filtered CryoSPARC tables',initial:holeOutputPath});if(picked.cancelled)return;holeOutputPath=picked.path;$('holeOutputChoice').textContent=picked.name;$('holeOutputChoice').title=picked.path;$('holeClearOutput').hidden=false}catch(e){$('holeSelectStatus').textContent='Could not choose output folder: '+e.message}};
$('holeClearOutput').onclick=()=>{holeOutputPath='';$('holeOutputChoice').textContent='ZIP download';$('holeOutputChoice').title='';$('holeClearOutput').hidden=true};
$('holeExport').onclick=async()=>{const b=$('holeExport');let particles=holeFilterPaths.particles||densityPaths.particles,passthrough=holeFilterPaths.passthrough||(holeFilterPaths.particles?'':densityPaths.passthrough);if(!particles&&!densityLoaded){if(!await chooseHoleFilterFile('particles'))return;particles=holeFilterPaths.particles;passthrough=holeFilterPaths.passthrough}b.disabled=true;$('holeExportDownload').hidden=true;$('holeSelectStatus').textContent='Filtering particle tables in chunks and preserving UIDs…';try{const started=await api('/api/hole-selection/export',{particles, passthrough,keep_unmatched:$('holeKeepUnmatched').checked,include_excluded:$('holeIncludeExcluded').checked,output_directory:holeOutputPath});const result=await waitJob(started.job);const link=$('holeExportDownload');if(result.key){link.href='/api/hole-selection/export/'+result.key;link.download='EPU_Mapper_filtered_particles.zip';link.hidden=false;link.textContent=`Download ${result.kept.toLocaleString()} kept particles (${result.excluded.toLocaleString()} excluded)`;link.click()}$('holeSelectStatus').textContent=`Export ready: ${result.kept.toLocaleString()} kept, ${result.excluded.toLocaleString()} excluded; ${result.unmatched.toLocaleString()} unmatched and ${result.ambiguous.toLocaleString()} ambiguous. ${result.key?'Download prepared.':'Saved to '+result.path}`}catch(e){$('holeSelectStatus').textContent='Particle export failed: '+e.message}finally{updateHoleFilterSource()}};
