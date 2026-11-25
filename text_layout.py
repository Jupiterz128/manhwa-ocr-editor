"""Translation exchange, AI bubble segmentation, frame-type presets, oval layout and PSD type data."""
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
import json
import os
import re
import sys
import threading
import cv2
import numpy as np
from PIL import Image
from bubble_cleaner import prepare_region, bounded_box
from frame_geometry import valid_quad, quad_angle, quad_bounds, box_quad, map_points

COMPONENT_VERSION='4.0'
FONT_NAME='TH Sarabun New Bold'
FONT_POSTSCRIPT='THSarabunNew-Bold'
FONT_CHOICES=[
    'TH Sarabun New Bold',
    'FC Muffin',
    'Itim',
    'TF MANIWIKA',
    'JS Toomtam',
    'EDMuzazhi',
    'TF LANNA',
    'TF PHETAI',
    'TF Phethai Italic',
    'iannnnn-TIGER',
    'Layiji TaMaiTine1',
    'Layiji JaRaKeFadHang',
    'TH Baijam Bold',
    'IrisUPC Bold',
    'MN KRASIP',
]

FRAME_TYPES=(
    ('normal','พูดทั่วไป'),
    ('shout','ตะโกน / แอ็กชัน'),
    ('system','กล่องระบบ / สเตตัส'),
    ('thought','คิดในใจ'),
    ('floating','คำลอยนอกกรอบ'),
    ('narration','ช่องบรรยาย'),
    ('terrible','บับเบิ้ลออร่ามืด / ข่มขู่'),
    ('happy','บับเบิ้ลดอกไม้ / สดใส'),
    ('phone','บับเบิ้ลแชท / โทรศัพท์'),
)
FRAME_TYPE_LABELS=dict(FRAME_TYPES)

DEFAULT_FONT_PRESETS={
    '1. มังฮวาเกาหลี':{
        'normal':'TH Sarabun New Bold',
        'shout':'Layiji TaMaiTine1',
        'system':'IrisUPC Bold',
        'thought':'TF Phethai Italic',
        'floating':'Itim',
        'narration':'IrisUPC Bold',
        'terrible':'MN KRASIP',
        'happy':'JS Toomtam',
        'phone':'TH Baijam Bold',
    },
    '2. การ์ตูนต่อสู้':{
        'normal':'TF LANNA',
        'shout':'TF Phethai Italic',
        'system':'IrisUPC Bold',
        'thought':'TF MANIWIKA',
        'floating':'Itim',
        'narration':'TF PHETAI',
        'terrible':'MN KRASIP',
        'happy':'JS Toomtam',
        'phone':'TH Baijam Bold',
    },
}

CHARACTER_STYLE={'tracking':0,'kerning':'metrics','antialias':'sharp','horizontal_scale':100,
                 'vertical_scale':100,'baseline_shift':0,'language':'English: UK'}

BUBBLE_SEG_URL='https://huggingface.co/kitsumed/yolov8m_seg-speech-bubble/resolve/main/model_dynamic.onnx'

_PUNCT_FALLBACKS={
    '\u2026':'...',
    '\u22ef':'...',
    '\u2013':'-',
    '\u2014':'-',
    '\u2015':'-',
    '\u2212':'-',
    '\u301c':'~',
    '\uff5e':'~',
    '\u2018':"'",
    '\u2019':"'",
    '\u201c':'"',
    '\u201d':'"',
    '\u300c':'"',
    '\u300d':'"',
    '\u300e':'"',
    '\u300f':'"',
    '\u3010':'[',
    '\u3011':']',
    '\uff08':'(',
    '\uff09':')',
    '\uff01':'!',
    '\uff1f':'?',
    '\uff1a':':',
    '\uff1b':';',
    '\uff0c':',',
    '\u3002':'.',
    '\u3001':',',
    '\u00b7':'.',
    '\u2022':'.',
    '\u00a0':' ',
    '\u3000':' ',
    '\u200b':'',
    '\u200c':'',
    '\u200d':'',
    '\ufeff':'',
}
_SECONDARY_PUNCT={
    '[':'(',
    ']':')',
    '{':'(',
    '}':')',
    '<':'(',
    '>':')',
    '~':'-',
    '_':'-',
    '/':' ',
    '|':' ',
}
_THAI_UPPER=frozenset('\u0e31\u0e34\u0e35\u0e36\u0e37\u0e47\u0e48\u0e49\u0e4a\u0e4b\u0e4c\u0e4d')
_THAI_LOWER=frozenset('\u0e38\u0e39\u0e3a')

_SYSTEM_RE=re.compile(
    r'(?:\b(?:STATUS|STATE|LEVEL|LV\.?\s*\d|HP\s*[:/]|MP\s*[:/]|SP\s*[:/]|EXP\b|'
    r'STRENGTH|AGILITY|INTELLIGENCE|PERCEPTION|STAMINA|VITALITY|SKILL|QUEST|SYSTEM|'
    r'TRAIT|EMBLEM|JOB\s*:|TITLE\s*:|RANK\s*:|REWARD|NOTIFICATION|WARNING)\b|'
    r'(?:สเตตัส|สถานะ\s*:|เลเวล|ภารกิจ|ทักษะ|สกิล|แจ้งเตือน|ระบบ|รางวัล|ฉายา\s*:|อาชีพ\s*:))',
    re.IGNORECASE,
)
_NUMBERED_TAG_RE=re.compile(r'^\s*\[#\s*(\d+)\s*\]\s*(.*)$')


def _normalize_rotation(value):
    if value is None or value == '' or value is False:
        return None
    try:
        ang = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(ang):
        return None
    return float(np.clip(ang, -89.0, 89.0))


def presets_config_path():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'ManhwaOCREditor'/'font_presets.json'


def load_font_presets():
    presets={name:dict(mapping) for name,mapping in DEFAULT_FONT_PRESETS.items()}
    path=presets_config_path()
    if path.is_file():
        try:
            saved=json.loads(path.read_text(encoding='utf-8'))
            if isinstance(saved,dict):
                for p_name,mapping in saved.items():
                    if isinstance(mapping,dict):
                        base=dict(DEFAULT_FONT_PRESETS.get(p_name,DEFAULT_FONT_PRESETS['1. มังฮวาเกาหลี']))
                        for k,_ in FRAME_TYPES:
                            if k in mapping:
                                base[k]=normalize_font_choice(mapping[k])
                        presets[p_name]=base
        except Exception:
            pass
    return presets


def save_font_presets(presets):
    path=presets_config_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    clean={}
    for p_name,mapping in presets.items():
        clean[p_name]={k:normalize_font_choice(mapping.get(k,FONT_NAME)) for k,_ in FRAME_TYPES}
    path.write_text(json.dumps(clean,ensure_ascii=False,indent=2),encoding='utf-8')


_TYPE_CLASSES=('normal','shout','thought','system','narration','floating')
_TAG_TYPES=dict(zip(('พูด','ตะโกน','คิด','ระบบ','บรรยาย','ลอย'),_TYPE_CLASSES))
_TAG_TYPES.update({key:key for key in FRAME_TYPE_LABELS})
_TAG_TYPES.update({'มืด':'terrible','ข่มขู่':'terrible','สดใส':'happy','แชท':'phone'})


def extract_frame_tag(text):
    """Return (category or None, display text). Only consume a recognised prefix."""
    match=re.match(r'^\s*\[([^\]\n]+)\]\s*',text)
    category=_TAG_TYPES.get(match[1].strip().casefold()) if match else None
    return (category,text[match.end():]) if category else (None,text)


_MODEL_TYPES={
    'Box bubble':'narration','Connected bubble':'normal','Happy bubble':'happy',
    'OT':'floating','Phone bubble':'phone','SFX':'floating','ST':'floating',
    'Shout bubble':'shout','Speech bubble':'normal','System Panel':'system',
    'Terriable bubble':'terrible','Thought bubble':'thought',
}


@lru_cache(maxsize=2)
def _load_type_model(folder,weights_stamp,names_stamp,gpu=False):
    import onnxruntime as ort
    names=(folder/'class_names.txt').read_text('utf-8-sig').splitlines()
    options=ort.SessionOptions()
    options.graph_optimization_level=ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads=max(2,min(6,os.cpu_count() or 4))
    options.inter_op_num_threads=1
    providers=['CPUExecutionProvider']
    if gpu and 'CUDAExecutionProvider' in ort.get_available_providers():
        providers.insert(0,'CUDAExecutionProvider')
    try:
        session=ort.InferenceSession(str(folder/'weights.onnx'),options,providers=providers)
    except Exception:
        if len(providers)==1:raise
        session=ort.InferenceSession(str(folder/'weights.onnx'),options,providers=['CPUExecutionProvider'])
    inp=session.get_inputs()[0]
    outputs={out.name:out.shape for out in session.get_outputs()}
    if outputs['labels'][-1]!=len(names) or outputs['dets'][-1]!=4:
        raise ValueError('Unexpected RF-DETR output dimensions')
    return session,inp,[name.strip() if name.strip() in _MODEL_TYPES else None for name in names]


class BubbleTypeDetector:
    """Keep detections for the current source page and reuse the RF-DETR session."""
    def __init__(self):
        self.lock=threading.Lock()
        self.page=None
        self.detections=[]
        self.last_error=None

    def scan(self,image,gpu=False):
        with self.lock:
            if image is not self.page or gpu!=getattr(self,'page_gpu',False):
                try:
                    folder=presets_config_path().parent/'seg_models'/'bubble_type_v11'
                    model=_load_type_model(folder,(folder/'weights.onnx').stat().st_mtime_ns,
                                           (folder/'class_names.txt').stat().st_mtime_ns,gpu)
                    self.page_gpu=gpu
                    self.detections=self._infer(image,*model)
                    self.last_error=None
                except Exception as exc:
                    self.detections=[]
                    self.last_error=str(exc)
                self.page=image
            return self.detections

    def signature(self):
        folder=presets_config_path().parent/'seg_models'/'bubble_type_v11'
        try:
            return folder,(folder/'weights.onnx').stat().st_mtime_ns,(folder/'class_names.txt').stat().st_mtime_ns
        except OSError:
            return None

    def detect_tile(self,tile,gpu=False):
        model=_load_type_model(*self.signature(),gpu)
        rows=self._infer(tile,*model,single=True)
        return [(name,float(score),[round(x),round(y),round(x+w),round(y+h)])
                for (x,y,w,h),score,name in rows]

    def _infer(self,image,session,inp,classes,single=False):
        height,width=inp.shape[-2:]
        w,h=image.size
        boxes=[];scores=[];nms_scores=[];types=[]
        for top in range(0,h,1280):
            bottom=h if single else min(top+1600,h)
            tile=image.crop((0,top,w,bottom)).convert('RGB').resize((width,height),Image.Resampling.BILINEAR)
            rgb=np.asarray(tile,dtype=np.float32)/255
            rgb-=np.array([.485,.456,.406],dtype=np.float32)
            rgb/=np.array([.229,.224,.225],dtype=np.float32)
            dets,logits=session.run(['dets','labels'],{inp.name:np.ascontiguousarray(rgb.transpose(2,0,1)[None])})
            for (cx,cy,bw,bh),row in zip(dets[0],logits[0]):
                idx=int(row.argmax())
                score=float(1/(1+np.exp(-np.clip(row[idx],-60,60))))
                if not classes[idx] or score<.26:continue
                x0,x1=np.clip([cx-bw/2,cx+bw/2],0,1)*w
                y0,y1=np.clip([cy-bh/2,cy+bh/2],0,1)*(bottom-top)+top
                if x1>x0 and y1>y0:
                    clipped=not single and ((top>0 and y0<=top+8) or (bottom<h and y1>=bottom-8))
                    boxes.append([float(x0),float(y0),float(x1-x0),float(y1-y0)])
                    scores.append(score)
                    nms_scores.append(score*(.5 if clipped else 1.0))
                    types.append(classes[idx])
            if bottom==h:break
        keep=cv2.dnn.NMSBoxes(boxes,nms_scores,0.,.50)
        return [(boxes[i],scores[i],types[i]) for i in np.asarray(keep,dtype=int).ravel()]

    def match(self,image,box,gpu=False):
        x0,y0,x1,y1=box
        area=max(1,(x1-x0)*(y1-y0))
        cx,cy=(x0+x1)/2,(y0+y1)/2
        best=0
        result=None
        for (x,y,w,h),confidence,category in self.scan(image,gpu):
            overlap=max(0,min(x1,x+w)-max(x0,x))*max(0,min(y1,y+h)-max(y0,y))
            coverage=overlap/area
            iou=overlap/max(1,w*h+area-overlap)
            centered=x<=cx<=x+w and y<=cy<=y+h
            if iou<.35 and not (centered and coverage>=.65):continue
            score=iou+.5*coverage+.2*centered+.1*confidence
            if score>best:
                result,best=_MODEL_TYPES.get(category,category),score
        return result


_BUBBLE_TYPE_DETECTOR=BubbleTypeDetector()


