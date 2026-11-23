"""Detailed bubble cleaning shared by the editor and batch workers."""
import copy
import cv2
import numpy as np
from bubble_cleaner import smooth_background,background_samples,_rect
from frame_geometry import paint_shape,valid_quad

COMPONENT_VERSION='3.1'


def _flat_surface(image,region,mask):
    """Certified constant surface when text sits near a flat bubble's edge.

    Cubic fitting rightly refuses to extrapolate outside the sample hull, which
    leaves edge-aligned dialogue unverified even in a pure solid balloon. A
    constant colour cannot overshoot; accept it only with strong flat evidence.
    """
    trusted=background_samples(mask,region.safe)
    ys,xs=np.nonzero(trusted)
    if len(xs)<100:return None,float('inf')
    sy,sx=np.nonzero(region.safe>0)
    if not len(sx):return None,float('inf')
    if np.ptp(xs)<np.ptp(sx)*.45 and np.ptp(ys)<np.ptp(sy)*.45:
        return None,float('inf')
    if not region.boundary_found and not region.manual_frame:
        return None,float('inf')
    arr=np.asarray(image.convert('RGB'))
    pixels=arr[ys,xs].astype(float)
    color=np.median(pixels,axis=0)
    diffs=np.max(np.abs(pixels-color),axis=1)
    p90=float(np.percentile(diffs,90))
    p95=float(np.percentile(diffs,95))
    if p90>3.5 or p95>5.0:return None,float('inf')
    surface=np.broadcast_to(np.rint(color).astype(np.uint8),arr.shape).copy()
    return surface,p90


