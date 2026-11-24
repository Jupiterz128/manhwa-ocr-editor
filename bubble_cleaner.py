"""Local, mask-limited speech-bubble cleaning. Original images are never written."""
from __future__ import annotations
import base64
import io
import os
from dataclasses import dataclass
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
from frame_geometry import paint_shape, valid_quad, outline_read

COMPONENT_VERSION='4.0'

LAMA_URL='https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt'


def lama_model_path():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'ManhwaOCREditor'/'lama_models'/'big-lama.pt'


class LamaModel:
    """TorchScript LaMa with Snap-to-8 Dynamic ROI (96 px context) + Reflection Padding.

    Providing 96 px of clean surrounding structure lets Fast Fourier Convolutions
    continue hair strands, collar lines, window frames and wall tiles straight
    across removed lettering without pulling distant panel borders into the fill.
    """
    def __init__(self):
        import torch
        path=lama_model_path()
        if not path.is_file():
            raise RuntimeError('ยังไม่มีโมเดล LaMa ให้รัน install_clean_lama_windows.bat หรือเลือก inpaint')
        self.torch=torch
        self.device=torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.fallback_reason=''
        if self.device.type=='cpu':torch.set_num_threads(max(1,min(4,torch.get_num_threads())))
        try:self.model=torch.jit.load(str(path),map_location=self.device).eval()
        except torch.cuda.OutOfMemoryError:
            self.device=torch.device('cpu');self.fallback_reason='หน่วยความจำ GPU ไม่พอ ใช้ LaMa บน CPU ที่ความละเอียดเดิม'
            torch.cuda.empty_cache();torch.set_num_threads(max(1,min(4,torch.get_num_threads())))
            self.model=torch.jit.load(str(path),map_location=self.device).eval()

    def __call__(self,image,mask):
        try:return self._predict(image,mask)
        except self.torch.cuda.OutOfMemoryError:
            if self.device.type!='cuda':raise
            self.device=self.torch.device('cpu');self.model=self.model.to(self.device)
            self.torch.cuda.empty_cache()
            self.torch.set_num_threads(max(1,min(4,self.torch.get_num_threads())))
            self.fallback_reason='หน่วยความจำ GPU ไม่พอ ใช้ LaMa บน CPU ที่ความละเอียดเดิม'
        return self._predict(image,mask)

    def _predict(self,image,mask):
        full_rgb=np.asarray(image.convert('RGB'))
        full_mask=(np.asarray(mask)>0)
        if not np.any(full_mask):
            return Image.fromarray(full_rgb.copy())
        H,W=full_rgb.shape[:2]
        ys,xs=np.nonzero(full_mask)
        ctx=96
        x0=max(0,int(xs.min())-ctx);y0=max(0,int(ys.min())-ctx)
        x1=min(W,int(xs.max())+1+ctx);y1=min(H,int(ys.max())+1+ctx)
        if x1-x0<48 and W>=48:
            mid=(x0+x1)//2;x0=max(0,min(W-48,mid-24));x1=x0+48
        if y1-y0<48 and H>=48:
            mid=(y0+y1)//2;y0=max(0,min(H-48,mid-24));y1=y0+48
        crop_rgb=full_rgb[y0:y1,x0:x1]
        crop_m=full_mask[y0:y1,x0:x1]
        rh,rw=crop_rgb.shape[:2]
        ph,pw=(-rh)%8,(-rw)%8
        pad_mode='reflect' if (rh>ph and rw>pw and rh>=2 and rw>=2) else 'symmetric'
        a=crop_rgb.astype(np.float32)/255.0
        b=crop_m.astype(np.float32)
        if ph or pw:
            a=np.pad(a,((0,ph),(0,pw),(0,0)),mode=pad_mode)
            b=np.pad(b,((0,ph),(0,pw)),mode=pad_mode)
        t=self.torch
        inp=t.from_numpy(a.transpose(2,0,1).copy()).unsqueeze(0).to(self.device)
        m=t.from_numpy(b.copy()).unsqueeze(0).unsqueeze(0).to(self.device)
        with t.inference_mode():out=self.model(inp,m)
        pixels=out[0].permute(1,2,0).detach().cpu().numpy()[:rh,:rw]
        if not np.all(np.isfinite(pixels)):raise RuntimeError('LaMa คืนค่าสีที่ไม่ถูกต้อง')
        patch=np.clip(np.rint(pixels*255.0),0,255).astype(np.uint8)
        canvas=full_rgb.copy()
        canvas[y0:y1,x0:x1]=patch
        return Image.fromarray(canvas)


@dataclass
class CleanRegion:
    box: list
    image: Image.Image
    mask: np.ndarray
    safe: np.ndarray
    boundary_found: bool
    note: str = ''
    manual_frame: bool = False
    manual_edited: bool = False
    auto_limit: np.ndarray | None = None


def bounded_box(box, size, pad=0):
    w,h=size
    x0,y0,x1,y1=[int(round(v)) for v in box]
    return [max(0,x0-pad),max(0,y0-pad),min(w,x1+pad),min(h,y1+pad)]


def _rect(mask, box, value=255, pad=0):
    x0,y0,x1,y1=bounded_box(box,(mask.shape[1],mask.shape[0]),pad)
    if x1>x0 and y1>y0:
        mask[y0:y1,x0:x1]=value


def background_samples(mask, safe):
    """Use only interior context, away from both glyph halos and bubble borders."""
    distance=cv2.distanceTransform((safe>0).astype(np.uint8),cv2.DIST_L2,5)
    radius=float(distance.max())
    gap=max(2,min(10,round(radius*.22)))
    edge=max(1,min(5,round(radius*.11)))
    exclusion=cv2.dilate((mask>0).astype(np.uint8),np.ones((2*gap+1,2*gap+1),np.uint8))
    interior=cv2.erode((safe>0).astype(np.uint8),np.ones((2*edge+1,2*edge+1),np.uint8),
                       borderType=cv2.BORDER_CONSTANT,borderValue=0)>0
    return interior & (exclusion==0)


def _carve_border_spikes(gray, safe, core, bubble_local_box, margin, bw, bh):
    """Protect radial speedlines — including floating diagonal arcs — in unclosed light balloons."""
    if bw/max(1,bh)>2.8 or not np.any(safe):return safe
    if float(np.median(gray[safe>0]))<205.0:return safe
    out=safe.copy()
    collar_scope=np.zeros_like(safe)
    _rect(collar_scope,bubble_local_box,pad=4)
    blackhat=cv2.morphologyEx(gray,cv2.MORPH_BLACKHAT,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(15,15)))
    ink=((blackhat>30)&(collar_scope>0)).astype(np.uint8)*255
    ink=cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    outside=(safe==0)&(collar_scope>0)
    x0,y0,x1,y1=bubble_local_box
    cx=(x0+x1)/2.0;cy=(y0+y1)/2.0
    rx=max(1.0,(x1-x0)/2.0);ry=max(1.0,(y1-y0)/2.0)
    yy,xx=np.indices(safe.shape)
    r2=((xx-cx)/rx)**2+((yy-cy)/ry)**2
    core_guard=cv2.dilate(core,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(21,21)))>0 if np.any(core) else np.zeros_like(safe,bool)
    n,labels,stats,_=cv2.connectedComponentsWithStats(ink)
    spikes=np.zeros_like(safe)
    for i in range(1,n):
        pixels=stats[i,cv2.CC_STAT_AREA]
        if pixels<5:continue
        comp=labels==i
        if np.count_nonzero(comp&(core>0))/max(1,pixels)>=.15:continue
        touches_wall=np.count_nonzero(comp&outside)>=2
        outer_arc=(bw/max(1,bh)<=2.5 and not np.any(comp&core_guard)
                   and np.count_nonzero(comp&(r2>.46))>=pixels*.60)
        if touches_wall or outer_arc:
            spikes[comp]=255
    if np.any(spikes):
        pad=max(4,min(8,margin))
        spikes=cv2.dilate(spikes,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*pad+1,2*pad+1)))
        out[spikes>0]=0
    if bw/max(1,bh)<=2.5:
        corners=r2>1.04
        if np.any(core):corners &= ~core_guard
        out[corners]=0
    return out