def classify_bubble_type(image,bubble,gpu=False,*,refresh=False):
    tag,clean=extract_frame_tag(bubble.translation or '')
    if tag:
        bubble.translation=clean
        bubble.frame_type=tag
        bubble.frame_type_locked=True
        return tag
    if getattr(bubble,'frame_type_locked',False) and bubble.frame_type in FRAME_TYPE_LABELS:
        return bubble.frame_type
    if not refresh and getattr(bubble,'detected_frame_type','') in FRAME_TYPE_LABELS:
        return bubble.detected_frame_type
    prediction=_BUBBLE_TYPE_DETECTOR.match(image,bubble.box,gpu)
    if prediction:
        return prediction
    speech=getattr(bubble,'detector_class',-1)==0
    texts=[(value or '').strip() for value in (bubble.text,bubble.translation)]
    joined=' '.join(texts)
    if _SYSTEM_RE.search(joined) or any(re.fullmatch(r'\[.+\]|【.+】|<.+>',v,re.S) for v in texts):
        return 'system'
    x0,y0,x1,y1=bubble.box
    bw=max(1,x1-x0);bh=max(1,y1-y0)
    roi=bounded_box(bubble.box,image.size,round(.4*max(bw,bh)))
    gray=np.asarray(image.crop(roi).convert('L'))
    if gray.size<16:return 'normal' if speech else 'floating'
    cx=(x0+x1)/2-roi[0];cy=(y0+y1)/2-roi[1]
    edges=cv2.Canny(gray,45,130)
    closed_edges=cv2.morphologyEx(edges,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    if roi[0]==0:closed_edges[:,0]=255
    if roi[1]==0:closed_edges[0,:]=255
    if roi[2]==image.width:closed_edges[:,-1]=255
    if roi[3]==image.height:closed_edges[-1,:]=255
    contours,_=cv2.findContours(closed_edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    dark=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV|cv2.THRESH_OTSU)[1]
    # Recover enclosed ink outlines split by Canny at sharp tips.
    ink_contours,_=cv2.findContours(dark,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    near_edge=cv2.dilate(edges,np.ones((5,5),np.uint8))
    contours=tuple(contours)+tuple(c for c in ink_contours
                                   if np.mean(near_edge[c[:,0,1],c[:,0,0]]>0)>.50)
    enclosing=[]
    for c in contours:
        x,y,w,h=cv2.boundingRect(c)
        if ((x==0 and roi[0]>0) or (y==0 and roi[1]>0) or
                (x+w>=gray.shape[1] and roi[2]<image.width) or
                (y+h>=gray.shape[0] and roi[3]<image.height) or
                (w>=gray.shape[1]-1 and h>=gray.shape[0]-1)):
            continue
        if cv2.contourArea(c)>=max(80,.30*bw*bh) and cv2.pointPolygonTest(c,(cx,cy),False)>=0:
            enclosing.append(c)
    c=max(enclosing,key=cv2.contourArea) if enclosing else None
    rect=spikes=False
    if c is not None:
        x,y,w,h=cv2.boundingRect(c);length=cv2.arcLength(c,True)
        polygon=cv2.approxPolyDP(c,.018*length,True)[:,0].astype(float)
        sides=np.abs(polygon-np.roll(polygon,1,axis=0))
        rect=(cv2.contourArea(c)/(w*h)>=.87 and 4<=len(polygon)<=6 and
              np.all(sides.min(axis=1)<=.16*sides.max(axis=1)+1))
        vertices=cv2.approxPolyDP(c,max(1,.003*length),True)[:,0].astype(float)
        a=np.roll(vertices,1,axis=0)-vertices;b=np.roll(vertices,-1,axis=0)-vertices
        cosine=(a*b).sum(1)/np.maximum(1,np.linalg.norm(a,axis=1)*np.linalg.norm(b,axis=1))
        spikes=length>=1.18*cv2.arcLength(cv2.convexHull(c),True) and np.count_nonzero(cosine>.17)>=7
    yy,xx=np.indices(gray.shape)
    radius=np.hypot((xx-cx)/max(1,bw*.5),(yy-cy)/max(1,bh*.5))
    inner=gray[radius<.65];outer=gray[(radius>1.18)&(radius<1.6)]
    if rect and inner.size and np.median(inner)<115 and np.percentile(inner,99)-np.median(inner)>55:return 'system'
    sectors=set();dots=set();dot_count=0
    # Count isolated thin strokes pointing towards the centre and small dots
    # spread around the perimeter; a single cluster of glyphs is insufficient.
    n,_,stats,centers=cv2.connectedComponentsWithStats(edges)
    for k in range(1,n):
        x,y,w,h,pixels=stats[k];px,py=centers[k]
        if not .72<radius[int(py),int(px)]<1.65:continue
        sector=int((np.arctan2(py-cy,px-cx)+np.pi)*8/np.pi)%16
        if 2<=pixels<=45 and max(w,h)<=max(7,min(bw,bh)*.12):
            dots.add(sector);dot_count+=1
    lines=cv2.HoughLinesP(edges,1,np.pi/180,threshold=10,minLineLength=max(8,int(min(bw,bh)*.12)),maxLineGap=2)
    for xa,ya,xb,yb in ([] if lines is None else lines[:,0]):
        mx,my=(xa+xb)/2,(ya+yb)/2
        if not .70<radius[int(my),int(mx)]<1.65:continue
        dx,dy=xb-xa,yb-ya
        if abs(dx*(mx-cx)+dy*(my-cy))>.93*np.hypot(dx,dy)*np.hypot(mx-cx,my-cy):
            sectors.add(int((np.arctan2(my-cy,mx-cx)+np.pi)*8/np.pi)%16)
    heights=[r['box'][3]-r['box'][1] for r in bubble.lines if r.get('box') and r.get('engine')!='MTP']
    if refresh:
        # Detached radial ink strokes distinguish a thought halo from the
        # connected zigzag outline of a shout, even if Canny joins their tips.
        if len(sectors)>=8:
            strokes,_=cv2.findContours((gray<128).astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            ray_sectors=set()
            for stroke in strokes:
                (px,py),(w,h),angle=cv2.minAreaRect(stroke)
                if not .65<radius[int(py),int(px)]<1.8 or max(w,h)<8 or max(w,h)<3*min(w,h):continue
                theta=np.deg2rad(angle+(90 if h>w else 0))
                dx,dy=px-cx,py-cy
                if abs(dx*np.cos(theta)+dy*np.sin(theta))>.85*np.hypot(dx,dy):
                    ray_sectors.add(int((np.arctan2(dy,dx)+np.pi)*8/np.pi)%16)
            if len(ray_sectors)>=8:return 'thought'
        if spikes:return 'shout'
        if len(sectors)>=8 or (dot_count>=8 and len(dots)>=6):return 'thought'
        if rect:return 'narration'
    if spikes or len(sectors)>=8 or (re.search(r'!!|!\?|\?!|！！|！？|？！',joined) and heights and
            np.median(heights)>=max(28,image.width*.045,bh*.35)):return 'shout'
    if rect:return 'narration'
    ring=(radius>.70)&(radius<1.45)
    soft=(c is None and inner.size and outer.size and np.median(inner)>np.median(outer)+22 and np.any(ring)
          and np.mean(edges[ring]>0)<.012 and np.ptp(np.percentile(gray[ring],[20,80]))>12)
    if any(re.fullmatch(r'\(.+\)|（.+）',v,re.S) for v in texts) or (dot_count>=8 and len(dots)>=6) or soft:return 'thought'
    return 'normal' if speech or c is not None else 'floating'


def bubble_seg_model_path():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'ManhwaOCREditor'/'seg_models'/'yolov8m_speech_bubble_seg.onnx'


class BubbleSegmenter:
    """YOLOv8m-seg ONNX speech-bubble interior segmenter (Priority 2)."""
    def __init__(self):
        self._lock=threading.Lock()
        self._session=None
        self._signature=None
        self._input_name=None
        self._nano_model=None

    def check(self,force=False):
        path=bubble_seg_model_path()
        nano=path.with_name('manga109-640.ts')
        if nano.is_file():
            try:
                import torch
                with self._lock,torch.inference_mode():
                    if force or self._nano_model is None:
                        self._nano_device='cuda' if torch.cuda.is_available() else 'cpu'
                        self._nano_model=torch.jit.load(str(nano),map_location=self._nano_device).eval()
                        self._nano_model(torch.zeros(1,3,640,640,device=self._nano_device))
                return {'ready':True,'path':str(nano),'message':f'YOLO11n Manga109 พร้อมใช้งาน ({self._nano_device})'}
            except (ImportError,RuntimeError,OSError) as exc:
                return {'ready':False,'path':str(nano),'message':f'YOLO11n: {exc}'}
        try:
            st=path.stat();sig=(str(path),st.st_size,st.st_mtime_ns)
        except OSError:
            return {'ready':False,'path':str(path),'message':'AI จับทรงบับเบิ้ล (ONNX): ยังไม่ได้โหลดโมเดล — ใช้โหมดเรขาคณิตอัตโนมัติ (กดโหลดได้ในเมนูเพิ่มเติม)'}
        with self._lock:
            if not force and self._session is not None and self._signature==sig:
                return {'ready':True,'path':str(path),'message':'AI จับทรงบับเบิ้ล (YOLOv8m-seg ONNX): พร้อมใช้งาน'}
            self._session=None
            try:
                import onnxruntime as ort
                opts=ort.SessionOptions()
                opts.intra_op_num_threads=max(1,min(4,os.cpu_count() or 2))
                opts.inter_op_num_threads=1
                sess=ort.InferenceSession(str(path),sess_options=opts,providers=['CPUExecutionProvider'])
                inp=sess.get_inputs()[0].name
                dummy=np.zeros((1,3,160,160),dtype=np.float32)
                outs=sess.run(None,{inp:dummy})
                if len(outs)<2:raise RuntimeError('ONNX output ไม่ใช่โมเดล Segmentation')
                self._session=sess;self._input_name=inp;self._signature=sig
                return {'ready':True,'path':str(path),'message':'AI จับทรงบับเบิ้ล (YOLOv8m-seg ONNX): พร้อมใช้งาน — ผ่านการทดสอบ'}
            except Exception as exc:
                return {'ready':False,'path':str(path),'message':f'AI จับทรงบับเบิ้ล โหลดไม่สำเร็จ: {exc}'}

    def download(self,progress=lambda msg:None,cancel=None):
        import requests
        path=bubble_seg_model_path()
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_suffix('.download')
        progress('กำลังดาวน์โหลดโมเดล AI จับทรงบับเบิ้ล YOLOv8m-seg ONNX (~104 MB)…')
        try:
            with requests.get(BUBBLE_SEG_URL,stream=True,timeout=90) as resp:
                resp.raise_for_status()
                total=int(resp.headers.get('content-length',0))
                done=0;last_pct=-1
                with tmp.open('wb') as f:
                    for chunk in resp.iter_content(chunk_size=1024*512):
                        if cancel is not None and cancel.is_set():
                            raise InterruptedError('ยกเลิกการดาวน์โหลดโมเดลแล้ว')
                        if not chunk:continue
                        f.write(chunk);done+=len(chunk)
                        if total>0:
                            pct=int(done*100/total)
                            if pct>=last_pct+10:
                                last_pct=pct
                                progress(f'กำลังดาวน์โหลด AI จับทรงบับเบิ้ล… {pct}% ({done//(1024*1024)}/{total//(1024*1024)} MB)')
            if tmp.stat().st_size<5_000_000:
                raise RuntimeError('ไฟล์โมเดลที่ดาวน์โหลดมีขนาดผิดปกติ')
            os.replace(tmp,path)
        finally:
            tmp.unlink(missing_ok=True)
        return self.check(force=True)

    def segment_bubble(self,image,bubble_box,margin=6):
        if os.environ.get('MANHWA_DISABLE_BUBBLE_SEG')=='1':
            return None
        nano_path=bubble_seg_model_path().with_name('manga109-640.ts')
        use_nano=nano_path.is_file()
        if not use_nano and self._session is None and not self.check()['ready']:
            return None
        bw=max(8.0,float(bubble_box[2]-bubble_box[0]))
        bh=max(8.0,float(bubble_box[3]-bubble_box[1]))
        if bw>2400 or bh>2400:return None
        base_pad=max(256,min(512,int(round(max(bw,bh)*.85)))) if use_nano else max(96,min(280,int(round(max(bw,bh)*.85))))
        wide_pad=min(720,max(base_pad*2,int(round(max(bw,bh)*1.6)),min(image.width,560)))
        pads=(base_pad,wide_pad) if wide_pad>base_pad else (base_pad,)

        for attempt,pad in enumerate(pads):
            cbox=bounded_box(bubble_box,image.size,pad)
            cw,ch=cbox[2]-cbox[0],cbox[3]-cbox[1]
            if cw<24 or ch<24:return None
            crop_rgb=np.asarray(image.crop(cbox).convert('RGB'))
            imgsz=640
            scale=min(float(imgsz)/ch,float(imgsz)/cw)
            nw,nh=max(1,int(round(cw*scale))),max(1,int(round(ch*scale)))
            resized=cv2.resize(crop_rgb,(nw,nh),interpolation=cv2.INTER_LINEAR)
            canvas=np.full((imgsz,imgsz,3),114,dtype=np.uint8)
            px=(imgsz-nw)//2;py=(imgsz-nh)//2
            canvas[py:py+nh,px:px+nw]=resized
            blob=(canvas.astype(np.float32)/255.0).transpose(2,0,1)[None,...]
            try:
                with self._lock:
                    if use_nano:
                        import torch
                        if self._nano_model is None:
                            self._nano_device='cuda' if torch.cuda.is_available() else 'cpu'
                            self._nano_model=torch.jit.load(str(nano_path),map_location=self._nano_device).eval()
                        with torch.inference_mode():
                            outputs=self._nano_model(torch.from_numpy(blob).to(self._nano_device))
                            outs=[output.cpu().numpy() for output in outputs]
                    else:
                        outs=self._session.run(None,{self._input_name:blob})
            except Exception:
                return None
            out0=outs[0][0]
            pred=out0.T if out0.shape[0]<out0.shape[1] else out0
            proto=outs[1][0]
            nm=proto.shape[0]
            nc=pred.shape[1]-4-nm
            if nc<1 or pred.shape[0]==0:continue
            scores=pred[:,4:4+nc].max(axis=1)
            keep=np.flatnonzero(scores>=0.28)
            if not len(keep):continue
            pred=pred[keep];scores=scores[keep]
            cx_p,cy_p,w_p,h_p=pred[:,0],pred[:,1],pred[:,2],pred[:,3]
            bx0,by0,bx1,by1=cx_p-w_p*.5,cy_p-h_p*.5,cx_p+w_p*.5,cy_p+h_p*.5
            rx0=np.clip((bx0-px)/scale,0,cw);ry0=np.clip((by0-py)/scale,0,ch)
            rx1=np.clip((bx1-px)/scale,0,cw);ry1=np.clip((by1-py)/scale,0,ch)
            lx0,ly0=bubble_box[0]-cbox[0],bubble_box[1]-cbox[1]
            lx1,ly1=bubble_box[2]-cbox[0],bubble_box[3]-cbox[1]
            lcx,lcy=(lx0+lx1)*.5,(ly0+ly1)*.5
            l_area=max(1.0,(lx1-lx0)*(ly1-ly0))
            best_idx=-1;best_rank=-1.0
            for i in range(len(scores)):
                ca=max(1.0,float((rx1[i]-rx0[i])*(ry1[i]-ry0[i])))
                if ca>cw*ch*.97 or ca<l_area*.30:continue
                ix0=max(lx0,float(rx0[i]));iy0=max(ly0,float(ry0[i]))
                ix1=min(lx1,float(rx1[i]));iy1=min(ly1,float(ry1[i]))
                inter=max(0.0,ix1-ix0)*max(0.0,iy1-iy0)
                ov=inter/max(1.0,min(ca,l_area))
                has_center=(rx0[i]<=lcx<=rx1[i] and ry0[i]<=lcy<=ry1[i])
                if ov<0.30 and not (has_center and ov>=0.15):continue
                size_pen=1.0/(1.0+abs(np.log(max(0.2,ca/l_area)))*.35)
                rank=float(scores[i])*(0.55+0.45*ov)*size_pen*(1.15 if has_center else 0.85)
                if rank>best_rank:best_rank=rank;best_idx=i
            if best_idx<0:continue
            coeff=pred[best_idx,4+nc:4+nc+nm]
            mh,mw=proto.shape[1],proto.shape[2]
            logits=np.tensordot(coeff,proto,axes=([0],[0]))
            sx=mw/float(imgsz);sy=mh/float(imgsz)
            mx0=max(0,int(np.floor(bx0[best_idx]*sx))-1);my0=max(0,int(np.floor(by0[best_idx]*sy))-1)
            mx1=min(mw,int(np.ceil(bx1[best_idx]*sx))+1);my1=min(mh,int(np.ceil(by1[best_idx]*sy))+1)
            cropped_logits=np.full_like(logits,-10.0)
            cropped_logits[my0:my1,mx0:mx1]=logits[my0:my1,mx0:mx1]
            up_640=cv2.resize(cropped_logits,(imgsz,imgsz),interpolation=cv2.INTER_LINEAR)
            sub=up_640[py:py+nh,px:px+nw]
            prob_crop=cv2.resize(sub,(cw,ch),interpolation=cv2.INTER_LINEAR)
            raw_mask=(prob_crop>0.0).astype(np.uint8)*255
            raw_mask=cv2.GaussianBlur(raw_mask,(7,7),1.8)
            raw_mask=(raw_mask>=128).astype(np.uint8)*255
            contours,_=cv2.findContours(raw_mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            if not contours:continue
            filled=np.zeros((ch,cw),np.uint8)
            main_c=max(contours,key=cv2.contourArea)
            if cv2.contourArea(main_c)<l_area*.28:continue
            cv2.drawContours(filled,[main_c],-1,255,cv2.FILLED)
            # Remove thin protrusions before finding the centre, then inset along the contour.
            distance=cv2.distanceTransform(filled,cv2.DIST_L2,5)
            radius=int(np.clip(distance.max()*.28,2,28))
            kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1))
            filled=cv2.bitwise_and(filled,cv2.morphologyEx(filled,cv2.MORPH_OPEN,kernel))
            if not filled[int(np.clip(lcy,0,ch-1)),int(np.clip(lcx,0,cw-1))]:continue
            interior_clipped=(
                (cbox[1]>0 and np.any(filled[0]))
                or (cbox[3]<image.height and np.any(filled[-1]))
                or (cbox[0]>0 and np.any(filled[:,0]))
                or (cbox[2]<image.width and np.any(filled[:,-1]))
            )
            if interior_clipped:
                continue
            erode_r=max(3,int(margin),round(distance.max()*.07))
            safe=cv2.erode(filled,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*erode_r+1,2*erode_r+1)),
                           borderType=cv2.BORDER_CONSTANT,borderValue=0)
            if np.count_nonzero(safe)<l_area*.22:continue
            return cbox,safe
        return None


