// Orbit charts. Hand-drawn SVG, no dependencies.
// fmt through mk, areaChart, barChart and waterfall are copied verbatim from docs/index.html;
// tests/test_charts_port.py fails if they drift. spark and everything below it are new.
(function(){
'use strict';
const fmt=(n,opt={})=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:0,...opt}).format(n);
const fmtS=n=>(n<0?'−':'')+fmt(Math.abs(n));
const fmtK=n=>{const a=Math.abs(n);const s=a>=1e6?(a/1e6).toFixed(2)+'M':a>=1e3?Math.round(a/1e3)+'k':Math.round(a);return (n<0?'−':'')+'$'+s};
const dayStr=d=>d.toLocaleDateString('en-US',{month:'short',day:'numeric'});
const el=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e};
const svgNS='http://www.w3.org/2000/svg';
function mk(tag,attrs){const e=document.createElementNS(svgNS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);return e}
function areaChart(host,series,opts={}){
  const W=600,H=opts.h||180,pL=4,pR=4,pT=12,pB=opts.axis?22:6;
  const n=series.length,min=Math.min(...series),max=Math.max(...series),span=(max-min)||1;
  const x=i=>pL+(i/(n-1))*(W-pL-pR),y=v=>pT+(1-(v-min)/span)*(H-pT-pB);
  const svg=mk('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':opts.label||'chart'});
  const gid='g'+Math.random().toString(36).slice(2,8);
  const defs=mk('defs',{});defs.innerHTML=`<linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--s1)" stop-opacity=".25"/><stop offset="1" stop-color="var(--s1)" stop-opacity="0"/></linearGradient>`;svg.appendChild(defs);
  if(opts.grid!==false){[0,.5,1].forEach(t=>{const yy=pT+t*(H-pT-pB);svg.appendChild(mk('line',{x1:pL,x2:W-pR,y1:yy,y2:yy,stroke:'var(--line)','stroke-width':1}));svg.appendChild(Object.assign(mk('text',{x:pL,y:yy-4,'font-size':10,fill:'var(--fg-3)','font-family':'var(--num)'}),{textContent:fmtK(max-t*span)}));});}
  let d='';series.forEach((v,i)=>{d+=(i?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)+' ';});
  svg.appendChild(mk('path',{d:d+`L${x(n-1).toFixed(1)} ${H-pB} L${x(0).toFixed(1)} ${H-pB} Z`,fill:`url(#${gid})`}));
  svg.appendChild(mk('path',{d,fill:'none',stroke:'var(--s1)','stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));
  if(opts.axis){const ticks=opts.ticks||4;for(let t=0;t<=ticks;t++){const i=Math.round(t/ticks*(n-1));svg.appendChild(Object.assign(mk('text',{x:x(i),y:H-6,'text-anchor':t===0?'start':t===ticks?'end':'middle','font-size':10,fill:'var(--fg-3)'}),{textContent:opts.axis(i)}));}}
  svg.appendChild(mk('circle',{cx:x(n-1),cy:y(series[n-1]),r:4,fill:'var(--s1)',stroke:'var(--surface)','stroke-width':2}));
  const cross=mk('line',{x1:0,x2:0,y1:pT,y2:H-pB,stroke:'var(--line-strong)','stroke-width':1,'stroke-dasharray':'3 3',opacity:0});
  const dot=mk('circle',{r:4,fill:'var(--s1)',stroke:'var(--surface)','stroke-width':2,opacity:0});svg.appendChild(cross);svg.appendChild(dot);
  host.innerHTML='';host.appendChild(svg);const tip=el('div','tip');host.appendChild(tip);
  const hit=mk('rect',{x:0,y:0,width:W,height:H,fill:'transparent'});svg.appendChild(hit);
  hit.addEventListener('pointermove',e=>{const r=svg.getBoundingClientRect();const fx=(e.clientX-r.left)/r.width*W;const i=Math.max(0,Math.min(n-1,Math.round((fx-pL)/(W-pL-pR)*(n-1))));const cx=x(i),cy=y(series[i]);cross.setAttribute('x1',cx);cross.setAttribute('x2',cx);cross.setAttribute('opacity',1);dot.setAttribute('cx',cx);dot.setAttribute('cy',cy);dot.setAttribute('opacity',1);tip.innerHTML=`${opts.tipLabel?opts.tipLabel(i):''}<b>${fmtS(Math.round(series[i]))}</b>`;tip.style.left=(cx/W*100)+'%';tip.style.top=(cy/H*100)+'%';tip.classList.add('on');});
  hit.addEventListener('pointerleave',()=>{cross.setAttribute('opacity',0);dot.setAttribute('opacity',0);tip.classList.remove('on')});
}
function barChart(host,labels,seriesList,opts={}){
  const W=600,H=opts.h||200,pL=4,pR=4,pT=12,pB=22;const all=seriesList.flat();const max=Math.max(0,...all),min=Math.min(0,...all),span=(max-min)||1;
  const y=v=>pT+(1-(v-min)/span)*(H-pT-pB),y0=y(0);const n=labels.length,gw=(W-pL-pR)/n,k=seriesList.length,bw=Math.min(28,(gw-10)/k-2);
  const svg=mk('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':opts.label||'bar chart'});
  [max,0,min].filter((v,i,a)=>a.indexOf(v)===i).forEach(v=>{svg.appendChild(mk('line',{x1:pL,x2:W-pR,y1:y(v),y2:y(v),stroke:v===0?'var(--line-strong)':'var(--line)','stroke-width':1}));if(v!==0)svg.appendChild(Object.assign(mk('text',{x:pL,y:v<0?y(v)+12:y(v)-4,'font-size':10,fill:'var(--fg-3)','font-family':'var(--num)'}),{textContent:fmtK(v)}));});
  host.innerHTML='';host.appendChild(svg);const tip=el('div','tip');host.appendChild(tip);
  labels.forEach((lb,i)=>{const gx=pL+i*gw+gw/2-(k*bw+(k-1)*2)/2;seriesList.forEach((s,j)=>{const v=s[i];const col=opts.colors?opts.colors(v,j):j===0?'var(--s1)':'var(--s2)';const top=Math.min(y(v),y0),h=Math.max(2,Math.abs(y(v)-y0));const r=mk('rect',{x:gx+j*(bw+2),y:top,width:bw,height:h,rx:2,fill:col});svg.appendChild(r);
    r.addEventListener('pointerenter',()=>{tip.innerHTML=`${lb}${opts.names?' · '+opts.names[j]:''} <b>${fmtS(Math.round(v))}</b>`;tip.style.left=((gx+j*(bw+2)+bw/2)/W*100)+'%';tip.style.top=(top/H*100)+'%';tip.classList.add('on')});r.addEventListener('pointerleave',()=>tip.classList.remove('on'));});
    svg.appendChild(Object.assign(mk('text',{x:pL+i*gw+gw/2,y:H-6,'text-anchor':'middle','font-size':10,fill:'var(--fg-3)'}),{textContent:lb}));});
}
function waterfall(host,steps){const W=600,H=230,pL=4,pR=4,pT=14,pB=36;let run=0,lo=0,hi=0;const pts=steps.map(s=>{const a=run;run+=s.v;lo=Math.min(lo,run,a);hi=Math.max(hi,run,a);return [a,run,s]});const span=(hi-lo)||1;const y=v=>pT+(1-(v-lo)/span)*(H-pT-pB);const n=steps.length,gw=(W-pL-pR)/n,bw=Math.min(44,gw-14);
  const svg=mk('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':'Gross pay to net worth waterfall'});svg.appendChild(mk('line',{x1:pL,x2:W-pR,y1:y(0),y2:y(0),stroke:'var(--line-strong)','stroke-width':1}));host.innerHTML='';host.appendChild(svg);const tip=el('div','tip');host.appendChild(tip);
  pts.forEach(([a,b,s],i)=>{const x=pL+i*gw+gw/2-bw/2;const top=y(Math.max(a,b)),h=Math.max(2,Math.abs(y(a)-y(b)));const col=s.total?'var(--fg)':s.v>=0?'var(--s1)':'var(--s2)';const r=mk('rect',{x,y:top,width:bw,height:h,rx:2,fill:col});svg.appendChild(r);if(i<n-1)svg.appendChild(mk('line',{x1:x+bw,x2:x+gw,y1:y(b),y2:y(b),stroke:'var(--line-strong)','stroke-dasharray':'2 3'}));
    svg.appendChild(Object.assign(mk('text',{x:x+bw/2,y:top-4,'text-anchor':'middle','font-size':10,fill:'var(--fg-2)','font-family':'var(--num)'}),{textContent:(s.total?'':s.v>=0?'+':'−')+fmtK(Math.abs(s.v))}));
    const lb=Object.assign(mk('text',{x:x+bw/2,y:H-22,'text-anchor':'middle','font-size':10,fill:'var(--fg-3)'}),{textContent:s.l});svg.appendChild(lb);if(s.l2)svg.appendChild(Object.assign(mk('text',{x:x+bw/2,y:H-10,'text-anchor':'middle','font-size':10,fill:'var(--fg-3)'}),{textContent:s.l2}));
    r.addEventListener('pointerenter',()=>{tip.innerHTML=`${s.l}${s.l2?' '+s.l2:''} <b>${fmtS(Math.round(s.v))}</b>`;tip.style.left=((x+bw/2)/W*100)+'%';tip.style.top=(top/H*100)+'%';tip.classList.add('on')});r.addEventListener('pointerleave',()=>tip.classList.remove('on'));});}