def _outlined_floating_mask(arr, gray, safe, line_shapes, grow):
    """Detect floating text with a white/dark sticker outline over comic artwork.

    Examples: `NO. THAT'S...` over a character's neck/shirt or `THE SWAP...` over
    a wall/window. Masks ONLY the letter strokes + their attached sticker border,
    leaving surrounding skin, hair, clothing and wall tiles 100% untouched.
    """
    if not line_shapes or not np.any(safe):
        return None
    line_union=np.zeros_like(safe)
    for s in line_shapes:
        line_union=cv2.bitwise_or(line_union,s)
    # Tight search band around actual OCR lines (never across unrelated neck/hair).
    band=cv2.dilate(line_union,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(23,23)))
    band_zone=(band>0)&(safe>0)
    in_lines=(cv2.dilate(line_union,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))>0)&(safe>0)
    if np.count_nonzero(in_lines)<30:
        return None
    min_ch=np.min(arr,axis=2)
    max_ch=np.max(arr,axis=2)
    sat=max_ch.astype(np.int16)-min_ch.astype(np.int16)
    # Dark ink strokes inside OCR line boxes + pure neutral white sticker halo around them.
    bh_ink=cv2.morphologyEx(gray,cv2.MORPH_BLACKHAT,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(17,17)))
    dark_strokes=((gray<=115)|(bh_ink>=26))&in_lines
    n_dark=int(np.count_nonzero(dark_strokes))
    if n_dark>=16:
        near_dark=cv2.dilate(dark_strokes.astype(np.uint8)*255,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(21,21)))>0
        ring_only=near_dark&(~dark_strokes)&band_zone
        white_sticker=(min_ch>=220)&(sat<=26)&ring_only
        n_white=int(np.count_nonzero(white_sticker))
        # Outside the sticker band, the artwork is NOT pure white (e.g. skin, shirt, wall).
        outer_probe=(cv2.dilate(band,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(31,31)))>0)&(band==0)&(safe>0)
        outer_not_white=(np.count_nonzero(outer_probe)<30 or float(np.median(min_ch[outer_probe]))<238.0)
        if n_white>=max(25,int(n_dark*.35)) and outer_not_white:
            # Keep only white-sticker components directly touching dark_strokes!
            seed=cv2.dilate(dark_strokes.astype(np.uint8)*255,np.ones((3,3),np.uint8))>0
            combined=(dark_strokes|white_sticker).astype(np.uint8)*255
            num,labels,stats,_=cv2.connectedComponentsWithStats(combined)
            kept=np.zeros_like(safe)
            for idx in range(1,num):
                comp=(labels==idx)
                if np.any(comp&seed):
                    kept[comp]=255
            if np.any(kept):
                kept=cv2.morphologyEx(kept,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
                pad_px=max(2,min(4,grow//2+1))
                kept=cv2.dilate(kept,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*pad_px+1,2*pad_px+1)))
                return cv2.bitwise_and(kept,safe)
    return None


def _glowing_text_mask(arr, gray, safe, bubble_local_box, line_boxes, grow, tight=False):
    """Recover bright/glowing text on genuine dark UI panels without masking skin/faces."""
    inner_box=np.zeros_like(safe)
    _rect(inner_box,bubble_local_box,pad=4)
    zone=(inner_box>0)&(safe>0)
    if np.count_nonzero(zone)<80:return np.zeros_like(safe)
    if float(np.median(gray[zone]))>=155.0:return np.zeros_like(safe)
    # Reject comic panels that mix bright skin/sky with dark clothes (high neutral spread outside strokes).
    if float(np.percentile(gray[zone],80))>=175.0 and float(np.percentile(gray[zone],20))<=80.0:
        # Check if the bright upper/side region is broad smooth flesh/wall rather than thin strokes.
        smooth_bright=(gray>=165)&(cv2.morphologyEx(gray,cv2.MORPH_OPEN,np.ones((19,19),np.uint8))>=155)&zone
        if np.count_nonzero(smooth_bright)>np.count_nonzero(zone)*.14:
            return np.zeros_like(safe)
    heights=[b[3]-b[1] for b in line_boxes if b[3]-b[1]>=6]
    bh=max(8,bubble_local_box[3]-bubble_local_box[1])
    bw=max(8,bubble_local_box[2]-bubble_local_box[0])
    ref_h=float(np.median(heights)) if heights else min(32.0,bh*.50)
    achrom=np.min(arr,axis=2)
    max_ch=np.max(arr,axis=2)
    sat=max_ch.astype(np.int16)-achrom.astype(np.int16)
    # Stroke-scale opening (<= 31 px) isolates glyph strokes without accepting wide necks/faces.
    kw_stroke=max(19,min(31,(max(int(ref_h),20)//2)*2+1))
    k_el=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(kw_stroke,kw_stroke))
    bg=cv2.morphologyEx(achrom,cv2.MORPH_OPEN,k_el)
    lift=achrom.astype(np.int16)-bg.astype(np.int16)
    floor=max(125.0,min(165.0,float(np.percentile(achrom[zone],30))+45.0))
    cores=(lift>=35)&(achrom>=floor)&(sat<=np.maximum(75,(max_ch*.45)))&zone
    if not np.any(cores):return np.zeros_like(safe)
    gap_w=max(6,min(26,int(round(ref_h*.55))))
    grouped=cv2.morphologyEx(cores.astype(np.uint8)*255,cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_RECT,(gap_w,5)))
    n,labels,stats,_=cv2.connectedComponentsWithStats(grouped)
    result=np.zeros_like(safe)
    eroded_safe=cv2.erode(safe,np.ones((3,3),np.uint8))
    for i in range(1,n):
        wx,wy,ww,wh,warea=stats[i]
        if warea<6 or wh<4 or wh>max(bh*.92,ref_h*2.6):continue
        if (ww>bw*.92 and wh<6) or (wh>bh*.88 and ww<8):continue
        comp=((labels==i)&cores).astype(np.uint8)*255
        if not np.any((comp>0)&(eroded_safe>0)):continue
        if tight:
            r_hug=max(4,min(7,grow+1))
            expanded=cv2.dilate(comp,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*r_hug+1,2*r_hug+1)))
        else:
            halo=max(grow+3,min(28,round(max(ref_h,float(wh))*.28)+grow))
            expanded=cv2.dilate(comp,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*halo+1,2*halo+1)))
        result=cv2.bitwise_or(result,expanded)
    if tight and np.any(result):
        bg_max=cv2.morphologyEx(max_ch,cv2.MORPH_OPEN,k_el)
        aura_lift=max_ch.astype(np.int16)-bg_max.astype(np.int16)
        near_glyph=cv2.dilate(result,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(15,15)))>0
        aura=((aura_lift>=45)&near_glyph&zone).astype(np.uint8)*255
        if np.any(aura):
            aura=cv2.dilate(aura,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
            result=cv2.bitwise_or(result,aura)
    return cv2.bitwise_and(result,safe)


def _needs_stroke_mask(arr, gray, safe, core):
    """Fast O(pixels) check for textured/crest/artwork backgrounds."""
    ring_free=(safe>0)&(cv2.dilate(core,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(11,11)))==0)
    if np.count_nonzero(ring_free)<80:return False
    pts=arr[ring_free]
    if len(pts)>4000:
        pts=pts[::max(1,len(pts)//4000)]
    med=np.median(pts,axis=0)
    p85=float(np.percentile(np.max(np.abs(pts.astype(float)-med),axis=1),85))
    local_step=cv2.morphologyEx(gray,cv2.MORPH_GRADIENT,np.ones((3,3),np.uint8))
    if float(np.percentile(local_step[ring_free],95))>12.0:
        return True
    if p85<=10.0:return False
    return float(np.percentile(local_step[ring_free],88))>5.5


def _stroke_hugging_mask(arr, gray, safe, bubble_local_box, line_shapes, support, grow):
    """Tight glyph + attached-outline/aura mask preserving surrounding artwork."""
    sticker=_outlined_floating_mask(arr,gray,safe,line_shapes,grow)
    if sticker is not None and np.any(sticker):
        return sticker
    line_union=np.zeros_like(safe)
    for s in line_shapes:line_union=cv2.bitwise_or(line_union,s)
    # Constrain stroke detection tightly around OCR line boxes (11 px), not wide `support`.
    tight_band=cv2.dilate(line_union,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(17,17))) if np.any(line_union) else support
    zone=(tight_band>0)&(safe>0)
    box_mask=np.zeros_like(safe);_rect(box_mask,bubble_local_box,pad=4)
    full_zone=((box_mask>0)|(support>0))&(safe>0)
    if not np.any(full_zone):return support.copy()
    med_g=float(np.median(gray[zone if np.any(zone) else full_zone]))
    bh=max(8,bubble_local_box[3]-bubble_local_box[1])
    r_hug=max(3,min(6,grow))
    hug_el=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*r_hug+1,2*r_hug+1))
    if med_g<155.0:
        achrom=np.min(arr,axis=2)
        max_ch=np.max(arr,axis=2)
        kw=max(19,min(31,(max(int(bh),24)//3)*2+1))
        k_el=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(kw,kw))
        bg_min=cv2.morphologyEx(achrom,cv2.MORPH_OPEN,k_el)
        lift_min=achrom.astype(np.int16)-bg_min.astype(np.int16)
        floor=max(115.0,min(155.0,float(np.percentile(achrom[full_zone],30))+38.0))
        glyphs=((lift_min>=24)&(achrom>=floor)&zone).astype(np.uint8)*255
        glyphs=cv2.morphologyEx(glyphs,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(3,3)))
        hugged=cv2.dilate(glyphs,hug_el)
        bg_max=cv2.morphologyEx(max_ch,cv2.MORPH_OPEN,k_el)
        aura_lift=max_ch.astype(np.int16)-bg_max.astype(np.int16)
        near_glyph=cv2.dilate(glyphs,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(15,15)))>0
        aura=((aura_lift>=44)&near_glyph&zone).astype(np.uint8)*255
        if np.any(aura):
            aura=cv2.dilate(aura,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
            hugged=cv2.bitwise_or(hugged,aura)
    else:
        kw=max(15,min(35,(max(int(bh),28)//3)*2+1))
        k_el=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(kw,kw))
        bh_ink=cv2.morphologyEx(gray,cv2.MORPH_BLACKHAT,k_el)
        strokes=((bh_ink>=24)&zone).astype(np.uint8)*255
        strokes=cv2.morphologyEx(strokes,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
        hugged=cv2.dilate(strokes,hug_el)
    fallback_el=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*max(2,grow-1)+1,2*max(2,grow-1)+1))
    for shape in line_shapes:
        s_px=np.count_nonzero((shape>0)&(safe>0))
        if s_px>0 and np.count_nonzero((hugged>0)&(shape>0))<s_px*.10:
            hugged=cv2.bitwise_or(hugged,cv2.dilate(shape,fallback_el))
    return cv2.bitwise_and(hugged,safe)


def auto_region(image, bubble, cached=None):
    """Build an OCR-backed mask, keeping verification retries inside the same safe zone."""
    import copy
    if cached is not None and cached.manual_edited:
        return cached
    if not any(c.isalnum() for c in bubble.text or '') or not bubble.score > 0:
        return None
    x,y,r,b = bounded_box(bubble.box, image.size)
    if r <= x or b <= y:
        return None
    lines = []
    for rec in bubble.lines:
        if not rec.get('box') or not any(c.isalnum() for c in rec.get('text',bubble.text)):
            continue
        l,t,rr,bb = bounded_box(rec['box'],image.size)
        clipped = [max(x,l),max(y,t),min(r,rr),min(b,bb)]
        if clipped[2]>clipped[0] and clipped[3]>clipped[1]:
            lines.append(dict(rec,box=clipped))
    large = (r-x) > .6*image.width and (b-y) > .35*image.height
    if large and not lines:
        return None
    target = copy.copy(bubble)
    target.lines = lines
    if lines:
        boxes = [rec['box'] for rec in lines]
        area = sum((rr-l)*(bb-t) for l,t,rr,bb in boxes)
        if large or (r-x)*(b-y)>7*area:
            if large and any((rr-l)>.6*image.width and (bb-t)>.35*image.height for l,t,rr,bb in boxes):
                return None
            target.box = bounded_box([min(v[0] for v in boxes), min(v[1] for v in boxes),
                                      max(v[2] for v in boxes), max(v[3] for v in boxes)],image.size,8)
            target.quad = []
    if cached is not None and cached.auto_limit is not None:
        return cached
    region = prepare_region(image,target,margin=6)
    safe = region.safe
    ox,oy = region.box[:2]
    support = safe.copy()
    if lines:
        support.fill(0)
        for rec in lines:
            l,t,rr,bb = rec['box']
            _rect(support,[l-ox,t-oy,rr-ox,bb-oy],pad=8)
    gray = np.asarray(region.image.convert('L'))
    count, labels, stats, _ = cv2.connectedComponentsWithStats((gray<90).astype(np.uint8))
    protected = np.zeros(count, dtype=np.uint8)
    for i in range(1,count):
        l,t,w,h,pixels = stats[i]
        if w < 0.45*(r-x) and h < 0.45*(b-y):
            continue
        touches = l+ox<=x+2 or t+oy<=y+2 or l+w+ox>=r-2 or t+h+oy>=b-2
        ring = w>.6*(r-x) and h>.6*(b-y) and pixels<.35*w*h
        if (touches or ring) and pixels < .55*w*h:
            protected[i] = 255
    walls = cv2.dilate(protected[labels], cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(11,11)))
    safe[walls>0] = 0
    region.auto_limit = safe & support
    region.mask &= region.auto_limit
    return region


def prepare_region(image, bubble, margin=6, grow=6, coverage=True):
    """Find a closed enclosing boundary, then mask glyphs only inside OCR lines."""
    margin=int(np.clip(margin,2,30));grow=int(np.clip(grow,0,20))
    bw=abs(bubble.box[2]-bubble.box[0]);bh=abs(bubble.box[3]-bubble.box[1])
    manual_quad=getattr(bubble,'quad',[])
    box=bounded_box(bubble.box,image.size,max(20,min(180,round(max(bw,bh)*.55))))
    if box[2]-box[0]<8 or box[3]-box[1]<8:
        raise ValueError('กรอบเล็กเกินไปสำหรับคลีน')
    if (box[2]-box[0])*(box[3]-box[1])>12_000_000:
        raise ValueError('กรอบใหญ่เกินไป กรุณาเลือกเฉพาะบับเบิ้ล')
    crop=image.crop(box).convert('RGB');arr=np.asarray(crop)
    h,w=arr.shape[:2]
    bubble_local=[bubble.box[0]-box[0],bubble.box[1]-box[1],bubble.box[2]-box[0],bubble.box[3]-box[1]]
    gray=cv2.cvtColor(arr,cv2.COLOR_RGB2GRAY)
    box_probe=np.zeros((h,w),np.uint8);_rect(box_probe,bubble_local)
    box_gray_med=float(np.median(gray[box_probe>0])) if np.any(box_probe) else 255.0
    raw_records=[]
    ignored_outlines=0
    for record in bubble.lines:
        b=record.get('box')
        if b and len(b)==4:
            if outline_read(record,bubble.box):
                ignored_outlines+=1;continue
            lb=bounded_box([b[0]-box[0],b[1]-box[1],b[2]-box[0],b[3]-box[1]],(w,h))
            if lb[2]>lb[0] and lb[3]>lb[1]:
                raw_records.append((record,lb))
    if box_gray_med>=200.0 and bw/max(1,bh)<=2.5 and len(raw_records)>=2:
        cx_b=(bubble_local[0]+bubble_local[2])/2.0;cy_b=(bubble_local[1]+bubble_local[3])/2.0
        rx_b=max(1.0,bw/2.0);ry_b=max(1.0,bh/2.0)
        def _is_real_word(rec,lb):
            txt=str(rec.get('text','')).strip()
            lx=(lb[0]+lb[2])/2.0;ly=(lb[1]+lb[3])/2.0
            lr2=((lx-cx_b)/rx_b)**2+((ly-cy_b)/ry_b)**2
            has_alpha=any(c.isalpha() and c not in 'lI' for c in txt)
            return has_alpha or lr2<=.46 or len(txt)>4
        if any(_is_real_word(rec,lb) for rec,lb in raw_records):
            raw_records=[(rec,lb) for rec,lb in raw_records if _is_real_word(rec,lb)]
    line_boxes=[];line_shapes=[]
    for record,lb in raw_records:
        line_boxes.append(lb)
        shape=np.zeros((h,w),np.uint8)
        if valid_quad(record.get('quad',[])):paint_shape(shape,record,box[:2])
        else:_rect(shape,lb)
        line_shapes.append(shape)
    core=np.zeros((h,w),np.uint8)
    for shape in line_shapes:core=cv2.bitwise_or(core,cv2.dilate(shape,np.ones((3,3),np.uint8)))
    centers=[((b[0]+b[2])/2,(b[1]+b[3])/2) for b in line_boxes]
    if not centers:
        centers=[((bubble.box[0]+bubble.box[2])/2-box[0],(bubble.box[1]+bubble.box[3])/2-box[1])]
    edges=cv2.Canny(gray,25,70)
    for channel in cv2.split(arr):edges=cv2.bitwise_or(edges,cv2.Canny(channel,25,70))
    closed=cv2.morphologyEx(edges,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    # Prevent punctuation near bubble walls (e.g. `CONTACT—`) from bridging into the wall contour.
    wall_edges=edges.copy()
    if np.any(core):wall_edges[core>0]=0
    closed_bridge=cv2.morphologyEx(wall_edges,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
    candidates=[]
    minimum=max(100,int(np.count_nonzero(core)*1.6),bw*bh*.22)
    manual=valid_quad(manual_quad)
    selection=np.zeros_like(core)
    if manual:
        paint_shape(selection,{'quad':manual_quad},box[:2])
        minimum=max(100,np.count_nonzero(selection)*.65)
        centers=[tuple(np.asarray(manual_quad).mean(axis=0)-box[:2])]
    # Fill text holes inside light threshold candidates so near-wall glyphs don't dent the contour.
    th215=cv2.threshold(gray,215,255,cv2.THRESH_BINARY)[1]
    th235=cv2.threshold(gray,235,255,cv2.THRESH_BINARY)[1]
    if np.any(core):
        th215=cv2.bitwise_or(th215,core)
        th235=cv2.bitwise_or(th235,core)
    binaries=[closed,closed_bridge,th215,th235,
              cv2.morphologyEx(cv2.threshold(gray,50,255,cv2.THRESH_BINARY_INV)[1],cv2.MORPH_CLOSE,np.ones((3,3),np.uint8)),
              cv2.morphologyEx(cv2.threshold(gray,80,255,cv2.THRESH_BINARY_INV)[1],cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))]
    if manual:
        low=cv2.Canny(gray,12,35)
        for channel in cv2.split(arr):low|=cv2.Canny(channel,12,35)
        binaries.append(cv2.morphologyEx(low,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8)))
        local_color=np.median(arr[selection>0],axis=0)
        near_color=(np.max(np.abs(arr.astype(float)-local_color),axis=2)<=18).astype(np.uint8)*255
        binaries.append(cv2.morphologyEx(near_color,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8)))
    else:
        bg_zone=np.zeros_like(core)
        _rect(bg_zone,bubble_local)
        bg_pts=arr[(bg_zone>0)&(core==0)]
        if len(bg_pts)>=60:
            local_color=np.median(bg_pts,axis=0)
            if np.percentile(np.max(np.abs(bg_pts.astype(float)-local_color),axis=1),80)<=20:
                near_color=(np.max(np.abs(arr.astype(float)-local_color),axis=2)<=18).astype(np.uint8)*255
                binaries.append(cv2.morphologyEx(near_color,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8)))
    for binary in binaries:
        contours,_=cv2.findContours(binary,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            a=cv2.contourArea(c)
            x,y,cw,ch=cv2.boundingRect(c)
            if a<minimum or a>w*h*.96 or cw<20 or ch<20:continue
            if x<=1 or y<=1 or x+cw>=w-1 or y+ch>=h-1:continue
            if not all(cv2.pointPolygonTest(c,point,False)>=0 for point in centers):continue
            inside=np.zeros_like(core);cv2.drawContours(inside,[c],-1,255,cv2.FILLED)
            if manual:
                if a>np.count_nonzero(selection)*2.2 or np.count_nonzero((inside>0)&(selection>0))<np.count_nonzero(selection)*.70:continue
            elif np.any(core) and np.count_nonzero((inside>0)&(core>0))<np.count_nonzero(core)*.97:continue
            hull_area=cv2.contourArea(cv2.convexHull(c))
            if (cw>bw*1.25 or ch>bh*1.25) and (a/max(1,hull_area)<.94 or a>bw*bh*9):continue
            candidates.append((a,c))
    safe=np.zeros((h,w),np.uint8)
    found=bool(candidates)
    text_shield=cv2.dilate(core,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(19,19))) if np.any(core) else np.zeros_like(core)
    if found:
        _,contour=min(candidates,key=lambda v:abs(np.log(max(1,v[0])/max(1,bw*bh*.78))))
        cv2.drawContours(safe,[contour],-1,255,cv2.FILLED)
        border_margin=min(margin,2) if valid_quad(manual_quad) else margin
        safe=cv2.erode(safe,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*border_margin+1,2*border_margin+1)))
        note='ตรวจพบขอบปิดและเว้นระยะด้านในแล้ว — ตรวจมาสก์ก่อนใช้'
    else:
        if bw/max(1,bh)>2.8:inset=-min(24,grow+12)
        elif box_gray_med<165.0:inset=-min(16,grow+8)
        else:inset=-min(10,grow+4) if np.any(core) else margin
        b=[bubble_local[0]+inset,bubble_local[1]+inset,
           bubble_local[2]-inset,bubble_local[3]-inset]
        _rect(safe,b)
        if box_gray_med>=165.0:
            n,labels,stats,_=cv2.connectedComponentsWithStats(edges)
            bx0,by0,bx1,by1=bubble_local
            guard=np.zeros_like(safe)
            for i in range(1,n):
                ex,ey,ew,eh,area=stats[i]
                near=ex<bx0+margin*2 or ey<by0+margin*2 or ex+ew>bx1-margin*2 or ey+eh>by1-margin*2
                component=labels==i
                text_overlap=np.count_nonzero(component&(core>0))/max(1,area)
                if near and (ew>bw*.45 or eh>bh*.45) and text_overlap<.50:guard[component]=255
            guard=cv2.dilate(guard,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*margin+1,2*margin+1)))
            # Never let background window/wall lines carve holes directly through OCR text + sticker outline.
            if np.any(text_shield):guard[text_shield>0]=0
            safe[guard>0]=0
            if not valid_quad(manual_quad):
                safe=_carve_border_spikes(gray,safe,core,bubble_local,margin,bw,bh)
            count,regions,_,_=cv2.connectedComponentsWithStats(safe)
            if count>2:
                selected=set()
                for cx,cy in centers:
                    ix,iy=int(cx),int(cy)
                    if 0<=ix<w and 0<=iy<h and regions[iy,ix]:selected.add(int(regions[iy,ix]))
                if selected:safe[~np.isin(regions,list(selected))]=0
        else:
            inner_keep=np.zeros_like(safe)
            _rect(inner_keep,[bubble_local[0]+2,bubble_local[1]+2,bubble_local[2]-2,bubble_local[3]-2])
            n,labels,stats,_=cv2.connectedComponentsWithStats(edges)
            guard=np.zeros_like(safe)
            for i in range(1,n):
                ex,ey,ew,eh,area=stats[i]
                comp=(labels==i)&(inner_keep==0)
                if np.any(comp) and (ew>bw*.60 or eh>bh*.60) and np.count_nonzero((labels==i)&(core>0))/max(1,area)<.25:
                    guard[comp]=255
            if np.any(guard):
                guard=cv2.dilate(guard,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(7,7)))
                guard[inner_keep>0]=0
                safe[guard>0]=0
        note='ตรวจขอบปิดไม่ชัด: ใช้พื้นที่ด้านในกรอบที่เลือก กรุณาตรวจมาสก์ก่อนคลีน — จะไม่ข้ามกรอบนี้'
    unclosed_light=bool(not found and box_gray_med>=205.0 and bw/max(1,bh)<=2.5)
    support=np.zeros_like(safe)
    for b,shape in zip(line_boxes,line_shapes):
        if unclosed_light:
            halo=min(13,max(grow+2,round((b[3]-b[1])*.22)+3))
        else:
            halo=max(grow+3,min(30,round((b[3]-b[1])*.30)+grow))
        support=cv2.bitwise_or(support,cv2.dilate(shape,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*halo+1,2*halo+1))))
    if valid_quad(manual_quad):
        selected=np.zeros_like(safe)
        paint_shape(selected,{'quad':manual_quad},box[:2])
        selected=cv2.erode(selected,np.ones((3,3),np.uint8))
        if found:safe=cv2.bitwise_and(safe,selected)
        else:
            safe=selected.copy()
            light_quad=np.any(selected) and float(np.median(gray[selected>0]))>=200.0
            spike_src=((gray<135)|(edges>0)).astype(np.uint8)*255 if light_quad else edges
            count,parts,stats,_=cv2.connectedComponentsWithStats(spike_src)
            crossing=np.zeros_like(safe)
            for i in range(1,count):
                ex,ey,ew,eh,pixels=stats[i];part=parts==i
                outside_frac=np.count_nonzero(part&(selected==0))/max(1,pixels)
                if max(ew,eh)>min(bw,bh)*.7 and outside_frac>.25:
                    crossing[part]=255
                elif light_quad and pixels>=8 and outside_frac>=.15 and np.count_nonzero(part&(core>0))==0:
                    crossing[part]=255
            crossing=cv2.dilate(crossing,np.ones((5,5),np.uint8))
            safe[crossing>0]=0
            if light_quad:
                safe=_carve_border_spikes(gray,safe,core,bubble_local,margin,bw,bh)
        note+=' | ใช้กรอบเอียง ตรวจให้มุมอยู่ด้านในเส้นบับเบิ้ล'

    if line_boxes and coverage and not valid_quad(manual_quad):
        sticker_mask=_outlined_floating_mask(arr,gray,safe,line_shapes,grow) if not found else None
        if sticker_mask is not None and np.any(sticker_mask):
            mask=sticker_mask
            note+=' | มาสก์รัดรูปตัวอักษรขอบสโตรกบนภาพวาด'
        else:
            use_tight=_needs_stroke_mask(arr,gray,safe,core)
            if use_tight:
                mask=_stroke_hugging_mask(arr,gray,safe,bubble_local,line_shapes,support,grow)
                note+=' | มาสก์รัดรูปตัวอักษรสำหรับพื้นลวดลาย'
            else:
                mask=support.copy()
                if found:
                    inner=cv2.erode(safe,np.ones((7,7),np.uint8))
                    clean_zone=(inner>0)&(support==0)
                    if np.count_nonzero(clean_zone)>=100:
                        bg_pts=arr[clean_zone]
                        bg_med=np.median(bg_pts,axis=0)
                        bg_var=float(np.percentile(np.max(np.abs(bg_pts.astype(float)-bg_med),axis=1),90))
                        if bg_var<=4.0:
                            near_support=cv2.dilate(support,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(33,17)))
                            diff=np.max(np.abs(arr.astype(np.int16)-np.rint(bg_med).astype(np.int16)),axis=2)
                            stray=((diff>max(15,round(bg_var*4)))&(near_support>0)&(inner>0)).astype(np.uint8)*255
                            if np.any(stray):
                                pad_r=max(2,min(5,grow))
                                stray=cv2.dilate(stray,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*pad_r+1,2*pad_r+1)))
                                mask=cv2.bitwise_or(mask,stray)
            glow_extra=_glowing_text_mask(arr,gray,safe,bubble_local,line_boxes,grow,tight=use_tight)
            if np.any(glow_extra):mask=cv2.bitwise_or(mask,glow_extra)
    else:
        kernel=min(31,(min(w,h)-1)//2*2+1)
        background=cv2.medianBlur(arr,max(3,kernel))
        residual=np.max(np.abs(arr.astype(np.int16)-background.astype(np.int16)),axis=2)
        if np.any(safe) and float(np.median(gray[safe>0]))<155.0:
            achrom_w=np.min(arr,axis=2)
            kw_res=max(19,min(31,(max(min(w,h),28)//3)*2+1))
            bg_w=cv2.morphologyEx(achrom_w,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(kw_res,kw_res)))
            residual=np.maximum(residual,achrom_w.astype(np.int16)-bg_w.astype(np.int16))
        if manual and np.any(safe):
            q=np.asarray(manual_quad);short=min(np.linalg.norm(q-np.roll(q,1,axis=0),axis=1))
            stroke=max(3,min(9,round(short*.10)))
            element=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(stroke*2+1,stroke*2+1))
            light=np.median(gray[safe>0])>=128
            grad=cv2.morphologyEx(gray,cv2.MORPH_GRADIENT,np.ones((3,3),np.uint8))
            flat_pts=arr[(safe>0)&(grad<=6)]
            if len(flat_pts)>=40:
                bg_med=np.median(flat_pts,axis=0)
                bg_var=float(np.percentile(np.max(np.abs(flat_pts.astype(float)-bg_med),axis=1),85))
            else:
                bg_med=None;bg_var=float('inf')
            if bg_med is not None and bg_var<=10.0:
                isolated=arr.copy();isolated[safe==0]=np.rint(bg_med).astype(np.uint8)
                closed_bg=cv2.morphologyEx(isolated,cv2.MORPH_CLOSE,element).astype(np.int16)
                opened_bg=cv2.morphologyEx(isolated,cv2.MORPH_OPEN,element).astype(np.int16)
                src16=isolated.astype(np.int16)
                morph_res=np.max(np.maximum(closed_bg-src16,src16-opened_bg),axis=2)
                flat_diff=np.max(np.abs(src16-np.rint(bg_med).astype(np.int16)),axis=2)
                residual=np.maximum(morph_res,np.where(flat_diff>max(8,round(bg_var*1.8)),flat_diff,0))
            else:
                background=cv2.morphologyEx(arr,cv2.MORPH_CLOSE if light else cv2.MORPH_OPEN,element)
                difference=(background.astype(np.int16)-arr.astype(np.int16)) if light else (arr.astype(np.int16)-background.astype(np.int16))
                residual=np.max(difference,axis=2)
                if not light:
                    achrom_w=np.min(arr,axis=2)
                    kw_q=max(19,min(31,(int(short)//3)*2+1))
                    bg_q=cv2.morphologyEx(achrom_w,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(kw_q,kw_q)))
                    residual=np.maximum(residual,achrom_w.astype(np.int16)-bg_q.astype(np.int16))
                if line_shapes:
                    line_union=np.zeros_like(safe)
                    for s in line_shapes:line_union=cv2.bitwise_or(line_union,s)
                    deep_safe=cv2.erode(safe,np.ones((stroke*2+3,stroke*2+3),np.uint8))
                    if np.any(deep_safe&(line_union>0)):
                        opp_bg=cv2.morphologyEx(arr,cv2.MORPH_OPEN if light else cv2.MORPH_CLOSE,element)
                        opp_diff=(arr.astype(np.int16)-opp_bg.astype(np.int16)) if light else (opp_bg.astype(np.int16)-arr.astype(np.int16))
                        opp_res=np.max(opp_diff,axis=2)
                        residual=np.where((deep_safe>0)&(line_union>0),np.maximum(residual,opp_res),residual)
        permitted=support if line_boxes else cv2.erode(safe,np.ones((7,7),np.uint8))
        if valid_quad(manual_quad):
            permitted=cv2.erode(safe,np.ones((3,3),np.uint8))
            if line_boxes and unclosed_light:
                permitted=cv2.bitwise_and(permitted,support)
        thresh=8 if manual else 10
        mask=((residual>thresh)&(permitted>0)).astype(np.uint8)*255
        if manual and np.any(mask):
            mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(3,3)))
        radius=grow
        if valid_quad(manual_quad):
            q=np.asarray(manual_quad);short=min(np.linalg.norm(q-np.roll(q,1,axis=0),axis=1))
            preferred=min(grow,max(2,min(4,round(short*.045))))
            fallback=min(grow,max(1,min(2,round(short*.025))))
            radius=fallback
            if preferred>fallback and np.any(mask):
                trial=cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*preferred+1,2*preferred+1)))
                trial=cv2.bitwise_and(trial,safe)
                if np.count_nonzero(background_samples(trial,safe))>=max(40,int(np.count_nonzero(safe)*.12)):
                    radius=preferred
        if radius:
            mask=cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1)))
        if line_boxes and (not valid_quad(manual_quad) or unclosed_light):
            mask=cv2.bitwise_and(mask,support)
        if coverage:
            use_tight=_needs_stroke_mask(arr,gray,safe,core)
            glow_extra=_glowing_text_mask(arr,gray,safe,bubble_local,line_boxes,grow,tight=use_tight)
            if np.any(glow_extra):mask=cv2.bitwise_or(mask,glow_extra)
    mask=cv2.bitwise_and(mask,safe)
    if not line_boxes:
        note+=' | ไม่มีพิกัด OCR: สร้างมาสก์จากความต่างสีในกรอบ ให้ตรวจหรือกดอ่านคำซ้ำ'
    if ignored_outlines:
        note+=' | แยกกรอบ OCR เครื่องหมายที่ครอบทั้งบับเบิ้ลออกจากพิกัดตัวอักษร และตรวจรอยจากภาพแทน'
    if not np.any(mask):
        note+=' | มาสก์ว่าง กรุณาอ่านคำซ้ำหรือระบาย/ลากสี่เหลี่ยมครอบคำ'
    return CleanRegion(box,crop,mask,safe,found,note,manual)


