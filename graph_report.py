"""Make an offline, self-contained interactive HTML report with no dependencies."""
import json
from pathlib import Path

TEMPLATE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>KnitCode static analysis report</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#c9d1d9;--muted:#8b949e;--accent:#58a6ff;--good:#3fb950;--warn:#d29922}
*{box-sizing:border-box}body{font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:22px}
h1{margin:0 0 10px;font-size:24px}.subtitle{color:var(--muted);margin-bottom:20px}p{margin:8px 0}.metrics{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
.metric{background:var(--panel);border:1px solid var(--border);border-radius:9px;padding:12px 18px;min-width:115px}
.metric b{display:block;font-size:24px;color:#fff}.metric span{color:var(--muted);font-size:12px}
main{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(320px,1fr);gap:16px}
section{border:1px solid var(--border);border-radius:9px;background:var(--panel);padding:14px;min-width:0}
h2{font-size:16px;margin:0 0 10px}input,select{background:#0d1117;border:1px solid var(--border);border-radius:6px;color:var(--fg);padding:9px;width:100%;margin-bottom:9px}
.list{max-height:64vh;overflow-y:auto}button.row{display:block;width:100%;border:0;background:transparent;text-align:left;color:var(--fg);padding:9px;border-bottom:1px solid var(--border);font:inherit;cursor:pointer;overflow-wrap:anywhere}
button.row:hover{background:#21262d}small{font-size:12px;color:var(--muted)}.type{color:var(--accent);font-size:11px;margin-right:7px}#focusSvg{background:#0d1117;border:1px solid var(--border);border-radius:8px;display:block;width:100%;height:330px}
.details{border-top:1px solid var(--border);padding-top:10px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:220px;overflow-y:auto;font:12px/1.5 ui-monospace,Consolas,monospace}
.classBars{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0 16px}.classBars button{cursor:pointer;background:#21262d;border:1px solid #30363d;color:var(--fg);border-radius:8px;padding:8px 12px;text-align:left}.classBars small{display:block}.classBars button:hover{border-color:var(--accent)}.tabs{display:flex;gap:5px;margin-bottom:9px}.tabs button{padding:6px 10px;background:#21262d;color:var(--fg);border:1px solid var(--border);border-radius:6px;cursor:pointer}.tabs button.active{border-color:var(--accent);color:var(--accent)}
@media(max-width:900px){main{grid-template-columns:1fr}.list{max-height:40vh}}
</style></head><body>
<h1>KnitCode — 정적 분석 결과</h1><p class="subtitle">분석 대상: <span id="repo"></span> · 호출 관계 탐색과 해석 근거 검토 (로컬 오프라인 HTML)</p>
<div class="metrics" id="metrics"></div>
<section style="margin-bottom:16px"><h2>전체 호출 상태 (v2.4)</h2><p class="subtitle">내부 호출 연결 / 출처가 확인된 비내부 호출 / 근거 부족 호출을 분리합니다. 타입 힌트로 연결한 메서드는 MAY_CALL이며 실제 런타임 타입을 증명하지 않습니다. UNKNOWN은 내부 호출 누락과 동의어가 아닙니다.</p><div id="statusBars" class="classBars"></div></section>
<section style="margin-bottom:16px"><h2>미해결 호출 재분류 (자동 추정, 정답 데이터 아님)</h2><p class="subtitle">원래 미해결 이유(reason)는 유지됩니다. 분류(category)는 호출 대상 성격을 추정한 값이며, 분기·재할당 등으로 불확실할 수 있습니다.</p><div id="classBars" class="classBars"></div></section>
<main><section><h2>관계·미해결 호출</h2><div class="tabs"><button id="tResolved" onclick="tab('edges')" class="active">Edges</button><button id="tUnresolved" onclick="tab('unresolved')">Unresolved</button><button id="tConditional" onclick="tab('conditional')">Conditional</button><button id="tStatus" onclick="tab('status')">All call statuses</button></div>
<input id="q" placeholder="함수명, 파일, 규칙, 호출 검색..." oninput="renderList()" />
<select id="status" onchange="renderList()"><option value="">모든 호출 상태</option><option>RESOLVED_INTERNAL</option><option>KNOWN_NON_INTERNAL</option><option>UNKNOWN</option></select>
<select id="category" onchange="renderList()"><option value="">모든 재분류</option></select>
<select id="type" onchange="renderList()"><option value="">모든 관계</option><option>CALLS</option><option>INSTANTIATES</option><option>CONTAINS</option><option>INHERITS</option></select><div id="list" class="list"></div></section>
<section><h2 id="focusTitle">호출 관계 미리보기</h2><svg id="focusSvg" viewBox="0 0 660 380" role="img" aria-label="Selected caller graph"></svg><h2 style="margin-top:14px">Evidence / 진단</h2><div id="details" class="details">왼쪽에서 관계를 선택하면 해당 호출의 근거를 보여줍니다.</div></section></main>
<script>
const data=__DATA__;
let currentTab='edges';
const nodes=Object.fromEntries(data.nodes.map(x=>[x.id,x]));
const pretty=x=>nodes[x]?.canonical_name||x||'-';
const byId=x=>document.getElementById(x);
byId('repo').textContent=data.repository;
byId('metrics').innerHTML=[['Symbols',data.node_count],['Call sites',data.total_call_sites],['Resolved',data.resolved_call_sites],['Unresolved',data.unresolved_call_sites],['Conditional',data.conditional_calls?.length||0],['Edges',data.edge_count],['Known non-internal',data.status_summary?.statuses?.KNOWN_NON_INTERNAL??'-'],['Uncertain',data.status_summary?.statuses?.UNKNOWN??'-']].map(([k,v])=>`<div class="metric"><b>${v}</b><span>${k}</span></div>`).join('');
const statusCounts=data.status_summary?.statuses||{};
for(const [k,n] of Object.entries(statusCounts)){
  const b=document.createElement('button');b.type='button';
  b.textContent=k+' · '+n; b.title='호출 상태 필터: '+k;
  b.onclick=()=>{byId('status').value=k;tab('status')};
  byId('statusBars').append(b);
}
const classification=data.classification_summary||{categories:{},category_labels:{}};
for(const [k,n] of Object.entries(classification.categories||{})){
  if(!n)continue;
  const option=elemForCategoryOption(k,classification.category_labels?.[k]||k,n);
  byId('category').append(option);
  const b=document.createElement('button');b.type='button';
  b.textContent=(classification.category_labels?.[k]||k)+' · '+n;
  b.title=k+' - 해당 미해결 호출 필터';
  b.onclick=()=>{byId('category').value=k;tab('unresolved')};
  byId('classBars').append(b);
}
function elemForCategoryOption(k,label,n){const e=document.createElement('option');e.value=k;e.textContent=label+' ('+n+')';return e}
function tab(t){currentTab=t;byId('status').style.display=t==='status'?'block':'none';byId('tStatus').classList.toggle('active',t==='status');byId('category').style.display=t==='unresolved'?'block':'none';byId('type').style.display=t==='edges'?'block':'none';byId('tResolved').classList.toggle('active',t==='edges');byId('tUnresolved').classList.toggle('active',t==='unresolved');byId('tConditional').classList.toggle('active',t==='conditional');renderList()}
function elem(tag,cls,content){const e=document.createElement(tag);if(cls)e.className=cls;if(content!=null)e.textContent=content;return e}
function renderList(){const q=byId('q').value.toLowerCase(),t=byId('type').value,root=byId('list');root.replaceChildren();const rows=currentTab==='edges'?data.edges:currentTab==='conditional'?(data.conditional_calls||[]):currentTab==='status'?(data.call_statuses||[]):data.unresolved_calls;
let count=0;for(const item of rows){if(currentTab==='edges'&&t&&item.type!==t)continue;
if(currentTab==='unresolved'&&byId('category').value&&item.category!==byId('category').value)continue;
if(currentTab==='status'&&byId('status').value&&item.status!==byId('status').value)continue;
let label=currentTab==='edges'?`${pretty(item.source)} → ${pretty(item.target)}`:`${pretty(item.caller_id)} → ${item.callee_text}`;
if(currentTab==='conditional')label+=' ⇢ '+(item.target_ids||[]).map(pretty).join(', ');
let full=(label+' '+JSON.stringify(item.evidence||{})+' '+(item.reason||'')+' '+(item.category||'')+' '+(item.status||'')+' '+(item.certainty||'')+' '+(item.classifier_rule||'')).toLowerCase();if(!full.includes(q))continue;
const button=elem('button','row');button.type='button';button.append(elem('span','type',currentTab==='edges'?item.type:currentTab==='conditional'?'MAY-UNBOUND':currentTab==='status'?item.status:'UNRESOLVED'),document.createTextNode(label),document.createElement('br'),elem('small','',currentTab==='edges'?`${item.evidence?.rule||''} · ${item.evidence?.file||''}:${item.evidence?.line||''}`:currentTab==='status'?`[${item.origin_kind}] ${item.certainty} · ${item.file}:${item.line}`:`[${item.category||'미분류'}] ${item.reason} · ${item.file}:${item.line}`));
button.onclick=()=>select(item);root.append(button);count++;if(count>=1500)break}
if(!count)root.append(elem('p','',"일치하는 결과가 없습니다."))}
function svgEl(tag,attr={},text=''){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attr))e.setAttribute(k,String(v));if(text)e.textContent=text;return e}
function select(item){byId('details').textContent=JSON.stringify(item,null,2);if(item.source){drawFocus(item.source,item.target)}else{byId('focusTitle').textContent='미해결 호출: '+item.callee_text;byId('focusSvg').replaceChildren()}}
function drawFocus(src,selected){const svg=byId('focusSvg');svg.replaceChildren();const all=data.edges.filter(e=>e.source===src&&(e.type==='CALLS'||e.type==='INSTANTIATES')).slice(0,20);
byId('focusTitle').textContent=pretty(src)+(data.edges.filter(e=>e.source===src).length>20?' (최대 20개 표시)':'');const cx=330,cy=190,r=155;
svg.append(svgEl('circle',{cx,cy,r:47,fill:'#1f6feb',stroke:'#58a6ff','stroke-width':2}));
const center=svgEl('text',{x:cx,y:cy,fill:'#fff','font-size':12,'text-anchor':'middle'},pretty(src).split('.').slice(-2).join('.').slice(0,17));svg.append(center);
all.forEach((edge,i)=>{let a=i*2*Math.PI/Math.max(1,all.length)-Math.PI/2,x=cx+r*Math.cos(a),y=cy+r*Math.sin(a);
svg.append(svgEl('line',{x1:cx,y1:cy,x2:x,y2:y,stroke:'#8b949e','stroke-width':1.5}));const circ=svgEl('circle',{cx:x,cy:y,r:35,fill:edge.target===selected?'#238636':'#30363d',stroke:'#8b949e'});svg.append(circ);const label=pretty(edge.target).split('.').slice(-2).join('.');const text=svgEl('text',{x,y,fill:'#fff','font-size':11,'text-anchor':'middle'},label.length>14?label.slice(0,12)+'…':label);svg.append(text);
const tip=svgEl('title',{},pretty(edge.target)+' | '+edge.type);circ.append(tip);circ.style.cursor='pointer';circ.addEventListener('click',()=>{byId('details').textContent=JSON.stringify(edge,null,2)})})}
byId('status').style.display='none';byId('category').style.display='none';renderList();const first=data.edges.find(e=>e.type==='CALLS');if(first)select(first);
</script></body></html>'''


def write_html(graph: dict, dest: str | Path) -> None:
    payload = json.dumps(graph, ensure_ascii=False).replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')
    html = TEMPLATE.replace('__DATA__', payload)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding='utf-8')
