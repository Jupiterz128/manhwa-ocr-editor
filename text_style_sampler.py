"""Conservative source lettering analysis. All colour distances are CIE76."""
import copy
import cv2
import numpy as np
from PIL import Image, ImageOps

DEFAULTS = {
    'stroke': {'enabled': False, 'color': [255, 255, 255], 'size': 3},
    'gradient': {
        'enabled': False,
        'start_color': [255, 255, 255],
        'end_color': [200, 200, 200],
        'stops': [
            {'pos': 0, 'color': '#FFFFFF'},
            {'pos': 100, 'color': '#C8C8C8'},
        ],
        'angle': 90,
        'scale': 100,
        'opacity': 100,
        'reverse': False,
        'style': 'linear',
        'blend_mode': 'normal',
        'align_with_layer': True,
    },
}

GRADIENT_STYLES = ('linear', 'radial', 'reflected')
GRADIENT_BLEND_MODES = ('normal', 'multiply', 'screen', 'overlay')


def _rgb_to_hex(rgb):
    return '#' + ''.join(f'{max(0, min(255, int(round(c)))):02X}' for c in rgb[:3])


def _parse_color_rgb(val):
    if isinstance(val, (tuple, list)) and len(val) == 3 and all(
        isinstance(n, (int, float)) and not isinstance(n, bool) and 0 <= n <= 255 for n in val
    ):
        return [int(round(n)) for n in val]
    if isinstance(val, str):
        s = val.strip().lstrip('#')
        if len(s) == 3:
            s = ''.join(ch * 2 for ch in s)
        if len(s) == 6:
            try:
                return [int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)]
            except ValueError:
                return None
    return None


def _normalize_stops(raw_stops, fallback_start, fallback_end):
    cleaned = []
    if isinstance(raw_stops, (list, tuple)):
        for item in raw_stops:
            if not isinstance(item, dict):
                continue
            pos = item.get('pos')
            if isinstance(pos, bool) or not isinstance(pos, (int, float)):
                continue
            rgb = _parse_color_rgb(item.get('color'))
            if rgb is None:
                continue
            p = max(0.0, min(100.0, float(pos)))
            p_clean = int(round(p)) if abs(p - round(p)) < 1e-6 else round(p, 2)
            cleaned.append({'pos': p_clean, 'color': _rgb_to_hex(rgb)})
    if len(cleaned) < 2:
        return [
            {'pos': 0, 'color': _rgb_to_hex(fallback_start)},
            {'pos': 100, 'color': _rgb_to_hex(fallback_end)},
        ]
    cleaned.sort(key=lambda s: float(s['pos']))
    return cleaned[:24]