def sample_background(image, mask, safe):
    arr=np.asarray(image.convert('RGB'))
    mask=(mask>0).astype(np.uint8)*255
    outer=cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(49,49)))
    trusted=background_samples(mask,safe)
    ring=(outer>0)&trusted
    pixels=arr[ring]
    if len(pixels)<20:pixels=arr[trusted]
    if len(pixels)<20:raise ValueError('พื้นที่สีพื้นรอบคำไม่พอ กรุณาลดมาสก์หรือเลือกกรอบใหม่')
    color=np.median(pixels,axis=0)
    variation=float(np.percentile(np.max(np.abs(pixels.astype(float)-color),axis=1),90))
    return np.rint(color).astype(np.uint8),variation


def smooth_background(image, mask, safe):
    """Robust cubic color surface from clean interior pixels, excluding the mask."""
    a=np.asarray(image.convert('RGB'));h,w=a.shape[:2]
    ys,xs=np.nonzero(background_samples(mask,safe))
    if len(xs)<100:return None,float('inf')
    indices=np.linspace(0,len(xs)-1,min(4000,len(xs)),dtype=int)
    xs,ys=xs[indices],ys[indices]
    def features(x,y):
        x=2*x/max(1,w-1)-1;y=2*y/max(1,h-1)-1
        return np.stack([np.ones_like(x),x,y,x*x,x*y,y*y,x*x*x,x*x*y,x*y*y,y*y*y],axis=-1)
    design=features(xs.astype(float),ys.astype(float));values=a[ys,xs].astype(float)
    support=np.zeros((h,w),np.uint8)
    cv2.fillConvexPoly(support,cv2.convexHull(np.column_stack((xs,ys)).astype(np.int32)),255)
    if np.any(mask) and np.count_nonzero((mask>0)&(support==0))>np.count_nonzero(mask)*.02:
        return None,float('inf')
    validation=np.random.default_rng(31).permutation(len(xs))[:max(20,len(xs)//5)]
    training=np.ones(len(xs),bool);training[validation]=False
    best=None
    for terms in (3,6,10):
        matrix=design[:,:terms];keep=training.copy()
        for _ in range(4):
            coeff,_,rank,_=np.linalg.lstsq(matrix[keep],values[keep],rcond=None)
            if rank<terms:break
            errors=np.max(np.abs(matrix@coeff-values),axis=1)
            threshold=max(2.0,float(np.percentile(errors[training],85)))
            keep=training&(errors<=threshold)
        if rank<terms:continue
        error=max(float(np.percentile(errors,90)),float(np.percentile(errors[validation],90)))
        tail_err=float(np.percentile(errors,98))
        # If thin tile/panel lines exist (`tail_err > 8`), inflate `error` so LaMa handles line art.
        if tail_err>8.5:error=max(error,tail_err*.5)
        if best is None or error<best[0]:best=(error,terms,coeff)
        if error<=1.0:break
    if best is None:return None,float('inf')
    error,terms,coeff=best
    out=np.empty_like(a)
    for row in range(0,h,256):
        yy,xx=np.mgrid[row:min(row+256,h),:w]
        prediction=features(xx.astype(float),yy.astype(float))[...,:terms]@coeff
        out[row:row+len(yy)]=np.clip(np.rint(prediction),0,255).astype(np.uint8)
    if np.any(mask):
        low=values.min(axis=0)-5;high=values.max(axis=0)+5
        predictions=out[mask>0].astype(float)
        if np.any((predictions<low)|(predictions>high)):return None,float('inf')
    return out,error


def _blend_seam(arr, result, mask, safe, chosen):
    """Feather subtle boundary seams strictly inside `mask > 0` without blurring edges."""
    if chosen not in ('gradient','lama'):return result
    collar=(cv2.dilate(mask,np.ones((3,3),np.uint8))>0)&(mask==0)&(safe>0)
    if np.count_nonzero(collar)<16:return result
    dist=cv2.distanceTransform((mask>0).astype(np.uint8),cv2.DIST_L2,5)
    if chosen=='gradient':
        diff=arr.astype(np.float32)-result.astype(np.float32)
        step=np.max(np.abs(diff),axis=2)
        if float(np.median(step[collar]))>14.0:return result
        valid=collar&(step<=16.0)
        if np.count_nonzero(valid)<12:return result
        c_float=valid.astype(np.float32)
        c_weight=cv2.GaussianBlur(c_float,(9,9),2.2)
        smooth=cv2.GaussianBlur(diff*c_float[:,:,None],(9,9),2.2)/np.maximum(1e-6,c_weight[:,:,None])
        fade=np.clip(1.0-(dist-1.0)/4.0,0.0,1.0)*(mask>0)*(c_weight>1e-3)
        return np.clip(np.rint(result.astype(np.float32)+smooth*fade[:,:,None]),0,255).astype(np.uint8)
    # For LaMa: feather only the outermost 1 px anti-aliased edge on smooth zones;
    # preserve 100% of LaMa's sharp reconstructed line art everywhere else.
    ext_grad=np.max(cv2.morphologyEx(arr,cv2.MORPH_GRADIENT,np.ones((3,3),np.uint8)),axis=2)
    smooth_collar=collar&(ext_grad<=8)
    if np.count_nonzero(smooth_collar)<12:return result
    c_float=smooth_collar.astype(np.float32)
    c_weight=cv2.GaussianBlur(c_float,(5,5),1.2)
    ext_color=cv2.GaussianBlur(arr.astype(np.float32)*c_float[:,:,None],(5,5),1.2)/np.maximum(1e-6,c_weight[:,:,None])
    rim=(dist>0)&(dist<=1.5)&(c_weight>.10)
    if not np.any(rim):return result
    seam_diff=ext_color-result.astype(np.float32)
    rim_step=np.max(np.abs(seam_diff),axis=2)
    if float(np.median(rim_step[rim]))>12.0:return result
    alpha=0.45*(rim&(rim_step<=14.0))
    return np.clip(np.rint(result.astype(np.float32)+seam_diff*alpha[:,:,None]),0,255).astype(np.uint8)


class Cleaner:
    def __init__(self):
        self.lama=None
        self._lama_status=None
        self._lama_signature=None
        self.last_diagnostics={}

    def check_lama(self, force=False):
        """Explicit local load + inference check; never download or change packages."""
        path=lama_model_path()
        try:
            stat=path.stat();signature=(str(path),stat.st_size,stat.st_mtime_ns)
        except OSError:
            signature=(str(path),None,None)
        if not force and self._lama_status is not None and signature==self._lama_signature:
            return dict(self._lama_status)
        self.lama=None
        status={'ready':False,'path':str(path),'bytes':signature[1],'code':'missing_weights',
                'message':'LaMa: ยังไม่มีน้ำหนัก — รัน install_clean_lama_windows.bat'}
        if signature[1] is not None:
            try:
                model=LamaModel()
                probe=Image.new('RGB',(64,64),(190,180,170))
                mask=Image.new('L',probe.size);mask.paste(255,(24,24,40,40))
                out=model(probe,mask)
                if out.size!=probe.size:raise RuntimeError('ขนาดภาพทดสอบไม่ตรงกัน')
                self.lama=model
                status.update(ready=True,code='ready',device=str(model.device),
                              message=f'LaMa: พร้อมใช้งาน ({model.device}) — ผ่านการโหลดและคลีนทดสอบ')
            except Exception as exc:
                status.update(code='load_or_inference_failed',
                              message='LaMa: โหลด/ทดสอบไม่สำเร็จ: '+str(exc))
        self._lama_signature=signature;self._lama_status=status
        return dict(status)

    def clean(self, region, method='auto', color=None, cancel=None):
        self.last_diagnostics={'requested_method':method,'boundary_found':bool(region.boundary_found)}
        if cancel and cancel.is_set():raise InterruptedError('หยุดคลีนแล้ว')
        arr=np.asarray(region.image.convert('RGB')).copy()
        mask=((region.mask>0)&(region.safe>0)).astype(np.uint8)*255
        if not np.any(mask):raise ValueError('ไม่มีมาสก์ตัวอักษร — อ่าน OCR ก่อนหรือระบายมาสก์ในหน้าคลีน')
        sampled=None;variation=float('inf');surface=None;error=float('inf')
        trusted=background_samples(mask,region.safe)
        sample_px=int(np.count_nonzero(trusted))
        if method in ('auto','solid','gradient'):
            try:sampled,variation=sample_background(region.image,mask,region.safe)
            except ValueError as exc:
                self.last_diagnostics['sample_error']=str(exc)
                if method=='solid' and color is None:raise
        if method=='solid' and color is not None:sampled,variation=np.array(color,dtype=np.uint8),0
        chosen=method
        white_interior=bool(method=='auto' and region.boundary_found and sampled is not None and
            np.min(sampled)>=248 and variation<=4 and sample_px>=100)
        if method=='gradient' or (method=='auto' and not white_interior):
            surface,error=smooth_background(region.image,mask,region.safe)
        self.last_diagnostics.update(sample_pixels=sample_px,
            variation=variation if np.isfinite(variation) else None,
            surface_error=error if np.isfinite(error) else None,
            mask_pixels=int(np.count_nonzero(mask)))
        if method=='auto':
            flat_error=float('inf')
            if surface is not None and sampled is not None:
                flat_error=float(np.max(np.abs(surface[mask>0].astype(float)-sampled)))
            self.last_diagnostics['flat_white_interior']=white_interior
            # Check if structural background lines cross directly into `mask` on open artwork.
            crossing_art=False
            if not region.boundary_found and not white_interior:
                collar=(cv2.dilate(mask,np.ones((5,5),np.uint8))>0)&(mask==0)&(region.safe>0)
                if np.count_nonzero(collar)>=20:
                    g_step=cv2.morphologyEx(cv2.cvtColor(arr,cv2.COLOR_RGB2GRAY),cv2.MORPH_GRADIENT,np.ones((3,3),np.uint8))
                    crossing_art=float(np.percentile(g_step[collar],95))>=18.0
            flat_context=False
            if region.manual_frame and sampled is not None and variation<=5 and not crossing_art:
                ys,xs=np.nonzero(trusted);my,mx=np.nonzero(mask)
                if len(xs)>=100 and len(mx) and np.ptp(xs)>=np.ptp(mx)*.75 and np.ptp(ys)>=np.ptp(my)*.75:
                    flat_context=np.percentile(np.max(np.abs(arr[trusted].astype(float)-sampled),axis=1),95)<=8
            self.last_diagnostics['flat_tilted_context']=bool(flat_context)
            if ((variation<=3 and error<=3.5 and flat_error<=3) or white_interior) and not crossing_art:chosen='solid'
            elif surface is not None and error<=3.5 and not crossing_art:chosen='gradient'
            elif flat_context:chosen='solid'
            elif self.lama is not None or lama_model_path().is_file():chosen='lama'
            else:raise RuntimeError('พื้นซับซ้อนและยังไม่มี LaMa: รัน install_clean_lama_windows.bat แล้วคลีนใหม่ หรือเลือกวิธีเอง; ไม่ใช้ inpaint แทนอัตโนมัติ')
        self.last_diagnostics['chosen_method']=chosen
        if chosen=='gradient' and surface is None:raise ValueError('พื้นที่พื้นสะอาดไม่พอสำหรับไล่เฉด ลอง LaMa หรือ inpaint')
        if chosen=='solid':
            result=arr.copy()
            result[mask>0]=sampled if color is None else np.array(color,dtype=np.uint8)
        elif chosen=='gradient':
            result=surface
        elif chosen=='inpaint':
            bgr=cv2.cvtColor(arr,cv2.COLOR_RGB2BGR)
            result=cv2.cvtColor(cv2.inpaint(bgr,mask,4,cv2.INPAINT_TELEA),cv2.COLOR_BGR2RGB)
        elif chosen=='lama':
            if self.lama is None:
                status=self.check_lama()
                if not status['ready']:raise RuntimeError(status['message'])
            mys,mxs=np.nonzero(mask)
            active_span=max(int(mys.max()-mys.min())+1,int(mxs.max()-mxs.min())+1)+192
            check_span=active_span if isinstance(self.lama,LamaModel) else max(region.image.size)
            if check_span>2048:
                raise ValueError('LaMa รองรับกรอบด้านยาวไม่เกิน 2048 px ในรุ่นนี้ กรุณาเลือกกรอบเล็กลง')
            result=np.asarray(self.lama(region.image,Image.fromarray(mask)).convert('RGB'))
            self.last_diagnostics['lama_device']=str(getattr(self.lama,'device','unknown'))
            self.last_diagnostics['lama_fallback']=getattr(self.lama,'fallback_reason','')
            if arr.shape[0]<=result.shape[0]<=arr.shape[0]+7 and arr.shape[1]<=result.shape[1]<=arr.shape[1]+7:
                result=result[:arr.shape[0],:arr.shape[1]]
            if result.shape!=arr.shape:raise RuntimeError('LaMa คืนขนาดภาพไม่ตรงต้นฉบับ')
        else:
            raise ValueError('โหมดคลีนไม่ถูกต้อง')
        if cancel and cancel.is_set():raise InterruptedError('หยุดคลีนแล้ว')
        result=_blend_seam(arr,result,mask,region.safe,chosen)
        output=arr.copy();output[mask>0]=result[mask>0]
        return Image.fromarray(output),mask,chosen


def encode_region(region):
    """Persist an editable draft using fast lossless PNG compression."""
    def png(image):
        stream=io.BytesIO();image.save(stream,format='PNG',compress_level=1)
        return base64.b64encode(stream.getvalue()).decode('ascii')
    return {'box':list(region.box),'image':png(region.image.convert('RGB')),
            'mask':png(Image.fromarray(region.mask)),'safe':png(Image.fromarray(region.safe)),
            'boundary_found':bool(region.boundary_found),'note':region.note,
            'manual_frame':bool(region.manual_frame),
            'manual_edited':bool(getattr(region,'manual_edited',False))}


def decode_region(record):
    box=record['box']
    if len(box)!=4 or not all(isinstance(v,int) for v in box) or min(box)<0:
        raise ValueError('พิกัดมาสก์ไม่ถูกต้อง')
    size=(box[2]-box[0],box[3]-box[1])
    if min(size)<=0 or size[0]*size[1]>12_000_000:raise ValueError('ขนาดมาสก์ไม่ถูกต้อง')
    def read(key,mode):
        with Image.open(io.BytesIO(base64.b64decode(record[key],validate=True))) as im:
            if im.size!=size:raise ValueError('ขนาดภาพ/มาสก์ไม่ตรงพิกัด')
            return im.convert(mode)
    image=read('image','RGB');mask=np.array(read('mask','L'));safe=np.array(read('safe','L'))
    safe=(safe>0).astype(np.uint8)*255;mask=((mask>0)&(safe>0)).astype(np.uint8)*255
    return CleanRegion(list(box),image,mask,safe,bool(record.get('boundary_found')),
                       str(record.get('note','')),bool(record.get('manual_frame',False)),
                       bool(record.get('manual_edited',False)))


def encode_patch(box, image, mask, method='', bubble_id=''):
    rgba=image.convert('RGBA');rgba.putalpha(Image.fromarray((mask>0).astype(np.uint8)*255))
    out=io.BytesIO();rgba.save(out,format='PNG',compress_level=1)
    return {'box':list(box),'png':base64.b64encode(out.getvalue()).decode('ascii'),
            'method':method,'bubble_id':bubble_id}


def decode_patch(patch):
    box=patch['box']
    if len(box)!=4 or not all(isinstance(v,int) for v in box):raise ValueError('พิกัดงานคลีนไม่ถูกต้อง')
    if min(box)<0 or box[2]<=box[0] or box[3]<=box[1]:raise ValueError('พิกัดงานคลีนไม่ถูกต้อง')
    if (box[2]-box[0])*(box[3]-box[1])>12_000_000:raise ValueError('งานคลีนมีขนาดใหญ่เกินไป')
    with Image.open(io.BytesIO(base64.b64decode(patch['png'],validate=True))) as im:
        if im.size!=(box[2]-box[0],box[3]-box[1]):raise ValueError('ขนาดงานคลีนไม่ตรงพิกัด')
        return im.convert('RGBA')


def compose_page(original, patches):
    out=original.convert('RGB').copy()
    for patch in patches:
        overlay=decode_patch(patch);b=patch['box']
        if b[2]>out.width or b[3]>out.height:raise ValueError('งานคลีนอยู่นอกภาพต้นฉบับ')
        out.paste(overlay,(b[0],b[1]),overlay.getchannel('A'))
    return out


def export_cleaned(original, patches, destination):
    """New PNG only. Exclusive creation avoids overwriting any existing file."""
    if not patches:raise ValueError('ยังไม่มีงานคลีนสำหรับหน้านี้')
    destination=Path(destination)
    image=compose_page(original,patches)
    with destination.open('xb') as stream:image.save(stream,format='PNG')
