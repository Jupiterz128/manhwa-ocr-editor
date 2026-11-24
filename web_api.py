"""Local desktop HTTP interface. Each project has its own SQLite snapshot and assets."""
import copy
import io
import json
import math
import os
import shutil
import socket
import sqlite3
import threading
import uuid
import webbrowser
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
import uvicorn

import manhwa_ocr as core
from bubble_cleaner import Cleaner, compose_page, prepare_region, encode_region, decode_region, encode_patch
from clean_quality import clean_checked
from text_layout import (
    FONT_CHOICES,
    detect_and_parse_translations,
    export_source_script,
    layout_text,
    resolve_font,
    validate_layer,
)
from text_style_sampler import effects, sample_style

ROOT = core.DATA_DIR / 'studio4'
WEB = Path(__file__).with_name('web')
VALID_OCR_LANGS = ('ko+en', 'en', 'zh+en', 'zh_tw+en')
VALID_OCR_ENGINES = ('rapid', 'paddle', 'easy', 'auto')
app = FastAPI(title='Manhwa OCR Editor 4.0', docs_url=None, redoc_url=None)
lock = threading.RLock()
cancel = threading.Event()
jobs = {}
mask_sessions = {}
pipelines = {}
pipeline = None
ocr_cache = None
cleaner_inst = None


def get_ocr_cache():
    global ocr_cache
    if ocr_cache is None:
        ocr_cache = core.OCRCache()
    return ocr_cache


def get_cleaner():
    global cleaner_inst
    if cleaner_inst is None:
        cleaner_inst = Cleaner()
    return cleaner_inst


def store():
    ROOT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ROOT / 'projects.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT,state TEXT)')
    return db


def load(key):
    with closing(store()) as db:
        row = db.execute('SELECT state FROM projects WHERE id=?', (key,)).fetchone()
    if not row:
        raise HTTPException(404, 'ไม่พบโปรเจกต์')
    data = json.loads(row[0])
    pages = []
    for record in data['pages']:
        record['bubbles'] = [core.Bubble(**b) for b in record['bubbles']]
        pages.append(core.Page(**record))
    return pages, data['masks']


def project_name(key):
    with closing(store()) as db:
        row = db.execute('SELECT name FROM projects WHERE id=?', (key,)).fetchone()
    return row[0] if row and row[0] else 'manhwa'


def save(key, name, pages, masks):
    state = json.dumps(dict(pages=[asdict(p) for p in pages], masks=masks), ensure_ascii=False)
    with closing(store()) as db, db:
        db.execute(
            'INSERT INTO projects VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state',
            (key, name, state),
        )


def summary(page):
    with Image.open(page.path) as im:
        width, height = im.size
    return dict(
        name=Path(page.path).name,
        width=width,
        height=height,
        clean=bool(page.clean_patches),
        cleaned_ids=[pt.get('bubble_id') for pt in page.clean_patches if pt.get('bubble_id')],
        bubbles=[
            {
                k: v
                for k, v in asdict(b).items()
                if k in ('id', 'box', 'text', 'translation', 'score', 'frame_type', 'text_layer', 'font_choice')
            }
            for b in page.bubbles
        ],
    )


@app.middleware('http')
async def local_only(request: Request, call_next):
    host = request.url.hostname
    if host not in ('127.0.0.1', 'localhost', 'testserver') or (
        request.method not in ('GET', 'HEAD') and request.headers.get('x-studio') != 'editor'
    ):
        return Response(status_code=403)
    try:
        return await call_next(request)
    except (ValueError, RuntimeError, OSError) as exc:
        body = {'detail': str(exc)}
        if getattr(exc, 'line', None) is not None:
            body['line'] = exc.line
        return JSONResponse(body, status_code=400)


@app.get('/api/projects')
def projects():
    with closing(store()) as db:
        return list(db.execute('SELECT id,name FROM projects ORDER BY rowid DESC'))


