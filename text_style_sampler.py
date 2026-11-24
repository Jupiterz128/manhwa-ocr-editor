"""Conservative source lettering analysis. All colour distances are CIE76."""
import copy
import cv2
import numpy as np
from PIL import Image, ImageOps

DEFAULTS = {
    'stroke': {'enabled': False, 'color': [255, 255, 255], 'size': 3},
    'gradient': {'enabled': False, 'start_color': [255, 255, 255],
                 'end_color': [200, 200, 200], 'angle': 90},
}


def effects(layer=None):
    style = copy.deepcopy(DEFAULTS)
    for key, default in style.items():
        supplied = (layer or {}).get(key, {})
        if not isinstance(supplied, dict):
            continue
        for field, value in default.items():
            v = supplied.get(field, value)
            if field == 'enabled':
                default[field] = v is True
            elif isinstance(value, list):
                if isinstance(v, (tuple, list)) and len(v)==3 and all(type(n) is int and 0<=n<=255 for n in v):
                    default[field] = list(v)
            elif field == 'size' and type(v) is int:
                default[field] = max(1, min(8, v))
            elif field == 'angle' and v in (0, 45, 90, 135):
                default[field] = int(v)
    return style


def sample_style(orig, cleaned, bubble, *, include_color=False):
    style = effects()
    bounds = [l['box'] for l in getattr(bubble,'lines',[]) if l.get('box')]
    x,y,r,b = map(int,bubble.box)
    pad = 10
    box = (max(0,x-pad),max(0,y-pad),min(orig.width,r+pad),min(orig.height,b+pad))
    if box[2]-box[0]<5 or box[3]-box[1]<5:
        return style
    rgb = np.asarray(orig.crop(box).convert('RGB'))
    lab = cv2.cvtColor(rgb.astype(np.float32)/255, cv2.COLOR_RGB2LAB)
    edge = np.concatenate((lab[0],lab[-1],lab[:,0],lab[:,-1]))
    background = np.median(edge,axis=0)
    mask=None
    if cleaned is not None and cleaned is not orig:
        bg = cv2.cvtColor(np.asarray(cleaned.crop(box).convert('RGB'),np.float32)/255,cv2.COLOR_RGB2LAB)
        mask = np.linalg.norm(lab-bg,axis=2)>12
    if mask is None or (include_color and not mask.any()):
        # A page can already contain patches for other bubbles. Sample the local
        # OCR line perimeters when this particular word has no clean reference.
        if include_color and bounds:
            samples=[]
            for lx,ly,rx,by in bounds:
                x0=max(0,int(lx)-box[0]-2);y0=max(0,int(ly)-box[1]-2)
                x1=min(lab.shape[1],int(rx)-box[0]+2);y1=min(lab.shape[0],int(by)-box[1]+2)
                region=lab[y0:y1,x0:x1]
                if region.size:samples.extend((region[0],region[-1],region[:,0],region[:,-1]))
            if samples:
                local=np.concatenate(samples)
                median=np.median(local,axis=0)
                if np.mean(np.linalg.norm(local-median,axis=1)<10)>=.90:edge,background=local,median
        if np.mean(np.linalg.norm(edge-background,axis=1)<10)<.90:
            return style
        bg = np.broadcast_to(background,lab.shape)
        mask = np.linalg.norm(lab-background,axis=2)>18
    if bounds:
        allowed=np.zeros(mask.shape,np.uint8)
        for x0,y0,x1,y1 in bounds:
            cv2.rectangle(allowed,(int(x0)-box[0]-3,int(y0)-box[1]-3),
                          (int(x1)-box[0]+3,int(y1)-box[1]+3),1,-1)
        mask &= allowed.astype(bool)
    valid = np.zeros_like(mask)
    valid[max(0,y-box[1]):b-box[1],max(0,x-box[0]):r-box[0]] = True
    mask &= valid
    if mask.sum()<32 or mask.mean()>.70:
        return style
    distance=cv2.distanceTransform(mask.astype(np.uint8),cv2.DIST_L2,5)
    core=distance>1.5
    if not core.any():core=mask & (distance>=distance.max()*.8)
    best=0
    for size in range(1,9):
        inside=distance>size+.4
        ring=mask & ~inside
        if inside.sum()<16 or ring.sum()<12:break
        ring_lab=np.median(lab[ring],axis=0)
        core_lab=np.median(lab[inside],axis=0)
        if min(np.linalg.norm(ring_lab-core_lab),np.linalg.norm(ring_lab-np.median(bg[ring],axis=0)))<=25:
            continue
        same=np.linalg.norm(lab-ring_lab,axis=2)<12
        coverage=same[ring].mean()
        score=coverage-same[inside].mean()
        # Reject antialiasing ramps along a plain foreground/background transition.
        base=np.median(bg[ring],axis=0)
        delta=core_lab-base
        mix=np.clip(np.dot(ring_lab-base,delta)/max(1,np.dot(delta,delta)),0,1)
        if np.linalg.norm(ring_lab-(base+mix*delta))<8 and coverage<.9:continue
        if coverage>=.60 and score>max(.48,best):
            best=score
            core=inside
            style['stroke'].update(enabled=True,color=np.median(rgb[ring],axis=0).astype(int).tolist(),size=size)
    if include_color and core.any():
        style['color']=np.median(rgb[core],axis=0).round().astype(int).tolist()
    if core.sum()<24:return style
    ys,xs=np.nonzero(core)
    strongest=18
    for coords,angle in ((-ys,90),(xs,0),(xs-ys,45),(xs+ys,135)):
        span=np.ptp(coords)
        if span<6:continue
        groups=np.minimum(2,3*(coords-coords.min())//(span+1))
        bands=[lab[core][groups==i] for i in range(3)]
        if min(map(len,bands))<6:continue
        a,m,z=[np.median(v,axis=0) for v in bands]
        delta=z-a
        length=np.linalg.norm(delta)
        if length<=strongest:continue
        t=np.dot(m-a,delta)/(length*length)
        if not .18<t<.82 or np.linalg.norm(m-a-t*delta)>max(5,.14*length):continue
        # Fit a continuous ramp; a few isolated flat colour blocks are insufficient.
        pos=(coords-coords.min())/span
        colors=lab[core]
        slope=(pos-pos.mean()) @ (colors-colors.mean(axis=0))/max(1e-8,((pos-pos.mean())**2).sum())
        residual=colors-(colors.mean(axis=0)+(pos-pos.mean())[:,None]*slope)
        if np.percentile(np.linalg.norm(residual,axis=1),85)>max(5,length*.15):continue
        ends=[np.median(rgb[core][v],axis=0).astype(int).tolist() for v in (pos<=.10,pos>=.90)]
        style['gradient'].update(enabled=True,start_color=ends[0],end_color=ends[1],angle=angle)
        strongest=length
    return style


def styled_tile(tile, layer):
    style=effects(layer)
    stroke,grad=style['stroke'],style['gradient']
    if grad['enabled']:
        w,h=tile.size
        angle=np.deg2rad(grad['angle'])
        y,x=np.ogrid[:h,:w]
        axis=x*np.cos(angle)-y*np.sin(angle)
        t=((axis-axis.min())/max(1,np.ptp(axis)))[:,:,None]
        if grad['angle']==135:
            t=1-t
        colors=(1-t)*np.array(grad['start_color'])+t*np.array(grad['end_color'])
        fill=Image.fromarray(np.broadcast_to(colors,(h,w,3)).round().astype('uint8'))
        fill.putalpha(tile.getchannel('A'))
        tile=fill
    if stroke['enabled']:
        n=stroke['size']
        tile=ImageOps.expand(tile.convert('RGBA'),border=n,fill=(0,0,0,0))
        kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*n+1,2*n+1))
        outline=Image.new('RGBA',tile.size,tuple(stroke['color'])+(0,))
        outline.putalpha(Image.fromarray(cv2.dilate(np.array(tile.getchannel('A')),kernel)))
        outline.alpha_composite(tile)
        tile=outline
    return tile
