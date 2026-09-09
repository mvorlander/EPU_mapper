// Diagnostic annotations only. Original hole positions and density stay intact.
const positionTools=document.createElement('details');positionTools.className='contrast';
positionTools.innerHTML=`<summary>Observed targeting shifts</summary>
<p>Compare the FoilHole preview with this GridSquare. Arrow: EPU position → your observed position. Original metadata is unchanged.</p>
<label><input type="checkbox" id="positionVisible" checked> Show observed-shift arrows</label>
<button id="positionMark">Mark observed position</button> <button id="positionCancel" hidden>Cancel (Esc)</button>
<button id="positionRemove">Remove selected correction</button>
<input id="positionNote" style="width:100%;max-width:400px" placeholder="Optional note for this observation" aria-label="Targeting shift note">
<p id="positionStatus" role="status">Select a hole with its own FoilHole preview, then mark its observed position on the GridSquare.</p>
<p id="positionStats"></p><a href="/api/position-corrections.json" download>Export observed shifts (JSON)</a>`;
views.grid.card.append(positionTools);
let positionRows=[],positionGrid='',positionToken=0,positionArmed=null,positionSaving=false,positionSuppressClick=false;
function selectedPosition(){return positionRows.find(r=>r.hole===String(hole)&&r.foil===views.foil.key)}
function cancelPosition(){positionArmed=null;$('positionCancel').hidden=true;views.grid.viewport.style.cursor='';}
function positionPoint(svg,x,y){const matrix=svg.getScreenCTM();if(!matrix)return null;const p=new DOMPoint(x,y).matrixTransform(matrix.inverse());return p.x>=0&&p.x<=1&&p.y>=0&&p.y<=1?[p.x,p.y]:null}
function drawPositions(){
 document.querySelector('#positionLayer')?.remove();
 if(positionArmed&&(positionArmed.gid!==grid?.id||positionArmed.hole!==String(hole)||positionArmed.foil!==views.foil.key))cancelPosition();
 if(positionGrid!==grid?.id)return;
 const rows=positionRows.filter(r=>r.grid_image===grid.image&&r.transform===(config.transform||'identity'));
 const n=rows.length,mean=axis=>n?rows.reduce((s,r)=>s+r[axis],0)/n:0;
 $('positionStats').textContent=n?`${n} marked previews · mean Δx ${(100*mean('dx')).toFixed(2)}%, Δy ${(100*mean('dy')).toFixed(2)}% of image width/height. Right/down are positive. Marked cases only—not proof of a systematic shift.`:'No observations for this GridSquare/image coordinate frame.';
 if(!$('positionVisible').checked)return;
 const ns='http://www.w3.org/2000/svg',layer=document.createElementNS(ns,'g');layer.id='positionLayer';layer.style.pointerEvents='none';
 for(const r of rows){
  const [x,y]=r.original,[u,v]=r.observed,angle=Math.atan2(v-y,u-x),size=.008;
  const shape=document.createElementNS(ns,'path');
  shape.setAttribute('d',`M ${x} ${y} L ${u} ${v} M ${u-size*Math.cos(angle-.5)} ${v-size*Math.sin(angle-.5)} L ${u} ${v} L ${u-size*Math.cos(angle+.5)} ${v-size*Math.sin(angle+.5)}`);
  shape.setAttribute('vector-effect','non-scaling-stroke');shape.style.fill='none';shape.style.stroke=r.hole===String(hole)?'#ffeb85':'#fa95ea';shape.style.strokeWidth=r.hole===String(hole)?'3px':'2px';
  shape.style.filter='drop-shadow(0 0 1px black)';layer.append(shape);
  const title=document.createElementNS(ns,'title');title.textContent=`FoilHole ${r.hole}: EPU → observed. ${r.note}`;shape.append(title);
 }
 views.grid.svg.append(layer);
}
async function loadPositions(){
 const token=++positionToken,gid=grid?.id;positionRows=[];positionGrid='';$('positionStats').textContent=gid?'Loading saved observations…':'';drawPositions();if(!gid)return;
 try{const result=await api('/api/position-corrections/'+gid);if(token!==positionToken||gid!==grid?.id)return;positionRows=result.corrections;positionGrid=gid;drawPositions()}
 catch(e){if(token===positionToken)$('positionStatus').textContent='Could not load observations: '+e.message}
}
const positionDrawHoles=drawHoles;drawHoles=function(){positionDrawHoles();drawPositions()};
const positionSelectGrid=selectGrid;selectGrid=async function(id){cancelPosition();await positionSelectGrid(id);await loadPositions()};
$('positionVisible').onchange=drawPositions;
$('positionCancel').onclick=()=>{cancelPosition();$('positionStatus').textContent='Position marking cancelled.'};
$('positionMark').onclick=()=>{
 const m=grid?.markers?.find(m=>String(m.hole)===String(hole));
 if(positionSaving)return;
 if(!m||views.foil.img.hidden||!views.foil.key||views.grid.img.hidden){$('positionStatus').textContent='Wait for the GridSquare and this hole’s own FoilHole preview to load. Beam-shift targets without a preview cannot be annotated with this tool.';return}
 positionArmed={gid:grid.id,hole:String(hole),foil:views.foil.key};positionTools.open=true;
 $('positionVisible').checked=true;$('positionCancel').hidden=false;views.grid.viewport.style.cursor='crosshair';
 $('positionStatus').textContent='Click the observed hole centre on the GridSquare. Scroll to zoom; Esc cancels. Clicking again after saving replaces this preview’s correction.';
};
async function savePosition(point,remove=false){
 const target=positionArmed||{gid:grid?.id,hole:String(hole),foil:views.foil.key};if(!target.gid||!target.foil||positionSaving)return;
 if(target.gid!==grid?.id||target.hole!==String(hole)||target.foil!==views.foil.key||views.foil.img.hidden){cancelPosition();$('positionStatus').textContent='Selection changed. Wait for the preview, then activate the tool again.';return}
 positionSaving=true;$('positionMark').disabled=true;$('positionRemove').disabled=true;cancelPosition();
 const activity=beginActivity('Saving observed targeting shift','grid');
 try{
  await api(`/api/position-corrections/${target.gid}/${target.hole}`,{foil:target.foil,x:point?.[0],y:point?.[1],remove,note:$('positionNote').value});
  if(grid?.id===target.gid){await loadPositions();$('positionStatus').textContent=remove?'Correction removed.':'Observed position saved locally. EPU positions and acquisition/density mapping are unchanged.'}
 }catch(e){$('positionStatus').textContent='Could not save observation: '+e.message}
 finally{positionSaving=false;$('positionMark').disabled=false;$('positionRemove').disabled=false;activity.finish()}
}
$('positionRemove').onclick=()=>{if(!selectedPosition()){$('positionStatus').textContent='No saved correction for the displayed FoilHole preview.';return}if(confirm('Remove the observed position for this FoilHole preview?'))savePosition(null,true)};
let positionDown=null;
views.grid.viewport.addEventListener('pointerdown',e=>{if(!positionArmed)return;e.preventDefault();e.stopImmediatePropagation();positionDown=[e.clientX,e.clientY];positionSuppressClick=true},true);
views.grid.viewport.addEventListener('pointerup',e=>{
 if(!positionArmed||!positionDown)return;e.preventDefault();e.stopImmediatePropagation();const start=positionDown;positionDown=null;
 if(Math.hypot(e.clientX-start[0],e.clientY-start[1])>5)return;
 const point=positionPoint(views.grid.svg,e.clientX,e.clientY);if(point)savePosition(point);else $('positionStatus').textContent='Click inside the GridSquare image, not its margins.';
},true);
views.grid.viewport.addEventListener('click',e=>{if(positionArmed||positionSuppressClick){e.preventDefault();e.stopImmediatePropagation();positionSuppressClick=false}},true);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&positionArmed){cancelPosition();$('positionStatus').textContent='Position marking cancelled.'}},true);
loadPositions();