def residual_mask(image,region,mask):
    """Detect color anomalies near text on a validated smooth background.

    OCR is not able to certify absence of a blurred letter shadow. A smooth color
    model supplies an independent check. Textured backgrounds remain unassessed.
    """
    surface,error=smooth_background(image,mask,region.safe)
    if surface is None:
        surface,error=_flat_surface(image,region,mask)
    if surface is None or error>3.5:return np.zeros_like(mask),False
    arr=np.asarray(image).astype(float)
    residual=np.max(np.abs(arr-surface.astype(float)),axis=2)
    safe=cv2.erode(region.safe,np.ones((7,7),np.uint8))>0
    if np.any(mask):
        radius=max(16,min(64,int(min(mask.shape)*.08)))
        near=cv2.dilate(mask,np.ones((radius*2+1,radius*2+1),np.uint8))>0
        safe &= near
    thresh=max(4.0,error*2.2+1.5) if error>1.0 else max(3.0,error*1.8)
    binary=((residual>thresh)&safe).astype(np.uint8)*255
    binary=cv2.morphologyEx(binary,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    n,labels,stats,_=cv2.connectedComponentsWithStats(binary)
    result=np.zeros_like(mask)
    pad_kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(13,13))
    for i in range(1,n):
        x,y,w,h,area=stats[i]
        if area<3 or area>=max(20,np.count_nonzero(region.safe)*.25):continue
        # Ignore tiny 8-bit gradient dithering specks while keeping real shadows.
        if error>1.0 and area<8 and float(residual[labels==i].max())<thresh+3.0:continue
        # Fill closed interiors of residual rings, then pad smoothly without box corners.
        component=(labels==i).astype(np.uint8)*255
        contours,_=cv2.findContours(component,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(component,contours,-1,255,cv2.FILLED)
        result=cv2.bitwise_or(result,cv2.dilate(component,pad_kernel))
    # Keep disconnected remnants separate. A hull joining every component can
    # erase art between lines or link a letter shadow to the balloon outline.
    return cv2.bitwise_and(result,region.safe),True


def clean_checked(cleaner,region,method='auto',color=None,cancel=None,reader=None,max_passes=4,progress=lambda s:None):
    """Clean, inspect, expand remaining text masks, repeat; never claim certainty.

    When `region.manual_edited` is True, strictly honour the user's painted/erased
    mask in a single pass without re-adding erased areas via OCR or residual checks.
    """
    work=copy.deepcopy(region)
    locked=bool(getattr(region,'manual_edited',False))
    report={'passes':0,'remaining':[],'verification_error':'','visual_checked':False,
            'visual_residual_pixels':0,'needs_review':True,'manual_locked':locked}
    if locked:
        if cancel and cancel.is_set():raise InterruptedError('หยุดคลีนแล้ว')
        progress('คลีนตามมาสก์ที่แก้ไขด้วยมือ (ไม่ขยายทับส่วนที่ลบ)')
        if np.any(work.mask):
            out,used,chosen=cleaner.clean(work,method,color,cancel)
        else:
            out,used,chosen=work.image.copy(),work.mask.copy(),'no-change'
        report.update(passes=1,visual_checked=True,visual_residual_pixels=0,
                      boundary_found=bool(region.boundary_found),needs_review=False)
        return out,used,chosen,report
    if method in ('auto','gradient','solid'):
        extra,_=residual_mask(work.image,work,work.mask)
        work.mask=cv2.bitwise_or(work.mask,extra)
    safe_dist=cv2.distanceTransform((work.safe>0).astype(np.uint8),cv2.DIST_L2,5)
    safe_radius=float(safe_dist.max()) if np.any(work.safe) else 0.0
    narrow=bool(work.manual_frame or safe_radius<36.0)
    ocr_verified_clean=False
    for attempt in range(max(1,max_passes)):
        if cancel and cancel.is_set():raise InterruptedError('หยุดคลีนแล้ว')
        progress(f'คลีนและตรวจละเอียด รอบ {attempt+1}/{max_passes}')
        if np.any(work.mask):
            try:out,used,chosen=cleaner.clean(work,method,color,cancel)
            except Exception:
                # Preserve the most recent expanded mask for retry after an error.
                region.mask=work.mask.copy()
                raise
        else:out,used,chosen=work.image.copy(),work.mask.copy(),'no-change'
        report['passes']=attempt+1
        extra,assessed=residual_mask(out,work,used)
        report['visual_checked']=assessed
        report['visual_residual_pixels']=int(np.count_nonzero(extra))
        remaining=[]
        if reader:
            if ocr_verified_clean and chosen in ('solid','gradient','no-change'):
                report['verification_error']=''
            else:
                try:
                    for line in reader(out):
                        box=line['box'];x=int((box[0]+box[2])/2-work.box[0]);y=int((box[1]+box[3])/2-work.box[1])
                        text=str(line.get('text',''))
                        if text and not any(c.isalnum() for c in text):
                            footprint=np.zeros_like(used);paint_shape(footprint,line,work.box[:2])
                            inside=(footprint>0)&(work.safe>0)
                            pixels=np.asarray(out)[inside if np.any(inside) else (footprint>0)]
                            # Do not expand masks for punctuation hallucinated on
                            # uniform interior pixels or triggered by the outer border.
                            if len(pixels) and np.max(np.ptp(pixels.astype(np.int16),axis=0))<=4:continue
                        if 0<=y<used.shape[0] and 0<=x<used.shape[1] and work.safe[y,x]:remaining.append(line)
                    report['verification_error']=''
                    if not remaining:ocr_verified_clean=True
                except Exception as exc:
                    if cancel and cancel.is_set():raise InterruptedError('หยุดคลีนแล้ว')
                    report['verification_error']=str(exc)
                    ocr_verified_clean=False
        else:report['verification_error']='ไม่ได้เปิดตรวจ OCR'
        report['remaining']=remaining
        if not remaining and not np.any(extra):break
        if attempt+1>=max_passes:break
        expanded=cv2.bitwise_or(used,extra)
        pad=8+attempt*3
        if narrow and safe_radius>0:
            pad=max(3,min(pad,round(safe_radius*(.22+attempt*.08))))
        rem_mask=np.zeros_like(used)
        for line in remaining:
            if valid_quad(line.get('quad',[])):paint_shape(rem_mask,line,work.box[:2],pad=pad)
            else:
                b=line['box'];_rect(rem_mask,[b[0]-work.box[0],b[1]-work.box[1],b[2]-work.box[0],b[3]-work.box[1]],pad=pad)
        # If OCR still sees letters within the old mask, retry with more context.
        if remaining:
            if narrow:
                rem_mask=cv2.dilate(rem_mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
                expanded=cv2.bitwise_or(expanded,rem_mask)
            else:
                expanded=cv2.bitwise_or(expanded,rem_mask)
                expanded=cv2.dilate(expanded,np.ones((7,7),np.uint8))
        expanded=cv2.bitwise_and(expanded,work.safe)
        if np.array_equal(expanded,used):break
        work.mask=expanded
    if cancel and cancel.is_set():raise InterruptedError('หยุดคลีนแล้ว')
    report['boundary_found']=bool(region.boundary_found)
    boundary_ok=bool(region.boundary_found or (region.manual_frame and report['visual_checked']))
    report['needs_review']=bool(report['remaining'] or report['verification_error'] or report['visual_residual_pixels'] or not report['visual_checked'] or not boundary_ok)
    return out,used,chosen,report


def quality_note(report):
    if report.get('manual_locked'):
        return 'คลีนเฉพาะจุดมาสก์ที่แก้ไขด้วยมือ (1 รอบ ไม่ขยายมาสก์เพิ่ม) — ดูภาพก่อนใช้'
    prefix=f"ตรวจ {report['passes']} รอบ"
    if report['verification_error']:return prefix+' | ตรวจ OCR ไม่ครบ: '+report['verification_error']
    if report['remaining']:return prefix+f" | ยังอ่านได้ {len(report['remaining'])} บรรทัด — ต้องตรวจเพิ่ม"
    if report['visual_residual_pixels']:return prefix+' | ยังพบความต่างสีที่อาจเป็นเงา/รอยปื้น — ต้องตรวจเพิ่ม'
    if not report['visual_checked']:return prefix+' | OCR ไม่พบคำ แต่พื้นซับซ้อนยังต้องตรวจด้วยตา'
    if not report.get('boundary_found',True) and report.get('needs_review',True):return prefix+' | ตรวจขอบบับเบิ้ลไม่ชัด — ต้องตรวจมาสก์และขอบด้วยตา'
    return prefix+' | OCR ไม่พบคำ และไม่พบรอยผิดปกติตามเกณฑ์พื้นเรียบ — ดูภาพก่อนใช้'