BUBBLE_SEGMENTER=BubbleSegmenter()


@dataclass(frozen=True)
class FontInfo:
    choice: str
    path: str
    family: str
    postscript: str
    weight: int
    italic: int
    cmap: frozenset


def _slug(value):
    return re.sub(r'[^a-z0-9]+','',(value or '').lower())


def _normalize_offset(offset):
    if not offset or not isinstance(offset,(list,tuple)) or len(offset)!=2:
        return []
    dx,dy=round(float(offset[0]),2),round(float(offset[1]),2)
    if not (np.isfinite(dx) and np.isfinite(dy)):
        return []
    return [dx,dy] if (abs(dx)>=0.5 or abs(dy)>=0.5) else []


def normalize_font_choice(choice):
    if not choice:return FONT_NAME
    text=str(choice).strip()
    if text in FONT_CHOICES:return text
    slug=_slug(Path(text).stem if text.lower().endswith(('.ttf','.otf')) else text)
    if slug in ('mnkrasip','mnkrasipregular','mnkrasipbold','krasip'):
        return 'MN KRASIP'
    alias_map={
        'thsarabunnewbold':'TH Sarabun New Bold',
        'thsarabunnew':'TH Sarabun New Bold',
        'fcmuffin':'FC Muffin',
        'fcmuffinregular':'FC Muffin',
        'fcmuffinbold':'FC Muffin',
        'itim':'Itim',
        'itimregular':'Itim',
        'tfmaniwika':'TF MANIWIKA',
        'tfmanawika':'TF MANIWIKA',
        'jstoomtam':'JS Toomtam',
        'edmuzazhi':'EDMuzazhi',
        'tflanna':'TF LANNA',
        'tflannabold':'TF LANNA',
        'tfphetai':'TF PHETAI',
        'tfphethai':'TF PHETAI',
        'tfphethaibold':'TF PHETAI',
        'tfphethaiitalic':'TF Phethai Italic',
        'tfphetaiitalic':'TF Phethai Italic',
        'iannnnntiger':'iannnnn-TIGER',
        '2005iannnnntiger':'iannnnn-TIGER',
        'layijitamaitine1':'Layiji TaMaiTine1',
        'layijitamaitine':'Layiji TaMaiTine1',
        'tamaitine1':'Layiji TaMaiTine1',
        'layijijarakefadhang':'Layiji JaRaKeFadHang',
        'jarakefadhang':'Layiji JaRaKeFadHang',
        'thbaijambold':'TH Baijam Bold',
        'thbaijam':'TH Baijam Bold',
        'baijambold':'TH Baijam Bold',
        'irisupcbold':'IrisUPC Bold',
        'irisupc':'IrisUPC Bold',
        'upcib':'IrisUPC Bold',
    }
    return alias_map.get(slug,FONT_NAME)


def _font_roots():
    roots=[]
    local_assets=Path(__file__).resolve().parent/'assets'/'fonts'
    if local_assets.is_dir():roots.append(local_assets)
    user_fonts=Path(os.environ.get('LOCALAPPDATA',''))/'Microsoft/Windows/Fonts'
    if user_fonts.is_dir():roots.append(user_fonts)
    win_fonts=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    if win_fonts.is_dir():roots.append(win_fonts)
    return roots


def _read_font_entry(path):
    from fontTools.ttLib import TTFont
    with TTFont(path,fontNumber=0) as font:
        names=font['name']
        family=(names.getDebugName(16) or names.getDebugName(1) or path.stem).strip()
        gdi_family=(names.getDebugName(1) or family).strip()
        sub=(names.getDebugName(17) or names.getDebugName(2) or '').strip()
        full=(names.getDebugName(4) or '').strip()
        ps=(names.getDebugName(6) or _slug(full or family)).strip()
        cmap=frozenset(font.getBestCmap() or ())
    sub_all=f'{sub} {full} {ps}'.lower()
    is_italic=int('italic' in sub_all or 'oblique' in sub_all)
    is_bold=int('bold' in sub_all)
    is_thin=int('thin' in sub_all or 'light' in sub_all)
    is_black=int('black' in sub_all or 'heavy' in sub_all)
    weight=700 if (is_bold or is_black) else (300 if is_thin else 400)
    hay=' '.join([_slug(family),_slug(gdi_family),_slug(full),_slug(ps),_slug(path.stem)])
    return {'path':str(path),'family':gdi_family,'sub':sub,'full':full,'ps':ps,
            'weight':weight,'italic':is_italic,'bold':is_bold,'thin':is_thin,'black':is_black,
            'hay':hay,'cmap':cmap}


@lru_cache(maxsize=1)
def _font_catalog(fast_only=True):
    try:import fontTools.ttLib  # noqa: F401
    except ImportError as exc:raise RuntimeError('รัน install_text_psd_windows.bat ก่อนใช้ Text') from exc
    hints=('sarab','muffin','itim','mani','mana','toom','muza','lanna','phet',
           'krasip','mn','tiger','ian','layiji','tamai','jarake','baijam','iris','upc','fc','tf','js','ed','th')
    paths=[];seen=set()
    env_font=os.environ.get('MANHWA_TEXT_FONT')
    if env_font and Path(env_font).is_file():
        p=Path(env_font).resolve();paths.append(p);seen.add(p)
    for root in _font_roots():
        for p in root.iterdir():
            if not p.is_file() or p.suffix.lower() not in ('.ttf','.otf'):continue
            low=p.stem.lower()
            if fast_only and root.name.lower()=='fonts' and not any(h in low for h in hints):continue
            rp=p.resolve()
            if rp not in seen:seen.add(rp);paths.append(p)
    records=[]
    for p in paths:
        try:
            if p.stat().st_size>15_000_000:continue
            records.append(_read_font_entry(p))
        except Exception:continue
    return records


def _match_entry(choice,records):
    choice=normalize_font_choice(choice)
    if choice=='MN KRASIP':
        matches=(r for r in records if 'krasip' in r['hay'])
        return min(matches,key=lambda r:(r['italic'],r['bold']),default=None)
    if choice=='TH Sarabun New Bold':
        exact=[r for r in records if r['ps']==FONT_POSTSCRIPT]
        if exact:return exact[0]
        cand=[r for r in records if 'thsarabunnew' in r['hay'] and r['bold'] and not r['italic']]
        return cand[0] if cand else None
    if choice=='TH Baijam Bold':
        exact=[r for r in records if r['ps'].lower()=='thbaijam-bold']
        if exact:return exact[0]
        bold_cand=[r for r in records if ('thbaijam' in r['hay'] or 'baijam' in r['hay']) and r['bold'] and not r['italic']]
        if bold_cand:return bold_cand[0]
        reg_cand=[r for r in records if ('thbaijam' in r['hay'] or 'baijam' in r['hay']) and not r['italic']]
        return reg_cand[0] if reg_cand else None
    if choice=='IrisUPC Bold':
        exact=[r for r in records if r['ps'].lower() in ('irisupcbold','irisupc-bold') and not r['italic']]
        if exact:return exact[0]
        bold_cand=[r for r in records if ('irisupc' in r['hay'] or 'upcib' in r['hay']) and r['bold'] and not r['italic']]
        if bold_cand:return bold_cand[0]
        reg_cand=[r for r in records if 'irisupc' in r['hay'] and not r['italic']]
        return reg_cand[0] if reg_cand else None
    if choice=='iannnnn-TIGER':
        cand=[]
        for r in records:
            if ('iannnnntiger' in r['hay'] or ('iannnnn' in r['hay'] and 'tiger' in r['hay'])) and not r['italic']:
                style_rank=0 if (not r['bold'] and not r['thin'] and not r['black']) else (1 if r['bold'] else (2 if r['black'] else 3))
                cand.append((style_rank,r))
        cand.sort(key=lambda x:x[0])
        return cand[0][1] if cand else None
    if choice=='Layiji TaMaiTine1':
        cand=[]
        for r in records:
            if any(k in r['hay'] for k in ('layijitamaitine1','tamaitine1','layijitamaitine','tamaitine','layijitamaitai','tamaitai')):
                has_one=0 if '1' in r['hay'] else 1
                is_shad=1 if 'shad' in r['hay'] else 0
                cand.append(((is_shad,has_one,r['italic']),r))
        cand.sort(key=lambda x:x[0])
        return cand[0][1] if cand else None
    if choice=='Layiji JaRaKeFadHang':
        cand=[r for r in records if any(k in r['hay'] for k in ('layijijarakefadhang','jarakefadhang','jarake'))]
        return cand[0] if cand else None
    rules={
        'FC Muffin':(('fcmuffin','muffin'),False),
        'Itim':(('itim',),False),
        'TF MANIWIKA':(('tfmaniwika','tfmanawika','maniwika','manawika'),False),
        'JS Toomtam':(('jstoomtam','toomtam'),False),
        'EDMuzazhi':(('edmuzazhi','muzazhi'),False),
        'TF LANNA':(('tflanna',),False),
        'TF PHETAI':(('tfphethai','tfphetai','phethai','phetai'),False),
        'TF Phethai Italic':(('tfphethai','tfphetai','phethai','phetai'),True),
    }
    tokens,want_italic=rules[choice]
    matches=[]
    for r in records:
        if not any(tok in r['hay'] for tok in tokens):continue
        if want_italic and not r['italic']:continue
        if not want_italic and r['italic']:continue
        rank=(0 if r['hay'].startswith(tokens[0]) else 1,r['bold'])
        matches.append((rank,r))
    if not matches and choice=='TF LANNA':
        for r in records:
            if 'lanna' in r['hay'] and not r['italic']:
                matches.append(((1,r['bold']),r))
    matches.sort(key=lambda item:item[0])
    return matches[0][1] if matches else None


@lru_cache(maxsize=32)
def resolve_font(choice=FONT_NAME):
    canonical=normalize_font_choice(choice)
    entry=_match_entry(canonical,_font_catalog(True))
    if entry is None:
        _font_catalog.cache_clear()
        entry=_match_entry(canonical,_font_catalog(False))
    if entry is None:
        if canonical==FONT_NAME:
            raise RuntimeError('ไม่พบ TH Sarabun New Bold กรุณาติดตั้งฟอนต์นี้แล้วเปิดโปรแกรมใหม่ (ไม่ใช้ฟอนต์อื่นแทน)')
        raise RuntimeError(f'ไม่พบฟอนต์ "{canonical}" ในเครื่อง กรุณาติดตั้งไฟล์ .ttf/.otf ลงใน Windows (หรือวางไว้ในโฟลเดอร์ assets/fonts) แล้วลองอีกครั้ง')
    return FontInfo(canonical,entry['path'],entry['family'],entry['ps'],entry['weight'],entry['italic'],entry['cmap'])


@lru_cache(maxsize=1)
def find_font():
    info=resolve_font(FONT_NAME)
    return info.path,info.cmap


@lru_cache(maxsize=32)
def _cached_body_ratio(path,family,weight,italic):
    try:
        from windows_text import TextRenderer
        with TextRenderer(48,path,family,weight,italic) as renderer:
            tile=renderer.render(['กขคงจฉชดตบปอ'])
        return float(np.clip(tile.height/48.0,0.28,0.78))
    except Exception:
        return 0.42


def _visual_body_ratio(info):
    return _cached_body_ratio(info.path,info.family,info.weight,info.italic)


def _source_layout_guide(bubble,bw,bh):
    raw_lines=getattr(bubble,'lines',None) or []
    if not raw_lines:
        return None
    bx0,by0,bx1,by1=[float(v) for v in bubble.box]
    valid_boxes=[];heights=[]
    for rec in raw_lines:
        b=rec.get('box') if isinstance(rec,dict) else getattr(rec,'box',None)
        txt=str((rec.get('text','') if isinstance(rec,dict) else getattr(rec,'text','')) or '').strip()
        if not b or len(b)!=4 or not txt:
            continue
        lw=float(b[2]-b[0]);lh=float(b[3]-b[1])
        if lw<6.0 or lh<6.0 or lh>bh*0.82 or lw>bw*1.15:
            continue
        cx=(b[0]+b[2])*0.5;cy=(b[1]+b[3])*0.5
        if not (bx0-12.0<=cx<=bx1+12.0 and by0-12.0<=cy<=by1+12.0):
            continue
        valid_boxes.append(b)
        heights.append(lh if lw>=lh*0.75 else lw)
    if not valid_boxes or not heights:
        return None
    ux0=min(float(b[0]) for b in valid_boxes);uy0=min(float(b[1]) for b in valid_boxes)
    ux1=max(float(b[2]) for b in valid_boxes);uy1=max(float(b[3]) for b in valid_boxes)
    return {
        'orig_w':max(16.0,ux1-ux0),
        'orig_h':max(14.0,uy1-uy0),
        'med_lh':float(np.median(heights)),
        'line_count':len(valid_boxes),
    }


