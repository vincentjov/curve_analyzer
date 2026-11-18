// code belongs to: Vincent Jovan Wijaya
const charts=[];
window.addEventListener('message',async function receive(event){
 if(event.origin!==location.origin||event.source!==opener||event.data?.type!=='curve-snapshot')return;
 window.removeEventListener('message',receive);
 for(const graph of event.data.graphs){const card=document.createElement('div');card.className='graph-card';const div=document.createElement('div');div.className='chart';card.append(div);document.getElementById('detachedGraphs').append(card);
 await Plotly.newPlot(div,graph.data,graph.layout,{responsive:true,displaylogo:false,scrollZoom:false,toImageButtonOptions:{format:'png',scale:2}});charts.push(div);
 div.addEventListener('wheel',e=>{e.preventDefault();const axis=e.shiftKey?'xaxis':'yaxis',r=div._fullLayout[axis].range,factor=e.deltaY>0?1.18:1/1.18,mid=(r[0]+r[1])/2;Plotly.relayout(div,{[axis+'.range']:[mid+(r[0]-mid)*factor,mid+(r[1]-mid)*factor],[axis+'.autorange']:false});},{passive:false});
 }
});
document.getElementById('reset').onclick=()=>charts.forEach(d=>Plotly.relayout(d,{'xaxis.autorange':true,'yaxis.autorange':true}));
document.getElementById('print').onclick=()=>window.print();
window.opener?.postMessage({type:'curve-detached-ready'},location.origin);
