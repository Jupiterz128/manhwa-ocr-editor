'use strict';
const $=id=>document.getElementById(id), el=(tag,txt)=>Object.assign(document.createElement(tag),{textContent:txt??''});
const btn=(text,fn)=>{let b=el('button',text);b.onclick=()=>run(fn);return b};
let key='',pages=[],fonts=[],fontPresets={},selectedPreset=localStorage.getItem('studio-preset')||'',n=0,id='',view='original',zoom=1,move=false,drawFrame=false,maskEdit=false,maskPreviewOn=false,hasMaskPreview=false,prevView='clean',erase=false,revision=0,busy=false,jsonPath='';
let ocrLang=sessionStorage.getItem('studio-ocr-lang')||'ko+en';
let ocrEngine=sessionStorage.getItem('studio-ocr-engine')||'rapid';
const FRAME_OPTIONS=[
 ['normal','normal — ปกติ (คำพูดทั่วไป)'],
 ['shout','shout — ตะโกน / แอ็กชัน'],
 ['thought','thought — ความคิดในใจ'],
 ['system','system — หน้าต่างระบบ'],
 ['narration','narration — กล่องบรรยาย'],
 ['floating','floating — ข้อความลอย / เสียงประกอบ'],
 ['happy','happy — ดีใจ / สดใส'],
 ['phone','phone — โทรศัพท์ / แชต'],
 ['terrible','terrible — น่ากลัว / กดดัน / สยองขวัญ']
];
const FRAME_SHORT={normal:'normal · ปกติ',shout:'shout · ตะโกน',thought:'thought · ความคิด',system:'system · ระบบ',narration:'narration · บรรยาย',floating:'floating · ลอย/SFX',happy:'happy · สดใส',phone:'phone · แชต',terrible:'terrible · กดดัน'};
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
let pending=null, timer, chain=Promise.resolve(), editSeq=0, sliderDragging=false;
function queueEdit(values){
 if(!bubble())return;
 editSeq++;
 if(!pending)pending={key,n,id,data:{},seq:editSeq};
 pending.seq=editSeq;
 Object.assign(pending.data,values);clearTimeout(timer);timer=setTimeout(()=>run(flush),320)
}
async function flush(){
 clearTimeout(timer);
 if(pending){
  const p=pending;pending=null;
  chain=chain.catch(()=>{}).then(async()=>{
   const res=await api(p.key+'/page/'+p.n+'/edit',{...p.data,id:p.id});
   if(key===p.key){
    if(res.presets)fontPresets=res.presets;
    for(const [idx,pg] of Object.entries(res.following_pages||{}))pages[idx]=pg
   }
   if(key===p.key && !pending && !drag && !sliderDragging && p.seq===editSeq){
    pages[p.n]=res.page;if(n===p.n){sidebar();draw()}
   }
  })
 }
 await chain
}
function statusOf(b){
 if((b.score??1)<0.9)return {cls:'st-low',label:'OCR '+Math.round((b.score||0)*100)+'%',pill:'OCR '+Math.round((b.score||0)*100)+'%'};
 if(!(b.translation||'').trim())return {cls:'st-untranslated',label:'รอแปล',pill:'รอแปล'};
 return {cls:'st-ready',label:'แปลแล้ว',pill:'OK'};
}
async function loadPresets(){
 try{
  const res=await api('font-presets');
  fontPresets=res.presets||{};
  if(!fonts.length&&res.fonts)fonts=res.fonts;
  renderPresets();
 }catch{}
}
function renderPresets(){
 let sel=$('presetSelect');if(!sel)return;
 const names=Object.keys(fontPresets);
 sel.replaceChildren(...names.map(x=>new Option(x,x)));
 if(names.length){
  if(!selectedPreset||!fontPresets[selectedPreset])selectedPreset=names[0];
  sel.value=selectedPreset;
  localStorage.setItem('studio-preset',selectedPreset);
 }
}
$('presetSelect').onchange=()=>run(async()=>{
 selectedPreset=$('presetSelect').value;
 localStorage.setItem('studio-preset',selectedPreset);
 if(key){
  const res=await api(key+'/font-preset',{preset:selectedPreset});
  if(res?.note)info(res.note)
 }
});
function isTextTransform(b){
 return !maskEdit && view==='thai' && !!(b&&b.translation&&b.translation.trim())
}
async function project(k,title){
 await flush();
 if(maskEdit)await cancelMaskEdit(true);
 let st=await api(k+'/state');key=k;localStorage.setItem('studio-project',k);pages=st.pages;fonts=st.fonts;jsonPath=st.json_path||'';
 if(st.font_preset){selectedPreset=st.font_preset;localStorage.setItem('studio-preset',selectedPreset);renderPresets()}
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
 const switchMask=maskEdit && i!==id;
 if(switchMask)await cancelMaskEdit(true);
 id=i;sidebar();inspect();bars();draw();
 if(switchMask)await startMaskEdit()
}
function sidebar(){
 $('pages').replaceChildren();pages.forEach((p,i)=>{let b=btn(String(i+1).padStart(2,'0')+'  '+p.name,()=>selectPage(i));let dot=el('i');dot.className='dot '+(p.bubbles.length&&p.bubbles.every(v=>v.translation)?'ready':p.clean?'cleaned':p.bubbles.length?'ocr':'');b.prepend(dot);b.classList.toggle('active',i===n);$('pages').append(b)});
 $('layers').replaceChildren();page()?.bubbles.forEach((b,i)=>{
  let line=el('div');line.className='layer';let l=b.text_layer||{};
  let st=statusOf(b), code='B'+String(i+1).padStart(2,'0');
  let txt=(b.translation||b.text||'…').replace(/\s+/g,' ').trim();
  let hasFx=!!(l.stroke?.enabled||l.gradient?.enabled);
  let isSel=id===b.id;
  let c=btn('',()=>selectBubble(b.id));
  c.className='layer-main'+(isSel?' active':'');
  let dot=el('i');dot.className='dot '+(isSel?'st-selected':st.cls);
  let tagId=el('span',code);tagId.className='bid-tag';
  let tagTxt=el('span',txt);tagTxt.className='layer-txt';
  let pill=el('span',isSel?'เลือกอยู่':st.pill);pill.className='st-pill '+(isSel?'st-selected':st.cls);
  c.append(dot,tagId,tagTxt,pill);
  if(hasFx){let fx=el('span','fx');fx.className='fx-tag';c.append(fx)}
  c.title=code+'  '+txt+' · '+(FRAME_SHORT[b.frame_type||'normal']||b.frame_type||'normal')+' ('+st.label+')';
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
let samplingPixel=false,maskSerial=0, spaceHeld=false, cursorPoint=null, strokeWrites=Promise.resolve();
const cursor=el('div');cursor.id='brushCursor';document.body.append(cursor);
function updateCursor(e){
 if(e)cursorPoint=[e.clientX,e.clientY];
 const vp=$('viewport'),pan=maskEdit&&spaceHeld;
 vp.classList.toggle('hand',pan);vp.classList.toggle('painting',maskEdit&&!pan&&!maskPreviewOn);
 cursor.hidden=!maskEdit||pan||maskPreviewOn||!cursorPoint;
 if(cursor.hidden)return;
 const size=2*Number($('radius').value)*zoom;
 Object.assign(cursor.style,{left:cursorPoint[0]+'px',top:cursorPoint[1]+'px',width:size+'px',height:size+'px'})
}
function image(){
 if(!page())return;
 const v=activeImageView(),version=++maskSerial,c=$('paint'),draft=v==='mask_draft';
 const url='/api/'+key+'/image/'+n+'?v='+revision+'&bid='+encodeURIComponent(id)+'&view=';
 $('art').src=url+(draft?'mask_base':v);
 c.style.opacity=draft?'.58':'1';
 if(draft){
  const bitmap=new Image();
  bitmap.onload=()=>{
   if(maskSerial!==version)return;
   c.width=page().width;c.height=page().height;
   c.getContext('2d').drawImage(bitmap,0,0);c.dataset.ready='yes'
  };
  c.dataset.ready='loading';bitmap.src=url+'mask_alpha'
 }else{c.width=1;c.dataset.ready=''}
 updateCursor()
}
function paintSegment(a,b,r,remove){
 const ctx=$('paint').getContext('2d');ctx.save();
 ctx.globalCompositeOperation=remove?'destination-out':'source-over';
 ctx.fillStyle=ctx.strokeStyle='#8b5cf6';ctx.lineWidth=2*r;ctx.lineCap='round';
 ctx.beginPath();ctx.moveTo(...a);ctx.lineTo(...b);ctx.stroke();
 ctx.beginPath();ctx.arc(b[0],b[1],r,0,2*Math.PI);ctx.fill();ctx.restore()
}
function fit(){if(page()){let vp=$('viewport');zoom=Math.min(1,(vp.clientWidth-28)/page().width,(vp.clientHeight-88)/page().height);zoom=Math.max(.025,zoom)}}
function scale(value){zoom=Math.max(.025,Math.min(5,value));draw()}
const maskBar=$('cleanPanel'), maskGrip=$('maskGrip'), maskViewport=$('viewport');
let barFrame=0,barTarget='',barPin=null,barDrag=null,barTransitionTimer=0;

function scheduleMaskBar(){
 if(maskEdit&&!barFrame)barFrame=requestAnimationFrame(positionMaskBar)
}

function positionMaskBar(){
 barFrame=0;
 if(!maskEdit)return;
 const target=JSON.stringify([key,n,id]);
 const changed=target!==barTarget;
 if(changed){barPin=null;barDrag=null}
 // Read geometry together before writing a single transform. While dragging
 // only pointer coordinates change, so reuse the bounds captured on press.
 const vp=barDrag?.viewport||maskViewport.getBoundingClientRect();
 const width=barDrag?.width??maskBar.offsetWidth,height=barDrag?.height??maskBar.offsetHeight;
 const clamp=(v,min,max)=>Math.max(min,Math.min(v,max));
 let x,y;
 if(barPin){x=barPin.x;y=barPin.y}
 else{
  const anchor=$('overlay').querySelector('.bubble.selected')?.getBoundingClientRect();
  if(!anchor)return;
  x=(anchor.left+anchor.right-width)/2;
  y=anchor.bottom+14;
  if(y+height>vp.bottom-8 && anchor.top-14-height>=vp.top+8)y=anchor.top-14-height
 }
 x=clamp(x,barPin?8:vp.left+8,(barPin?innerWidth:vp.right)-width-8);
 y=clamp(y,barPin?8:vp.top+8,(barPin?innerHeight:vp.bottom)-height-8);
 if(changed&&barTarget){
  maskBar.classList.add('reanchor');
  clearTimeout(barTransitionTimer);
  barTransitionTimer=setTimeout(()=>maskBar.classList.remove('reanchor'),180)
 }
 barTarget=target;
 maskBar.style.transform='translate3d('+x+'px,'+y+'px,0)';
 maskBar.classList.add('visible')
}

maskGrip.onpointerdown=e=>{
 if(e.button!==0||!maskEdit)return;
 const rect=maskBar.getBoundingClientRect(),viewport=maskViewport.getBoundingClientRect();
 barDrag={id:e.pointerId,viewport,width:rect.width,height:rect.height,dx:e.clientX-rect.left,dy:e.clientY-rect.top};
 barPin={x:rect.left,y:rect.top};
 maskBar.classList.remove('reanchor');maskGrip.setPointerCapture(e.pointerId);e.preventDefault();
 scheduleMaskBar()
};
maskGrip.onpointermove=e=>{
 if(barDrag?.id!==e.pointerId)return;
 barPin={x:e.clientX-barDrag.dx,y:e.clientY-barDrag.dy};
 scheduleMaskBar()
};
maskGrip.onlostpointercapture=()=>{barDrag=null;scheduleMaskBar()};
maskViewport.addEventListener('scroll',scheduleMaskBar,{passive:true});
const maskBarResize=new ResizeObserver(entries=>{
 for(const entry of entries)if(entry.target===maskViewport)maskBar.style.setProperty('--mask-canvas-width',entry.contentRect.width+'px');
 scheduleMaskBar()
});
maskBarResize.observe(maskViewport);maskBarResize.observe(maskBar);

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
 $('maskOptions').hidden=!maskEdit;
 $('cleanPanel').inert=!maskEdit;
 if(!maskEdit)$('cleanPanel').classList.remove('visible');
 scheduleMaskBar();
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
 const bx=b.box||box, fs=l.font_size||24;
 const padX=Math.max(16,fs*0.45), padY=Math.max(20,fs*0.65);
 const cx=(box[0]+box[2])/2, cy=(box[1]+box[3])/2;
 const [tw,th]=l.text_size||[box[2]-box[0],box[3]-box[1]];
 const p=page();
 const W=p?p.width:Math.max(bx[2],box[2]), H=p?p.height:Math.max(bx[3],box[3]);
 const fw=Math.min(W,Math.max(bx[2]-bx[0],tw+2*padX));
 const fh=Math.min(H,Math.max(bx[3]-bx[1],th+2*padY));
 const ncx=Math.min(Math.max(cx,fw/2),W-fw/2);
 const ncy=Math.min(Math.max(cy,fh/2),H-fh/2);
 return [ncx-fw/2,ncy-fh/2,ncx+fw/2,ncy+fh/2]
}
const historyMap=new Map();
let lastArrowPush={time:0,id:''};
function pageHistory(){
 const hk=key+':'+n;
 if(!historyMap.has(hk))historyMap.set(hk,{undo:[],redo:[]});
 return historyMap.get(hk)
}
function captureTransform(b){
 if(!b)return null;
 const l=b.text_layer||{};
 return {
  id:b.id,
  bubble_box:[...b.box],
  has_text:!!(b.translation&&b.translation.trim()&&(l.box||l.editor_box)),
  editor_box:(l.box||l.editor_box)?bounds(b):null,
  font_size:l.font_size??null,
  rotation:l.rotation??0,
  wrapped:l.text??null
 }
}
function sameTransform(a,b){
 if(!a||!b||a.id!==b.id)return false;
 return JSON.stringify(a.bubble_box)===JSON.stringify(b.bubble_box)
  && JSON.stringify(a.editor_box)===JSON.stringify(b.editor_box)
  && a.font_size===b.font_size && a.rotation===b.rotation && a.wrapped===b.wrapped
}
function pushTransformHistory(before,after,label='การปรับตำแหน่ง',coalesceArrow=false){
 if(!before||!after||sameTransform(before,after))return;
 const h=pageHistory(), now=Date.now();
 if(coalesceArrow&&h.undo.length&&lastArrowPush.id===before.id&&now-lastArrowPush.time<500){
  h.undo[h.undo.length-1].after=after;
  lastArrowPush.time=now;
  h.redo=[];
  return
 }
 h.undo.push({before,after,label});
 if(h.undo.length>60)h.undo.shift();
 h.redo=[];
 if(coalesceArrow)lastArrowPush={time:now,id:before.id};
 else lastArrowPush={time:0,id:''}
}
async function applyTransformSnap(snap,toastMsg){
 if(!snap||!page())return;
 await flush();
 if(snap.fonts){
  const res=await api(key+'/page/'+n+'/page_font',{fonts:snap.fonts});
  pages[n]=res.page;revision++;image();inspect();sidebar();draw();info(toastMsg);return
 }
 const target=page().bubbles.find(b=>b.id===snap.id);
 if(!target)return;
 id=target.id;
 target.box=[...snap.bubble_box];
 const payload={id:target.id,bubble_box:[...snap.bubble_box]};
 if(snap.has_text&&snap.editor_box){
  target.text_layer=target.text_layer||{};
  target.text_layer.editor_box=[...snap.editor_box];
  if(snap.font_size!=null)target.text_layer.font_size=snap.font_size;
  target.text_layer.rotation=snap.rotation??0;
  if(snap.wrapped!=null)target.text_layer.text=snap.wrapped;
  Object.assign(payload,{box:[...snap.editor_box],font_size:target.text_layer.font_size,rotation:target.text_layer.rotation});
  if(snap.wrapped!=null)payload.wrapped=snap.wrapped
 }
 const res=await api(key+'/page/'+n+'/edit',payload);
 pages[n]=res.page;
 sidebar();inspect();draw();
 info(toastMsg)
}
async function performUndo(){
 const h=pageHistory();
 if(!h.undo.length){info('ไม่มีรายการปรับตำแหน่งให้ย้อนกลับ');return}
 const entry=h.undo.pop();
 h.redo.push(entry);
 lastArrowPush={time:0,id:''};
 await applyTransformSnap(entry.before,'Undo: ย้อน'+entry.label)
}
async function performRedo(){
 const h=pageHistory();
 if(!h.redo.length){info('ไม่มีรายการให้ทำซ้ำ (Redo)');return}
 const entry=h.redo.pop();
 h.undo.push(entry);
 lastArrowPush={time:0,id:''};
 await applyTransformSnap(entry.after,'Redo: ทำซ้ำ'+entry.label)
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
function applyLiveBubbleStyle(){
 const b=bubble();if(!b)return;
 const node=$('overlay')?.querySelector('.bubble.selected');
 if(!node){draw();return}
 const l=b.text_layer||{}, textMode=isTextTransform(b);
 const rect=textMode?bounds(b):b.box,[x,y,r,bt]=rect;
 Object.assign(node.style,{
  left:x*zoom+'px',top:y*zoom+'px',width:Math.max(2,(r-x)*zoom)+'px',height:Math.max(2,(bt-y)*zoom)+'px',
  fontSize:(l.font_size||24)*zoom+'px',transform:'rotate('+(textMode?l.rotation||0:0)+'deg)'
 });
 const sp=node.querySelector('span');
 if(sp&&textMode&&!l.hidden)sp.textContent=l.text||b.translation;
 updateCoordInfo()
}
function updateCoordInfo(){
 let node=$('coordInfo'),b=bubble();if(!node||!b)return;
 let l=b.text_layer||{}, textMode=isTextTransform(b);
 let rect=textMode?bounds(b):b.box,[x,y,r,bt]=rect;
 let rot=Math.round((l.rotation||0)*10)/10;
 node.textContent=textMode
  ? `พิกัด ${Math.round(x)}, ${Math.round(y)} · ขนาด ${Math.round(r-x)}×${Math.round(bt-y)} · ฟอนต์ ${Math.round(l.font_size||24)} px`+(rot?` · หมุน ${rot}°`:'')
  : `กรอบ ${Math.round(x)}, ${Math.round(y)} · ${Math.round(r-x)}×${Math.round(bt-y)} px`;
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
  if(textMode&&g?.enabled){label.style.backgroundImage='linear-gradient('+((g.angle===135?135:90-g.angle)+'deg')+','+rgb(g.start_color)+','+rgb(g.end_color)+')';label.style.backgroundClip='text';label.style.webkitTextFillColor='transparent'}
  div.append(label);
  // Show corner status badge only in ต้นฉบับ / คลีน views — keep Text view completely unobstructed!
  if(!showText){
   let badge=el('small','B'+String(idx+1).padStart(2,'0')+' · '+(b.id===id?'เลือกอยู่':stInfo.label));
   badge.className='badge';div.append(badge)
  }
  ov.append(div);
  if(b.id===id){
   div.classList.add('selected');
   if(move&&!maskEdit){
    for(const [handle,hx,hy] of [['nw',0,0],['n',50,0],['ne',100,0],['e',100,50],['se',100,100],['s',50,100],['sw',0,100],['w',0,50]]){
     let a=el('i');a.className='handle';a.dataset.handle=handle;Object.assign(a.style,{left:hx+'%',top:hy+'%',cursor:handle+'-resize'});div.append(a)}
    if(textMode){
     for(const corner of ['nw','ne','se','sw']){
      let rz=el('i');rz.className='rot-zone';rz.dataset.rot=corner;rz.title='ลากเพื่อหมุนข้อความ (กด Shift ค้างเพื่อ Snap ทีละ 15°)';div.append(rz)
     }
    }
   }
  }
 });
 scheduleMaskBar();
 updateCoordInfo()
}
function fillSelect(node,values,v){node.replaceChildren(...values.map(x=>Array.isArray(x)?new Option(x[1],x[0]):new Option(x,x)));node.value=v}
function inspect(){
 let host=$('inspector'),b=bubble();host.replaceChildren();if(!b){host.append(el('p','เลือกกรอบบนภาพหรือในรายการ Layers'));return}
 host.append($('fields').content.cloneNode(true));let l=b.text_layer||{},s=l.stroke||{},g=l.gradient||{};
 $('bid').textContent=bubbleCode(b);$('bid').title=b.id;
 let ft=b.frame_type||'normal';
 $('frameBadge').textContent=FRAME_SHORT[ft]||ft;
 let pct=Math.round((b.score??1)*100);
 $('score').textContent='OCR '+pct+'%';
 $('score').className=pct<90?'score-low':'score-ok';
 fillSelect($('frame'),FRAME_OPTIONS,ft);
 fillSelect($('font'),fonts,b.font_choice);$('source').value=b.text;$('thai').value=b.translation;$('ink').value=rgb(l.color);$('rotation').value=l.rotation||0;$('align').value=l.alignment||'center';$('size').value=l.font_size||24;$('sizeValue').value=$('size').value;
 updateCoordInfo();
 $('stroke').checked=!!s.enabled;$('edge').value=rgb(s.color||[255,255,255]);$('width').value=s.size||3;$('gradient').checked=!!g.enabled;$('start').value=rgb(g.start_color||[255,255,255]);$('end').value=rgb(g.end_color||[200,200,200]);$('angle').value=g.angle??90;
 $('thai').oninput=()=>{b.translation=$('thai').value;l.text=b.translation;queueEdit({translation:b.translation});sidebar();draw()};
 $('source').oninput=()=>{b.text=$('source').value;queueEdit({text:b.text});sidebar()};
 $('pageFont').onclick=()=>run(async()=>{
  const font=$('font').value;
  await flush();
  const result=await api(key+'/page/'+n+'/page_font',{font});
  const h=pageHistory();
  h.undo.push({before:{fonts:result.previous},after:{fonts:result.applied},label:'เปลี่ยนฟอนต์ทั้งหน้า'});
  if(h.undo.length>60)h.undo.shift();
  lastArrowPush={time:0,id:''};
  h.redo.length=0;
  pages[n]=result.page;revision++;image();inspect();sidebar();draw();info(result.note)
 });
 $('font').onchange=()=>{b.font_choice=$('font').value;if(b.text_layer)b.text_layer.font_choice=b.font_choice;queueEdit({font_choice:b.font_choice});run(flush);draw()};
 $('frame').onchange=()=>run(async()=>{
  const ft=$('frame').value, activeP=$('presetSelect')?.value||selectedPreset;
  b.frame_type=ft;$('frameBadge').textContent=FRAME_SHORT[ft]||ft;
  const mapped=fontPresets[activeP]?.[ft];
  if(mapped&&fonts.includes(mapped)){
   b.font_choice=mapped;if(b.text_layer)b.text_layer.font_choice=mapped;
   if($('font'))$('font').value=mapped
  }
  draw();
  queueEdit({frame_type:ft,font_preset:activeP});
  await flush();
  inspect();draw()
 });
 function edit(k,v){l[k]=v;queueEdit({[k]:v});draw()}
 $('align').onchange=()=>edit('alignment',$('align').value);$('ink').oninput=()=>edit('color',triplet($('ink').value));
 $('rotation').onchange=()=>{let before=captureTransform(bubble()),v=+$('rotation').value;layer().rotation=v;pushTransformHistory(before,captureTransform(bubble()),'การหมุนข้อความ');queueEdit({box:bounds(bubble()),rotation:v});run(flush);applyLiveBubbleStyle()};
 let sizeBefore=null;
 $('size').onpointerdown=()=>{sliderDragging=true;sizeBefore=captureTransform(bubble())};
 $('size').oninput=()=>{let v=+$('size').value;$('sizeValue').value=v;layer().font_size=v;applyLiveBubbleStyle()};
 $('size').onchange=()=>{sliderDragging=false;let v=+$('size').value;$('sizeValue').value=v;layer().font_size=v;if(sizeBefore){pushTransformHistory(sizeBefore,captureTransform(bubble()),'การปรับขนาดฟอนต์');sizeBefore=null}queueEdit({box:bounds(bubble()),font_size:v});run(flush);applyLiveBubbleStyle()};
 $('size').onpointerup=$('size').onpointercancel=()=>{sliderDragging=false};
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
  samplingPixel=false;$('maskMethod').value='auto';prevView=view;move=false;drawFrame=false;maskEdit=true;maskPreviewOn=false;hasMaskPreview=false;
  const res=await api(key+'/page/'+n+'/mask_start',{id});
  revision++;refresh();info(res.note)
 },'กำลังเปิดโหมดแก้ Mask…')
}
async function cancelMaskEdit(silent=false){
 await strokeWrites.catch(()=>{});strokeWrites=Promise.resolve();
 if(!maskEdit)return;
 const bid=id;
 maskEdit=false;maskPreviewOn=false;hasMaskPreview=false;view=prevView||view;
 try{if(key&&bid)await api(key+'/page/'+n+'/mask_cancel',{id:bid})}catch{}
 revision++;refresh();if(!silent)info('ยกเลิกการแก้ Mask แล้ว')
}
async function applyMaskEdit(){
 await strokeWrites;
 if(!maskEdit||!bubble())return;
 await withBusy(async()=>{
  const res=await api(key+'/page/'+n+'/mask_apply',{id,method:$('maskMethod').value,color:$('maskMethod').value==='solid'?triplet($('solidColor').value):null});
  pages[n]=res.page;maskEdit=false;maskPreviewOn=false;hasMaskPreview=false;view='clean';
  revision++;refresh();info(res.note)
 },'กำลังบันทึกผล Clean…')
}
async function previewMaskEdit(){
 await strokeWrites;
 if(!maskEdit||!bubble())return;
 await withBusy(async()=>{
  const res=await api(key+'/page/'+n+'/mask_preview',{id,method:$('maskMethod').value,color:$('maskMethod').value==='solid'?triplet($('solidColor').value):null});
  hasMaskPreview=!!res.has_preview;maskPreviewOn=hasMaskPreview;
  revision++;bars();image();draw();info(res.note)
 },'กำลังประมวลผล Preview…')
}
async function undoMaskEdit(){
 await strokeWrites.catch(()=>{});strokeWrites=Promise.resolve();
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
 const src=(l.text||b.translation).replace(/\r\n/g,'\n');
 const segment=new Intl.Segmenter('th',{granularity:'word'});
 function lines(max){
  let out=[];for(let para of src.split('\n')){let line='';for(let {segment:word} of segment.segment(para)){
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
 if(maskEdit&&samplingPixel&&!spaceHeld){
  samplingPixel=false;
  run(async()=>{const result=await api(key+'/page/'+n+'/pick_color',{id,point:p});$('solidColor').value=rgb(result.color);$('maskMethod').value='solid';info('เลือกสีพื้น '+rgb(result.color))});return
 }
 if(maskEdit&&spaceHeld&&e.button===0){
  updateCursor(e);
  drag={kind:'pan',p:[e.clientX,e.clientY],sx:$('viewport').scrollLeft,sy:$('viewport').scrollTop};
  $('viewport').setPointerCapture(e.pointerId);return
 }
 if(drawFrame){
  drag={kind:'draw',start:p,end:p};
  let c=$('paint');c.width=page().width;c.height=page().height;
  $('viewport').setPointerCapture(e.pointerId);return
 }
 if(maskEdit&&bubble()){
  if(maskPreviewOn){maskPreviewOn=false;image();bars()}
  if($('paint').dataset.ready!=='yes')return;
  drag={kind:'maskPaint',points:[p],radius:+$('radius').value,erase};paintSegment(p,p,drag.radius,drag.erase);
  $('viewport').setPointerCapture(e.pointerId);return
 }
 if(target){
  if(target.dataset.id!==id){run(()=>selectBubble(target.dataset.id));return}
  if(!move)return;
  let b=bubble(), before=captureTransform(b);
  if(e.target.closest('.rot-zone')&&isTextTransform(b)){
   let rect=bounds(b), cx=(rect[0]+rect[2])/2, cy=(rect[1]+rect[3])/2;
   let startAngle=Math.atan2(p[1]-cy,p[0]-cx)*180/Math.PI;
   drag={kind:'rotate',cx,cy,startAngle,baseRot:layer().rotation||0,before};
   $('viewport').setPointerCapture(e.pointerId);return
  }
  if(isTextTransform(b)){
   drag={kind:'moveText',handle:e.target.dataset.handle||'',p,rect:bounds(b),size:layer().font_size||24,before}
  }else{
   drag={kind:'moveBox',handle:e.target.dataset.handle||'',p,rect:[...b.box],before}
  }
 }else drag={kind:'pan',p:[e.clientX,e.clientY],sx:$('viewport').scrollLeft,sy:$('viewport').scrollTop};
 $('viewport').setPointerCapture(e.pointerId)
};
$('viewport').onpointermove=e=>{
 updateCursor(e);if(!drag)return;let p=point(e);
 if(drag.kind==='draw'){
  drag.end=p;let c=$('paint'),ctx=c.getContext('2d');
  ctx.clearRect(0,0,c.width,c.height);
  let x=Math.min(drag.start[0],p[0]),y=Math.min(drag.start[1],p[1]),w=Math.abs(p[0]-drag.start[0]),h=Math.abs(p[1]-drag.start[1]);
  ctx.fillStyle='rgba(59,130,246,0.16)';ctx.fillRect(x,y,w,h);
  ctx.strokeStyle='#3B82F6';ctx.lineWidth=Math.max(2,2/zoom);ctx.setLineDash([8,4]);ctx.strokeRect(x,y,w,h);
  return
 }
 if(drag.kind==='maskPaint'){
  for(const ev of (e.getCoalescedEvents?.().length?e.getCoalescedEvents():[e])){
   const next=point(ev);paintSegment(drag.points.at(-1),next,drag.radius,drag.erase);drag.points.push(next)
  }
  return
 }
 if(drag.kind==='pan'){let v=$('viewport');v.scrollLeft=drag.sx+drag.p[0]-e.clientX;v.scrollTop=drag.sy+drag.p[1]-e.clientY;return}
 if(drag.kind==='rotate'){
  let cur=Math.atan2(p[1]-drag.cy,p[0]-drag.cx)*180/Math.PI;
  let rot=drag.baseRot+(cur-drag.startAngle);
  while(rot>180)rot-=360;while(rot<-180)rot+=360;
  rot=e.shiftKey?Math.round(rot/15)*15:Math.round(rot);
  if(rot===-180)rot=180;
  layer().rotation=rot;
  if($('rotation'))$('rotation').value=rot;
  applyLiveBubbleStyle();return
 }
 const dx=p[0]-drag.p[0],dy=p[1]-drag.p[1],k=drag.handle;
 const W=page().width,H=page().height;
 let [x,y,r,bt]=drag.rect;
 if(drag.kind==='moveBox'){
  if(!k){x+=dx;r+=dx;y+=dy;bt+=dy}else{
   if(k.includes('w'))x=Math.min(r-10,x+dx);
   if(k.includes('e'))r=Math.max(x+10,r+dx);
   if(k.includes('n'))y=Math.min(bt-10,y+dy);
   if(k.includes('s'))bt=Math.max(y+10,bt+dy);
  }
  const bw=r-x,bh=bt-y;
  if(!k){x=Math.max(0,Math.min(W-bw,x));y=Math.max(0,Math.min(H-bh,y));r=x+bw;bt=y+bh}
  else{x=Math.max(0,x);y=Math.max(0,y);r=Math.min(W,r);bt=Math.min(H,bt)}
  bubble().box=[Math.round(x),Math.round(y),Math.round(r),Math.round(bt)];
  applyLiveBubbleStyle();return
 }
 if(!k){
  const bw=r-x,bh=bt-y;
  x=Math.max(0,Math.min(W-bw,x+dx));y=Math.max(0,Math.min(H-bh,y+dy));
  r=x+bw;bt=y+bh
 }else{
  const rotDeg=layer().rotation||0,theta=rotDeg*Math.PI/180,c=Math.cos(theta),s=Math.sin(theta);
  const hx=k.includes('w')?-1:k.includes('e')?1:0,hy=k.includes('n')?-1:k.includes('s')?1:0;
  const w=r-x,h=bt-y,lx=dx*c+dy*s,ly=-dx*s+dy*c;
  let nw=hx?Math.max(20,w+lx*hx):w, nh=hy?Math.max(20,h+ly*hy):h;
  if(Math.abs(rotDeg)<1e-6){
   // Clamp dragged edge to page bounds while keeping the opposite corner strictly pinned!
   if(hx===1)nw=Math.min(nw,W-x);
   if(hx===-1)nw=Math.min(nw,r);
   if(hy===1)nh=Math.min(nh,H-y);
   if(hy===-1)nh=Math.min(nh,bt);
  }
  if(k.length===2){
   let maxF=220/drag.size;
   if(Math.abs(rotDeg)<1e-6){
    if(hx===1)maxF=Math.min(maxF,(W-x)/w);
    if(hx===-1)maxF=Math.min(maxF,r/w);
    if(hy===1)maxF=Math.min(maxF,(H-y)/h);
    if(hy===-1)maxF=Math.min(maxF,bt/h);
   }
   const minF=Math.max(6/drag.size,20/w,20/h);
   const rawF=(nw*w+nh*h)/(w*w+h*h);
   const factor=Math.max(minF,Math.min(maxF,rawF));
   nw=w*factor;nh=h*factor;
   layer().font_size=Math.max(6,Math.min(220,Math.round(drag.size*factor)));
   $('size').value=$('sizeValue').value=layer().font_size
  }
  const shiftX=(nw-w)*hx/2,shiftY=(nh-h)*hy/2;
  const cx=(x+r)/2+shiftX*c-shiftY*s,cy=(y+bt)/2+shiftX*s+shiftY*c;
  x=cx-nw/2;r=cx+nw/2;y=cy-nh/2;bt=cy+nh/2
 }
 let rect=[x,y,r,bt];layer().editor_box=rect;if(k.length===1)layer().text=reflow(bubble(),r-x,bt-y);applyLiveBubbleStyle()
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
  const path=key+'/page/'+n+'/mask_paint',payload={id,points:d.points,radius:d.radius,erase:d.erase};
  hasMaskPreview=false;maskPreviewOn=false;bars();
  strokeWrites=strokeWrites.catch(()=>{}).then(()=>api(path,payload)).catch(err=>{info(err.message);revision++;image();throw err});
  strokeWrites.catch(()=>{});
  return
 }
 if(d.kind==='rotate'){
  pushTransformHistory(d.before,captureTransform(bubble()),'การหมุนข้อความ');
  queueEdit({box:bounds(bubble()),rotation:layer().rotation});run(flush);return
 }
 if(d.kind==='moveBox'){
  pushTransformHistory(d.before,captureTransform(bubble()),'การปรับกรอบ');
  queueEdit({bubble_box:bubble().box});run(flush);return
 }
 if(d.kind==='moveText'){
  pushTransformHistory(d.before,captureTransform(bubble()),d.handle?'การปรับขนาด':'การปรับตำแหน่ง');
  const payload={box:bounds(bubble()),font_size:layer().font_size};
  if(d.handle&&d.handle.length===1&&layer().text)payload.wrapped=layer().text;
  queueEdit(payload);run(flush)
 }
};
$('viewport').onpointercancel=()=>{drag=null;$('paint').width=1;run(async()=>{if(key){pages=(await api(key+'/state')).pages;refresh()}})};
$('viewport').onwheel=e=>{if(e.ctrlKey){e.preventDefault();if(maskEdit){let r=$('radius');r.value=Math.min(+r.max,Math.max(+r.min,+r.value+(e.deltaY>0?-1:1)));r.oninput();updateCursor(e)}else scale(zoom*(e.deltaY>0?.9:1.1))}};
function side(which){let node=$(which);node.hidden=!node.hidden;document.querySelector('main').style.setProperty('--'+which,node.hidden?'0px':which==='left'?'268px':'320px')}
async function applyFontPreset(){
 if(!key||!pages.length){info('เปิดโปรเจกต์ก่อนจัดฟอนต์ตามโปรไฟล์');return}
 const preset=$('presetSelect')?.value||selectedPreset;
 await withBusy(async()=>{
  const res=await api(key+'/apply-font-preset',{preset,page:n,all_pages:$('presetScope').value==='all'});
  pages=res.pages;if(!bubble())id=page()?.bubbles[0]?.id||'';
  view='thai';revision++;refresh();info(res.note)
 },'กำลังจัดฟอนต์ตามโปรไฟล์…')
}
async function openPresetEditor(isNew=false){
 if(!Object.keys(fontPresets).length)await loadPresets();
 const current=$('presetSelect')?.value||selectedPreset||Object.keys(fontPresets)[0]||'1. มังฮวาเกาหลี';
 const baseMap=fontPresets[current]||{};
 let m=modal(isNew?'เพิ่มโปรไฟล์ฟอนต์ใหม่':'ตั้งค่าฟอนต์ — '+current);
 let nameInput=null;
 if(isNew){
  let lbl=el('label','ชื่อโปรไฟล์ใหม่');
  nameInput=el('input');nameInput.id='newPresetName';nameInput.placeholder='เช่น 3. โรแมนซ์ / แฟนตาซี';
  lbl.append(nameInput);m.append(lbl)
 }
 let grid=el('div');grid.className='preset-grid';
 const selects={};
 for(const [ftype,flabel] of FRAME_OPTIONS){
  let row=el('div');row.className='preset-row';
  row.append(el('small',flabel));
  let s=el('select');s.dataset.ftype=ftype;
  fillSelect(s,fonts,baseMap[ftype]||fonts[0]||'TH Sarabun New Bold');
  selects[ftype]=s;row.append(s);grid.append(row)
 }
 let status=el('p','');
 let actions=el('div');actions.className='modal-actions';
 let cancelBtn=btn('ยกเลิก',()=>$('dialog').close());
 let saveBtn=btn(isNew?'สร้างและบันทึกโปรไฟล์':'บันทึกโปรไฟล์',async()=>{
  const name=isNew?nameInput.value.trim():current;
  if(!name){status.className='err-note';status.textContent='กรุณาระบุชื่อโปรไฟล์ก่อนบันทึก';nameInput?.focus();return}
  saveBtn.disabled=true;
  try{
   const mapping={};for(const [k,s] of Object.entries(selects))mapping[k]=s.value;
   const res=await api('font-presets',{name,mapping});
   fontPresets=res.presets;selectedPreset=res.name;renderPresets();
   if(key)await api(key+'/font-preset',{preset:selectedPreset});
   info(res.note);$('dialog').close()
  }catch(err){status.className='err-note';status.textContent=err.message}
  finally{saveBtn.disabled=false}
 });
 saveBtn.className='primary';saveBtn.id='savePresetBtn';
 actions.append(cancelBtn,saveBtn);
 m.append(grid,status,actions);
 if(nameInput)nameInput.focus()
}
const commands={
 ocrOptions:['1 OCR ทุกหน้า (เลือกภาษาและโมเดล)',openOcrModal,'1'],
 advanced:['เครื่องมือเดิม · Smart Guides, Lasso, OCR ทางเลือก',async()=>{await flush();const r=await api(key+'/legacy',{});info('เปิดเครื่องมือเดิมแล้ว บันทึก JSON แล้วเปิดกลับใน Studio · '+r.path,r.path)},''],
 stitch:['SmartStitch · รวม/แบ่งหน้าเป็นโปรเจกต์ใหม่',()=>{const m=modal('SmartStitch');m.insertAdjacentHTML('beforeend','<label>ความสูงเป้าหมาย <input id="stitchH" type="number" value="15000"></label><label>ความกว้าง <input id="stitchW" type="number" value="720"></label>');m.append(btn('สร้างโปรเจกต์',async()=>{const height=+$('stitchH').value,width=+$('stitchW').value;$('dialog').close();await flush();const r=await withBusy(()=>api(key+'/stitch',{height,width}),'กำลังรวมและแบ่งหน้า…');if(r){await project(r.key,r.name);info(r.note)}}))},''],
 rebuild:['สร้าง Mask ใหม่ตาม Margin/Grow',async()=>{if(!maskEdit)return;await strokeWrites;const r=await api(key+'/page/'+n+'/mask_rebuild',{id,margin:+$('maskMargin').value,grow:+$('maskGrow').value});hasMaskPreview=false;maskPreviewOn=false;revision++;image();bars();info(r.note)},''],
 pipette:['ดูดสีพื้นจากภาพ',()=>{if(maskEdit){samplingPixel=true;info('คลิกบนภาพเพื่อเลือกสีพื้นต้นฉบับ')}} ,''],
 batchMask:['2 Mask ทุกหน้า',()=>batch('mask',{},'original'),'2'],
 batchClean:['3 Clean ทุกหน้า',()=>batch('clean',{},'clean'),'3'],
 move:['Move / Transform (ย้าย/ย่อขยาย 8 มุม)',async()=>{if(maskEdit)await cancelMaskEdit(true);drawFrame=false;move=!move;if(move&&bubble()?.translation?.trim()){view='thai';image()}bars();draw()},'V'],
 addFrame:['สร้างกรอบบอลลูนใหม่',async()=>{if(!page())return;if(maskEdit)await cancelMaskEdit(true);move=false;drawFrame=!drawFrame;bars();draw();info(drawFrame?'ลากบนภาพเพื่อวาดกรอบใหม่ (กด Esc เพื่อยกเลิก)':'')},'N'],
 deleteFrame:['ลบกรอบที่เลือก',()=>{if(!bubble()){info('เลือกกรอบก่อนลบ');return}return deleteBubbleById(id)},'Del'],
 rapidBubble:['RapidOCR ช่องนี้',()=>{if(!bubble()){info('เลือกกรอบข้อความก่อนอ่าน RapidOCR');return}return command('ocr_bubble',false,{lang:ocrLang})},''],
 applyPreset:['จัดฟอนต์ตามโปรไฟล์',applyFontPreset,''],
 editPreset:['ตั้งค่าฟอนต์โปรไฟล์',()=>openPresetEditor(false),''],
 newPreset:['เพิ่มโปรไฟล์ฟอนต์ใหม่',()=>openPresetEditor(true),''],
 maskEdit:['แก้ Mask รายช่อง',()=>maskEdit?cancelMaskEdit():startMaskEdit(),'M'],
 maskUndo:['ย้อน Mask',undoMaskEdit,''],
 maskPreview:['Preview Clean รายช่อง',previewMaskEdit,''],
 maskApply:['บันทึกผล Clean (ตกลง)',applyMaskEdit,''],
 maskCancel:['ยกเลิกแก้ Mask',()=>cancelMaskEdit(),'Esc'],
 txtModal:['ส่งออก / นำเข้าคำแปล TXT',openTxtModal,'T'],
 saveJson:['บันทึกโปรเจกต์ JSON',()=>saveProjectJson(false),'Ctrl+S'],
 saveJsonAs:['บันทึกโปรเจกต์ JSON เป็นไฟล์ใหม่ (Save As)',()=>saveProjectJson(true),'Ctrl+Shift+S'],
 undoTransform:['Undo ย้อนการปรับตำแหน่ง/ขนาด/หมุน',performUndo,'Ctrl+Z'],
 redoTransform:['Redo ทำซ้ำการปรับตำแหน่ง/ขนาด/หมุน',performRedo,'Ctrl+Y'],
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
 let loadBtn=btn('เปิดไฟล์ .txt',async()=>{
  loadBtn.disabled=true;
  try{
   const res=await api('open-txt',{});
   if(!res||res.cancelled)return;
   area.value=res.text;
   status.className='';status.textContent='โหลดไฟล์ '+res.filename+' แล้ว — กด "นำเข้าคำแปลกลับเข้าโปรเจกต์"'
  }catch{
   $('txtFile').click()
  }finally{loadBtn.disabled=false}
 });
 $('txtFile').onchange=async()=>{
  let f=$('txtFile').files[0];if(!f)return;
  area.value=await f.text();$('txtFile').value='';
  status.className='';status.textContent='โหลดไฟล์ '+f.name+' แล้ว — กด "นำเข้าคำแปลกลับเข้าโปรเจกต์"'
 };
 let importBtn=btn('นำเข้าคำแปลกลับเข้าโปรเจกต์',async()=>{
  if(!area.value.trim()){
   status.className='err-note';status.textContent='กรุณาวางคำแปลหรือเปิดไฟล์ .txt ก่อนนำเข้า';return
  }
  importBtn.disabled=true;
  importBtn.textContent='กำลังนำเข้าและจัดวาง…';
  status.className='';status.textContent='กำลังจับคู่คำแปลและจัดวางข้อความ…';
  try{
   const res=await api(key+'/txt/import',{text:area.value});
   pages=res.pages;if(!bubble())id=page()?.bubbles[0]?.id||'';
   view='thai';revision++;refresh();
   $('dialog').close();
   info(res.note)
  }catch(err){
   status.className='err-note';
   status.textContent='นำเข้าไม่สำเร็จ: '+err.message;
   info('นำเข้าคำแปลไม่สำเร็จ: '+err.message)
  }
  finally{importBtn.disabled=false;importBtn.textContent='นำเข้าคำแปลกลับเข้าโปรเจกต์'}
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
  +'<progress max="100" value="0"></progress><output id="exportStatus" aria-live="polite"></output><div id="exportResult"></div>'
 );
 $('exportDir').value=lastDir;
 $('pickExportDir').onclick=()=>run(async()=>{
  submit.disabled=true;submit.classList.add('working');submit.textContent='กำลังเลือกโฟลเดอร์…';try{
  const res=await api('pick-folder',{title:'เลือกโฟลเดอร์ปลายทางสำหรับ Export PSD / MTP',initial_dir:$('exportDir').value});
  if(res&&!res.cancelled&&res.path){$('exportDir').value=res.path;localStorage.setItem('studio-export-dir',res.path)}else{info('ยกเลิกการเลือกโฟลเดอร์')}
  }finally{submit.disabled=false;submit.classList.remove('working');submit.textContent='ส่งออก'}
 });
 let bar=m.querySelector('progress'),outBox=$('exportResult');
 let submit=btn('ส่งออก',async()=>{
  submit.disabled=true;submit.classList.add('working');submit.textContent='กำลังส่งออก...';$('close').disabled=true;$('dialog').oncancel=e=>e.preventDefault();$('pickExportDir').disabled=true;outBox.replaceChildren();bar.value=0;const status=$('exportStatus');status.textContent='0% · กำลังเตรียมส่งออก';
  try{
   const dest=$('exportDir').value.trim();
   const j=await api(key+'/export',{backend:$('backend').value,format:$('format').value,fx:$('fxOn').checked,drive:$('drive').checked,destination:dest});
   while(true){
    let r=await api('jobs/'+j+'/progress');bar.value=r.value;status.textContent=r.value+'% · '+(r.status||'กำลังประมวลผล');
    if(r.done){
     for(let f of r.files){let a=el('a',f);a.href='/api/jobs/'+j+'/file/'+encodeURIComponent(f);a.download=f;outBox.append(el('br'),a)}
     if(r.error){status.textContent='ส่งออกไม่สำเร็จ';outBox.append(el('p',r.error));info(r.error)}
     else{
      const target=(r.paths&&r.paths[0])||r.folder;
      status.textContent='✓ 100% · ส่งออกสำเร็จ';status.className='export-ok';outBox.append(el('p',r.folder));
      let openBtn=btn('📂 เปิดโฟลเดอร์',()=>api('reveal',{path:target}));openBtn.className='btn-reveal';
      outBox.append(openBtn);
      info('ส่งออกสำเร็จ: '+r.folder,target)
     }
     break
    }
    await new Promise(ok=>setTimeout(ok,500))
   }
  }catch(e){status.textContent='ส่งออกไม่สำเร็จ · '+e.message;throw e}finally{$('close').disabled=false;$('dialog').oncancel=null;submit.disabled=false;submit.classList.remove('working');submit.textContent='ส่งออก';if($('pickExportDir'))$('pickExportDir').disabled=false}
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
$('radius').oninput=()=>{$('radiusValue').value=$('radius').value;updateCursor()};
$('maskToggle').onclick=()=>{if(!hasMaskPreview)return;maskPreviewOn=!maskPreviewOn;image();bars()};
document.onkeydown=e=>{
 if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();palette();return}
 if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();run(()=>saveProjectJson(e.shiftKey));return}
 if($('dialog').open||e.target.closest('input,textarea,select'))return;
 if((e.ctrlKey||e.metaKey)&&!e.altKey){
  const k=e.key.toLowerCase();
  if(k==='z'&&!e.shiftKey){e.preventDefault();run(maskEdit?undoMaskEdit:performUndo);return}
  if(k==='y'||(k==='z'&&e.shiftKey)){e.preventDefault();if(!maskEdit)run(performRedo);return}
 }
 if(e.key==='Escape'){
  if(drawFrame){e.preventDefault();drawFrame=false;bars();draw();info('');return}
  if(maskEdit){e.preventDefault();run(()=>cancelMaskEdit());return}
 }
 if(e.key==='Delete'){e.preventDefault();run(commands.deleteFrame[1]);return}
 let c={'1':'ocrOptions','2':'batchMask','3':'batchClean','v':'move','n':'addFrame','m':'maskEdit','t':'txtModal','[':'left',']':'right'}[e.key.toLowerCase()];
 if(c&&!e.ctrlKey&&!e.metaKey&&!e.altKey){e.preventDefault();run(commands[c][1]);return}
 if(move&&!maskEdit&&bubble()&&e.key.startsWith('Arrow')){
  e.preventDefault();
  let before=captureTransform(bubble());
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
  pushTransformHistory(before,captureTransform(bubble()),'การปรับตำแหน่ง',true);
  draw()
 }
};
refresh();
run(async()=>{await loadPresets();let last=localStorage.getItem('studio-project');if(last){let record=(await api('projects')).find(x=>x[0]===last);if(record)await project(...record)}});

$('viewport').addEventListener('pointerleave',()=>{cursorPoint=null;updateCursor()});
document.addEventListener('keydown',e=>{if(maskEdit&&e.code==='Space'&&!e.target.closest('textarea,input[type=text],select')){
 e.preventDefault();
 if(!spaceHeld&&drag?.kind==='maskPaint'&&cursorPoint){
  $('viewport').onpointerup(e);
  drag={kind:'pan',p:[...cursorPoint],sx:$('viewport').scrollLeft,sy:$('viewport').scrollTop}
 }
 spaceHeld=true;updateCursor()
}},true);
function releaseHand(resume){
 spaceHeld=false;
 if(drag?.kind==='pan'&&maskEdit){
  drag=null;
  if(resume&&cursorPoint&&$('paint').dataset.ready==='yes'){
   let p=point({clientX:cursorPoint[0],clientY:cursorPoint[1]});
   drag={kind:'maskPaint',points:[p],radius:+$('radius').value,erase}
  }
 }
 updateCursor()
}
document.addEventListener('keyup',e=>{if(e.code==='Space')releaseHand(true)});window.addEventListener('blur',()=>releaseHand(false));
