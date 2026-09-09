const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element{
 constructor(){this.style={};this.children=[];this.checked=true;this.hidden=false;this.value='';this.handlers={}}
 append(c){this.children.push(c)}
 setAttribute(k,v){this[k]=v}
 addEventListener(k,v){this.handlers[k]=v}
 remove(){svg.children=svg.children.filter(x=>x!==this)}
}
const svg=new Element();svg.getScreenCTM=()=>({inverse:()=>({sx:1/400,sy:1/200,tx:-.25,ty:-.25})});
const elements=Object.fromEntries(['positionVisible','positionMark','positionCancel','positionRemove','positionNote','positionStatus','positionStats'].map(k=>[k,new Element()]));
const views={grid:{card:new Element(),viewport:new Element(),svg,img:{hidden:false}},foil:{key:'f',img:{hidden:false}}};
const context=vm.createContext({console,views,grid:{id:'g',image:'image',markers:[{hole:'1',x:.2,y:.3}]},hole:'1',config:{transform:'identity'},
 $:k=>elements[k],document:{createElement:()=>new Element(),createElementNS:()=>new Element(),querySelector:()=>svg.children.find(x=>x.id==='positionLayer'),addEventListener(){}},
 DOMPoint:class{constructor(x,y){this.x=x;this.y=y}matrixTransform(m){return{x:this.x*m.sx+m.tx,y:this.y*m.sy+m.ty}}},
 api:async()=>({corrections:[]}),drawHoles(){},selectGrid:async()=>{},beginActivity:()=>({finish(){}}),confirm:()=>true});
vm.runInContext(fs.readFileSync('src/position_corrections.js','utf8'),context);
(async()=>{
 await vm.runInContext('loadPositions()',context);
 const point=vm.runInContext('positionPoint(views.grid.svg,300,150)',context);
 assert.equal(point[0],.5);assert.equal(point[1],.5);
 assert.equal(vm.runInContext('positionPoint(views.grid.svg,0,0)',context),null);
 elements.positionMark.onclick();assert.equal(vm.runInContext('positionArmed.foil',context),'f');
 context.hole='2';vm.runInContext('drawPositions()',context);assert.equal(vm.runInContext('positionArmed',context),null);
 views.foil.img.hidden=true;elements.positionMark.onclick();assert.match(elements.positionStatus.textContent,/own FoilHole preview/);
 vm.runInContext("positionGrid='g';positionRows=[{grid_image:'image',transform:'identity',original:[.2,.3],observed:[.4,.5],hole:'1',dx:.2,dy:.2,note:''}];drawPositions()",context);
 assert.equal(svg.children.length,1);assert.equal(svg.children[0].children[0].style.fill,'none');
 assert.match(svg.children[0].children[0].d,/M 0.2 0.3 L 0.4 0.5/);
 elements.positionVisible.checked=false;elements.positionVisible.onchange();assert.equal(svg.children.length,0);
 console.log('Observed-position UI: zoom/pan conversion, bounds, selection guard, arrow direction and layer toggle passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