def effects(layer=None):
    style = copy.deepcopy(DEFAULTS)
    src = layer or {}
    # Stroke
    s_in = src.get('stroke', {})
    if isinstance(s_in, dict):
        st = style['stroke']
        st['enabled'] = s_in.get('enabled', st['enabled']) is True
        col = _parse_color_rgb(s_in.get('color'))
        if col is not None:
            st['color'] = col
        sz = s_in.get('size')
        if isinstance(sz, int) and not isinstance(sz, bool):
            st['size'] = max(1, min(8, sz))

    # Gradient Overlay (backwards-compatible with start_color/end_color + full Photoshop Classic multi-stop)
    g_in = src.get('gradient', {})
    if isinstance(g_in, dict):
        gd = style['gradient']
        gd['enabled'] = g_in.get('enabled', gd['enabled']) is True
        sc = _parse_color_rgb(g_in.get('start_color'))
        ec = _parse_color_rgb(g_in.get('end_color'))
        if sc is not None:
            gd['start_color'] = sc
        if ec is not None:
            gd['end_color'] = ec
        raw_stops = g_in.get('stops')
        if isinstance(raw_stops, (list, tuple)) and len(raw_stops) >= 2:
            gd['stops'] = _normalize_stops(raw_stops, gd['start_color'], gd['end_color'])
            gd['start_color'] = _parse_color_rgb(gd['stops'][0]['color']) or gd['start_color']
            gd['end_color'] = _parse_color_rgb(gd['stops'][-1]['color']) or gd['end_color']
        else:
            gd['stops'] = _normalize_stops(None, gd['start_color'], gd['end_color'])
        ang = g_in.get('angle')
        if isinstance(ang, (int, float)) and not isinstance(ang, bool) and np.isfinite(ang):
            a = float(ang)
            while a > 180:
                a -= 360
            while a < -180:
                a += 360
            gd['angle'] = int(round(a))
        scl = g_in.get('scale')
        if isinstance(scl, (int, float)) and not isinstance(scl, bool) and np.isfinite(scl):
            gd['scale'] = max(10, min(150, int(round(float(scl)))))
        opct = g_in.get('opacity')
        if isinstance(opct, (int, float)) and not isinstance(opct, bool) and np.isfinite(opct):
            gd['opacity'] = max(0, min(100, int(round(float(opct)))))
        if 'reverse' in g_in:
            gd['reverse'] = g_in.get('reverse') is True
        gst = str(g_in.get('style') or '').strip().lower()
        if gst in GRADIENT_STYLES:
            gd['style'] = gst
        bm = str(g_in.get('blend_mode') or '').strip().lower()
        if bm in GRADIENT_BLEND_MODES:
            gd['blend_mode'] = bm
        if 'align_with_layer' in g_in:
            gd['align_with_layer'] = g_in.get('align_with_layer') is not False
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
        style['gradient'].update(
            enabled=True,
            start_color=ends[0],
            end_color=ends[1],
            stops=[{'pos': 0, 'color': _rgb_to_hex(ends[0])}, {'pos': 100, 'color': _rgb_to_hex(ends[1])}],
            angle=angle,
        )
        strongest=length
    return style


def styled_tile(tile, layer):
    style=effects(layer)
    stroke,grad=style['stroke'],style['gradient']
    if grad['enabled']:
        w,h=tile.size
        angle=np.deg2rad(grad['angle'])
        y,x=np.ogrid[:h,:w]
        gstyle=grad.get('style','linear')
        scale=max(0.1, float(grad.get('scale',100))/100.0)
        if gstyle=='radial':
            cx,cy=(w-1)/2.0,(h-1)/2.0
            rad=max(1.0, np.hypot(cx,cy))
            t=np.hypot(x-cx,y-cy)/(rad*scale)
            t=np.clip(t,0.0,1.0)
        else:
            axis=x*np.cos(angle)-y*np.sin(angle)
            t=(axis-axis.min())/max(1.0,float(np.ptp(axis)))
            if gstyle=='reflected':
                t=np.abs(t-0.5)*2.0/scale
            else:
                t=0.5+(t-0.5)/scale
            t=np.clip(t,0.0,1.0)
        if grad.get('reverse'):
            t=1.0-t
        stops=grad.get('stops') or [
            {'pos':0,'color':_rgb_to_hex(grad['start_color'])},
            {'pos':100,'color':_rgb_to_hex(grad['end_color'])},
        ]
        s_pos=np.array([float(s['pos'])/100.0 for s in stops],dtype=np.float32)
        s_rgb=np.array([_parse_color_rgb(s['color']) or [255,255,255] for s in stops],dtype=np.float32)
        flat=t.ravel().astype(np.float32)
        grad_rgb=np.stack([np.interp(flat,s_pos,s_rgb[:,ch]) for ch in range(3)],axis=-1).reshape(h,w,3)
        base_rgb=np.asarray(tile.convert('RGB'),dtype=np.float32)
        bmode=grad.get('blend_mode','normal')
        if bmode=='multiply':
            blended=(base_rgb/255.0)*(grad_rgb/255.0)*255.0
        elif bmode=='screen':
            blended=(1.0-(1.0-base_rgb/255.0)*(1.0-grad_rgb/255.0))*255.0
        elif bmode=='overlay':
            bn,gn=base_rgb/255.0,grad_rgb/255.0
            blended=np.where(bn<0.5,2.0*bn*gn,1.0-2.0*(1.0-bn)*(1.0-gn))*255.0
        else:
            blended=grad_rgb
        opct=max(0.0,min(1.0,float(grad.get('opacity',100))/100.0))
        colors=base_rgb*(1.0-opct)+blended*opct
        fill=Image.fromarray(np.clip(colors,0,255).round().astype('uint8'))
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
