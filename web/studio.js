'use strict';
const $=id=>document.getElementById(id), el=(tag,txt)=>Object.assign(document.createElement(tag),{textContent:txt??''});
const btn=(text,fn)=>{let b=el('button',text);b.onclick=()=>run(fn);return b};
let key='',pages=[],fonts=[],n=0,id='',view='original',zoom=1,move=false,drawFrame=false,maskEdit=false,maskPreviewOn=false,hasMaskPreview=false,prevView='clean',erase=false,revision=0,busy=false,jsonPath='';
let ocrLang=sessionStorage.getItem('studio-ocr-lang')||'ko+en';
let ocrEngine=sessionStorage.getItem('studio-ocr-engine')||'rapid';
const page=()=>pages[n], bubble=()=>page()?.bubbles.find(b=>b.id===id), layer=()=>bubble()?.text_layer||{};
const bubbleIndex=b=>(page()?.bubbles||[]).findIndex(x=>x.id===b?.id);
const bubbleCode=b=>{let i=bubbleIndex(b);return i>=0?'B'+String(i+1).padStart(2,'0'):(b?.id||'')};
const rgb=color=>'#'+(color||[0,0,0]).map(x=>Number(x).toString(16).padStart(2,'0')).join('');
const triplet=s=>[1,3,5].map(i=>parseInt(s.slice(i,i+2),16));
function info(txt,revealPath=''){
 let box=$('notice');box.replaceChildren();
 if(!txt)return;
 let s=el('span',txt);s.title=txt;box.append(s);
 if(revealPath){
  let openBtn=btn('📂 เปิดโฟลเดอร์',()=>api('reveal',{path:revealPath}));
  openBtn.className='mini btn-reveal';
  let xBtn=btn('×',()=>info(''));xBtn.className='toast-close';xBtn.title='ปิดแจ้งเตือน';
  box.append(openBtn,xBtn)
 }
}
async function run(fn){try{await fn()}catch(e){info(e.message)}}
async function api(path,data){
 let opts=data===undefined?{}:{method:'POST',headers:{'x-studio':'editor'},body:data};
 if(data!==undefined && !(data instanceof FormData)){opts.body=JSON.stringify(data);opts.headers['Content-Type']='application/json'}
 let r=await fetch('/api/'+path,opts);if(!r.ok){let t=await r.text();try{let j=JSON.parse(t);t=j.line?`บรรทัด ${j.line}: ${j.detail}`:j.detail}catch{}throw Error(t)}return r.json()
}
function modal(title){$('modal').replaceChildren(el('h2',title));if(!$('dialog').open)$('dialog').showModal();return $('modal')}
$('close').onclick=()=>$('dialog').close();
let pending=null, timer, chain=Promise.resolve();
function queueEdit(values){
 if(!bubble())return;
 if(!pending)pending={key,n,id,data:{}};
 Object.assign(pending.data,values);clearTimeout(timer);timer=setTimeout(()=>run(flush),320)
}
async function flush(){
 clearTimeout(timer);
 if(pending){
  const p=pending;pending=null;
  chain=chain.catch(()=>{}).then(async()=>{
   const res=await api(p.key+'/page/'+p.n+'/edit',{...p.data,id:p.id});
   if(key===p.key && !pending){pages[p.n]=res.page;if(n===p.n){sidebar();draw()}}
  })
 }
 await chain
}
function statusOf(b){
 if((b.score??1)<0.9)return {cls:'st-low',label:'OCR '+Math.round((b.score||0)*100)+'%'};
 if(!(b.translation||'').trim())return {cls:'st-untranslated',label:'รอแปล'};
 return {cls:'st-ready',label:'แปลแล้ว'};
}
function isTextTransform(b){
 return !maskEdit && view==='thai' && !!(b&&b.translation&&b.translation.trim())
}
async function project(k,title){
 await flush();
 if(maskEdit)await cancelMaskEdit(true);
 let st=await api(k+'/state');key=k;localStorage.setItem('studio-project',k);pages=st.pages;fonts=st.fonts;jsonPath=st.json_path||'';
 $('projectName').textContent=title;n=0;id=page()?.bubbles[0]?.id||'';drawFrame=false;maskEdit=false;fit();refresh()
}
async function projectList(){
 let m=modal('เปิดโปรเจกต์');
 let openJsonBtn=btn('📂 เปิดไฟล์โปรเจกต์ .json',async()=>{
  const res=await api('open-json',{});
  if(res&&!res.cancelled){$('dialog').close();await project(res.key,res.name);info('เปิดโปรเจกต์ JSON แล้ว: '+res.path,res.path)}
 });
 openJsonBtn.className='primary';
 m.append(openJsonBtn,btn('นำเข้าภาพ / .mtp / .json',()=>{$('dialog').close();$('files').click()}));
 for(const [k,title] of await api('projects'))m.append(btn(title,async()=>{$('dialog').close();await project(k,title)}))
}
async function saveProjectJson(saveAs=false){
 if(!key){info('เปิดโปรเจกต์ก่อนบันทึก JSON');return}
 await flush();
 const res=await api(key+'/save-json',{save_as:!!saveAs});
 if(!res||res.cancelled)return;
 jsonPath=res.path||'';
 info(res.note,res.path)
}
$('open').onclick=()=>run(projectList);$('create').onclick=$('pick').onclick=()=>$('files').click();
$('files').onchange=()=>run(async()=>{if(!$('files').files.length)return;await flush();let form=new FormData();for(let f of $('files').files)form.append('files',f);info('กำลังเปิดโปรเจกต์…');let k=await api('projects',form);await project(k,$('files').files[0].name);$('files').value='';info('')});
async function selectPage(i){
 await flush();
 if(maskEdit)await cancelMaskEdit(true);
 n=i;id=page().bubbles[0]?.id||'';drawFrame=false;fit();refresh()
}
async function selectBubble(i){
 await flush();
 if(maskEdit && i!==id)await cancelMaskEdit(true);
 id=i;sidebar();inspect();bars();draw()
}
function sidebar(){
 $('pages').replaceChildren();pages.forEach((p,i)=>{let b=btn(String(i+1).padStart(2,'0')+'  '+p.name,()=>selectPage(i));let dot=el('i');dot.className='dot '+(p.bubbles.length&&p.bubbles.every(v=>v.translation)?'ready':p.clean?'cleaned':p.bubbles.length?'ocr':'');b.prepend(dot);b.classList.toggle('active',i===n);$('pages').append(b)});
 $('layers').replaceChildren();page()?.bubbles.forEach((b,i)=>{
  let line=el('div');line.className='layer';let l=b.text_layer||{};
  let st=statusOf(b), code='B'+String(i+1).padStart(2,'0');
  let txt=b.translation||b.text||'…';
  let hasFx=!!(l.stroke?.enabled||l.gradient?.enabled);
  let c=btn('',()=>selectBubble(b.id));
  let dot=el('i');dot.className='dot '+(id===b.id?'st-selected':st.cls);
  let tagId=el('span',code);tagId.className='bid-tag';
  let tagTxt=el('span',txt);tagTxt.className='layer-txt';
  c.append(dot,tagId,tagTxt);
  if(hasFx){let fx=el('span','fx');fx.className='fx-tag';c.append(fx)}
  c.title=code+'  '+txt+' · '+(b.frame_type||'normal')+' ('+st.label+')';
  c.classList.toggle('active',id===b.id);
  let eye=btn(l.hidden?'○':'◉',async()=>{await selectBubble(b.id);queueEdit({hidden:!l.hidden});await flush()});eye.className='eye';eye.title=l.hidden?'แสดงข้อความ':'ซ่อนข้อความ';
  let del=btn('🗑',()=>deleteBubbleById(b.id));del.className='del';del.title='ลบกรอบนี้ (Del)';
  line.append(c,eye,del);$('layers').append(line)
 })
}
function refresh(){sidebar();inspect();bars();image();draw();$('empty').hidden=!!page()}
function activeImageView(){
 if(maskEdit&&bubble())return (maskPreviewOn&&hasMaskPreview)?'mask_preview':'mask_draft';
 return view
}
function image(){
 if(!page())return;
 let v=activeImageView();
 let q='?view='+v+'&v='+revision+(maskEdit&&id?'&bid='+encodeURIComponent(id):'');
 $('art').src='/api/'+key+'/image/'+n+q
}
function fit(){if(page()){let vp=$('viewport');zoom=Math.min(1,(vp.clientWidth-28)/page().width,(vp.clientHeight-88)/page().height);zoom=Math.max(.025,zoom)}}
function scale(value){zoom=Math.max(.025,Math.min(5,value));draw()}
function bars(){
 $('modes').replaceChildren();
 [['1 OCR','1','ocrOptions','ตั้งค่าภาษา/โมเดลแล้วรัน OCR ทุกหน้า'],['2 Mask','2','batchMask','สร้าง Mask ทุกหน้า'],['3 Clean','3','batchClean','รัน Clean (LaMa/Auto) ทุกหน้า']].forEach(([label,k,cmd,tip])=>{
  let b=btn('',commands[cmd][1]);b.append(document.createTextNode(label+' '),el('kbd',k));b.title=tip;$('modes').append(b)
 });
 $('views').replaceChildren();
 for(let [v,label] of Object.entries({original:'ต้นฉบับ',clean:'คลีน',thai:'Text'})){
  let b=btn(label,()=>{if(maskEdit){maskPreviewOn=false}view=v;image();draw();bars()});
  b.classList.toggle('active',!maskEdit&&view===v);$('views').append(b)
 }
 $('cleanPanel').hidden=!maskEdit;
 if(maskEdit){
  $('maskTarget').textContent=bubbleCode(bubble());
  $('maskToggle').hidden=!hasMaskPreview;
  $('maskToggle').textContent=maskPreviewOn?'สลับไปดู Mask (แก้เพิ่ม)':'สลับไปดู Preview';
 }
 document.querySelector('[data-cmd=move]')?.classList.toggle('active',move);
 document.querySelectorAll('[data-cmd=addFrame]').forEach(x=>x.classList.toggle('active',drawFrame));
 document.querySelector('[data-cmd=maskEdit]')?.classList.toggle('active',maskEdit);
 $('viewport').classList.toggle('crosshair',drawFrame||maskEdit);
 $('eraser').classList.toggle('active',erase);$('brush').classList.toggle('active',!erase)
}
function bounds(b){
 const l=b.text_layer||{},box=l.box||b.box;
 if(l.editor_box)return [...l.editor_box];
 if(!l.text_size)return [...box];
 const [w,h]=l.text_size,cx=(box[0]+box[2])/2,cy=(box[1]+box[3])/2;
 return [cx-w/2,cy-h/2,cx+w/2,cy+h/2]
}
const loadedFonts=new Map();
function family(font){
 if(!font)return 'sans-serif';
 if(!loadedFonts.has(font)){
  let name='studio'+loadedFonts.size;loadedFonts.set(font,name);
  let f=new FontFace(name,'url("/api/font/'+encodeURIComponent(font)+'")');
  f.load().then(face=>document.fonts.add(face)).catch(()=>{})
 }
 return loadedFonts.get(font)
}
function draw(){
 const p=page();if(!p)return;let st=$('stage'),w=p.width*zoom,h=p.height*zoom;
 st.style.width=w+'px';st.style.height=h+'px';st.style.marginTop=Math.max(0,($('viewport').clientHeight-h)/2)+'px';$('percent').textContent=Math.round(zoom*100)+'%';
 let ov=$('overlay');ov.replaceChildren();
 const showText=!maskEdit&&view==='thai';
 p.bubbles.forEach((b,idx)=>{
  let l=b.text_layer||{};
  let textMode=isTextTransform(b);
  let rect=textMode?bounds(b):b.box,[x,y,r,bt]=rect;
  let div=el('div'),label=el('span',textMode&&!l.hidden?(l.text||b.translation):'');
  let stInfo=statusOf(b);
  div.className='bubble '+stInfo.cls+(showText?' view-thai':'');
  if(maskEdit)div.classList.add(b.id===id?'mask-active':'mask-dim');
  div.dataset.id=b.id;
  Object.assign(div.style,{
   left:x*zoom+'px',top:y*zoom+'px',width:Math.max(2,(r-x)*zoom)+'px',height:Math.max(2,(bt-y)*zoom)+'px',
   fontSize:(l.font_size||24)*zoom+'px',fontFamily:family(b.font_choice),textAlign:l.alignment||'center',
   color:rgb(l.color),transform:'rotate('+(textMode?l.rotation||0:0)+'deg)'
  });
  let s=l.stroke,g=l.gradient;
  if(textMode&&s?.enabled){label.style.webkitTextStroke=2*s.size*zoom+'px '+rgb(s.color);label.style.paintOrder='stroke fill'}
  if(textMode&&g?.enabled){label.style.backgroundImage='linear-gradient('+(g.angle===90?'to top':'to right')+','+rgb(g.start_color)+','+rgb(g.end_color)+')';label.style.backgroundClip='text';label.style.webkitTextFillColor='transparent'}
  let badge=el('small','B'+String(idx+1).padStart(2,'0')+' · '+(b.id===id?'เลือกอยู่':stInfo.label));
  badge.className='badge';
  div.append(label,badge);ov.append(div);
  if(b.id===id){
   div.classList.add('selected');
   if(move&&!maskEdit){
    for(const [handle,hx,hy] of [['nw',0,0],['n',50,0],['ne',100,0],['e',100,50],['se',100,100],['s',50,100],['sw',0,100],['w',0,50]]){
     let a=el('i');a.className='handle';a.dataset.handle=handle;Object.assign(a.style,{left:hx+'%',top:hy+'%',cursor:handle+'-resize'});div.append(a)}
    let hudTxt=textMode?(Math.round(x)+', '+Math.round(y)+' · '+Math.round(l.font_size||24)+' px'):(Math.round(x)+', '+Math.round(y)+' · '+Math.round(r-x)+'×'+Math.round(bt-y));
    let hud=el('small',hudTxt);hud.className='hud';div.append(hud)
   }
  }
 })
}
function fillSelect(node,values,v){node.replaceChildren(...values.map(x=>new Option(x,x)));node.value=v}
function inspect(){
 let host=$('inspector'),b=bubble();host.replaceChildren();if(!b){host.append(el('p','เลือกกรอบบนภาพหรือในรายการ Layers'));return}
 host.append($('fields').content.cloneNode(true));let l=b.text_layer||{},s=l.stroke||{},g=l.gradient||{};
 $('bid').textContent=bubbleCode(b);$('bid').title=b.id;
 $('frameBadge').textContent=b.frame_type||'normal';
 let pct=Math.round((b.score??1)*100);
 $('score').textContent='OCR '+pct+'%';
 $('score').className=pct<90?'score-low':'score-ok';
 fillSelect($('frame'),['normal','shout','thought','system','narration','floating','happy','phone','terrible'],b.frame_type||'normal');
 fillSelect($('font'),fonts,b.font_choice);$('source').value=b.text;$('thai').value=b.translation;$('ink').value=rgb(l.color);$('rotation').value=l.rotation||0;$('align').value=l.alignment||'center';$('size').value=l.font_size||24;$('sizeValue').value=$('size').value;
 $('stroke').checked=!!s.enabled;$('edge').value=rgb(s.color);$('width').value=s.size||3;$('gradient').checked=!!g.enabled;$('start').value=rgb(g.start_color||[255,255,255]);$('end').value=rgb(g.end_color||[200,200,200]);$('angle').value=g.angle??90;
 $('thai').oninput=()=>{b.translation=$('thai').value;l.text=b.translation;queueEdit({translation:b.translation});sidebar();draw()};
 $('source').oninput=()=>{b.text=$('source').value;queueEdit({text:b.text});sidebar()};
 $('font').onchange=()=>{b.font_choice=$('font').value;queueEdit({font_choice:b.font_choice});draw()};
 $('frame').onchange=()=>{b.frame_type=$('frame').value;$('frameBadge').textContent=b.frame_type;queueEdit({frame_type:b.frame_type})};
 function edit(k,v){l[k]=v;queueEdit({[k]:v});draw()}
 $('align').onchange=()=>edit('alignment',$('align').value);$('ink').oninput=()=>edit('color',triplet($('ink').value));$('rotation').onchange=()=>edit('rotation',+$('rotation').value);
 $('size').oninput=()=>{let v=+$('size').value;$('sizeValue').value=v;edit('font_size',v)};
 for(const k of ['stroke','edge','width','gradient','start','end','angle'])$(k).oninput=()=>{
  let style={stroke:{enabled:$('stroke').checked,color:triplet($('edge').value),size:Math.max(1,Math.min(8,+$('width').value))},gradient:{enabled:$('gradient').checked,start_color:triplet($('start').value),end_color:triplet($('end').value),angle:+$('angle').value}};
  Object.assign(l,style);queueEdit({style});draw()
 }
}
async function withBusy(fn,msg='กำลังประมวลผล…'){
 if(!page()||busy)return;
 await flush();busy=true;
 document.querySelector('main').inert=true;document.querySelector('header').inert=true;
 info(msg);
 try{return await fn()}finally{busy=false;document.querySelector('main').inert=false;document.querySelector('header').inert=false}
}
async function command(action,all=false,extra={}){
 return withBusy(async()=>{
  const result=await api(key+'/page/'+n+'/'+action,{id:all?null:id,...extra});
  pages[n]=result.page;
  if(result.id)id=result.id;
  else if(!bubble())id=page().bubbles[0]?.id||'';
  revision++;refresh();info(result.note||'บันทึกแล้ว');
  return result
 })
}
async function batch(action,extra={},targetView=null){
 return withBusy(async()=>{
  if(maskEdit)await cancelMaskEdit(true);
  const result=await api(key+'/batch/'+action,extra);
  pages=result.pages;
  if(!bubble())id=page()?.bubbles[0]?.id||'';
  if(targetView)view=targetView;
  revision++;refresh();info(result.note||'เสร็จสิ้น');
  return result
 })
}
async function deleteBubbleById(targetId){
 if(!page()||!targetId)return;
 if(maskEdit&&targetId===id)await cancelMaskEdit(true);
 await command('delete_bubble',false,{id:targetId})
}
async function startMaskEdit(){
 if(!bubble()){info('เลือกกรอบข้อความก่อนแก้ Mask');return}
 await withBusy(async()=>{
  prevView=view;move=false;drawFrame=false;maskEdit=true;maskPreviewOn=false;hasMaskPreview=false;
  const res=await api(key+'/page/'+n+'/mask_start',{id});
  revision++;refresh();info(res.note)
 },'กำลังเปิดโหมดแก้ Mask…')
}
async function cancelMaskEdit(silent=false){
 if(!maskEdit)return;
 const bid=id;
 maskEdit=false;maskPreviewOn=false;hasMaskPreview=false;view=prevView||view;
 try{if(key&&bid)await api(key+'/page/'+n+'/mask_cancel',{id:bid})}catch{}
 revision++;refresh();if(!silent)info('ยกเลิกการแก้ Mask แล้ว')
}
async function applyMaskEdit(){
 if(!maskEdit||!bubble())return;
 await withBusy(async()=>{
  const res=await api(key+'/page/'+n+'/mask_apply',{id});
  pages[n]=res.page;maskEdit=false;maskPreviewOn=false;hasMaskPreview=false;view='clean';
  revision++;refresh();info(res.note)
 },'กำลังบันทึกผล Clean…')
}
async function previewMaskEdit(){
 if(!maskEdit||!bubble())return;
 await withBusy(async()=>{
  const res=await api(key+'/page/'+n+'/mask_preview',{id});
  hasMaskPreview=!!res.has_preview;maskPreviewOn=hasMaskPreview;
  revision++;bars();image();draw();info(res.note)
 },'กำลังประมวลผล Preview…')
}
async function undoMaskEdit(){
 if(!maskEdit||!bubble())return;
 await withBusy(async()=>{
  const res=await api(key+'/page/'+n+'/mask_undo',{id});
  hasMaskPreview=false;maskPreviewOn=false;
  revision++;bars();image();draw();info(res.note)
 },'กำลังย้อน Mask…')
}
function reflow(b,w,h){
 const l=b.text_layer,ctx=document.createElement('canvas').getContext('2d');
 ctx.font=l.font_size+'px '+family(b.font_choice);
 const segment=new Intl.Segmenter('th',{granularity:'word'});
 function lines(max){
  let out=[];for(let para of b.translation.split('\n')){let line='';for(let {segment:word} of segment.segment(para)){
   if(line && ctx.measureText(line+word).width>max){out.push(line.trimEnd());line=''}line+=word
  }out.push(line.trimEnd())}return out
 }
 let result=lines(w),limit=Math.max(1,Math.floor(h/(1.25*l.font_size)));
 if(result.length<limit){let candidate=lines(Math.max(w*.5,w*result.length/limit));if(candidate.length<=limit)result=candidate}
 return result.join('\n')
}
let drag=null;
const point=e=>{let r=$('stage').getBoundingClientRect();return [(e.clientX-r.left)/zoom,(e.clientY-r.top)/zoom]};
$('viewport').onpointerdown=e=>{
 if(busy||!page())return;
 let target=e.target.closest('.bubble'),p=point(e);
 if(drawFrame){
  drag={kind:'draw',start:p,end:p};
  let c=$('paint');c.width=page().width;c.height=page().height;
  $('viewport').setPointerCapture(e.pointerId);return
 }
 if(maskEdit&&bubble()){
  if(maskPreviewOn){maskPreviewOn=false;image();bars()}
  drag={kind:'maskPaint',points:[p]};
  let c=$('paint');c.width=page().width;c.height=page().height;
  $('viewport').setPointerCapture(e.pointerId);return
 }
 if(target){
  if(target.dataset.id!==id){run(()=>selectBubble(target.dataset.id));return}
  if(!move)return;
  let b=bubble();
  if(isTextTransform(b)){
   drag={kind:'moveText',handle:e.target.dataset.handle||'',p,rect:bounds(b),size:layer().font_size||24}
  }else{
   drag={kind:'moveBox',handle:e.target.dataset.handle||'',p,rect:[...b.box]}
  }
 }else drag={kind:'pan',p:[e.clientX,e.clientY],sx:$('viewport').scrollLeft,sy:$('viewport').scrollTop};
 $('viewport').setPointerCapture(e.pointerId)
};
$('viewport').onpointermove=e=>{
 if(!drag)return;let p=point(e);
 if(drag.kind==='draw'){
  drag.end=p;let c=$('paint'),ctx=c.getContext('2d');
  ctx.clearRect(0,0,c.width,c.height);
  let x=Math.min(drag.start[0],p[0]),y=Math.min(drag.start[1],p[1]),w=Math.abs(p[0]-drag.start[0]),h=Math.abs(p[1]-drag.start[1]);
  ctx.fillStyle='rgba(59,130,246,0.16)';ctx.fillRect(x,y,w,h);
  ctx.strokeStyle='#3B82F6';ctx.lineWidth=Math.max(2,2/zoom);ctx.setLineDash([8,4]);ctx.strokeRect(x,y,w,h);
  return
 }
 if(drag.kind==='maskPaint'){
  let prev=drag.points.at(-1);drag.points.push(p);let ctx=$('paint').getContext('2d');
  ctx.strokeStyle=erase?'#17181c':'#8b5cf6';ctx.lineWidth=2*+$('radius').value;ctx.lineCap='round';
  ctx.beginPath();ctx.moveTo(...prev);ctx.lineTo(...p);ctx.stroke();return
 }
 if(drag.kind==='pan'){let v=$('viewport');v.scrollLeft=drag.sx+drag.p[0]-e.clientX;v.scrollTop=drag.sy+drag.p[1]-e.clientY;return}
 const dx=p[0]-drag.p[0],dy=p[1]-drag.p[1],k=drag.handle;
 let [x,y,r,bt]=drag.rect;
 if(drag.kind==='moveBox'){
  if(!k){x+=dx;r+=dx;y+=dy;bt+=dy}else{
   if(k.includes('w'))x=Math.min(r-10,x+dx);
   if(k.includes('e'))r=Math.max(x+10,r+dx);
   if(k.includes('n'))y=Math.min(bt-10,y+dy);
   if(k.includes('s'))bt=Math.max(y+10,bt+dy);
  }
  const W=page().width,H=page().height,bw=r-x,bh=bt-y;
  if(!k){x=Math.max(0,Math.min(W-bw,x));y=Math.max(0,Math.min(H-bh,y));r=x+bw;bt=y+bh}
  else{x=Math.max(0,x);y=Math.max(0,y);r=Math.min(W,r);bt=Math.min(H,bt)}
  bubble().box=[Math.round(x),Math.round(y),Math.round(r),Math.round(bt)];
  draw();return
 }
 if(!k){x+=dx;r+=dx;y+=dy;bt+=dy}else{
  const theta=(layer().rotation||0)*Math.PI/180,c=Math.cos(theta),s=Math.sin(theta);
  const hx=k.includes('w')?-1:k.includes('e')?1:0,hy=k.includes('n')?-1:k.includes('s')?1:0;
  const w=r-x,h=bt-y,lx=dx*c+dy*s,ly=-dx*s+dy*c;
  let nw=Math.max(10,w+lx*hx),nh=Math.max(10,h+ly*hy);
  if(k.length===2){
   const factor=Math.max(6/drag.size,Math.min(220/drag.size,(nw*w+nh*h)/(w*w+h*h)));
   nw=w*factor;nh=h*factor;layer().font_size=Math.round(drag.size*factor);
   $('size').value=$('sizeValue').value=layer().font_size
  }
  const shiftX=(nw-w)*hx/2,shiftY=(nh-h)*hy/2;
  const cx=(x+r)/2+shiftX*c-shiftY*s,cy=(y+bt)/2+shiftX*s+shiftY*c;
  x=cx-nw/2;r=cx+nw/2;y=cy-nh/2;bt=cy+nh/2
 }
 let rect=[x,y,r,bt];layer().editor_box=rect;if(k.length===1)layer().text=reflow(bubble(),r-x,bt-y);draw()
};
$('viewport').onpointerup=e=>{
 if(!drag)return;let d=drag;drag=null;
 if(d.kind==='draw'){
  $('paint').width=1;
  let box=[Math.min(d.start[0],d.end[0]),Math.min(d.start[1],d.end[1]),Math.max(d.start[0],d.end[0]),Math.max(d.start[1],d.end[1])];
  if(box[2]-box[0]>=12&&box[3]-box[1]>=12){
   drawFrame=false;
   run(async()=>{
    const res=await command('add_bubble',false,{box});
    if(res&&res.id){move=true;bars();draw()}
   })
  }else info('ลากกรอบให้กว้างและสูงอย่างน้อย 12 px')
  return
 }
 if(d.kind==='maskPaint'){
  run(async()=>{
   try{
    const res=await withBusy(()=>api(key+'/page/'+n+'/mask_paint',{id,points:d.points,radius:+$('radius').value,erase}),'กำลังอัปเดต Mask…');
    if(res){hasMaskPreview=false;maskPreviewOn=false;revision++;bars();image();draw();info(res.note)}
   }finally{$('paint').width=1}
  });
  return
 }
 if(d.kind==='moveBox'){queueEdit({bubble_box:bubble().box});run(flush);return}
 if(d.kind==='moveText'){queueEdit({box:bounds(bubble()),font_size:layer().font_size,wrapped:layer().text});run(flush)}
};
$('viewport').onpointercancel=()=>{drag=null;$('paint').width=1;run(async()=>{if(key){pages=(await api(key+'/state')).pages;refresh()}})};
$('viewport').onwheel=e=>{if(e.ctrlKey){e.preventDefault();scale(zoom*(e.deltaY>0?.9:1.1))}};
function side(which){let node=$(which);node.hidden=!node.hidden;document.querySelector('main').style.setProperty('--'+which,node.hidden?'0px':which==='left'?'248px':'320px')}
const commands={
 ocrOptions:['1 OCR ทุกหน้า (เลือกภาษาและโมเดล)',openOcrModal,'1'],
 batchMask:['2 Mask ทุกหน้า',()=>batch('mask',{},'original'),'2'],
 batchClean:['3 Clean ทุกหน้า',()=>batch('clean',{},'clean'),'3'],
 move:['Move / Transform (ย้าย/ย่อขยาย 8 มุม)',async()=>{if(maskEdit)await cancelMaskEdit(true);drawFrame=false;move=!move;if(move&&bubble()?.translation?.trim()){view='thai';image()}bars();draw()},'V'],
 addFrame:['สร้างกรอบบอลลูนใหม่',async()=>{if(!page())return;if(maskEdit)await cancelMaskEdit(true);move=false;drawFrame=!drawFrame;bars();draw();info(drawFrame?'ลากบนภาพเพื่อวาดกรอบใหม่ (กด Esc เพื่อยกเลิก)':'')},'N'],
 deleteFrame:['ลบกรอบที่เลือก',()=>{if(!bubble()){info('เลือกกรอบก่อนลบ');return}return deleteBubbleById(id)},'Del'],
 rapidBubble:['RapidOCR ช่องนี้',()=>{if(!bubble()){info('เลือกกรอบข้อความก่อนอ่าน RapidOCR');return}return command('ocr_bubble',false,{lang:ocrLang})},''],
 maskEdit:['แก้ Mask รายช่อง',()=>maskEdit?cancelMaskEdit():startMaskEdit(),'M'],
 maskUndo:['ย้อน Mask',undoMaskEdit,''],
 maskPreview:['Preview Clean รายช่อง',previewMaskEdit,''],
 maskApply:['บันทึกผล Clean (ตกลง)',applyMaskEdit,''],
 maskCancel:['ยกเลิกแก้ Mask',()=>cancelMaskEdit(),'Esc'],
 txtModal:['ส่งออก / นำเข้าคำแปล TXT',openTxtModal,'T'],
 saveJson:['บันทึกโปรเจกต์ JSON',()=>saveProjectJson(false),'Ctrl+S'],
 saveJsonAs:['บันทึกโปรเจกต์ JSON เป็นไฟล์ใหม่ (Save As)',()=>saveProjectJson(true),'Ctrl+Shift+S'],
 gemini:['Gemini แปลทั้งหน้า',()=>command('translate',true),''],
 polish:['เกลาสำนวนกรอบนี้',()=>command('translate'),''],
 fx:['ดูดสี Auto fx',()=>command('fx'),''],
 reset:['ล้าง fx',()=>command('edit',false,{style:{}}),''],
 fit:['ซูมพอดีหน้า',()=>{fit();draw()},''],
 left:['ซ่อน/แสดงแถบซ้าย',()=>side('left'),'['],
 right:['ซ่อน/แสดง Inspector',()=>side('right'),']'],
 export:['ส่งออก PSD / MTP',exportModal,''],
 open:['เปิดโปรเจกต์',projectList,''],
 create:['สร้างโปรเจกต์',()=>$('files').click(),'']
};
function palette(){
 let m=modal('Command Palette'),input=el('input'),list=el('div');
 input.placeholder='ค้นหาคำสั่งหรือคีย์ลัด…';m.append(input,list);
 function filter(){
  list.replaceChildren();
  const q=input.value.toLowerCase();
  for(let [label,fn,shortcut] of Object.values(commands)){
   if(label.toLowerCase().includes(q)||shortcut.toLowerCase().includes(q)){
    let b=btn('',()=>{$('dialog').close();return fn()});
    b.className='pal-item';
    b.append(el('span',label),shortcut?el('kbd',shortcut):el('span',''));
    list.append(b)
   }
  }
 }
 input.oninput=filter;
 input.onkeydown=e=>{if(e.key==='Enter')list.firstChild?.click();if(e.key==='ArrowDown')list.firstChild?.focus()};
 filter();input.focus()
}
function openOcrModal(){
 if(!key||!pages.length){info('เปิดโปรเจกต์ก่อนเริ่ม OCR');return}
 let m=modal('ตั้งค่า OCR ทุกหน้า');
 m.insertAdjacentHTML('beforeend',
  '<label>ภาษา OCR<select id="ocrLang">'
  +'<option value="ko+en">ko+en — เกาหลี + อังกฤษ (ค่าเริ่มต้น)</option>'
  +'<option value="en">en — อังกฤษ</option>'
  +'<option value="zh+en">zh+en — จีนตัวย่อ</option>'
  +'<option value="zh_tw+en">zh_tw+en — จีนตัวเต็ม</option>'
  +'</select></label>'
  +'<label>โมเดล / เอนจิน OCR<select id="ocrEngine">'
  +'<option value="rapid">RapidOCR (ONNX — เร็ว แนะนำ)</option>'
  +'<option value="paddle">PaddleOCR (ขั้นสูง)</option>'
  +'<option value="easy">EasyOCR (พื้นฐาน)</option>'
  +'</select></label>'
 );
 $('ocrLang').value=ocrLang;
 $('ocrEngine').value=ocrEngine;
 $('ocrLang').onchange=()=>{ocrLang=$('ocrLang').value;sessionStorage.setItem('studio-ocr-lang',ocrLang)};
 $('ocrEngine').onchange=()=>{ocrEngine=$('ocrEngine').value;sessionStorage.setItem('studio-ocr-engine',ocrEngine)};
 if(pages.some(p=>p.bubbles.length)){
  let w=el('div','โปรเจกต์นี้มีกรอบข้อความอยู่แล้ว การเริ่ม OCR ใหม่จะแทนที่กรอบ คำแปล และผลคลีนเดิมทั้งหมด');
  w.className='warn-note';m.append(w)
 }
 let actions=el('div');actions.className='modal-actions';
 let cancelBtn=btn('ยกเลิก',()=>$('dialog').close());
 let startBtn=btn('เริ่ม OCR ทุกหน้า',async()=>{
  ocrLang=$('ocrLang').value;ocrEngine=$('ocrEngine').value;
  sessionStorage.setItem('studio-ocr-lang',ocrLang);
  sessionStorage.setItem('studio-ocr-engine',ocrEngine);
  $('dialog').close();
  await batch('ocr',{replace:true,lang:ocrLang,engine:ocrEngine},'original')
 });
 startBtn.className='primary';startBtn.id='startOcrBtn';
 actions.append(cancelBtn,startBtn);
 m.append(actions)
}
async function openTxtModal(){
 if(!key){info('เปิดโปรเจกต์ก่อนส่งออก TXT');return}
 await flush();
 let m=modal('ส่งออก / นำเข้าคำแปล TXT');
 let bar=el('div');bar.className='txt-bar';
 let fmt=el('select');fmt.id='txtMode';
 fmt.append(new Option('แบบรหัสกรอบ (id_template)','id_template'),new Option('แบบเลขข้อ (numbered)','numbered'),new Option('ต้นฉบับล้วน (plain_lines)','plain_lines'));
 let area=el('textarea');area.id='txtArea';area.className='txt-area';area.placeholder='วางคำแปลภาษาไทย หรือโหลดจากไฟล์ .txt แล้วกดนำเข้า…';
 let status=el('p','');status.id='txtStatus';
 let filename='manhwa_ocr.txt';
 async function loadExport(){
  const res=await api(key+'/txt?mode='+encodeURIComponent(fmt.value));
  area.value=res.text;filename=res.filename||'manhwa_ocr.txt';
  status.textContent='พร้อมคัดลอก บันทึก หรือวางคำแปลเพื่อนำเข้ากลับเข้าโปรเจกต์'
 }
 fmt.onchange=()=>run(loadExport);
 let copyBtn=btn('คัดลอก',async()=>{
  try{await navigator.clipboard.writeText(area.value)}catch{area.select();document.execCommand('copy')}
  status.textContent='คัดลอกข้อความแล้ว'
 });
 let saveBtn=btn('บันทึก _ocr.txt',async()=>{
  saveBtn.disabled=true;
  try{
   const res=await api(key+'/txt/save',{mode:fmt.value,text:area.value});
   if(res.cancelled){status.textContent='ยกเลิกการบันทึก';return}
   status.replaceChildren(el('span',res.note+' '),Object.assign(btn('📂 เปิดโฟลเดอร์',()=>api('reveal',{path:res.path})),{className:'mini btn-reveal'}));
   info(res.note,res.path)
  }finally{saveBtn.disabled=false}
 });
 let loadBtn=btn('เปิดไฟล์ .txt',()=>$('txtFile').click());
 $('txtFile').onchange=async()=>{
  let f=$('txtFile').files[0];if(!f)return;
  area.value=await f.text();$('txtFile').value='';
  status.textContent='โหลดไฟล์ '+f.name+' แล้ว — กด "นำเข้าคำแปลกลับเข้าโปรเจกต์"'
 };
 let importBtn=btn('นำเข้าคำแปลกลับเข้าโปรเจกต์',async()=>{
  importBtn.disabled=true;
  try{
   const res=await api(key+'/txt/import',{text:area.value});
   pages=res.pages;if(!bubble())id=page()?.bubbles[0]?.id||'';
   view='thai';revision++;refresh();
   status.textContent=res.note;info(res.note);
   $('dialog').close()
  }catch(err){status.textContent='นำเข้าไม่สำเร็จ: '+err.message}
  finally{importBtn.disabled=false}
 });
 importBtn.className='primary';importBtn.id='txtImportBtn';
 bar.append(fmt,copyBtn,saveBtn,loadBtn);
 m.append(bar,area,importBtn,status);
 await loadExport()
}
async function exportModal(){
 if(!key)return;await flush();
 let m=modal('ส่งออกโปรเจกต์');
 const lastDir=localStorage.getItem('studio-export-dir')||'';
 m.insertAdjacentHTML('beforeend',
  '<label>โฟลเดอร์ปลายทาง</label>'
  +'<div class="export-dir-row"><input id="exportDir" readonly placeholder="โฟลเดอร์โปรเจกต์ (กดเลือกโฟลเดอร์เพื่อเปลี่ยน)"><button type="button" id="pickExportDir">📂 เลือกโฟลเดอร์…</button></div>'
  +'<label>Backend<select id="backend"><option value="photopea">Photopea Headless</option><option value="photoshop">Adobe Photoshop</option></select></label>'
  +'<label>รูปแบบ<select id="format"><option value="psd">PSD</option><option value="mtp">MTP</option><option value="all">PSD + MTP</option></select></label>'
  +'<label><input type="checkbox" id="fxOn" checked> แนบ Live Layer Style</label>'
  +'<label><input type="checkbox" id="drive"> อัปโหลด Google Drive</label>'
  +'<progress max="100" value="0"></progress><div id="exportResult"></div>'
 );
 $('exportDir').value=lastDir;
 $('pickExportDir').onclick=()=>run(async()=>{
  const res=await api('pick-folder',{title:'เลือกโฟลเดอร์ปลายทางสำหรับ Export PSD / MTP',initial_dir:$('exportDir').value});
  if(res&&!res.cancelled&&res.path){$('exportDir').value=res.path;localStorage.setItem('studio-export-dir',res.path)}
 });
 let bar=m.querySelector('progress'),outBox=$('exportResult');
 let submit=btn('ส่งออก',async()=>{
  submit.disabled=true;outBox.replaceChildren();
  try{
   const dest=$('exportDir').value.trim();
   const j=await api(key+'/export',{backend:$('backend').value,format:$('format').value,fx:$('fxOn').checked,drive:$('drive').checked,destination:dest});
   while(true){
    let r=await api('jobs/'+j+'/progress');bar.value=r.value;
    if(r.done){
     for(let f of r.files){let a=el('a',f);a.href='/api/jobs/'+j+'/file/'+encodeURIComponent(f);a.download=f;outBox.append(el('br'),a)}
     if(r.error){outBox.append(el('p',r.error))}
     else{
      const target=(r.paths&&r.paths[0])||r.folder;
      outBox.append(el('p',r.folder));
      let openBtn=btn('📂 เปิดโฟลเดอร์',()=>api('reveal',{path:target}));openBtn.className='btn-reveal';
      outBox.append(openBtn);
      info('ส่งออกสำเร็จ: '+r.folder,target)
     }
     break
    }
    await new Promise(ok=>setTimeout(ok,500))
   }
  }finally{submit.disabled=false}
 });
 submit.className='primary';
 m.append(submit)
}
document.onclick=e=>{
 let c=e.target.closest('[data-cmd]')?.dataset.cmd;
 if(c)run(c==='palette'?palette:commands[c][1]);
 let z=e.target.closest('[data-zoom]');if(z)scale(zoom*+z.dataset.zoom)
};
$('percent').onclick=()=>scale(1);
$('brush').onclick=()=>{erase=false;bars()};
$('eraser').onclick=()=>{erase=true;bars()};
$('radius').oninput=()=>{$('radiusValue').value=$('radius').value};
$('maskToggle').onclick=()=>{if(!hasMaskPreview)return;maskPreviewOn=!maskPreviewOn;image();bars()};
document.onkeydown=e=>{
 if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();palette();return}
 if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();run(()=>saveProjectJson(e.shiftKey));return}
 if($('dialog').open||e.target.closest('input,textarea,select'))return;
 if(e.key==='Escape'){
  if(drawFrame){e.preventDefault();drawFrame=false;bars();draw();info('');return}
  if(maskEdit){e.preventDefault();run(()=>cancelMaskEdit());return}
 }
 if(e.key==='Delete'){e.preventDefault();run(commands.deleteFrame[1]);return}
 let c={'1':'ocrOptions','2':'batchMask','3':'batchClean','v':'move','n':'addFrame','m':'maskEdit','t':'txtModal','[':'left',']':'right'}[e.key.toLowerCase()];
 if(c&&!e.ctrlKey&&!e.metaKey&&!e.altKey){e.preventDefault();run(commands[c][1]);return}
 if(move&&!maskEdit&&bubble()&&e.key.startsWith('Arrow')){
  e.preventDefault();
  let dx=({ArrowLeft:-1,ArrowRight:1}[e.key]||0)*(e.shiftKey?5:1),dy=({ArrowUp:-1,ArrowDown:1}[e.key]||0)*(e.shiftKey?5:1);
  if(isTextTransform(bubble())){
   layer().editor_box=bounds(bubble()).map((v,i)=>v+(i%2?dy:dx));
   queueEdit({box:bounds(bubble())});
  }else{
   let [x0,y0,x1,y1]=bubble().box;
   let W=page().width,H=page().height,bw=x1-x0,bh=y1-y0;
   let nx=Math.max(0,Math.min(W-bw,x0+dx)),ny=Math.max(0,Math.min(H-bh,y0+dy));
   bubble().box=[Math.round(nx),Math.round(ny),Math.round(nx+bw),Math.round(ny+bh)];
   queueEdit({bubble_box:bubble().box});
  }
  draw()
 }
};
refresh();
run(async()=>{let last=localStorage.getItem('studio-project');if(last){let record=(await api('projects')).find(x=>x[0]===last);if(record)await project(...record)}});
