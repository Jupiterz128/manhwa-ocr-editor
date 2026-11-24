"""Page-space quadrilaterals; perspective OCR crops never change source pixels."""
import cv2
import numpy as np
from PIL import Image
COMPONENT_VERSION='4.0'


def outline_read(record,box):
    text=str(record.get('text','')).strip();b=record['box']
    return bool(text and not any(c.isalnum() for c in text) and
                (b[2]-b[0])*(b[3]-b[1])>(box[2]-box[0])*(box[3]-box[1])*.45)


def box_quad(box):
    x0,y0,x1,y1=box
    return [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]


def valid_quad(points):
    try:
        q=np.asarray(points,dtype=np.float32)
        return bool(q.shape==(4,2) and np.isfinite(q).all() and
                cv2.isContourConvex(q) and cv2.contourArea(q,oriented=True)>16 and
                min(np.linalg.norm(q-np.roll(q,1,axis=0),axis=1))>=3)
    except (ValueError,TypeError,cv2.error):return False


def quad_bounds(points):
    q=np.asarray(points,dtype=float)
    return [float(q[:,0].min()),float(q[:,1].min()),float(q[:,0].max()),float(q[:,1].max())]


def frame_quad(bubble):
    q=getattr(bubble,'quad',[])
    return q if valid_quad(q) else box_quad(bubble.box)


def quad_angle(points):
    """Clockwise degrees in image coordinates; average the two text baselines."""
    if not valid_quad(points):return 0.0
    q=np.asarray(points,float)
    vector=(q[1]-q[0])+(q[2]-q[3])
    return float(np.degrees(np.arctan2(vector[1],vector[0])))


def auto_tilt(bubble,page_size=None):
    """Use agreeing OCR polygons, never invent an angle from axis-aligned boxes.

    Existing user corners are authoritative. Uncertain/mixed orientations remain
    editable rectangles. Confidence scores do not exclude faint text.
    """
    if getattr(bubble,'quad',[]):return bubble
    records=[r for r in bubble.lines if valid_quad(r.get('quad',[])) and
             not outline_read(r,bubble.box)]
    if not records:return bubble
    angles=np.array([quad_angle(r['quad']) for r in records])
    weights=np.array([max(1,np.linalg.norm(np.asarray(r['quad'])[1]-r['quad'][0])) for r in records])
    order=np.argsort(angles)
    angle=float(angles[order[np.searchsorted(np.cumsum(weights[order]),weights.sum()/2)]])
    if not 3<=abs(angle)<=45 or np.sum(weights[np.abs(angles-angle)>10])>weights.sum()*.20:return bubble
    # Include ALL line corners, including box-only lines, so no words get cut off.
    points=np.concatenate([np.asarray(r.get('quad') or box_quad(r['box']),float) for r in bubble.lines])
    radians=np.radians(angle);basis=np.array([[np.cos(radians),-np.sin(radians)],[np.sin(radians),np.cos(radians)]])
    local=points@basis;lo=local.min(axis=0);hi=local.max(axis=0)
    heights=[np.linalg.norm(np.asarray(r['quad'])[3]-r['quad'][0]) for r in records]
    pad=max(2,min(12,float(np.median(heights))*.20))
    # Keep the geometry positive near a page edge, without skewing its angle.
    for margin in (pad,0):
        q=np.asarray(box_quad([*(lo-margin),*(hi+margin)]))@basis.T
        if q.min()>=0 and (page_size is None or np.all(q<=np.asarray(page_size))):break
    if q.min()<0 or not valid_quad(q) or (page_size is not None and np.any(q>np.asarray(page_size))):return bubble
    bubble.quad=q.tolist();bubble.box=quad_bounds(q)
    return bubble


def map_points(points,matrix):
    return cv2.perspectiveTransform(np.asarray(points,dtype=np.float32).reshape(1,-1,2),
                                    np.asarray(matrix,dtype=float))[0].astype(float).tolist()


def move_line(line,matrix):
    """Map both representations together, including old cached box-only lines."""
    line.quad=map_points(line.quad or box_quad(line.box),matrix)
    line.box=quad_bounds(line.quad)
    return line


def translation(x=0,y=0,sx=1,sy=1):
    return np.array([[sx,0,x],[0,sy,y],[0,0,1]],dtype=float)


def ocr_crop(image,bubble,pad=0):
    """Return readable crop and its inverse transform to original page space."""
    if not getattr(bubble,'quad',[]):
        x0,y0,x1,y1=bubble.box
        box=[max(0,int(x0)-pad),max(0,int(y0)-pad),
             min(image.width,int(np.ceil(x1))+pad),min(image.height,int(np.ceil(y1))+pad)]
        return image.crop(box),translation(box[0],box[1])
    if not valid_quad(bubble.quad):raise ValueError('Invalid tilted frame')
    q=np.asarray(bubble.quad,np.float32)
    width=max(4,round(max(np.linalg.norm(q[1]-q[0]),np.linalg.norm(q[2]-q[3]))))
    height=max(4,round(max(np.linalg.norm(q[3]-q[0]),np.linalg.norm(q[2]-q[1]))))
    if width*height>12_000_000:raise ValueError('Tilted frame too large')
    dest=np.float32([[0,0],[width,0],[width,height],[0,height]])
    forward=cv2.getPerspectiveTransform(q,dest)
    result=cv2.warpPerspective(np.asarray(image.convert('RGB')),forward,(width,height),
                               flags=cv2.INTER_CUBIC,borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(result),np.linalg.inv(forward)


def paint_shape(mask,record,origin=(0,0),pad=0):
    """Paint line geometry without inflating a tilted line to its bounding box."""
    q=record.get('quad') or box_quad(record['box'])
    q=np.rint(np.asarray(q)-origin).astype(np.int32)
    shape=np.zeros_like(mask)
    cv2.fillPoly(shape,[q],255)
    if pad:shape=cv2.dilate(shape,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*pad+1,2*pad+1)))
    cv2.bitwise_or(mask,shape,dst=mask)


def psd_stem(path,page_number):
    """Preserve the final page number: 67_1 -> 01, 01_4 -> 04."""
    from pathlib import Path
    import re
    found=re.search(r'(\d+)$',Path(path).stem)
    return f'{int(found[1]) if found else page_number:02d}'