function spark(host,series,opts={}){
  const W=opts.w||80,H=opts.h||20,pad=2,n=series.length;host.innerHTML='';if(n<2)return;
  const min=Math.min(...series),max=Math.max(...series),span=(max-min)||1;
  const x=i=>pad+(i/(n-1))*(W-2*pad),y=v=>pad+(1-(v-min)/span)*(H-2*pad);
  const svg=mk('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':opts.label||'trend'});
  let d='';series.forEach((v,i)=>{d+=(i?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)+' ';});
  svg.appendChild(mk('path',{d,fill:'none',stroke:opts.color||'var(--s1)','stroke-width':1.5,'stroke-linejoin':'round','stroke-linecap':'round'}));
  host.appendChild(svg);
}

// Parse YYYY-MM-DD as a local date; new Date(string) reads it as UTC and shows the day before in US time zones.
const parseDay=s=>{const [y,m,d]=s.split('-').map(Number);return new Date(y,m-1,d)};
function drawAreaFromData(host){
  const c=JSON.parse(host.dataset.chart);
  if(c.series.length<2){host.innerHTML='<div class="empty">Not enough history yet.</div>';return}
  const dates=c.dates.map(parseDay);
  areaChart(host,c.series,{h:c.h||200,axis:i=>c.monthly?dates[i].toLocaleDateString('en-US',{month:'short',year:'2-digit'}):dayStr(dates[i]),tipLabel:i=>dayStr(dates[i])+' ',label:c.label||'Net worth over time'});
}
// The server puts each chart's points in data-chart. htmx.onLoad fires for the first page and for every swap.
htmx.onLoad(root=>{const hosts=root.matches&&root.matches('[data-chart]')?[root]:root.querySelectorAll('[data-chart]');hosts.forEach(drawAreaFromData)});
// Mark the range button only once its chart has arrived, so a failed request leaves the old state showing.
document.addEventListener('htmx:afterRequest',e=>{const b=e.detail.elt;if(!e.detail.successful||!b.matches('.seg button'))return;b.closest('.seg').querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',String(x===b)))});
window.OrbitCharts={areaChart,barChart,waterfall,spark,fmt,fmtS,fmtK};
})();