def _adapt_punctuation(text,cmap):
    """Sanitize punctuation/symbols for legacy Thai fonts (`TF LANNA`, `Layiji`, etc.) without failing."""
    out=[]
    for ch in text:
        if ch=='\n':
            out.append('\n');continue
        if ch.isspace():
            out.append(' ');continue
        if ord(ch) in cmap:
            out.append(ch);continue
        rep=_PUNCT_FALLBACKS.get(ch)
        if rep is not None:
            if rep=='' or all(ord(c) in cmap or c.isspace() for c in rep):
                out.append(rep);continue
            ch=rep[0] if len(rep)==1 else rep
            if len(ch)>1:
                kept=''.join(c for c in ch if ord(c) in cmap or c.isspace())
                if kept:out.append(kept);continue
        sec=_SECONDARY_PUNCT.get(ch)
        if sec is not None and all(ord(c) in cmap or c.isspace() for c in sec):
            out.append(sec);continue
        if not ch.isalnum() and not ('\u0e00'<=ch<='\u0e7f'):
            if ord('.') in cmap and ch in ('-','—','–','…','~'):
                out.append('.')
            elif ord(' ') in cmap:
                out.append(' ')
            continue
        out.append(ch)
    cleaned='\n'.join(re.sub(r' {2,}',' ',ln).strip() for ln in ''.join(out).split('\n'))
    return cleaned.strip() or text.strip()


def _prepare_render_text_and_font(raw_text,chosen):
    """Adapt text to `chosen` font; if legacy font lacks core letters/digits, fall back to `FONT_NAME`."""
    info=resolve_font(chosen)
    adapted=_adapt_punctuation(raw_text,info.cmap)
    missing=[ch for ch in adapted if not ch.isspace() and ord(ch) not in info.cmap]
    if missing and chosen!=FONT_NAME:
        fb_info=resolve_font(FONT_NAME)
        fb_adapted=_adapt_punctuation(raw_text,fb_info.cmap)
        if not any(ord(ch) not in fb_info.cmap for ch in fb_adapted if not ch.isspace()):
            return fb_adapted,fb_info,FONT_NAME
    if missing:
        stripped=''.join((ch if (ch.isspace() or ord(ch) in info.cmap) else ' ') for ch in adapted)
        stripped='\n'.join(re.sub(r' {2,}',' ',ln).strip() for ln in stripped.split('\n')).strip()
        if stripped:
            return stripped,info,chosen
        raise ValueError(f'ฟอนต์ {chosen} ไม่รองรับข้อความนี้')
    return adapted,info,chosen


def _clamp_box_in_page(box,page_size):
    W,H=page_size
    x0,y0,x1,y1=[float(v) for v in box]
    sw=min(float(W),max(8.0,x1-x0))
    sh=min(float(H),max(8.0,y1-y0))
    nx0=max(0.0,min(float(W)-sw,x0))
    ny0=max(0.0,min(float(H)-sh,y0))
    return [int(round(nx0)),int(round(ny0)),min(int(W),int(round(nx0+sw))),min(int(H),int(round(ny0+sh)))]


def _shift_box_preserving_span(box,dx,dy,page_size,as_int=False):
    W,H=page_size
    x0,y0,x1,y1=[float(v) for v in box]
    sw=x1-x0;sh=y1-y0
    if sw<=W and sh<=H:
        nx0=max(0.0,min(float(W)-sw,x0+float(dx)))
        ny0=max(0.0,min(float(H)-sh,y0+float(dy)))
        res=[nx0,ny0,nx0+sw,ny0+sh]
    else:
        res=[float(v) for v in _clamp_box_in_page([x0+dx,y0+dy,x1+dx,y1+dy],page_size)]
    return [int(round(v)) for v in res] if as_int else res


def _sample_text_color(image,box):
    sample_box=_clamp_box_in_page(box,image.size)
    pixels=np.asarray(image.crop(sample_box).convert('RGB'),dtype=float)/255
    linear=np.where(pixels<=.04045,pixels/12.92,((pixels+.055)/1.055)**2.4)
    luminance=float(np.median(linear@np.array([.2126,.7152,.0722])))
    return [255,255,255] if luminance<.179 else [0,0,0]


def _apply_angle_to_layer(image,layer,angle,custom_rot=None):
    """Update `layer['box']`, `text_size`, and `rotation` around its center `(cx, cy)`."""
    old_b=layer['box']
    cx=(float(old_b[0])+float(old_b[2]))*0.5
    cy=(float(old_b[1])+float(old_b[3]))*0.5
    base_size=layer.get('text_size')
    if isinstance(base_size,list) and len(base_size)==2 and base_size[0]>0 and base_size[1]>0:
        tw,th=float(base_size[0]),float(base_size[1])
    else:
        tile=rendered_tile(layer['text'],layer['font_size'],layer.get('font_choice',FONT_NAME))
        tw=max(12.0,float(tile.width+6));th=max(12.0,float(tile.height+6))

    ang=float(angle or 0.0)
    if abs(ang)>=0.1:
        r=np.radians(ang)
        rw=abs(tw*np.cos(r))+abs(th*np.sin(r))
        rh=abs(tw*np.sin(r))+abs(th*np.cos(r))
        bounds=_shift_box_preserving_span([cx-rw/2.0,cy-rh/2.0,cx+rw/2.0,cy+rh/2.0],0.0,0.0,image.size,as_int=False)
        layer['box']=bounds
        layer['text_size']=[tw,th]
        layer['rotation']=round(ang,2)
        layer['color']=_sample_text_color(image,bounds)
    else:
        box=_clamp_box_in_page([cx-tw/2.0,cy-th/2.0,cx+tw/2.0,cy+th/2.0],image.size)
        layer['box']=box
        layer.pop('text_size',None)
        layer.pop('rotation',None)
        layer['color']=_sample_text_color(image,box)

    if custom_rot is not None:
        layer['custom_rotation']=round(float(custom_rot),2)
    else:
        layer.pop('custom_rotation',None)
    return layer


def rotate_text_layer(image,bubble,new_angle):
    """Fast O(1) live rotation of a bubble's text layer to `new_angle` degrees."""
    c_rot=_normalize_rotation(new_angle)
    bubble.custom_rotation=c_rot
    layer=getattr(bubble,'text_layer',None)
    if not layer or not layer.get('box'):
        return layout_text(image,bubble)
    eff_angle=c_rot if c_rot is not None else (quad_angle(bubble.quad) if valid_quad(getattr(bubble,'quad',[])) else 0.0)
    updated=dict(layer)
    return _apply_angle_to_layer(image,updated,eff_angle,custom_rot=c_rot)


def fast_direct_layer(image,bubble,translation,font_choice=None,preferred_size=0,center_xy=None,preferred_rot=None):
    """O(1) single-render layer builder for `.mtp` import (avoids slow multi-pass OpenCV on pre-laid chapters)."""
    from windows_text import TextRenderer
    raw_text=(translation or '').strip().replace('\r\n','\n').replace('\r','\n')
    if not raw_text:raise ValueError('กรอบนี้ยังไม่มีคำแปลไทย')
    chosen_req=normalize_font_choice(font_choice or getattr(bubble,'font_choice',None) or FONT_NAME)
    render_text,info,chosen=_prepare_render_text_and_font(raw_text,chosen_req)
    bx0,by0,bx1,by1=[float(v) for v in bubble.box]
    bw=max(24.0,bx1-bx0);bh=max(20.0,by1-by0)
    if preferred_size and preferred_size>=6:
        size=int(np.clip(round(preferred_size),8,140))
    else:
        body_r=_visual_body_ratio(info)
        est_body=max(9.0,min(20.0,min(bw,bh)*0.11))
        size=int(np.clip(round(est_body/body_r),12,64))
    wrap_w=max(36,min(image.width-12,int(round(bw*0.86))))
    with TextRenderer(size,info.path,info.family,info.weight,info.italic) as renderer:
        if '\n' in render_text:
            lines=[ln.strip() for ln in render_text.split('\n') if ln.strip()] or [render_text.strip()]
        else:
            lines=_force_wrap_text(render_text,wrap_w,renderer)
        tile=renderer.render(lines)
    if center_xy is not None and len(center_xy)==2:
        cx,cy=float(center_xy[0]),float(center_xy[1])
    else:
        cx=(bx0+bx1)*0.5;cy=(by0+by1)*0.5
    pad_w=max(12,tile.width+6);pad_h=max(12,tile.height+6)
    box=_clamp_box_in_page([cx-pad_w/2.0,cy-pad_h/2.0,cx+pad_w/2.0,cy+pad_h/2.0],image.size)
    color=_sample_text_color(image,box)
    q=[list(p) for p in getattr(bubble,'quad',[])]
    out={'text':'\n'.join(lines),'translation':raw_text,'box':box,'bubble_box':list(bubble.box),
         'bubble_quad':q,'font':info.postscript,'font_size':size,'color':color,'auto_leading':True,
         'alignment':'center','layout_version':'3.1','paragraph_style':'manga-centred',
         'boundary_found':True,'needs_review':False,**CHARACTER_STYLE}
    if chosen!=FONT_NAME:out['font_choice']=chosen
    c_rot=_normalize_rotation(preferred_rot if preferred_rot is not None else getattr(bubble,'custom_rotation',None))
    if c_rot is not None:
        bubble.custom_rotation=c_rot
        out['text_size']=[float(pad_w),float(pad_h)]
        _apply_angle_to_layer(image,out,c_rot,custom_rot=c_rot)
    elif valid_quad(q) and abs(quad_angle(q))>=0.1:
        out['text_size']=[float(pad_w),float(pad_h)]
        _apply_angle_to_layer(image,out,quad_angle(q),custom_rot=None)
    return out


def scale_text_layer(image,bubble,old_layer,sx,sy,shift_y):
    """Instant O(1) proportional scaling & vertical slice shift of an existing `text_layer` for SmartStitch."""
    raw_text=str(old_layer.get('translation') or bubble.translation or '').strip().replace('\r\n','\n').replace('\r','\n')
    if not raw_text:return {}
    chosen_req=normalize_font_choice(old_layer.get('font_choice') or getattr(bubble,'font_choice',None) or FONT_NAME)
    laid_text=str(old_layer.get('text') or raw_text).strip().replace('\r\n','\n').replace('\r','\n')
    render_text,info,chosen=_prepare_render_text_and_font(laid_text,chosen_req)
    old_sz=float(old_layer.get('font_size',24))
    new_sz=int(np.clip(round(old_sz*min(sx,sy)),6,200))
    tile=rendered_tile(render_text,new_sz,chosen)
    old_b=old_layer.get('box') or bubble.box
    cx=((float(old_b[0])+float(old_b[2]))*0.5)*sx
    cy=((float(old_b[1])+float(old_b[3]))*0.5)*sy+float(shift_y)
    q=[list(p) for p in getattr(bubble,'quad',[])]
    c_rot=_normalize_rotation(getattr(bubble,'custom_rotation',old_layer.get('custom_rotation')))
    angle=c_rot if c_rot is not None else float(old_layer.get('rotation',0) or 0.0)
    pad_w=max(12,tile.width+6);pad_h=max(12,tile.height+6)
    box=_clamp_box_in_page([cx-pad_w/2.0,cy-pad_h/2.0,cx+pad_w/2.0,cy+pad_h/2.0],image.size)
    color=_sample_text_color(image,box)
    out={'text':render_text,'translation':raw_text,'box':box,'bubble_box':list(bubble.box),
         'bubble_quad':q,'font':info.postscript,'font_size':new_sz,'color':color,'auto_leading':True,
         'alignment':'center','layout_version':'3.1','paragraph_style':'manga-centred',
         'boundary_found':bool(old_layer.get('boundary_found',True)),
         'needs_review':bool(old_layer.get('needs_review',False)),**CHARACTER_STYLE}
    if abs(angle)>=0.1 or c_rot is not None:
        out['text_size']=[float(pad_w),float(pad_h)]
        _apply_angle_to_layer(image,out,angle,custom_rot=c_rot)
    if chosen!=FONT_NAME:out['font_choice']=chosen
    c_size=int(getattr(bubble,'custom_size',0) or 0)
    c_width=int(getattr(bubble,'custom_width',0) or 0)
    c_off=_normalize_offset(getattr(bubble,'text_offset',[]))
    if c_size>0:out['custom_size']=c_size
    if c_width>0:out['custom_width']=c_width
    if c_off:out['text_offset']=c_off
    out.update({k:old_layer[k] for k in ('stroke','gradient','manual_effects') if k in old_layer})
    return out


def move_text_layer(image,bubble,delta_xy):
    """Fast O(1) reposition of an existing laid-out text layer by `delta_xy`."""
    cur_off=_normalize_offset(getattr(bubble,'text_offset',[])) or [0.0,0.0]
    new_off=_normalize_offset([cur_off[0]+float(delta_xy[0]),cur_off[1]+float(delta_xy[1])])
    bubble.text_offset=new_off
    layer=getattr(bubble,'text_layer',None)
    if not layer or not layer.get('box'):
        return layout_text(image,bubble)
    old_applied=_normalize_offset(layer.get('text_offset',[])) or [0.0,0.0]
    actual_target=new_off or [0.0,0.0]
    inc_dx=actual_target[0]-old_applied[0]
    inc_dy=actual_target[1]-old_applied[1]
    is_tilted=bool(layer.get('rotation',0))
    old_box=layer['box']
    new_box=_shift_box_preserving_span(old_box,inc_dx,inc_dy,image.size,as_int=not is_tilted)
    real_dx=(new_box[0]-old_box[0])+old_applied[0]
    real_dy=(new_box[1]-old_box[1])+old_applied[1]
    real_off=_normalize_offset([real_dx,real_dy])
    bubble.text_offset=real_off
    updated=dict(layer,box=new_box,color=_sample_text_color(image,new_box))
    if real_off:updated['text_offset']=real_off
    else:updated.pop('text_offset',None)
    return updated


def normalize(value):return ' '.join(value.split())


class TranslationFormatError(ValueError):
    def __init__(self,message,line):
        self.line=line
        super().__init__(f'บรรทัด {line}: {message}')


def translation_template(pages):
    return '\n\n'.join(f'รหัส: {b.id}\nคำต้นฉบับ: {normalize(b.text)}\nคำแปลไทย: {b.translation.strip()}'
                      for p in pages for b in p.bubbles)


def _one_label_typo(a,b):
    if abs(len(a)-len(b))>1:return False
    if len(a)==len(b):return sum(x!=y for x,y in zip(a,b))<=1
    if len(a)>len(b):a,b=b,a
    for i in range(len(a)):
        if a[i]!=b[i]:return a[i:]==b[i+1:]
    return True


def translation_header(line):
    keys={'รหัส':'id','คำต้นฉบับ':'source','คำแปลไทย':'translation'}
    aliases={'id':'id','รหัสกรอบ':'id','ต้นฉบับ':'source','ข้อความต้นฉบับ':'source',
             'original':'source','source':'source','คำแปล':'translation',
             'คำแปลภาษาไทย':'translation','translation':'translation','thai':'translation'}
    label=re.sub(r'^(?:[-*•]\s+|\d+[.)]\s+)','',line)
    label=re.sub(r'^(\*\*|__)([^:：\n]{1,30}?)(\s*[:：])?\1',lambda m:m[2]+(m[3] or ''),label)
    match=re.match(r'^([^:：]{1,30})\s*[:：]\s*(.*)$',label)
    if not match:return None
    name=re.sub(r'[\s\u200b\ufeff]','',match[1]).casefold()
    key=keys.get(name) or aliases.get(name)
    if not key and len(name)>=6:
        matches=[value for title,value in keys.items() if len(title)>=6 and _one_label_typo(name,title)]
        if len(matches)==1:key=matches[0]
    if not key:return None
    canonical=next(title for title,value in keys.items() if value==key)
    return key,match[2],canonical,(canonical!=match[1].strip())


