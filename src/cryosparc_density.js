// Optional acquisition dashboard extension; no CryoSPARC connection required.
const densityPanel=document.createElement('details');
densityPanel.style.cssText='margin:12px 18px;padding:12px;background:white;border:2px solid #8069c8;border-radius:10px';
densityPanel.innerHTML=`<summary style="cursor:pointer;font-weight:600">CryoSPARC particle density · optional</summary>
<p class="muted">Import a selected particle .cs file and its matching passthrough if needed. No particle stacks or Data MRCs are read.</p>
<p class="muted">Compare subsets in this session: choose another .cs pair and import again. This replaces density only; your EPU session and annotations stay unchanged.</p>
<label>Particles <input type="file" id="densityMain" accept=".cs"></label>
<label>Passthrough (optional) <input type="file" id="densityPass" accept=".cs"></label>
<button id="densityImport">Import particle density</button>
<button id="densityClear" disabled>Clear density</button>
<label><input type="checkbox" id="densityVisible" checked> Show density on GridSquare</label>
<label>Display <select id="densityMode"><option value="holes">Mean density per FoilHole</option><option value="spatial">Binned spatial density</option></select></label>
<label>Color gradient <select id="densityPalette"><option value="viridis">Purple → teal → yellow</option><option value="heat">Blue → cyan → yellow → red</option><option value="gray">Black → white</option></select></label>
<label>Opacity <input id="densityOpacity" type="range" min="10" max="90" value="65"></label>
<p class="muted">Hole outline = acquisition group (solid: centering hole; dashed: beam-shift hole). Hole fill = particle density only. Selection thickens the outline without changing density.</p>
<p id="densityStatus" role="status">No particles imported. Blank regions are unknown, not zero density.</p>
<p id="densityLegend" class="muted"></p>`;
document.querySelector('header').after(densityPanel);
let densityResult=null,densityRequest=0,densityLoaded=false;
let densityGridCounts=null;
const originalRenderGrids=renderGrids;
renderGrids=function(){
 originalRenderGrids();
 if(!densityLoaded)return;
 const q=$('search').value.toLowerCase();
 const visible=grids.filter(g=>g.name.toLowerCase().includes(q));
 Array.from($('grids').children).forEach((button,i)=>{
  const count=densityGridCounts===null?null:(densityGridCounts[visible[i].id]||0);
  const label=document.createElement('small');label.className='particle-count';
  label.textContent=count===null?'Particle counts unavailable · restart server':`${count.toLocaleString()} mapped ${count===1?'particle':'particles'}`;
  label.style.fontWeight='600';label.style.color='#6851a3';
  label.title='Particles from the imported subset matched to this square’s Data images; not total abundance. Counts do not require spatial calibration.';
  button.append(label);
 });
};
$('search').oninput=()=>renderGrids();
function updateDensityCounts(summary){
 densityLoaded=true;densityGridCounts=summary.grid_counts&&Object.values(summary.grid_counts).reduce((a,b)=>a+b,0)===(summary.matched??Object.values(summary.grid_counts).reduce((a,b)=>a+b,0))?summary.grid_counts:null;renderGrids();
 $('foilStyle').disabled=true;$('densityClear').disabled=false;drawHoles();
}
const densityScale=document.createElement('small');densityScale.className='controls';views.grid.card.append(densityScale);
const densityPalettes={viridis:['#440154','#3b528b','#21918c','#5ec962','#fde725'],heat:['#1857ef','#00d5e8','#f4e822','#ed3024'],gray:['#101010','#ffffff']};
function densityColor(value,max){
 const colors=densityPalettes[$('densityPalette').value]||densityPalettes.viridis;
 const t=max>0?Math.max(0,Math.min(1,value/max)):0,pos=t*(colors.length-1),i=Math.min(colors.length-2,Math.floor(pos)),f=pos-i;
 const rgb=h=>[1,3,5].map(k=>parseInt(h.slice(k,k+2),16));const a=rgb(colors[i]),b=rgb(colors[i+1]);
 return `rgb(${a.map((v,k)=>Math.round(v+(b[k]-v)*f)).join(',')})`;
}
// Acquisition identity belongs exclusively to the stroke, even if the main
// viewer has a saved preference for filled markers. Density owns the fill.
const originalStyleFoilCircle=styleFoilCircle;
styleFoilCircle=function(circle,selected,marker){originalStyleFoilCircle(circle,selected,marker);if(densityLoaded)circle.style.fill='none'};
$('foilStyle').title='While density is loaded, outlines show acquisition groups and fills show particle density.';
function drawDensity(){
 document.querySelector('#densityLayer')?.remove();
 densityScale.textContent='';
 if(!$('densityVisible').checked||!densityResult||densityResult.gid!==grid?.id)return;
 const layer=document.createElementNS('http://www.w3.org/2000/svg','g');layer.id='densityLayer';layer.style.pointerEvents='none';
 const opacity=Number($('densityOpacity').value)/100;
 const mean=$('densityMode').value==='holes';
 const max=mean?Math.max(0,...densityResult.holes.map(h=>h.density)):densityResult.max_density;
 $('densityLegend').textContent=`Fill: 0–${(max||0).toFixed(1)} particles/µm². Linear gradient, scaled within this GridSquare. Transparent = unrepresented, not zero. `+densityResult.note;
 const bar=document.createElement('span');bar.style.cssText='display:inline-block;width:150px;height:12px;border:1px solid #aab4c0;border-radius:3px';
 bar.style.background='linear-gradient(to right,'+densityPalettes[$('densityPalette').value].join(',')+')';
 densityScale.append('Density fill: 0 ',bar,` ${(max||0).toFixed(1)} particles/µm² · square-relative · blank = unknown`);
 if(mean){
  for(const h of densityResult.holes){
   const m=grid.markers?.find(m=>m.hole===h.hole);if(!m)continue;
   const c=document.createElementNS(layer.namespaceURI,'circle');c.setAttribute('cx',m.x);c.setAttribute('cy',m.y);c.setAttribute('r',foilOverlay.radius/100);
   c.style.fill=densityColor(h.density,max);c.style.fillOpacity=opacity;c.style.stroke='none';layer.append(c);
  }
 }else{
  for(const cell of densityResult.cells){
   const p=document.createElementNS(layer.namespaceURI,'polygon');p.setAttribute('points',cell.points.map(p=>p.join(',')).join(' '));
   p.style.fill=densityColor(cell.density,max);p.style.fillOpacity=opacity;p.style.stroke='none';layer.append(p);
  }
 }
 // Behind outlines and acquisition hit targets; density never steals clicks.
 views.grid.svg.prepend(layer);
}
const originalDrawHoles=drawHoles;
drawHoles=function(){originalDrawHoles();drawDensity()};
async function loadDensity(){
 const token=++densityRequest,gid=grid?.id;
 densityResult=null;drawDensity();
 if(!densityLoaded||!gid)return;
 $('densityLegend').textContent='Mapping density using this square’s EPU metadata…';
 try{
  const job=await api('/api/density/grid/'+gid,{});
  const result=await waitJob(job.job,()=>token===densityRequest&&grid?.id===gid);
  if(!result||token!==densityRequest||grid?.id!==gid)return;
  densityResult={...result,gid};drawDensity();
 }catch(e){if(token===densityRequest)$('densityLegend').textContent='Density unavailable: '+e.message}
}
const originalSelectGrid=selectGrid;
selectGrid=async function(id){await originalSelectGrid(id);await loadDensity()};
$('densityVisible').onchange=drawDensity;$('densityMode').onchange=drawDensity;$('densityOpacity').oninput=drawDensity;
$('densityPalette').onchange=drawDensity;
$('densityClear').onclick=async()=>{
 try{
  await api('/api/density/clear',{});densityRequest++;densityResult=null;densityLoaded=false;densityGridCounts=null;
  $('foilStyle').disabled=false;$('densityClear').disabled=true;
  $('densityStatus').textContent='Density cleared. Import another particle subset when needed.';
  $('densityLegend').textContent='';renderGrids();drawHoles();
 }catch(e){$('densityStatus').textContent='Could not clear density: '+e.message}
};
drawHoles();
$('densityImport').onclick=async()=>{
 const main=$('densityMain').files[0];if(!main){$('densityStatus').textContent='Choose a particle .cs file first.';return}
 const data=new FormData();data.append('particles',main);
 if($('densityPass').files[0])data.append('passthrough',$('densityPass').files[0]);
 $('densityImport').disabled=true;$('densityClear').disabled=true;densityPanel.open=true;
 $('densityStatus').textContent='Uploading particle tables…';
 const activity=beginActivity('Uploading particle tables');
 try{
  const response=await fetch('/api/density/import',{method:'POST',body:data});
  if(!response.ok)throw Error(await response.text());
  activity.update('Matching particle coordinates to EPU exposures');
  $('densityStatus').textContent='Matching particle coordinates to EPU exposures…';
  const job=(await response.json()).job;jobActivities.set(job,['Matching particle coordinates to EPU exposures','']);
  activity.finish();
  const result=await waitJob(job);
  updateDensityCounts(result);
  $('densityStatus').textContent=`${result.matched.toLocaleString()} / ${result.particles.toLocaleString()} particles matched; ${result.represented_holes} holes, ${result.represented_exposures} exposures. Unmatched: ${result.unmatched}; ambiguous: ${result.ambiguous}; invalid coordinates: ${result.invalid}. `+result.warning;
  await loadDensity();
 }catch(e){$('densityStatus').textContent='Import failed: '+e.message}
 finally{activity.finish();$('densityImport').disabled=false;$('densityClear').disabled=!densityLoaded}
};
api('/api/density/summary').then(s=>{
 if(s.loaded){updateDensityCounts(s);$('densityStatus').textContent=`${s.matched.toLocaleString()} selected particles loaded across ${s.represented_holes} holes. `+s.warning;loadDensity()}
}).catch(e=>{$('densityStatus').textContent='Density service unavailable: '+e.message});