@app.post('/api/projects')
def create(files: list[UploadFile] = File(...)):
    key = uuid.uuid4().hex
    folder = ROOT / key
    folder.mkdir(parents=True)
    pages = []
    try:
        for idx, item in enumerate(files):
            dest = folder / str(idx) / Path(item.filename or 'page.png').name
            dest.parent.mkdir()
            with dest.open('xb') as stream:
                shutil.copyfileobj(item.file, stream)
            if dest.suffix.lower() == '.mtp':
                imported, _, _ = core.read_mtp(dest, cancel, lambda _: None)
                pages.extend(imported)
            else:
                with Image.open(dest) as im:
                    im.verify()
                pages.append(core.Page(str(dest)))
        save(key, Path(files[0].filename).name, pages, {})
    except Exception:
        shutil.rmtree(folder)
        raise
    return key


@app.get('/api/{key}/state')
def state(key: str):
    pages, _ = load(key)
    return dict(pages=[summary(p) for p in pages], fonts=list(FONT_CHOICES))


@app.get('/api/font/{font}')
def font_file(font: str):
    if font not in FONT_CHOICES:
        raise HTTPException(404)
    return FileResponse(resolve_font(font).path)


def find(pages, n, bid):
    if not 0 <= n < len(pages):
        raise HTTPException(404, 'ไม่พบหน้า')
    page = pages[n]
    if bid is None:
        return page, None
    for b in page.bubbles:
        if b.id == bid:
            return page, b
    raise HTTPException(404, 'ไม่พบกรอบ')


def _paint_on_region(reg, points, radius, erase=False):
    pts = np.asarray(points, np.int32).reshape(-1, 2) - np.asarray(reg.box[:2], np.int32)
    r = max(1, min(128, int(radius)))
    value = 0 if erase else 255
    if len(pts):
        cv2.polylines(reg.mask, [pts], False, value, 2 * r)
        for pt in (pts[0], pts[-1]):
            cv2.circle(reg.mask, (int(pt[0]), int(pt[1])), r, value, -1)
    reg.mask[reg.safe == 0] = 0
    reg.manual_edited = True