def _text_skeleton(value):
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFKC',value or '').casefold()
                   if c.isalnum() or unicodedata.category(c).startswith('M'))


def _has_thai(value):
    return any('\u0e00'<=ch<='\u0e7f' for ch in (value or ''))


def export_source_script(pages, mode='numbered'):
    if mode == 'id_template':
        return translation_template(pages)
    if mode not in ('numbered', 'plain_lines'):
        raise ValueError('รูปแบบส่งออกไม่ถูกต้อง')
    lines = [normalize(b.text) or '[กรอบว่าง]' for p in pages for b in p.bubbles]
    if mode == 'numbered':
        lines = [f'{i}. {line}' for i, line in enumerate(lines, 1)]
    return '\n'.join(lines)


_LIST_ITEM = re.compile(r'^\s*(?:\[#?(\d+)\]|#(\d+)|([0-9]+)(?:[.)]|(?=\s)))\s*(.*)$')
_INDEX_CELL = re.compile(r'(?:\[#?(\d+)\]|#?(\d+))')


def _align_sources(bubbles, items):
    """Monotone source alignment. items contain (optional number, source, translation)."""
    m, n = len(bubbles), len(items)
    back = np.zeros((m+1,n+1),dtype=np.uint8)
    prev = np.zeros(n+1)
    sources = [_text_skeleton(source) for _, source, _ in items]
    for i, bubble in enumerate(bubbles,1):
        key = _text_skeleton(bubble.text)
        current = np.zeros(n+1)
        for j,(number,source,_) in enumerate(items,1):
            other = sources[j-1]
            if key and other:
                similarity = 1 if key == other else SequenceMatcher(None,key,other).ratio()
            else:
                similarity = float(normalize(bubble.text)==normalize(source))
            position = 1-abs((i-.5)/max(1,m)-(j-.5)/max(1,n))
            score = prev[j-1]+similarity+.15*position+.18*(number==i) if similarity>=.55 else -1
            if score>=max(prev[j],current[j-1]):
                current[j]=score
                back[i,j]=3
            elif prev[j]>=current[j-1]:
                current[j]=prev[j]
                back[i,j]=1
            else:
                current[j]=current[j-1]
                back[i,j]=2
        prev=current
    matches={}
    i,j=m,n
    while i and j:
        op=back[i,j]
        if op==3:
            matches[i-1]=j-1
            i-=1
            j-=1
        elif op==1:
            i-=1
        else:
            j-=1
    return matches


def _labelled_items(rows):
    items=[]
    record={}
    field=None
    for line in rows:
        header=translation_header(line.strip())
        if header:
            field,value=header[:2]
            if field=='source' and record:
                if 'translation' not in record:raise ValueError('รายการขาดคำแปลไทย')
                items.append((None,record['source'],record['translation']))
                record={}
            if field in record or (field=='translation' and 'source' not in record):
                raise ValueError('หัวข้อคำต้นฉบับและคำแปลไทยไม่เข้าคู่')
            record[field]=value
        elif field:
            record[field]+='\n'+line.strip()
        else:
            raise ValueError('พบข้อความก่อนหัวข้อคำต้นฉบับ')
    if 'translation' not in record:raise ValueError('รายการขาดคำแปลไทย')
    items.append((None,record['source'],record['translation']))
    return items


def detect_and_parse_translations(text,pages):
    bubbles=[b for p in pages for b in p.bubbles]
    if len({b.id for b in bubbles})!=len(bubbles):raise ValueError('รหัสกรอบซ้ำในโปรเจกต์')
    rows=[r for r in text.lstrip('\ufeff').splitlines() if r.strip()]
    if len(rows)>=2 and re.fullmatch(r'```(?:txt|text|plaintext)?',rows[0].strip(),re.I) and rows[-1].strip()=='```':
        rows=rows[1:-1]
    if not rows:raise ValueError('ยังไม่มีคำแปลให้นำเข้า')
    headers=[(translation_header(r.strip()) or (None,))[0] for r in rows]
    notes=[]
    changes={}
    items=[]
    extra=0
    if 'id' in headers:
        fmt='id_template'
        changes=_parse_id_template(text,pages,notes)
    elif sum('\t' in r for r in rows)>.6*len(rows):
        fmt='tsv'
        for row in rows:
            cells=[c.strip() for c in row.split('\t')]
            if len(cells)<2:
                extra+=1
                continue
            num=_INDEX_CELL.fullmatch(cells[0])
            index=int(next(g for g in num.groups() if g)) if num else None
            if len(cells)>=3:
                items.append((index,cells[1], '\t'.join(cells[2:])))
            elif num or not cells[0]:
                items.append((index if num else len(items)+1,None,cells[1]))
            else:
                items.append((None,cells[0],cells[1]))
    elif _LIST_ITEM.match(rows[0]):
        fmt='numbered'
        groups={}
        for row in rows:
            match=_LIST_ITEM.match(row)
            if match:
                number=int(next(x for x in match.groups()[:3] if x))
                groups.setdefault(number,[]).append(match[4].strip())
            else:
                groups[number][-1]+='\n'+row.strip()
        for number,parts in sorted(groups.items()):
            if len(parts)==1:
                items.append((number,None,parts[0]))
            elif len(parts)==2:
                if _has_thai(parts[0]) and _has_thai(parts[1]):
                    raise ValueError(f'พบคำแปลสองรายการสำหรับเลขข้อ {number}')
                items.append((number,parts[0],parts[1]))
            else:raise ValueError(f'เลขข้อ {number} ซ้ำ กรุณาตรวจไฟล์')
    elif 'source' in headers or 'translation' in headers:
        fmt='labelled_pairs'
        items=_labelled_items(rows)
    elif len(rows)%2==0 and all(not _has_thai(a) and _has_thai(b) for a,b in zip(rows[::2],rows[1::2])):
        fmt='alternating_pairs'
        items=[(None,a,b) for a,b in zip(rows[::2],rows[1::2])]
    else:
        fmt='plain_lines'
        items=[(i,None,re.sub(r'\s*(?://|\|)\s*','\n',row.strip())) for i,row in enumerate(rows,1)]
        if len(rows)!=len(bubbles):
            notes.append(f'มีคำแปล {len(rows)} บรรทัด แต่โปรเจกต์มี {len(bubbles)} กรอบ โปรดตรวจลำดับก่อนยืนยัน')
    paired=[]
    for index,source,value in items:
        if source is not None:
            paired.append((index,source,value))
        elif index is not None and 1<=index<=len(bubbles) and bubbles[index-1].id not in changes:
            if value.strip():changes[bubbles[index-1].id]=value.strip()
        else:
            extra+=1
    if paired:
        free=[i for i,b in enumerate(bubbles) if b.id not in changes]
        matches=_align_sources([bubbles[i] for i in free],paired)
        extra+=len(paired)-len(matches)
        for i,j in matches.items():
            value=paired[j][2].strip()
            if value:changes[bubbles[free[i]].id]=value
    tags={}
    for bid,value in changes.items():
        category,changes[bid]=extract_frame_tag(value)
        if category:tags[bid]=category
    samples=[(i,b.text,changes[b.id]) for i,b in enumerate(bubbles,1) if b.id in changes][:5]
    if extra:notes.append(f'มี {extra} รายการที่จับคู่ไม่ได้')
    return changes,dict(detected_format=fmt,matched_count=len(changes),total_bubbles=len(bubbles),
                        unmatched_bubbles=len(bubbles)-len(changes),extra_items=extra,frame_tags=tags,samples=samples,warnings=notes)


def parse_translations(text,pages,notices=None):
    changes,report=detect_and_parse_translations(text,pages)
    # Preserve the legacy API's ambiguity/error policy for a single unnumbered pair.
    if report['detected_format']=='labelled_pairs' and sum(
            (translation_header(row.strip()) or (None,))[0]=='source' for row in text.splitlines())==1:
        _parse_id_template(text,pages,None)
    for page in pages:
        for bubble in page.bubbles:
            if bubble.id in report["frame_tags"]:
                bubble.frame_type=report["frame_tags"][bubble.id]
                bubble.frame_type_locked=True
    if notices is not None:notices.extend(report['warnings'])
    if not changes:raise ValueError('ไม่มีคำแปลที่จับคู่ได้')
    return changes


def _parse_id_template(text,pages,notices=None):
    """Use exact IDs as authority, or align `[#N]` scripts automatically without manual OCR fixes."""
    notes=[]
    targets={b.id:b for p in pages for b in p.bubbles}
    if len(targets)!=sum(len(p.bubbles) for p in pages):raise ValueError('โปรเจกต์มีรหัสกรอบซ้ำ กรุณาตรวจโปรเจกต์ก่อนนำเข้า')
    rows=text.lstrip('\ufeff').splitlines()
    meaningful=[i for i,row in enumerate(rows) if row.strip()]
    if len(meaningful)>=2 and re.fullmatch(r'```(?:txt|text|plaintext)?',rows[meaningful[0]].strip(),re.I) and rows[meaningful[-1]].strip()=='```':
        rows[meaningful[0]]=rows[meaningful[-1]]=''
    records=[];current={};field=None;start=1
    keys={'รหัส':'id','คำต้นฉบับ':'source','คำแปลไทย':'translation'}
    for lineno,raw in enumerate(rows,1):
        line=raw.strip()
        if not line:continue
        match=translation_header(line)
        if match:
            key,value,canonical,corrected=match
            if corrected:notes.append(f'บรรทัด {lineno}: อ่านหัวข้อเป็น {canonical}:')
            if key=='id' or (key=='source' and 'source' in current):
                if current:records.append((start,current))
                current={}
            if not current:start=lineno
            if key in current:raise TranslationFormatError('หัวข้อซ้ำในรายการคำแปล: '+canonical,lineno)
            current[key]=value;field=key
        elif field in ('source','translation'):current[field]+='\n'+line
        else:raise TranslationFormatError('อ่านหัวข้อไม่ออก: '+line[:100]+'\nใช้ รหัส: / คำต้นฉบับ: / คำแปลไทย: หรือรูปแบบ [#1] จากไดร์ฟ',lineno)
    if current:records.append((start,current))
    changes={};seen=set()
    for lineno,record in records:
        required=('translation',) if record.get('id') else ('source','translation')
        if any(key not in record for key in required):
            missing=[label+':' for label,key in keys.items() if key in required and key not in record]
            raise TranslationFormatError('รายการนี้ขาดหัวข้อ '+', '.join(missing),lineno)
        bid=record.get('id')
        if bid is None:
            matches=[b.id for b in targets.values() if normalize(b.text)==normalize(record['source'])]
            if len(matches)!=1:
                skel=_text_skeleton(record['source'])
                matches=[b.id for b in targets.values() if skel and _text_skeleton(b.text)==skel] if skel else []
            if len(matches)!=1:raise TranslationFormatError('คำต้นฉบับซ้ำหรือหาไม่พบ กรุณาใช้ไฟล์ส่งแปลที่มีรหัสกรอบ หรือรูปแบบ [#1]',lineno)
            bid=matches[0]
        if bid not in targets:raise TranslationFormatError('ไม่พบรหัสกรอบในโปรเจกต์นี้: '+bid,lineno)
        if bid in seen:raise TranslationFormatError('รหัสกรอบซ้ำในคำแปล: '+bid,lineno)
        seen.add(bid)
        if 'source' not in record or normalize(targets[bid].text)!=normalize(record['source']):
            notes.append(f'บรรทัด {lineno}: ใช้รหัสกรอบ {bid} จับคู่คำแปล แม้ต้นฉบับต่างหรือไม่มี — เก็บต้นฉบับในโปรเจกต์เดิม')
        value=record['translation'].strip()
        if value:changes[bid]=value
    if not changes:raise ValueError('ยังไม่มีคำแปลไทยให้นำเข้า — ตรวจว่าเปิดไฟล์ที่แปลเสร็จแล้ว และมีข้อความหลัง คำแปลไทย:')
    if notices is not None:notices.extend(notes)
    return changes


def largest_rectangle(mask):
    heights=np.zeros(mask.shape[1],dtype=int);best=(0,[0,0,0,0])
    for y,row in enumerate(mask):
        heights=np.where(row>0,heights+1,0);stack=[]
        for x in range(len(heights)+1):
            value=int(heights[x]) if x<len(heights) else 0;start=x
            while stack and stack[-1][1]>value:
                left,height=stack.pop();size=height*(x-left)
                if size>best[0]:best=(size,[left,y-height+1,x,y+1])
                start=left
            if not stack or stack[-1][1]<value:stack.append((start,value))
    return best[1]


def pole_of_inaccessibility(mask):
    binary=(mask>0).astype(np.uint8)
    if not np.any(binary):
        return mask.shape[1]//2,mask.shape[0]//2,0.0
    distance=cv2.distanceTransform(np.pad(binary,1),cv2.DIST_L2,5)[1:-1,1:-1]
    r_max=float(distance.max())
    if r_max<=1.0:
        ys,xs=np.nonzero(binary)
        return int(round(float(xs.mean()))),int(round(float(ys.mean()))),r_max
    peak_y,peak_x=np.unravel_index(int(np.argmax(distance)),distance.shape)
    core_mask=(distance>=r_max*.55).astype(np.uint8)
    n,labels=cv2.connectedComponents(core_mask)
    peak_label=int(labels[peak_y,peak_x])
    lobe=(labels==peak_label) if peak_label>0 else (distance>=r_max*.55)
    weight=np.where(lobe,np.maximum(0.0,distance-r_max*.45)**3,0.0)
    if not np.any(weight):
        return int(peak_x),int(peak_y),r_max
    yy,xx=np.indices(mask.shape)
    cx=int(round(float((xx*weight).sum()/weight.sum())))
    cy=int(round(float((yy*weight).sum()/weight.sum())))
    if not binary[cy,cx]:
        return int(peak_x),int(peak_y),r_max
    return cx,cy,r_max


def centered_rectangle(mask,alternatives=False):
    binary=(mask>0).astype(np.uint8)
    cx,cy,r_max=pole_of_inaccessibility(binary)
    if r_max<=0.0 or not binary[cy,cx]:
        return [largest_rectangle(mask)] if alternatives else largest_rectangle(mask)
    halfwidth=min(cx,mask.shape[1]-cx-1);best=(0,[cx,cy,cx+1,cy+1]);candidates=[]
    for halfheight in range(min(cy,mask.shape[0]-cy-1)+1):
        for y in {cy-halfheight,cy+halfheight}:
            if halfwidth<0:break
            row=binary[y]
            missing=np.flatnonzero(row[cx-halfwidth:cx+halfwidth+1]==0)
            if len(missing):halfwidth=min(halfwidth,int(np.abs(missing-halfwidth).min())-1)
        if halfwidth<0:break
        area=(2*halfwidth+1)*(2*halfheight+1)
        candidates.append((area,[cx-halfwidth,cy-halfheight,cx+halfwidth+1,cy+halfheight+1]))
        if area>best[0]:best=(area,[cx-halfwidth,cy-halfheight,cx+halfwidth+1,cy+halfheight+1])
    if alternatives:
        usable=[box for area,box in candidates if area>=best[0]*.68 and box[3]-box[1]>=18]
        if not usable:return [best[1]]
        return [usable[i] for i in np.unique(np.linspace(0,len(usable)-1,min(15,len(usable)),dtype=int))]
    return best[1]


