// Headless regression checks for the experimental overlay's independent layers.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element{
 constructor(){this.style={};this.children=[];this.attributes={};this.value='';this.checked=true;this._text=''}
 set textContent(value){this._text=value;this.children=[]}
 get textContent(){return this._text}
 setAttribute(k,v){this.attributes[k]=v}
 append(...items){this.children.push(...items)}
 prepend(item){this.children.unshift(item)}
 after(){}
 remove(){svg.children=svg.children.filter(c=>c!==this)}
}
const svg=new Element(),card=new Element();
const elements=Object.fromEntries(['densityClear','grids','search','densityPalette','densityVisible','densityMode','densityOpacity','densityLegend','densityStatus','densityImport','densityMain','densityPass','foilStyle'].map(id=>[id,new Element()]));
elements.densityPalette.value='viridis';elements.densityMode.value='holes';elements.densityOpacity.value='65';
const context=vm.createContext({console,document:{
 createElement:()=>new Element(),createElementNS:()=>new Element(),
 querySelector:q=>q==='#densityLayer'?svg.children.find(c=>c.id==='densityLayer'):new Element()
},$:id=>elements[id],views:{grid:{svg,card}},foilOverlay:{radius:1.2,style:'filled'},
 grid:{id:'g',markers:[{hole:'1',x:.3,y:.4}]},
 styleFoilCircle:(circle,selected,marker)=>{circle.style.fill='red';circle.style.stroke=marker.color;circle.style.strokeDasharray=marker.role==='shifted'?'dashed':'';circle.style.strokeWidth=selected?3:1},
 grids:[{id:'g',name:'GridSquare_1'},{id:'empty',name:'GridSquare_2'}],
 renderGrids(){elements.grids.children=context.grids.filter(g=>g.name.toLowerCase().includes(elements.search.value.toLowerCase())).map(()=>new Element())},
 drawHoles(){},selectGrid:async()=>{},api:async()=>({loaded:false})});
vm.runInContext(fs.readFileSync('src/cryosparc_density.js','utf8'),context);
vm.runInContext(`
 densityResult={gid:'g',holes:[{hole:'1',density:10}],cells:[],max_density:10,note:'test'};
 drawDensity();
`,context);
assert.equal(svg.children.length,1);
assert.equal(svg.children[0].children[0].style.stroke,'none');
assert.equal(svg.children[0].children[0].style.fill,'rgb(253,231,37)');
assert.equal(svg.children[0].style.pointerEvents,'none');
vm.runInContext('updateDensityCounts({grid_counts:{g:10},matched:10})',context);
assert.equal(elements.foilStyle.disabled,true);
const circle=new Element();context.circle=circle;
vm.runInContext("styleFoilCircle(circle,true,{color:'blue',role:'shifted'})",context);
assert.equal(circle.style.fill,'none');assert.equal(circle.style.stroke,'blue');
assert.equal(circle.style.strokeDasharray,'dashed');assert.equal(circle.style.strokeWidth,3);
elements.densityPalette.value='gray';elements.densityPalette.onchange();
assert.equal(svg.children[0].children[0].style.fill,'rgb(255,255,255)');
assert.equal(vm.runInContext('densityColor(5,10)',context),'rgb(136,136,136)');
vm.runInContext('foilOverlay.radius=2;drawHoles()',context);
assert.equal(svg.children[0].children[0].attributes.r,.02);
elements.densityVisible.checked=false;elements.densityVisible.onchange();
assert.equal(svg.children.length,0);assert.equal(circle.style.stroke,'blue');
console.log('Density UI: independent outlines, gradient endpoints/interpolation, radius and visibility passed.');
vm.runInContext('densityLoaded=false;renderGrids()',context);
assert.equal(elements.grids.children[0].children.length,0);
vm.runInContext('updateDensityCounts({grid_counts:{g:1200}})',context);
assert.match(elements.grids.children[0].children[0].textContent,/1[,.]200 mapped particles/);
assert.equal(elements.grids.children[1].children[0].textContent,'0 mapped particles');
elements.search.value='square_2';elements.search.oninput();
assert.equal(elements.grids.children.length,1);
assert.equal(elements.grids.children[0].children[0].textContent,'0 mapped particles');
vm.runInContext('updateDensityCounts({grid_counts:{empty:1}})',context);
assert.equal(elements.grids.children[0].children[0].textContent,'1 mapped particle');
vm.runInContext('renderGrids()',context);
assert.equal(elements.grids.children[0].children.length,1);
console.log('Density list counts: import, zero matches, filtering, replacement and redraw passed.');
vm.runInContext('updateDensityCounts({matched:9910})',context);
assert.match(elements.grids.children[0].children[0].textContent,/unavailable/);
vm.runInContext('updateDensityCounts({matched:9910,grid_counts:{empty:0}})',context);
assert.match(elements.grids.children[0].children[0].textContent,/unavailable/);