def _overlay_mask(im, reg):
    im.paste((139, 92, 246), reg.box, Image.fromarray((reg.mask * 0.58).astype('uint8')))
    if np.any(reg.safe):
        contours, _ = cv2.findContours(reg.safe, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            edge = np.zeros_like(reg.safe)
            cv2.drawContours(edge, contours, -1, 255, 2)
            im.paste((56, 189, 248), reg.box, Image.fromarray((edge * 0.85).astype('uint8')))


@app.get('/api/{key}/image/{n}')
def image(key: str, n: int, view: str = 'original', bid: str = ''):
    pages, masks = load(key)
    page, _ = find(pages, n, None)
    im = core.load_image(page.path)
    if view in ('thai', 'clean'):
        im = compose_page(im, page.clean_patches)
    elif view == 'mask':
        for b in page.bubbles:
            reg = decode_region(masks[b.id]) if b.id in masks else prepare_region(im, b)
            im.paste((109, 85, 240), reg.box, Image.fromarray((reg.mask * 0.55).astype('uint8')))
    elif view == 'mask_draft' and bid:
        other = [pt for pt in page.clean_patches if pt.get('bubble_id') != bid]
        base = compose_page(im, other) if other else im.copy()
        sess = mask_sessions.get((key, n, bid))
        if sess is not None:
            reg = sess['region']
        else:
            _, b = find(pages, n, bid)
            reg = decode_region(masks[b.id]) if b.id in masks else prepare_region(im, b)
        _overlay_mask(base, reg)
        im = base
    elif view == 'mask_preview' and bid:
        other = [pt for pt in page.clean_patches if pt.get('bubble_id') != bid]
        sess = mask_sessions.get((key, n, bid))
        preview_patch = sess.get('preview_patch') if sess else None
        patches = other + ([preview_patch] if preview_patch else [])
        im = compose_page(im, patches) if patches else im
    buf = io.BytesIO()
    im.save(buf, 'PNG', compress_level=1)
    return Response(buf.getvalue(), media_type='image/png')


def _clip_bubble_box(raw, im):
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError('พิกัดกรอบไม่ถูกต้อง')
    vals = [float(v) for v in raw]
    if not all(map(math.isfinite, vals)):
        raise ValueError('พิกัดกรอบไม่ถูกต้อง')
    x0, x1 = sorted((vals[0], vals[2]))
    y0, y1 = sorted((vals[1], vals[3]))
    x0 = max(0, min(im.width - 1, int(round(x0))))
    y0 = max(0, min(im.height - 1, int(round(y0))))
    x1 = max(0, min(im.width, int(round(x1))))
    y1 = max(0, min(im.height, int(round(y1))))
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError('กรอบเล็กเกินไป (ต้องกว้าง/สูงอย่างน้อย 8 px)')
    return [x0, y0, x1, y1]


def text_update(im, page, b, d):
    if 'bubble_box' in d:
        b.box = _clip_bubble_box(d['bubble_box'], im)
        b.quad = []
        if b.text_layer:
            b.text_layer['bubble_box'] = list(b.box)
            b.text_layer['bubble_quad'] = []
    for k in ('text', 'translation', 'frame_type', 'font_choice'):
        if k in d:
            setattr(b, k, d[k])
    if 'frame_type' in d:
        b.frame_type_locked = True
    if not b.translation.strip():
        b.text_layer = {}
        return
    if 'bubble_box' in d and not any(
        k in d for k in ('translation', 'font_choice', 'box', 'rotation', 'font_size', 'alignment', 'wrapped', 'color', 'hidden', 'style')
    ):
        return
    previous = b.text_layer
    layer = previous
    if not layer or any(k in d for k in ('translation', 'font_choice')):
        layer = layout_text(compose_page(im, page.clean_patches), b)
        if previous.get('editor_box'):
            for field in ('editor_box', 'rotation', 'alignment', 'color', 'hidden', 'font_size'):
                if field in previous:
                    layer[field] = previous[field]
    old = layer.get('editor_box')
    if old is None:
        x0, y0, x1, y1 = layer['box']
        w, h = layer.get('text_size', (x1 - x0, y1 - y0))
        old = [(x0 + x1 - w) / 2, (y0 + y1 - h) / 2, (x0 + x1 + w) / 2, (y0 + y1 + h) / 2]
    rect = list(map(float, d.get('box', old)))
    x, y, r, bt = rect
    if not all(map(math.isfinite, rect)) or r <= x or bt <= y:
        raise ValueError('ขนาดกรอบไม่ถูกต้อง')
    angle = float(d.get('rotation', layer.get('rotation', 0)))
    size = float(d.get('font_size', layer['font_size']))
    align = d.get('alignment', layer.get('alignment', 'center'))
    if not -180 <= angle <= 180 or not 6 <= size <= 220 or align not in ('left', 'center', 'right'):
        raise ValueError('รูปแบบข้อความไม่ถูกต้อง')
    w, h = r - x, bt - y
    cs, sn = abs(math.cos(math.radians(angle))), abs(math.sin(math.radians(angle)))
    rw, rh = w * cs + h * sn, w * sn + h * cs
    if rw > im.width + 1 or rh > im.height + 1:
        raise ValueError('กรอบข้อความใหญ่เกินหน้าภาพ')
    cx = min(max((x + r) / 2, rw / 2), im.width - rw / 2)
    cy = min(max((y + bt) / 2, rh / 2), im.height - rh / 2)
    rect = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
    layer.update(
        box=[cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2],
        editor_box=rect,
        font_size=size,
        rotation=angle,
        custom_rotation=angle,
        alignment=align,
        auto_leading=True,
        text_size=[w, h],
        custom_size=round(size),
    )
    if 'wrapped' in d:
        if ''.join(d['wrapped'].split()) != ''.join(b.translation.split()):
            raise ValueError('ข้อความพรีวิวไม่ตรงกับคำแปล')
        layer['text'] = d['wrapped']
    b.custom_size = round(size)
    b.custom_rotation = angle
    for k in ('color', 'hidden'):
        if k in d:
            layer[k] = d[k]
    if 'style' in d:
        layer.update(effects(d['style']), manual_effects=True)
    validate_layer(layer)
    b.text_layer = layer


def reader(lang='ko+en', engine='auto', neural=True, type_scan=False):
    global pipeline
    if lang not in VALID_OCR_LANGS:
        raise ValueError(f'ไม่รองรับภาษา OCR: {lang}')
    if engine not in VALID_OCR_ENGINES:
        raise ValueError(f'ไม่รองรับโมเดล OCR: {engine}')
    key = (engine, lang, bool(neural), bool(type_scan))
    pipe = pipelines.get(key)
    if pipe is None:
        pipe = core.LocalPipeline(
            get_ocr_cache(),
            mode=engine,
            lang=lang,
            gpu=core._auto_gpu_default(),
            neural=bool(neural),
            retry=False,
            fast=True,
            bubble_only=True,
            type_scan=bool(type_scan),
            prefer_rapid=True,
            log=print,
            cancel=cancel,
        )
        pipelines[key] = pipe
    if key == ('auto', 'ko+en', True, False):
        pipeline = pipe
    return pipe


def _filter_ocr_bubbles(bubbles):
    """Drop empty / 0%-confidence false-positive detections from auto OCR."""
    return [b for b in bubbles if (b.text or '').strip() and float(getattr(b, 'score', 0) or 0) > 0]


def _ocr_single_bubble_rapid(im, b, lang='ko+en'):
    """Delegate single-bubble RapidOCR to manhwa_ocr.LocalPipeline.candidates."""
    pipe = reader(lang=lang, engine='rapid', neural=False, type_scan=False)
    probe = copy.copy(b)
    probe.text = ''
    candidates = pipe.candidates(im, probe)
    if not candidates:
        b.text = ''
        b.score = 0.0
        b.engine = 'RapidOCR (PP-OCRv5)'
        b.candidates = []
        return '', 0.0
    best = max(candidates, key=lambda c: float(c.get('score', 0) or 0))
    b.text = best['text']
    b.score = float(best.get('score', 0) or 0)
    b.engine = best.get('engine', 'RapidOCR (PP-OCRv5)')
    b.candidates = candidates
    return b.text, b.score


def _make_verifier(reg):
    def verify(result):
        pipe = reader(lang='ko+en', engine='auto', neural=False, type_scan=False)
        lines = pipe.read(next(pipe.iter_engines()), result, 'studio-clean')
        return [
            dict(
                box=[
                    v.box[0] + reg.box[0],
                    v.box[1] + reg.box[1],
                    v.box[2] + reg.box[0],
                    v.box[3] + reg.box[1],
                ],
                text=v.text,
            )
            for v in lines
        ]

    return verify


def _get_or_init_mask_session(key, n, im, b, masks):
    skey = (key, n, b.id)
    sess = mask_sessions.get(skey)
    if sess is None:
        reg = decode_region(masks[b.id]) if b.id in masks else prepare_region(im, b)
        sess = {'region': copy.deepcopy(reg), 'undo': [], 'preview_patch': None}
        mask_sessions[skey] = sess
    return sess


@app.get('/api/{key}/txt')
def export_txt(key: str, mode: str = 'id_template'):
    pages, _ = load(key)
    if mode not in ('id_template', 'numbered', 'plain_lines'):
        raise ValueError('รูปแบบส่งออก TXT ไม่ถูกต้อง')
    text = export_source_script(pages, mode)
    stem = Path(project_name(key)).stem or 'manhwa'
    return dict(mode=mode, text=text, filename=f'{stem}_ocr.txt')


@app.post('/api/{key}/txt/import')
def import_txt(key: str, d: dict):
    with lock:
        pages, masks = load(key)
        raw = str(d.get('text', ''))
        changes, report = detect_and_parse_translations(raw, pages)
        if not changes:
            raise ValueError('ไม่พบคำแปลที่จับคู่กับกรอบในโปรเจกต์นี้')
        frame_tags = report.get('frame_tags') or {}
        for p in pages:
            im = None
            for b in p.bubbles:
                if b.id not in changes:
                    continue
                if im is None:
                    im = core.load_image(p.path)
                payload = {'translation': changes[b.id]}
                if b.id in frame_tags:
                    payload['frame_type'] = frame_tags[b.id]
                text_update(im, p, b, payload)
        save(key, '', pages, masks)
        return dict(
            pages=[summary(p) for p in pages],
            report=report,
            applied=len(changes),
            note=f'นำเข้าคำแปล {len(changes)}/{report["total_bubbles"]} กรอบ',
        )


@app.post('/api/{key}/batch/{action}')
def batch_action(key: str, action: str, d: dict):
    with lock:
        pages, masks = load(key)
        if not pages:
            raise ValueError('ยังไม่มีหน้าการ์ตูนในโปรเจกต์')
        note = ''
        if action == 'ocr':
            if any(p.bubbles for p in pages) and not d.get('replace'):
                raise ValueError('ยืนยันการอ่าน OCR ใหม่ทุกหน้าก่อน')
            lang = d.get('lang', 'ko+en')
            engine = d.get('engine', 'rapid')
            for k_sess in [k for k in mask_sessions if k[0] == key]:
                mask_sessions.pop(k_sess, None)
            pipe = reader(lang=lang, engine=engine, neural=True, type_scan=False)
            total = 0
            for p in pages:
                for old in p.bubbles:
                    masks.pop(old.id, None)
                    masks.pop('undo:' + old.id, None)
                im = core.load_image(p.path)
                p.bubbles = _filter_ocr_bubbles(pipe.scan(im))
                p.clean_patches = []
                total += len(p.bubbles)
            note = f'OCR ({engine} · {lang}) สำเร็จ {len(pages)} หน้า • {total} กรอบ'
        elif action == 'mask':
            total = 0
            for p in pages:
                if not p.bubbles:
                    continue
                im = core.load_image(p.path)
                for b in p.bubbles:
                    if not d.get('reset') and b.id in masks and masks[b.id].get('manual_edited'):
                        total += 1
                        continue
                    reg = prepare_region(im, b)
                    masks[b.id] = encode_region(reg)
                    total += 1
            note = f'สร้าง Mask ทุกหน้าสำเร็จ {len(pages)} หน้า • {total} กรอบ'
        elif action == 'clean':
            cleaner = get_cleaner()
            total = 0
            review = 0
            for p in pages:
                if not p.bubbles:
                    continue
                im = core.load_image(p.path)
                for item in p.bubbles:
                    reg = decode_region(masks[item.id]) if item.id in masks else prepare_region(im, item)
                    repaired, mask, method, quality = clean_checked(
                        cleaner, reg, cancel=cancel, reader=_make_verifier(reg)
                    )
                    patch = encode_patch(reg.box, repaired, mask, method, item.id)
                    patch['quality'] = quality
                    review += int(quality['needs_review'])
                    p.clean_patches = [v for v in p.clean_patches if v.get('bubble_id') != item.id] + [patch]
                    total += 1
            note = f'Clean ทุกหน้าสำเร็จ {len(pages)} หน้า • {total} กรอบ • ต้องตรวจเพิ่ม {review}'
        else:
            raise ValueError('ไม่รองรับคำสั่งทุกหน้านี้')
        save(key, '', pages, masks)
        return dict(pages=[summary(p) for p in pages], note=note)


@app.post('/api/{key}/page/{n}/{action}')
def action(key: str, n: int, action: str, d: dict):
    with lock:
        pages, masks = load(key)
        p, b = find(pages, n, d.get('id'))
        targets = [b] if b else p.bubbles
        im = core.load_image(p.path)
        note = ''
        extra_out = {}
        if action == 'edit' and b:
            text_update(im, p, b, d)
            if 'bubble_box' in d:
                masks.pop(b.id, None)
                masks.pop('undo:' + b.id, None)
        elif action == 'add_bubble':
            box = _clip_bubble_box(d.get('box'), im)
            new_b = core.Bubble(
                box=box,
                text='',
                translation='',
                lines=[],
                score=1.0,
                engine='manual',
                frame_type='normal',
            )
            p.bubbles.append(new_b)
            extra_out['id'] = new_b.id
            note = 'เพิ่มกรอบใหม่แล้ว — ย้าย/ปรับขนาดได้ด้วย V หรือกด RapidOCR ช่องนี้'
        elif action == 'delete_bubble' and b:
            p.bubbles = [v for v in p.bubbles if v.id != b.id]
            p.clean_patches = [v for v in p.clean_patches if v.get('bubble_id') != b.id]
            masks.pop(b.id, None)
            masks.pop('undo:' + b.id, None)
            mask_sessions.pop((key, n, b.id), None)
            note = 'ลบกรอบแล้ว'
        elif action == 'ocr_bubble' and b:
            lang = d.get('lang', 'ko+en')
            txt, sc = _ocr_single_bubble_rapid(im, b, lang=lang)
            extra_out['text'] = txt
            extra_out['score'] = sc
            note = f'RapidOCR ช่องนี้สำเร็จ ({round(sc * 100)}%)' if txt else 'RapidOCR ไม่พบข้อความในกรอบนี้'
        elif action == 'fx' and b and b.translation:
            text_update(
                im, p, b, {'style': sample_style(im, compose_page(im, p.clean_patches) if p.clean_patches else None, b)}
            )
        elif action == 'ocr':
            if p.bubbles and not d.get('replace'):
                raise ValueError('ยืนยันการอ่าน OCR ใหม่ก่อน')
            lang = d.get('lang', 'ko+en')
            engine = d.get('engine', 'auto')
            for old in p.bubbles:
                masks.pop(old.id, None)
                masks.pop('undo:' + old.id, None)
                mask_sessions.pop((key, n, old.id), None)
            p.bubbles = _filter_ocr_bubbles(reader(lang=lang, engine=engine, neural=True, type_scan=False).scan(im))
            p.clean_patches = []
        elif action == 'translate':
            from gemini_translator import translate_pages_with_gemini

            temp = core.Page(p.path, targets)
            hint = ('เกลาสำนวนคำแปลเดิม: ' + b.translation) if b else ''
            translate_pages_with_gemini([temp], overwrite=True, translation_note=hint)
            for item in targets:
                text_update(im, p, item, {'translation': item.translation})
        elif action == 'paint' and b:
            reg = decode_region(masks[b.id]) if b.id in masks else prepare_region(im, b)
            history = masks.setdefault('undo:' + b.id, [])
            history.append(masks.get(b.id))
            del history[:-8]
            _paint_on_region(reg, d['points'], d['radius'], erase=bool(d.get('erase')))
            masks[b.id] = encode_region(reg)
        elif action == 'undo' and b:
            history = masks.get('undo:' + b.id, [])
            if history:
                prev = history.pop()
                if prev:
                    masks[b.id] = prev
                else:
                    masks.pop(b.id, None)
        elif action == 'mask_start' and b:
            mask_sessions.pop((key, n, b.id), None)
            _get_or_init_mask_session(key, n, im, b, masks)
            return dict(
                page=summary(p),
                has_preview=False,
                note='โหมดแก้ Mask รายช่อง — ระบายเพิ่ม/ลบ แล้วกด Preview เพื่อดูผลก่อนบันทึก',
            )
        elif action == 'mask_paint' and b:
            sess = _get_or_init_mask_session(key, n, im, b, masks)
            reg = sess['region']
            sess['undo'].append((reg.mask.copy(), bool(reg.manual_edited)))
            del sess['undo'][:-12]
            _paint_on_region(reg, d['points'], d['radius'], erase=bool(d.get('erase')))
            sess['preview_patch'] = None
            return dict(page=summary(p), has_preview=False, note='ปรับ Mask แล้ว — กด Preview เพื่อดูผลลัพธ์')
        elif action == 'mask_undo' and b:
            sess = _get_or_init_mask_session(key, n, im, b, masks)
            if sess['undo']:
                prev_mask, prev_edited = sess['undo'].pop()
                sess['region'].mask = prev_mask
                sess['region'].manual_edited = prev_edited
                sess['preview_patch'] = None
                note = 'ย้อน Mask แล้ว'
            else:
                note = 'ไม่มีประวัติ Mask ให้ย้อน'
            return dict(page=summary(p), has_preview=False, note=note)
        elif action == 'mask_preview' and b:
            sess = _get_or_init_mask_session(key, n, im, b, masks)
            reg = sess['region']
            cleaner = get_cleaner()
            verify = None if reg.manual_edited else _make_verifier(reg)
            repaired, mask, method, quality = clean_checked(
                cleaner, reg, method=d.get('method', 'auto'), cancel=cancel, reader=verify
            )
            patch = encode_patch(reg.box, repaired, mask, method, b.id)
            patch['quality'] = quality
            sess['preview_patch'] = patch
            return dict(
                page=summary(p),
                has_preview=True,
                method=method,
                note=f'Preview ({method}) พร้อมแล้ว — กด ตกลง เพื่อบันทึก หรือแก้ Mask เพิ่ม',
            )
        elif action == 'mask_apply' and b:
            sess = _get_or_init_mask_session(key, n, im, b, masks)
            reg = sess['region']
            patch = sess.get('preview_patch')
            if patch is None:
                cleaner = get_cleaner()
                verify = None if reg.manual_edited else _make_verifier(reg)
                repaired, mask, method, quality = clean_checked(
                    cleaner, reg, method=d.get('method', 'auto'), cancel=cancel, reader=verify
                )
                patch = encode_patch(reg.box, repaired, mask, method, b.id)
                patch['quality'] = quality
            masks[b.id] = encode_region(reg)
            p.clean_patches = [v for v in p.clean_patches if v.get('bubble_id') != b.id] + [patch]
            mask_sessions.pop((key, n, b.id), None)
            note = 'บันทึกผล Clean ของกรอบนี้แล้ว'
        elif action == 'mask_cancel' and b:
            mask_sessions.pop((key, n, b.id), None)
            return dict(page=summary(p), has_preview=False, note='ยกเลิกการแก้ Mask แล้ว (ไม่บันทึกทับ)')
        elif action == 'clean':
            cleaner = get_cleaner()
            review = 0
            for item in targets:
                reg = decode_region(masks[item.id]) if item.id in masks else prepare_region(im, item)
                repaired, mask, method, quality = clean_checked(
                    cleaner, reg, cancel=cancel, reader=_make_verifier(reg)
                )
                patch = encode_patch(reg.box, repaired, mask, method, item.id)
                patch['quality'] = quality
                review += int(quality['needs_review'])
                p.clean_patches = [v for v in p.clean_patches if v.get('bubble_id') != item.id] + [patch]
            note = f'คลีน {len(targets)} กรอบ • ต้องตรวจเพิ่ม {review}'
        else:
            raise ValueError('เลือกกรอบข้อความก่อนใช้คำสั่งนี้')
        save(key, '', pages, masks)
        return dict(page=summary(p), note=note, **extra_out)


@app.post('/api/{key}/export')
def export(key: str, d: dict):
    from photoshop_export import export_psd

    pages, _ = load(key)
    fmt = d.get('format', 'psd')
    if fmt not in ('psd', 'mtp', 'all') or d.get('backend') not in ('photoshop', 'photopea'):
        raise ValueError('ตัวเลือกส่งออกไม่ถูกต้อง')
    job = uuid.uuid4().hex
    folder = ROOT / key / 'exports' / job
    folder.mkdir(parents=True)
    progress = jobs[job] = dict(done=False, value=0, files=[], folder=str(folder))

    def run():
        try:
            files = []
            for i, p in enumerate(pages, 1):
                for b in p.bubbles:
                    if not d.get('fx', True) and b.text_layer:
                        b.text_layer.update(effects())
                if fmt != 'mtp':
                    visible = copy.copy(p)
                    visible.bubbles = [b for b in p.bubbles if not b.text_layer.get('hidden')]
                    out = folder / f'{i:02}.psd'
                    export_psd(core.load_image(p.path), visible, out, backend=d['backend'])
                    files.append(out)
                progress['value'] = int(i * 80 / len(pages))
            if fmt != 'psd':
                out = folder / 'project.mtp'
                core.write_mtp(out, pages, {}, cancel, lambda _: None)
                files.append(out)
            progress['files'] = [f.name for f in files]
            if d.get('drive'):
                import gdrive_sync as drive

                dest = drive.create_job_subfolder(drive.PSD_OUTPUT_FOLDER_ID, Path(pages[0].path).stem)
                for f in files:
                    drive.upload_file_to_folder(f, dest)
            progress['value'] = 100
        except Exception as exc:
            progress['error'] = str(exc)
        finally:
            progress['done'] = True

    threading.Thread(target=run, daemon=True).start()
    return job


@app.get('/api/jobs/{job}/progress')
def progress(job: str):
    if job not in jobs:
        raise HTTPException(404)
    return jobs[job]


@app.get('/api/jobs/{job}/file/{name}')
def download(job: str, name: str):
    data = progress(job)
    if name not in data['files']:
        raise HTTPException(404)
    return FileResponse(Path(data['folder']) / name, filename=name)


app.mount('/', StaticFiles(directory=WEB, html=True), name='studio')


def main():
    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        import time

        while not server.started and thread.is_alive():
            time.sleep(0.03)
        if not server.started:
            raise RuntimeError('เปิดบริการ localhost ไม่สำเร็จ')
        try:
            import webview

            webview.create_window('Manhwa OCR Editor 4.0', url, width=1440, height=900, maximized=True)
            webview.start()
        except Exception:
            print(url, flush=True)
            webbrowser.open(url)
            thread.join()
        finally:
            server.should_exit = True
            thread.join(5)


if __name__ == '__main__':
    main()