def _symmetric_row_halfwidths(safe_mask,cx):
    h,w=safe_mask.shape
    max_hw=min(cx,w-1-cx)
    if max_hw<=2:return np.zeros(h,dtype=int)
    binary=(safe_mask>0)
    out=np.zeros(h,dtype=int)
    for y in range(h):
        if not binary[y,cx]:continue
        row=binary[y,cx-max_hw:cx+max_hw+1]
        zeros=np.flatnonzero(~row)
        if not len(zeros):out[y]=max_hw
        else:out[y]=max(0,int(np.abs(zeros-max_hw).min())-1)
    return out


def _anchor_lobe(mask, anchor):
    """Keep the chamber nearest this OCR anchor when a narrow neck joins balloons."""
    distance=cv2.distanceTransform(mask,cv2.DIST_L2,5)
    x=int(np.clip(round(anchor[0]),0,mask.shape[1]-1))
    y=int(np.clip(round(anchor[1]),0,mask.shape[0]-1))
    for fraction in (.32,.45,.58,.68):
        seeds=(distance>distance.max()*fraction).astype(np.uint8)
        count,labels,stats,_=cv2.connectedComponentsWithStats(seeds)
        if count<3:continue
        sizes=stats[1:,cv2.CC_STAT_AREA]
        kept=np.flatnonzero(sizes>=max(100,sizes.max()*.14))+1
        if len(kept)<2:continue
        _,nearest=cv2.distanceTransformWithLabels((~np.isin(labels,kept)).astype(np.uint8),
                                                 cv2.DIST_L2,5,labelType=cv2.DIST_LABEL_CCOMP)
        return np.where(nearest==nearest[y,x],mask,0).astype(np.uint8)
    return mask


def _mtp_enclosed_body(image,box):
    """Recover a closed balloon surrounding a small imported text anchor."""
    roi=bounded_box(box,image.size,max(640,min(900,3*int(max(box[2]-box[0],box[3]-box[1])))))
    gray=np.asarray(image.crop(roi).convert('L'))
    edges=cv2.morphologyEx(cv2.Canny(gray,30,100),cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    contours,_=cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    center=((box[0]+box[2])/2-roi[0],(box[1]+box[3])/2-roi[1])
    anchor_area=(box[2]-box[0])*(box[3]-box[1])
    for contour in sorted(contours,key=cv2.contourArea):
        x,y,w,h=cv2.boundingRect(contour)
        if x<=1 or y<=1 or x+w>=gray.shape[1]-1 or y+h>=gray.shape[0]-1:continue
        area=cv2.contourArea(contour)
        if area<max(400,anchor_area*.85) or area/(w*h)<.55:continue
        if cv2.pointPolygonTest(contour,center,True)<min(w,h)*.12:continue
        safe=np.zeros_like(gray)
        cv2.drawContours(safe,[contour],-1,255,-1)
        if np.percentile(gray[safe>0],80)-np.percentile(gray[safe>0],20)>45:continue
        return roi,cv2.erode(safe,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(13,13)))
    # Joined white balloons can have a low fill ratio across their common box.
    # Reject open regions and page margins before separating the chambers.
    light=(gray>=225).astype(np.uint8)
    shapes,_=cv2.findContours(light,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(shapes,key=cv2.contourArea):
        x,y,w,h=cv2.boundingRect(contour)
        if x<=1 or y<=1 or x+w>=gray.shape[1]-1 or y+h>=gray.shape[0]-1:continue
        if cv2.contourArea(contour)<anchor_area or cv2.pointPolygonTest(contour,center,True)<4:continue
        body=np.zeros_like(gray)
        cv2.drawContours(body,[contour],-1,255,-1)
        if np.mean(light[body>0])<.72:continue
        return roi,cv2.erode(body,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(13,13)))
    for diameter in (15,31):
        barrier=cv2.morphologyEx((gray<175).astype(np.uint8),cv2.MORPH_CLOSE,
                                  cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(diameter,diameter)))
        free=(barrier==0).astype(np.uint8)
        seed=tuple(int(round(v)) for v in center)
        if not free[seed[1],seed[0]]:continue
        count,_,_,(x,y,w,h)=cv2.floodFill(free,None,seed,2)
        if x<=0 or y<=0 or x+w>=gray.shape[1] or y+h>=gray.shape[0]:continue
        if count<max(400,anchor_area) or count<.48*w*h:continue
        body=(free==2).astype(np.uint8)*255
        outline,_=cv2.findContours(body,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(body,outline,-1,255,cv2.FILLED)
        return roi,cv2.erode(body,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(13,13)))
    return None


def _bubble_interior_region(image,bubble,page_bubbles=()):
    bx0,by0,bx1,by1=[int(round(v)) for v in bubble.box]
    bw=max(1,bx1-bx0);bh=max(1,by1-by0)
    if bw<=16 and bh<=16 and getattr(bubble,'engine','')!='MTP':
        raise ValueError('พื้นที่บับเบิ้ลเล็กเกินไปสำหรับข้อความ')
    if bw*bh>3_500_000:
        pad=12
        rbox=bounded_box(bubble.box,image.size,pad)
        rw,rh=rbox[2]-rbox[0],rbox[3]-rbox[1]
        safe=np.zeros((rh,rw),np.uint8)
        lx0=max(0,bx0-rbox[0]+8);ly0=max(0,by0-rbox[1]+8)
        lx1=min(rw,bx1-rbox[0]-8);ly1=min(rh,by1-rbox[1]-8)
        if lx1>lx0 and ly1>ly0:safe[ly0:ly1,lx0:lx1]=255
        else:safe[:,:]=255
        return rbox,safe,False
    seg_out=BUBBLE_SEGMENTER.segment_bubble(image,bubble.box,margin=6)
    if seg_out is None and getattr(bubble,'engine','')=='MTP':
        seg_out=_mtp_enclosed_body(image,bubble.box)
    if seg_out is not None:
        rbox,safe=seg_out
        safe=_anchor_lobe(safe,((bx0+bx1)/2-rbox[0],(by0+by1)/2-rbox[1]))
        found=True
    else:
        region=prepare_region(image,bubble,margin=6)
        rbox=region.box;safe=region.safe.copy();found=bool(region.boundary_found)

    yy,xx=np.indices(safe.shape)
    gx=xx+rbox[0];gy=yy+rbox[1]
    contours,_=cv2.findContours(safe,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    expand=False
    if found and contours:
        contour=max(contours,key=cv2.contourArea);c_area=float(cv2.contourArea(contour))
        x,y,cw,ch=cv2.boundingRect(contour)
        hull_a=max(1.0,float(cv2.contourArea(cv2.convexHull(contour))))
        solidity=c_area/hull_a
        not_touching=(x>0 and y>0 and x+cw<safe.shape[1] and y+ch<safe.shape[0])
        expand=bool(not_touching and solidity>=0.72 and
                    bw*bh*0.55<=c_area<=bw*bh*2.85 and cw<=bw*1.75 and ch<=bh*1.75)
    if not expand and seg_out is None:
        safe[(gx<bx0)|(gx>=bx1)|(gy<by0)|(gy>=by1)]=0
    if found and np.count_nonzero(safe)<bw*bh*.20:
        found=False
        safe[:]=0
        safe[(gx>=bx0+10)&(gx<bx1-10)&(gy>=by0+10)&(gy<by1-10)]=255
    narration=not found and bw/max(1,bh)>2.8
    if not found and not narration:
        rx=max(1.0,bw/2.0);ry=max(1.0,bh/2.0)
        safe[((gx-(bx0+bx1)/2.0)/rx)**2+((gy-(by0+by1)/2.0)/ry)**2>1.0]=0
    # Multiple dialogue anchors can share one connected interior. Partition it in
    # page coordinates before choosing the text centre and fitting the curved rows.
    cx,cy=(bx0+bx1)/2,(by0+by1)/2
    interior=safe
    coincident=[]
    for other in page_bubbles:
        x0,y0,x1,y1=other.box
        ox,oy=(x0+x1)/2,(y0+y1)/2
        if (ox,oy)==(cx,cy):
            coincident.append(other.id)
            continue
        ix,iy=round(ox-rbox[0]),round(oy-rbox[1])
        if not (0<=iy<safe.shape[0] and 0<=ix<safe.shape[1] and interior[iy,ix]):continue
        dx,dy=ox-cx,oy-cy
        distance=np.hypot(dx,dy)
        if distance<1:continue
        if safe is interior:safe=interior.copy()
        gap=min(4.0,distance*.06)
        safe[(gx-(cx+ox)/2)*dx+(gy-(cy+oy)/2)*dy >= -gap*distance]=0
    if len(coincident)>1 and bubble.id in coincident and np.any(safe):
        rows=np.flatnonzero(safe.any(axis=1))
        cuts=np.linspace(rows[0],rows[-1]+1,len(coincident)+1).round().astype(int)
        rank=coincident.index(bubble.id)
        safe=safe.copy()
        safe[:cuts[rank]+2]=0
        safe[max(0,cuts[rank+1]-2):]=0
    return rbox,safe,found


def text_box(image,bubble,alternatives=False):
    bx0,by0,bx1,by1=[int(round(v)) for v in bubble.box]
    rbox,safe,found=_bubble_interior_region(image,bubble)
    boxes=[]
    for x0,y0,x1,y1 in centered_rectangle(safe,alternatives=True):
        padx=max(3,round((x1-x0)*.06));pady=max(3,round((y1-y0)*.06))
        box=[x0+rbox[0]+padx,y0+rbox[1]+pady,x1+rbox[0]-padx,y1+rbox[1]-pady]
        if box[2]-box[0]>=12 and box[3]-box[1]>=12:boxes.append(box)
    if not boxes:
        fallback=_clamp_box_in_page([bx0+2,by0+2,max(bx0+16,bx1-2),max(by0+16,by1-2)],image.size)
        if fallback[2]-fallback[0]<12 or fallback[3]-fallback[1]<12:
            raise ValueError('พื้นที่บับเบิ้ลเล็กเกินไปสำหรับข้อความ')
        boxes=[fallback];found=False
    return (boxes if alternatives else max(boxes,key=lambda b:(b[2]-b[0])*(b[3]-b[1]))),found


def wrap_text(text,width,renderer):
    try:from pythainlp.tokenize import word_tokenize,subword_tokenize
    except ImportError as exc:raise RuntimeError('รัน install_text_psd_windows.bat ก่อนใช้ Text') from exc
    lines=[]
    for paragraph in text.split('\n'):
        current=''
        for token in word_tokenize(paragraph,engine='newmm',keep_whitespace=True):
            parts=[token] if renderer.measure(token)[0]<=width else subword_tokenize(token,engine='tcc')
            for part in parts:
                if renderer.measure(current+part)[0]<=width:current+=part
                else:
                    if current.strip():lines.append(current.rstrip())
                    current=part.lstrip()
                    if renderer.measure(current)[0]>width:return None
        lines.append(current.rstrip())
    return lines


def _force_wrap_text(text,width,renderer):
    try:from pythainlp.tokenize import word_tokenize,subword_tokenize
    except ImportError as exc:raise RuntimeError('รัน install_text_psd_windows.bat ก่อนใช้ Text') from exc
    width=max(24,int(width))
    lines=[]
    for paragraph in text.split('\n'):
        if not paragraph.strip():continue
        current=''
        for token in word_tokenize(paragraph,engine='newmm',keep_whitespace=True):
            parts=[token] if renderer.measure(token)[0]<=width else subword_tokenize(token,engine='tcc')
            for part in parts:
                if not current:
                    current=part.lstrip()
                elif renderer.measure(current+part)[0]<=width:
                    current+=part
                else:
                    if current.strip():lines.append(current.rstrip())
                    current=part.lstrip()
        if current.strip():lines.append(current.rstrip())
    return lines or [text.strip()]


def manga_line_score(widths):
    if len(widths)<2:return 0.0
    values=np.asarray(widths,dtype=float);mean=max(1,float(values.mean()))
    if len(values)==2:target=np.array([.95,1.05])
    elif len(values)==3:target=np.array([.88,1.24,.88])
    else:
        target=.82+.26*np.sin(np.linspace(0,np.pi,len(values)));target/=target.mean()
    score=float(np.mean((values/mean-target)**2))
    if len(values)==2:score+=6*min(.15,max(0,(values[0]-values[1]+mean*.02)/mean))**2
    elif len(values)==3:score+=6*max(0,(max(values[0],values[2])-values[1]+mean*.02)/mean)**2
    score+=max(0,.30-float(values.min()/max(1,values.max())))**2*4
    return score


def manga_wrap(text,width,renderer,count):
    if '\n' in text or count<2:return wrap_text(text,width,renderer)
    from pythainlp.tokenize import word_tokenize,subword_tokenize
    tokens=[]
    for token in word_tokenize(text,engine='newmm',keep_whitespace=True):
        tokens.extend([token] if renderer.measure(token)[0]<=width else subword_tokenize(token,engine='tcc'))
    if len(tokens)>120 or count>12:return wrap_text(text,width,renderer)
    if len(tokens)<count:return None
    total=max(1,renderer.measure(text)[0]);mean=total/count
    profile=([.95,1.05] if count==2 else [.88,1.24,.88] if count==3 else
             (.82+.26*np.sin(np.linspace(0,np.pi,count))).tolist())
    profile=np.asarray(profile);profile/=profile.mean()
    segments={}
    for start in range(len(tokens)):
        line=''
        for end in range(start+1,len(tokens)+1):
            line+=tokens[end-1];trimmed=line.strip()
            size=renderer.measure(trimmed)[0]
            if size>width:break
            if trimmed:segments[start,end]=(trimmed,size)
    states={0:[(0.,[],[])]}
    for row in range(count):
        following={}
        for start,options in states.items():
            for end in range(start+1,len(tokens)+1):
                if len(tokens)-end<count-row-1:break
                if row==count-1 and end!=len(tokens):continue
                segment=segments.get((start,end))
                if segment is None:continue
                line,size=segment
                for cost,lines,widths in options:
                    candidate=(cost+(size/mean-profile[row])**2,lines+[line],widths+[size])
                    following.setdefault(end,[]).append(candidate)
        states={end:sorted(options,key=lambda x:x[0])[:16] for end,options in following.items()}
        if not states:return None
    choices=states.get(len(tokens),[])
    return min(choices,key=lambda x:manga_line_score(x[2]))[1] if choices else None


def _lines_fit_contour_chords(lines,tile,renderer,row_hw,cy,pad_ratio=0.82,v_margin_ratio=0.16,max_block_h=0):
    n_lines=len(lines)
    if n_lines==0 or tile is None:return False
    if max_block_h>0 and tile.height>max_block_h:return False
    h=len(row_hw)
    active_ys=np.flatnonzero(row_hw>0)
    if not len(active_ys):return False
    span_top=int(active_ys[0]);span_bot=int(active_ys[-1])+1
    span_h=max(1,span_bot-span_top)
    v_pad=max(4,int(round(span_h*v_margin_ratio)))
    top_y=int(round(cy-tile.height/2.0))
    bot_y=top_y+tile.height
    if top_y<span_top+v_pad or bot_y>span_bot-v_pad:return False
    line_step=tile.height/max(1,n_lines)
    for idx,ln in enumerate(lines):
        if not ln.strip():continue
        lw=renderer.measure(ln.strip())[0]
        ly0=max(0,int(np.floor(top_y+idx*line_step)))
        ly1=min(h,max(ly0+1,int(np.ceil(top_y+(idx+1)*line_step))))
        avail_w=int(np.min(row_hw[ly0:ly1]))*2
        if avail_w<12 or lw>int(avail_w*pad_ratio):
            return False
    return True


def _thai_optical_dy(text,font_size,safe_mask,cx,cy,tile_w,tile_h):
    upper=sum(1 for c in text if c in _THAI_UPPER)
    lower=sum(1 for c in text if c in _THAI_LOWER)
    if upper<=lower:return 0
    shift=max(1,min(5,int(round(font_size*0.035))))
    ny0=cy-shift-tile_h//2
    nx0=cx-tile_w//2;nx1=nx0+tile_w
    if ny0>=3 and nx0>=0 and nx1<=safe_mask.shape[1] and np.all(safe_mask[ny0:ny0+4,max(0,cx-tile_w//3):min(safe_mask.shape[1],cx+tile_w//3)]>0):
        return -shift
    return 0


def layout_text(image,bubble,translation=None,font_choice=None,custom_size=None,custom_width=None,text_offset=None,custom_rotation=None,
                *, original=None, cleaned=None, auto_stroke=False, auto_gradient=False, page_bubbles=(), interior_region=None):
    from text_style_sampler import effects, sample_style
    previous = getattr(bubble, 'text_layer', None) or {}
    geometry = original if original is not None else image
    layer = _position_text(geometry,bubble,translation,font_choice,custom_size,custom_width,text_offset,custom_rotation,page_bubbles,interior_region)
    if geometry is not image:
        layer['color'] = _sample_text_color(image,layer['box'])
    style = effects(previous)
    manual = previous.get('manual_effects', False)
    if (auto_stroke or auto_gradient) and not manual:
        sampled = sample_style(original if original is not None else image, cleaned, bubble)
        if auto_stroke:
            style['stroke'] = sampled['stroke']
        if auto_gradient:
            style['gradient'] = sampled['gradient']
    layer.update(style)
    layer['manual_effects'] = manual
    return layer


def _position_text(image,bubble,translation=None,font_choice=None,custom_size=None,custom_width=None,text_offset=None,custom_rotation=None,page_bubbles=(),interior_region=None):
    """Fit in the frame's own axes, then carry its rotation and user offset to preview and PSD."""
    chosen=normalize_font_choice(font_choice or getattr(bubble,'font_choice',None) or FONT_NAME)
    c_size=int(getattr(bubble,'custom_size',0) if custom_size is None else custom_size)
    c_width=int(getattr(bubble,'custom_width',0) if custom_width is None else custom_width)
    offset=_normalize_offset(getattr(bubble,'text_offset',[]) if text_offset is None else text_offset)
    c_rot=_normalize_rotation(getattr(bubble,'custom_rotation',None) if custom_rotation is None else custom_rotation)
    q=[list(p) for p in getattr(bubble,'quad',[])]
    if not valid_quad(q) or abs(quad_angle(q))<.1:
        layer=_layout_text(image,bubble,translation,font_choice=chosen,custom_size=c_size,custom_width=c_width,page_bubbles=page_bubbles,interior_region=interior_region)
        layer['bubble_quad']=q
        if offset:
            old_b=layer['box']
            new_b=_shift_box_preserving_span(old_b,offset[0],offset[1],image.size,as_int=True)
            real_off=_normalize_offset([new_b[0]-old_b[0],new_b[1]-old_b[1]])
            layer['box']=new_b
            layer['color']=_sample_text_color(image,new_b)
            if real_off:layer['text_offset']=real_off
            if hasattr(bubble,'text_offset') and text_offset is None:
                bubble.text_offset=real_off
        if c_rot is not None:
            ub=layer['box']
            layer['text_size']=[max(8.0,float(ub[2]-ub[0])),max(8.0,float(ub[3]-ub[1]))]
            _apply_angle_to_layer(image,layer,c_rot,custom_rot=c_rot)
        return layer
    from types import SimpleNamespace
    angle=quad_angle(q);r=np.radians(angle)
    basis=np.array([[np.cos(r),-np.sin(r)],[np.sin(r),np.cos(r)]])
    mtp_shape=None
    if getattr(bubble,'engine','')=='MTP':
        rbox,mask,found=interior_region or _bubble_interior_region(image,bubble,page_bubbles)
        if found and np.any(mask):
            mtp_shape=(rbox,mask)
    points=np.asarray(q)
    if mtp_shape is not None:
        points=cv2.findNonZero(mask).reshape(-1,2)+np.asarray(rbox[:2])
    local=points@basis;lo=local.min(axis=0);hi=local.max(axis=0)
    width,height=np.ceil(hi-lo).astype(int)
    if width*height>12_000_000:raise ValueError('กรอบ Text ใหญ่เกินไป')
    inverse=np.eye(3);inverse[:2,:2]=basis;inverse[:2,2]=basis@lo
    forward=np.linalg.inv(inverse)
    if mtp_shape is None:
        region=prepare_region(image,bubble,margin=6)
        rbox,mask,found=region.box,region.safe,region.boundary_found
    roi=np.eye(3);roi[:2,2]=rbox[:2]
    transform=forward@roi
    pixels=cv2.warpPerspective(np.asarray(image.crop(rbox)),transform,(width,height),
                               flags=cv2.INTER_CUBIC,borderMode=cv2.BORDER_REPLICATE)
    safe=cv2.warpPerspective(mask,transform,(width,height),flags=cv2.INTER_NEAREST)
    boxes=[]
    for x0,y0,x1,y1 in centered_rectangle(safe,alternatives=True):
        px=max(3,round((x1-x0)*.06));py=max(3,round((y1-y0)*.06))
        box=[x0+px,y0+py,x1-px,y1-py]
        if box[2]-box[0]>=12 and box[3]-box[1]>=12:boxes.append(box)
    if not boxes:
        boxes=[[2,2,max(14,int(width)-2),max(14,int(height)-2)]]
    proxy=SimpleNamespace(box=[0,0,int(width),int(height)],translation=bubble.translation,
                          engine=getattr(bubble,"engine",""),lines=[],font_choice=chosen,custom_size=c_size,custom_width=c_width)
    layer=_layout_text(Image.fromarray(pixels),proxy,translation,(boxes,found),
                       font_choice=chosen,custom_size=c_size,custom_width=c_width,local_safe=safe,local_origin=(0,0))
    rect=layer['box'];tw=max(8.0,float(rect[2]-rect[0]));th=max(8.0,float(rect[3]-rect[1]))
    corners=map_points(box_quad(rect),inverse)
    bounds=_shift_box_preserving_span(quad_bounds(corners),0.0,0.0,image.size,as_int=False)
    if offset:
        old_b=list(bounds)
        bounds=_shift_box_preserving_span(old_b,offset[0],offset[1],image.size,as_int=False)
        real_off=_normalize_offset([bounds[0]-old_b[0],bounds[1]-old_b[1]])
        layer['color']=_sample_text_color(image,bounds)
        if real_off:layer['text_offset']=real_off
        if hasattr(bubble,'text_offset') and text_offset is None:
            bubble.text_offset=real_off
    final_angle=c_rot if c_rot is not None else angle
    layer.update(box=bounds,text_size=[tw,th],
                 rotation=angle,bubble_box=list(bubble.box),bubble_quad=q)
    if c_rot is not None:
        _apply_angle_to_layer(image,layer,final_angle,custom_rot=c_rot)
    return layer


def _try_fit_boxes(render_text,boxes,info,low,high,safe_mask=None,safe_origin=(0,0),
                   w_scale=0.83,h_scale=0.66,oval_pad=0.81,oval_vmargin=0.16,max_block_w=0,max_block_h=0):
    from windows_text import TextRenderer
    best=None
    oval_center=None;row_hw=None;belly_w=0
    if safe_mask is not None and np.count_nonzero(safe_mask)>=120:
        scx,scy,rmax=pole_of_inaccessibility(safe_mask)
        if rmax>=8.0:
            row_hw=_symmetric_row_halfwidths(safe_mask,scx)
            belly_band=row_hw[max(0,scy-int(rmax*.4)):min(len(row_hw),scy+int(rmax*.4)+1)]
            belly_w=int(belly_band.max()*2*oval_pad) if len(belly_band) else 0
            if max_block_w>0 and belly_w>0:
                belly_w=min(belly_w,int(max_block_w))
            oval_center=(scx,scy)
    lo,hi=low,high
    while lo<=hi:
        size=(lo+hi)//2
        fits=[]
        with TextRenderer(size,info.path,info.family,info.weight,info.italic) as renderer:
            for box in boxes:
                width=box[2]-box[0];height=box[3]-box[1];aspect=height/max(1,width)
                max_width=int(width*(w_scale if aspect>1.35 else min(0.90,w_scale+0.03)))
                max_height=int(height*(h_scale if aspect>1.35 else min(0.84,h_scale+0.08)))
                if max_block_w>0:max_width=min(max_width,int(max_block_w))
                if max_block_h>0:max_height=min(max_height,int(max_block_h))
                if max_width<18 or max_height<14:continue
                lines=wrap_text(render_text,max_width,renderer)
                tile=renderer.render(lines) if lines else None
                if tile is not None and tile.width<=max_width and tile.height<=max_height:
                    widths=[renderer.measure(line)[0] for line in lines if line]
                    balance=float(np.std(widths)/max(1,np.mean(widths)))
                    fits.append((balance+len(lines)*.02,lines,tile,box,max_width,max_height))
            if oval_center is not None and belly_w>=24:
                scx,scy=oval_center
                for ratio in (0.96,0.88,0.78):
                    cand_w=max(20,int(belly_w*ratio))
                    lines=wrap_text(render_text,cand_w,renderer)
                    if not lines:continue
                    if '\n' not in render_text and 2<=len(lines)<=8:
                        shaped=manga_wrap(render_text,cand_w,renderer,len(lines))
                        if shaped:lines=shaped
                    tile=renderer.render(lines)
                    if _lines_fit_contour_chords(lines,tile,renderer,row_hw,scy,
                                                 pad_ratio=oval_pad,v_margin_ratio=oval_vmargin,max_block_h=max_block_h):
                        dy=_thai_optical_dy(render_text,size,safe_mask,scx,scy,tile.width,tile.height)
                        gx=scx+safe_origin[0];gy=scy+dy+safe_origin[1]
                        pad_w=max(12,tile.width+8);pad_h=max(12,tile.height+8)
                        obox=[int(round(gx-pad_w/2.0)),int(round(gy-pad_h/2.0)),
                              int(round(gx+pad_w/2.0)),int(round(gy+pad_h/2.0))]
                        eb_h=max_block_h if max_block_h>0 else pad_h
                        widths=[renderer.measure(line)[0] for line in lines if line]
                        score=manga_line_score(widths)+len(lines)*.018
                        fits.append((score,lines,tile,obox,cand_w,eb_h))
        if fits:
            _,lines,tile,box,max_width,max_height=min(fits,key=lambda v:v[0])
            best=(size,lines,tile,box,max_width,max_height);lo=size+1
        else:hi=size-1
    return best


def _layout_text(image,bubble,translation=None,box_options=None,font_choice=None,custom_size=0,custom_width=0,local_safe=None,local_origin=None,page_bubbles=(),interior_region=None):
    from windows_text import TextRenderer
    raw_text=(bubble.translation if translation is None else translation).strip().replace('\r\n','\n').replace('\r','\n')
    if not raw_text:raise ValueError('กรอบนี้ยังไม่มีคำแปลไทย')
    if len(raw_text)>10000:raise ValueError('ข้อความยาวเกิน 10,000 ตัวอักษรในกรอบเดียว')
    chosen_req=normalize_font_choice(font_choice or getattr(bubble,'font_choice',None) or FONT_NAME)
    render_text,info,chosen=_prepare_render_text_and_font(raw_text,chosen_req)

    if box_options is not None:
        boxes,found=box_options
        safe_mask=local_safe;safe_origin=local_origin or (0,0)
    else:
        rbox,safe_mask,found=interior_region or _bubble_interior_region(image,bubble,page_bubbles)
        safe_origin=(rbox[0],rbox[1])
        boxes=[]
        for x0,y0,x1,y1 in centered_rectangle(safe_mask,alternatives=True):
            padx=max(3,round((x1-x0)*.06));pady=max(3,round((y1-y0)*.06))
            b_cand=[x0+rbox[0]+padx,y0+rbox[1]+pady,x1+rbox[0]-padx,y1+rbox[1]-pady]
            if b_cand[2]-b_cand[0]>=12 and b_cand[3]-b_cand[1]>=12:boxes.append(b_cand)
        if not boxes:
            bx0,by0,bx1,by1=[int(round(v)) for v in bubble.box]
            fallback=_clamp_box_in_page([bx0+2,by0+2,max(bx0+16,bx1-2),max(by0+16,by1-2)],image.size)
            if fallback[2]-fallback[0]<12 or fallback[3]-fallback[1]<12:
                raise ValueError('พื้นที่บับเบิ้ลเล็กเกินไปสำหรับข้อความ')
            boxes=[fallback];found=False

    c_size=int(custom_size or 0)
    c_width=int(custom_width or 0)
    if safe_mask is not None and np.count_nonzero(safe_mask)>=80:
        pcx,pcy,_=pole_of_inaccessibility(safe_mask)
        cx=float(pcx+safe_origin[0]);cy=float(pcy+safe_origin[1])
    else:
        anchor=max(boxes,key=lambda b:(b[2]-b[0])*(b[3]-b[1]))
        cx=(anchor[0]+anchor[2])/2.0;cy=(anchor[1]+anchor[3])/2.0
    overflow_fallback=False

    if c_size>0 or c_width>0:
        target_size=int(np.clip(c_size if c_size>0 else 24,6,200))
        anchor=max(boxes,key=lambda b:(b[2]-b[0])*(b[3]-b[1]))
        base_w=c_width if c_width>0 else max(int(round((bubble.box[2]-bubble.box[0])*.92)),anchor[2]-anchor[0])
        base_w=max(28,min(image.width-8,int(round(base_w))))
        while target_size>6:
            with TextRenderer(target_size,info.path,info.family,info.weight,info.italic) as renderer:
                if '\n' in render_text and c_width<=0:
                    lines=[ln.strip() for ln in render_text.split('\n') if ln.strip()] or [render_text.strip()]
                else:
                    lines=_force_wrap_text(render_text,base_w,renderer)
                tile=renderer.render(lines)
            if tile.width<=image.width-6 and tile.height<=image.height-6:
                break
            target_size=max(6,target_size-2)
        size=target_size
        bw_pad=max(12,tile.width+6);bh_pad=max(12,tile.height+6)
        box=_clamp_box_in_page([cx-bw_pad/2.0,cy-bh_pad/2.0,cx+bw_pad/2.0,cy+bh_pad/2.0],image.size)
    else:
        bw_full=max(16.0,float(bubble.box[2]-bubble.box[0]))
        bh_full=max(16.0,float(bubble.box[3]-bubble.box[1]))
        body_ratio=_visual_body_ratio(info)
        guide=_source_layout_guide(bubble,bw_full,bh_full)
        short=len(re.sub(r'\s','',render_text))<=12
        narration=not found and bw_full/max(1.0,bh_full)>2.8
        extent=max(min(b[2]-b[0],b[3]-b[1]) for b in boxes)

        max_block_w=0;max_block_h=0
        if guide is not None:
            scale_k=0.80 if short else 0.72
            target_body=max(8.5,min(24.0,guide['med_lh']*scale_k))
            visual_cap=int(round(target_body/body_ratio))
            geom_cap=int(extent*(.85 if narration else (.36 if short else .62)))
            cap=min(110,max(12,min(visual_cap,geom_cap)))
            max_block_h=int(round(min(bh_full*0.70,max(guide['orig_h']*1.22,bh_full*0.52))))
            max_block_w=int(round(min(bw_full*0.85,max(guide['orig_w']*1.25,bw_full*0.72))))
        else:
            # Without measured source glyphs, let the contour fit determine the size.
            cap=min(110,max(12,round(extent*(.32 if short else .64))))

        if found and safe_mask is not None:
            # Thai tone marks share a glyph cell with their base consonant.
            import unicodedata
            units=sum(1 for char in render_text if not char.isspace() and not unicodedata.combining(char)
                      and unicodedata.category(char) not in ('Mn','Mc'))
            area=float(np.count_nonzero(safe_mask))
            fullness=min(.46,.08+.008*units)
            density_cap=max(8,round(np.sqrt(area*fullness/max(1,units))/body_ratio))
            if area>bw_full*bh_full*1.2 or getattr(bubble,'engine','')=='MTP':
                cap=min(110,max(8,round(extent*(.34 if short else .65))),density_cap)
                max_block_w=max_block_h=0
            else:
                cap=min(cap,density_cap)
            if units>12:
                max_block_w=max_block_h=0

        comfortable_min=max(11,int(round(7.2/body_ratio)))
        used_w_scale=0.83;used_h_scale=0.66
        best=_try_fit_boxes(
            render_text,boxes,info,8,max(8,cap),
            safe_mask=safe_mask,safe_origin=safe_origin,
            w_scale=0.83,h_scale=0.66,oval_pad=0.81,oval_vmargin=0.16,
            max_block_w=max_block_w,max_block_h=max_block_h,
        )
        if best is None or best[0]<comfortable_min:
            relaxed=_try_fit_boxes(
                render_text,boxes,info,8,max(8,cap),
                safe_mask=safe_mask,safe_origin=safe_origin,
                w_scale=0.91,h_scale=0.84,oval_pad=0.89,oval_vmargin=0.07,
                max_block_w=0,max_block_h=0,
            )
            if relaxed is not None and (best is None or relaxed[0]>best[0]):
                best=relaxed;used_w_scale=0.91;used_h_scale=0.84;max_block_w=0;max_block_h=0

        if best is None:
            bx0,by0,bx1,by1=[int(round(v)) for v in bubble.box]
            full_box=_clamp_box_in_page([bx0+2,by0+2,max(bx0+14,bx1-2),max(by0+14,by1-2)],image.size)
            full_cap=min(cap,max(8,int(min(full_box[2]-full_box[0],full_box[3]-full_box[1])*.85)))
            best=_try_fit_boxes(render_text,[full_box],info,8,full_cap,w_scale=0.92,h_scale=0.88)
            if best is not None:
                found=False
        if best is None:
            overflow_fallback=True;found=False
            bx0,by0,bx1,by1=[float(v) for v in bubble.box]
            span=min(max(24.0,bx1-bx0),max(24.0,by1-by0))
            size=int(np.clip(round(min(cap,span*.30)),10,18))
            wrap_w=max(40,min(image.width-12,int(round(max(bx1-bx0,80)*1.10))))
            while size>8:
                with TextRenderer(size,info.path,info.family,info.weight,info.italic) as renderer:
                    lines=_force_wrap_text(render_text,wrap_w,renderer)
                    tile=renderer.render(lines)
                if tile.width<=image.width-6 and tile.height<=image.height-6:
                    break
                size-=1
            else:
                with TextRenderer(size,info.path,info.family,info.weight,info.italic) as renderer:
                    lines=_force_wrap_text(render_text,wrap_w,renderer)
                    tile=renderer.render(lines)
            bw_pad=max(12,tile.width+6);bh_pad=max(12,tile.height+6)
            box=_clamp_box_in_page([cx-bw_pad/2.0,cy-bh_pad/2.0,cx+bw_pad/2.0,cy+bh_pad/2.0],image.size)
        else:
            size,lines,tile,box,fit_max_w,fit_max_h=best
            if '\n' not in render_text and len(lines)>1:
                largest=size;count=len(lines);choice=None
                for candidate_size in range(largest,max(8,int(np.ceil(largest*.85)))-1,-1):
                    with TextRenderer(candidate_size,info.path,info.family,info.weight,info.italic) as renderer:
                        for k,candidate_box in enumerate([box]+boxes):
                            width=candidate_box[2]-candidate_box[0];height=candidate_box[3]-candidate_box[1]
                            aspect=height/max(1,width)
                            cw=int(width*(used_w_scale if aspect>1.35 else min(0.90,used_w_scale+0.03)))
                            ch=int(height*(used_h_scale if aspect>1.35 else min(0.84,used_h_scale+0.08)))
                            if k==0:
                                cw,ch=fit_max_w,fit_max_h
                            if max_block_w>0:cw=min(cw,int(max_block_w))
                            if max_block_h>0:ch=min(ch,int(max_block_h))
                            if cw<18 or ch<14 or (count-1)*candidate_size*1.2>ch:continue
                            candidate=manga_wrap(render_text,cw,renderer,count)
                            if not candidate or len(candidate)!=count:continue
                            candidate_tile=renderer.render(candidate)
                            if candidate_tile.width>cw or candidate_tile.height>ch:continue
                            score=manga_line_score([renderer.measure(line)[0] for line in candidate])+.25*(largest-candidate_size)/largest
                            if choice is None or score<choice[0]:choice=(score,candidate_size,candidate,candidate_tile,candidate_box)
                    if choice is not None and .25*(largest-candidate_size+1)/largest>choice[0]:break
                if choice is not None:
                    _,size,lines,tile,box=choice
            bx_c=(box[0]+box[2])/2.0;by_c=(box[1]+box[3])/2.0
            pad_w=max(12,tile.width+6);pad_h=max(12,tile.height+6)
            box=_clamp_box_in_page([bx_c-pad_w/2.0,by_c-pad_h/2.0,bx_c+pad_w/2.0,by_c+pad_h/2.0],image.size)

    if found and not (c_width or c_size) and safe_mask is not None:
        # Check the shaped ink itself, including Thai combining marks, after wrapping.
        center_x=(box[0]+box[2])/2
        center_y=(box[1]+box[3])/2
        while True:
            x=round(center_x-tile.width/2)-safe_origin[0]
            y=round(center_y-tile.height/2)-safe_origin[1]
            if x>=0 and y>=0 and x+tile.width<=safe_mask.shape[1] and y+tile.height<=safe_mask.shape[0]:
                interior=safe_mask[y:y+tile.height,x:x+tile.width]
                if not np.any((np.asarray(tile.getchannel('A'))>0)&(interior==0)):
                    break
            if size<=8:
                overflow_fallback=True
                break
            size-=1
            with TextRenderer(size,info.path,info.family,info.weight,info.italic) as renderer:
                tile=renderer.render(lines)
            box=_clamp_box_in_page([center_x-tile.width/2-3,center_y-tile.height/2-3,
                                    center_x+tile.width/2+3,center_y+tile.height/2+3],image.size)
    color=_sample_text_color(image,box)
    out={'text':'\n'.join(lines),'translation':raw_text,'box':box,'bubble_box':list(bubble.box),
         'font':info.postscript,'font_size':size,'color':color,'auto_leading':True,
         'alignment':'center','layout_version':'3.1','paragraph_style':'manga-centred',
         'boundary_found':bool(found),'needs_review':bool(overflow_fallback or not found or size<14),**CHARACTER_STYLE}
    if chosen!=FONT_NAME:out['font_choice']=chosen
    if c_size>0:out['custom_size']=c_size
    if c_width>0:out['custom_width']=c_width
    return out


def validate_layer(layer):
    if not isinstance(layer,dict):raise ValueError('ข้อมูลชั้นข้อความไม่ถูกต้อง')
    for key in ('text','translation'):
        if not isinstance(layer.get(key),str) or not layer[key].strip() or len(layer[key])>20000:raise ValueError('ข้อความใน layer ไม่ถูกต้อง')
    for key in ('box','bubble_box'):
        box=layer.get(key)
        if not isinstance(box,list) or len(box)!=4 or not all(isinstance(v,(int,float)) and np.isfinite(v) for v in box) or box[0]<0 or box[1]<0 or box[2]<=box[0] or box[3]<=box[1]:raise ValueError('พิกัด Text ไม่ถูกต้อง')
    size=layer.get('font_size')
    if not isinstance(size,(int,float)) or not np.isfinite(size) or not 6<=size<=220:raise ValueError('ขนาด Text ไม่ถูกต้อง')
    choice=layer.get('font_choice',FONT_NAME)
    ps_font=layer.get('font')
    if choice not in FONT_CHOICES or not isinstance(ps_font,str) or not ps_font.strip():
        raise ValueError('รูปแบบฟอนต์ Text ไม่ตรงกับค่าที่รองรับ')
    if choice==FONT_NAME and ps_font!=FONT_POSTSCRIPT:
        raise ValueError('รูปแบบ Text ไม่ตรงกับค่าที่รองรับ')
    if not (isinstance(layer.get('color'),list) and len(layer['color'])==3 and all(type(c) is int and 0<=c<=255 for c in layer['color'])) or layer.get('auto_leading') is not True:
        raise ValueError('รูปแบบ Text ไม่ตรงกับค่าที่รองรับ')
    if any(layer.get(key,value)!=value for key,value in CHARACTER_STYLE.items()):raise ValueError('ค่า Character ต้องเป็น Tracking 0 / Metrics / Sharp / Scale 100% / Baseline 0 / English: UK')
    angle=layer.get('rotation',0)
    if not isinstance(angle,(int,float)) or not np.isfinite(angle) or abs(angle)>180:raise ValueError('มุม Text ไม่ถูกต้อง')
    if angle:
        size=layer.get('text_size');q=layer.get('bubble_quad')
        has_custom_rot=_normalize_rotation(layer.get('custom_rotation')) is not None
        if (not has_custom_rot and q and not valid_quad(q)) or (not has_custom_rot and not q):
            raise ValueError('พื้นที่ Text เอียงไม่ถูกต้อง')
        if not isinstance(size,list) or len(size)!=2 or not all(isinstance(v,(int,float)) and np.isfinite(v) and v>0 for v in size):
            raise ValueError('พื้นที่ Text เอียงไม่ถูกต้อง')
        r=np.radians(angle);w,h=size;b=layer['box']
        if not np.allclose([b[2]-b[0],b[3]-b[1]],[abs(w*np.cos(r))+abs(h*np.sin(r)),abs(w*np.sin(r))+abs(h*np.cos(r))],atol=.25):
            raise ValueError('ขนาด Text ไม่ตรงกับมุมเอียง')


def is_current(bubble):
    layer=getattr(bubble,'text_layer',None)
    if not layer:return False
    chosen=normalize_font_choice(getattr(bubble,'font_choice',None) or FONT_NAME)
    layer_font=normalize_font_choice(layer.get('font_choice',FONT_NAME))
    c_size=int(getattr(bubble,'custom_size',0) or 0)
    c_width=int(getattr(bubble,'custom_width',0) or 0)
    c_off=_normalize_offset(getattr(bubble,'text_offset',[]))
    c_rot=_normalize_rotation(getattr(bubble,'custom_rotation',None))
    l_rot=_normalize_rotation(layer.get('custom_rotation',None))
    font_ok=(layer_font==chosen or layer_font==FONT_NAME)
    return bool(layer['translation']==bubble.translation.strip().replace('\r\n','\n').replace('\r','\n')
                and layer['bubble_box']==list(bubble.box)
                and layer.get('bubble_quad',[])==getattr(bubble,'quad',[])
                and font_ok
                and int(layer.get('custom_size',0) or 0)==c_size
                and int(layer.get('custom_width',0) or 0)==c_width
                and _normalize_offset(layer.get('text_offset',[]))==c_off
                and l_rot==c_rot)


@lru_cache(maxsize=256)
def rendered_tile(text,size,font_choice=FONT_NAME):
    from windows_text import TextRenderer
    render_text,info,_=_prepare_render_text_and_font(text,font_choice)
    with TextRenderer(round(size),info.path,info.family,info.weight,info.italic) as renderer:
        return renderer.render(render_text.split('\n'))


def composite_text_crop(crop,bubbles,origin):
    out=crop.convert('RGBA');x,y=origin
    for bubble in bubbles:
        if not is_current(bubble):continue
        layer=bubble.text_layer;b=layer['box']
        stroke_cfg=layer.get('stroke') or {}
        pad=(max(1,min(20,int(stroke_cfg.get('size',3))))+4) if stroke_cfg.get('enabled') else 0
        if b[2]+pad<=x or b[0]-pad>=x+crop.width or b[3]+pad<=y or b[1]-pad>=y+crop.height:continue
        tile=rendered_tile(layer['text'],layer['font_size'],layer.get('font_choice',FONT_NAME))
        if layer.get('color')==[255,255,255]:
            white=Image.new('RGBA',tile.size,(255,255,255,0))
            white.putalpha(tile.getchannel('A'));tile=white
        if layer.get('rotation'):
            tile=tile.rotate(-layer['rotation'],resample=Image.Resampling.BICUBIC,expand=True)
            bounds=tile.getchannel('A').getbbox()
            if bounds:tile=tile.crop(bounds)
        if any((layer.get(k) or {}).get('enabled') for k in ('stroke','gradient')):
            from text_style_sampler import styled_tile
            tile=styled_tile(tile,layer)
        left=round((b[0]+b[2]-tile.width)/2)-x;top=round((b[1]+b[3]-tile.height)/2)-y
        out.alpha_composite(tile,(left,top))
    return out.convert('RGB')
