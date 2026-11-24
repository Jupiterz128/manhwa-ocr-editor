"""Local desktop HTTP interface. Each project has its own SQLite snapshot and assets."""
import copy
import io
import json
import math
import os
import shutil
import socket
import sqlite3
import subprocess
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
    DEFAULT_FONT_PRESETS,
    FONT_CHOICES,
    FONT_NAME,
    FRAME_TYPES,
    FRAME_TYPE_LABELS,
    classify_bubble_type,
    detect_and_parse_translations,
    export_source_script,
    layout_text,
    load_font_presets,
    normalize_font_choice,
    resolve_font,
    save_font_presets,
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
main_window = None


def _pick_save_path(default_name, filetypes, title='บันทึกไฟล์', initial_dir=''):
    """Open native Windows Save As dialog via pywebview (preferred) or Tk fallback."""
    if main_window is not None:
        try:
            import webview

            dialog_type = getattr(getattr(webview, 'FileDialog', None), 'SAVE', None) or getattr(webview, 'SAVE_DIALOG', 30)
            ft = tuple(f'{label} ({pat})' for label, pat in filetypes)
            chosen = main_window.create_file_dialog(
                dialog_type, directory=str(initial_dir or ''), save_filename=default_name, file_types=ft
            )
            if isinstance(chosen, (list, tuple)):
                chosen = chosen[0] if chosen else ''
            return Path(chosen) if chosen else None
        except Exception:
            pass
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes('-topmost', True)
    except Exception:
        pass
    try:
        ext = Path(default_name).suffix or ''
        chosen = filedialog.asksaveasfilename(
            parent=root,
            title=title,
            initialdir=str(initial_dir) if initial_dir else None,
            initialfile=default_name,
            defaultextension=ext,
            filetypes=list(filetypes) + [('All files', '*.*')],
        )
        return Path(chosen) if chosen else None
    finally:
        root.destroy()


def _pick_open_path(filetypes, title='เปิดไฟล์โปรเจกต์', initial_dir=''):
    """Open native Windows Open File dialog via pywebview or Tk fallback."""
    if main_window is not None:
        try:
            import webview

            dialog_type = getattr(getattr(webview, 'FileDialog', None), 'OPEN', None) or getattr(webview, 'OPEN_DIALOG', 10)
            ft = tuple(f'{label} ({pat})' for label, pat in filetypes)
            chosen = main_window.create_file_dialog(
                dialog_type, directory=str(initial_dir or ''), allow_multiple=False, file_types=ft
            )
            if isinstance(chosen, (list, tuple)):
                chosen = chosen[0] if chosen else ''
            return Path(chosen) if chosen else None
        except Exception:
            pass
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes('-topmost', True)
    except Exception:
        pass
    try:
        chosen = filedialog.askopenfilename(
            parent=root,
            title=title,
            initialdir=str(initial_dir) if initial_dir else None,
            filetypes=list(filetypes) + [('All files', '*.*')],
        )
        return Path(chosen) if chosen else None
    finally:
        root.destroy()


def _pick_folder_path(title='เลือกโฟลเดอร์ปลายทาง', initial_dir=''):
    """Open native Windows Folder picker via pywebview or Tk fallback."""
    if main_window is not None:
        try:
            import webview

            dialog_type = getattr(getattr(webview, 'FileDialog', None), 'FOLDER', None) or getattr(webview, 'FOLDER_DIALOG', 20)
            chosen = main_window.create_file_dialog(dialog_type, directory=str(initial_dir or ''))
            if isinstance(chosen, (list, tuple)):
                chosen = chosen[0] if chosen else ''
            return Path(chosen) if chosen else None
        except Exception:
            pass
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes('-topmost', True)
    except Exception:
        pass
    try:
        chosen = filedialog.askdirectory(
            parent=root,
            title=title,
            initialdir=str(initial_dir) if initial_dir else None,
        )
        return Path(chosen) if chosen else None
    finally:
        root.destroy()


def _reveal_in_explorer(target: Path):
    target = Path(target).resolve()
    if not target.exists():
        if target.parent.exists():
            target = target.parent
        else:
            raise ValueError(f'ไม่พบไฟล์หรือโฟลเดอร์: {target}')
    if os.name == 'nt':
        if target.is_file():
            subprocess.Popen(['explorer', f'/select,{str(target)}'])
        else:
            subprocess.Popen(['explorer', str(target)])
    return str(target)


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


def project_json_path(key):
    with closing(store()) as db:
        row = db.execute('SELECT state FROM projects WHERE id=?', (key,)).fetchone()
    if not row:
        return ''
    try:
        return str(json.loads(row[0]).get('json_path') or '')
    except Exception:
        return ''


def save(key, name, pages, masks, json_path=None):
    if json_path is None:
        json_path = project_json_path(key)
    state = json.dumps(
        dict(pages=[asdict(p) for p in pages], masks=masks, json_path=str(json_path or '')),
        ensure_ascii=False,
    )
    with closing(store()) as db, db:
        db.execute(
            'INSERT INTO projects VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state',
            (key, name, state),
        )


def _load_json_project_file(json_file: Path):
    data = json.loads(json_file.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('format') != 'manhwa-ocr-project' or not isinstance(data.get('pages'), list):
        raise ValueError('ไฟล์ .json นี้ไม่ใช่โปรเจกต์ Manhwa OCR Editor ที่ถูกต้อง')
    pages, drafts, missing = core.read_project_pages(data, json_file)
    if missing:
        raise ValueError(f'ไม่พบไฟล์ภาพต้นฉบับในโปรเจกต์ .json: {missing[0].path}')
    masks = {}
    raw_studio_masks = data.get('studio_masks')
    if isinstance(raw_studio_masks, dict):
        masks.update(raw_studio_masks)
    for (_, bid), reg in drafts.items():
        if bid not in masks:
            masks[bid] = encode_region(reg)
    title = str(data.get('name') or json_file.name)
    return pages, masks, title


def _write_json_project_file(dest: Path, pages, masks, title):
    dest = Path(dest)
    if dest.suffix.lower() != '.json':
        dest = dest.with_suffix('.json')
    dest.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for p in pages:
        rec = asdict(p)
        rec['clean_drafts'] = {
            b.id: masks[b.id]
            for b in p.bubbles
            if b.id in masks and isinstance(masks[b.id], dict) and 'image' in masks[b.id]
        }
        rec['path'] = str(Path(p.path).resolve())
        records.append(rec)
    payload = {
        'format': 'manhwa-ocr-project',
        'version': 5,
        'studio_version': '4.0',
        'name': title,
        'pages': records,
        'studio_masks': masks,
    }
    core.atomic_json(dest, payload)
    return dest.resolve()


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
    masks = {}
    title = Path(files[0].filename or 'page.png').name
    try:
        for idx, item in enumerate(files):
            dest = folder / str(idx) / Path(item.filename or 'page.png').name
            dest.parent.mkdir()
            with dest.open('xb') as stream:
                shutil.copyfileobj(item.file, stream)
            ext = dest.suffix.lower()
            if ext == '.mtp':
                imported, _, _ = core.read_mtp(dest, cancel, lambda _: None)
                pages.extend(imported)
            elif ext == '.json':
                j_pages, j_masks, j_title = _load_json_project_file(dest)
                pages.extend(j_pages)
                masks.update(j_masks)
                if idx == 0:
                    title = j_title
            else:
                with Image.open(dest) as im:
                    im.verify()
                pages.append(core.Page(str(dest)))
        save(key, title, pages, masks, json_path='')
    except Exception:
        shutil.rmtree(folder)
        raise
    return key


@app.post('/api/open-json')
def open_json_project(d: dict | None = None):
    d = d or {}
    if d.get('path'):
        chosen = Path(d['path'])
    else:
        chosen = _pick_open_path([('Manhwa OCR Project', '*.json')], title='เปิดไฟล์โปรเจกต์ (.json)')
    if chosen is None:
        return dict(cancelled=True)
    chosen = chosen.resolve()
    if not chosen.is_file():
        raise ValueError(f'ไม่พบไฟล์: {chosen}')
    pages, masks, title = _load_json_project_file(chosen)
    key = uuid.uuid4().hex
    save(key, title, pages, masks, json_path=str(chosen))
    return dict(cancelled=False, key=key, name=title, path=str(chosen))


@app.post('/api/pick-folder')
def pick_folder(d: dict | None = None):
    d = d or {}
    chosen = _pick_folder_path(
        title=str(d.get('title') or 'เลือกโฟลเดอร์ปลายทาง'),
        initial_dir=str(d.get('initial_dir') or ''),
    )
    if chosen is None:
        return dict(cancelled=True, path='')
    return dict(cancelled=False, path=str(chosen.resolve()))


@app.post('/api/reveal')
def reveal_path(d: dict):
    raw = str(d.get('path') or '').strip()
    if not raw:
        raise ValueError('ไม่ได้ระบุพาธไฟล์หรือโฟลเดอร์')
    opened = _reveal_in_explorer(Path(raw))
    return dict(ok=True, path=opened)


@app.post('/api/open-txt')
def open_txt_file(d: dict | None = None):
    d = d or {}
    if d.get('path'):
        chosen = Path(d['path'])
    else:
        chosen = _pick_open_path([('Text file', '*.txt')], title='เปิดไฟล์คำแปล (.txt)')
    if chosen is None:
        return dict(cancelled=True)
    chosen = chosen.resolve()
    if not chosen.is_file():
        raise ValueError(f'ไม่พบไฟล์: {chosen}')
    content = chosen.read_text(encoding='utf-8-sig')
    return dict(cancelled=False, path=str(chosen), filename=chosen.name, text=content)


@app.get('/api/font-presets')
def get_font_presets():
    presets = load_font_presets()
    return dict(
        presets=presets,
        fonts=list(FONT_CHOICES),
        frame_types=[dict(key=k, label=v) for k, v in FRAME_TYPES],
    )


@app.post('/api/font-presets')
def update_font_presets(d: dict):
    with lock:
        presets = load_font_presets()
        delete_name = str(d.get('delete') or '').strip()
        if delete_name:
            if delete_name not in presets:
                raise ValueError(f'ไม่พบโปรไฟล์ฟอนต์ "{delete_name}"')
            if len(presets) <= 1:
                raise ValueError('ต้องเหลือโปรไฟล์ฟอนต์อย่างน้อย 1 รายการ')
            del presets[delete_name]
            save_font_presets(presets)
            return dict(presets=load_font_presets(), name=next(iter(presets)), note=f'ลบโปรไฟล์ฟอนต์ "{delete_name}" แล้ว')
        name = str(d.get('name') or '').strip()
        if not name:
            raise ValueError('กรุณาตั้งชื่อโปรไฟล์ฟอนต์')
        raw_map = d.get('mapping')
        if not isinstance(raw_map, dict):
            base_name = str(d.get('base') or '').strip()
            raw_map = dict(presets.get(base_name) or DEFAULT_FONT_PRESETS['1. มังฮวาเกาหลี'])
        cleaned = {k: normalize_font_choice(raw_map.get(k, FONT_NAME)) for k, _ in FRAME_TYPES}
        presets[name] = cleaned
        save_font_presets(presets)
        return dict(presets=load_font_presets(), name=name, note=f'บันทึกโปรไฟล์ฟอนต์ "{name}" แล้ว')


@app.post('/api/{key}/apply-font-preset')
def apply_font_preset(key: str, d: dict):
    with lock:
        pages, masks = load(key)
        if not pages:
            raise ValueError('ยังไม่มีหน้าการ์ตูนในโปรเจกต์')
        presets = load_font_presets()
        preset_name = str(d.get('preset') or '').strip()
        if not preset_name or preset_name not in presets:
            preset_name = next(iter(presets))
        raw_map = presets.get(preset_name) or DEFAULT_FONT_PRESETS['1. มังฮวาเกาหลี']

        fallback_font = FONT_NAME
        normal_candidate = normalize_font_choice(raw_map.get('normal', FONT_NAME))
        try:
            resolve_font(normal_candidate)
            fallback_font = normal_candidate
        except Exception:
            pass

        resolved_map = {}
        missing_fonts = set()
        for fkey, font_wanted in raw_map.items():
            canon = normalize_font_choice(font_wanted)
            try:
                resolve_font(canon)
                resolved_map[fkey] = canon
            except Exception:
                missing_fonts.add(canon)
                resolved_map[fkey] = fallback_font

        all_pages = bool(d.get('all_pages', True))
        if all_pages:
            targets = [p for p in pages if p.bubbles]
        else:
            p_idx = int(d.get('page', 0))
            pg, _ = find(pages, p_idx, None)
            targets = [pg] if pg.bubbles else []

        total = 0
        laid_out = 0
        failed = 0
        for p in targets:
            raw_im = core.load_image(p.path)
            work_im = compose_page(raw_im, p.clean_patches) if p.clean_patches else raw_im
            for b in p.bubbles:
                total += 1
                ftype = classify_bubble_type(raw_im, b)
                chosen_font = resolved_map.get(ftype, fallback_font)
                b.frame_type = ftype
                b.font_choice = chosen_font
                b.custom_size = 0
                b.custom_width = 0
                b.text_offset = []
                b.custom_rotation = None
                if b.translation.strip():
                    prev_fx = {k: b.text_layer[k] for k in ('stroke', 'gradient', 'manual_effects') if b.text_layer and k in b.text_layer}
                    try:
                        layer = layout_text(
                            raw_im if b.engine == 'MTP' else work_im,
                            b,
                            b.translation,
                            font_choice=chosen_font,
                            original=raw_im,
                            cleaned=work_im if p.clean_patches else None,
                            custom_size=0,
                            custom_width=0,
                            text_offset=[],
                            custom_rotation=None,
                        )
                        layer['editor_box'] = _default_editor_box(b, layer, raw_im)
                        if prev_fx.get('manual_effects'):
                            layer.update(prev_fx)
                        validate_layer(layer)
                        b.text_layer = layer
                        laid_out += 1
                    except Exception:
                        failed += 1
        save(key, '', pages, masks)
        note = f'จัดฟอนต์ตามโปรไฟล์ "{preset_name}" สำเร็จ {laid_out}/{total} บอลลูน'
        if failed:
            note += f' (จัดวางไม่สำเร็จ {failed})'
        if missing_fonts:
            note += f' • ใช้ฟอนต์สำรองแทน {", ".join(sorted(missing_fonts))}'
        return dict(
            pages=[summary(p) for p in pages],
            preset=preset_name,
            total=total,
            laid_out=laid_out,
            failed=failed,
            missing_fonts=sorted(missing_fonts),
            note=note,
        )


@app.get('/api/{key}/state')
def state(key: str):
    pages, _ = load(key)
    return dict(pages=[summary(p) for p in pages], fonts=list(FONT_CHOICES), json_path=project_json_path(key))


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


def _default_editor_box(b, layer, im):
    """Full-bubble transform frame centered on text layout with generous padding so borders/handles never cut Thai text."""
    x0, y0, x1, y1 = layer['box']
    bx0, by0, bx1, by1 = b.box
    fs = float(layer.get('font_size', 24) or 24)
    pad_x = max(16.0, fs * 0.45)
    pad_y = max(20.0, fs * 0.65)
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    tw, th = layer.get('text_size', (x1 - x0, y1 - y0))
    full_w = min(float(im.width), max(float(bx1 - bx0), float(tw) + 2.0 * pad_x))
    full_h = min(float(im.height), max(float(by1 - by0), float(th) + 2.0 * pad_y))
    cx = min(max(cx, full_w / 2.0), float(im.width) - full_w / 2.0)
    cy = min(max(cy, full_h / 2.0), float(im.height) - full_h / 2.0)
    return [cx - full_w / 2.0, cy - full_h / 2.0, cx + full_w / 2.0, cy + full_h / 2.0]


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
        old = _default_editor_box(b, layer, im)
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
    tw_fit, th_fit = w, h
    if rw > im.width or rh > im.height:
        scale_fit = min(im.width / max(1e-6, rw), im.height / max(1e-6, rh)) * 0.998
        tw_fit, th_fit = w * scale_fit, h * scale_fit
        rw, rh = tw_fit * cs + th_fit * sn, tw_fit * sn + th_fit * cs
    cx = min(max((x + r) / 2, rw / 2), im.width - rw / 2)
    cy = min(max((y + bt) / 2, rh / 2), im.height - rh / 2)
    rect = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
    layer.update(
        box=[max(0.0, cx - rw / 2), max(0.0, cy - rh / 2), min(float(im.width), cx + rw / 2), min(float(im.height), cy + rh / 2)],
        editor_box=rect,
        font_size=size,
        rotation=angle,
        custom_rotation=angle,
        alignment=align,
        auto_leading=True,
        text_size=[tw_fit, th_fit],
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


@app.post('/api/{key}/txt/save')
def save_txt_file(key: str, d: dict):
    pages, _ = load(key)
    mode = str(d.get('mode') or 'id_template')
    if mode not in ('id_template', 'numbered', 'plain_lines'):
        raise ValueError('รูปแบบส่งออก TXT ไม่ถูกต้อง')
    content = str(d['text']) if 'text' in d and d['text'] is not None else export_source_script(pages, mode)
    stem = Path(project_name(key)).stem or 'manhwa'
    default_name = f'{stem}_ocr.txt'
    if d.get('path'):
        dest = Path(d['path'])
    else:
        dest = _pick_save_path(default_name, [('Text file', '*.txt')], title='บันทึกไฟล์ส่งออก TXT')
    if dest is None:
        return dict(cancelled=True)
    if dest.suffix.lower() != '.txt':
        dest = dest.with_suffix('.txt')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content if content.endswith('\n') else content + '\n', encoding='utf-8')
    resolved = str(dest.resolve())
    return dict(cancelled=False, path=resolved, filename=dest.name, note=f'บันทึก TXT แล้ว: {resolved}')


@app.post('/api/{key}/save-json')
def save_json_project(key: str, d: dict | None = None):
    d = d or {}
    with lock:
        pages, masks = load(key)
        title = project_name(key)
        stem = Path(title).stem or 'manhwa_project'
        existing = project_json_path(key)
        save_as = bool(d.get('save_as'))
        if d.get('path'):
            dest = Path(d['path'])
        elif existing and not save_as:
            dest = Path(existing)
        else:
            initial_dir = str(Path(existing).parent) if existing else ''
            default_name = Path(existing).name if existing else f'{stem}.json'
            dest = _pick_save_path(
                default_name,
                [('Manhwa OCR Project', '*.json')],
                title='บันทึกโปรเจกต์ (.json)',
                initial_dir=initial_dir,
            )
        if dest is None:
            return dict(cancelled=True)
        resolved = _write_json_project_file(dest, pages, masks, title)
        save(key, title, pages, masks, json_path=str(resolved))
        return dict(
            cancelled=False,
            path=str(resolved),
            filename=resolved.name,
            note=f'บันทึก JSON แล้ว ({resolved.name})',
        )


@app.post('/api/{key}/txt/import')
def import_txt(key: str, d: dict):
    with lock:
        pages, masks = load(key)
        raw = str(d.get('text', ''))
        changes, report = detect_and_parse_translations(raw, pages)
        if not changes:
            raise ValueError('ไม่พบคำแปลที่จับคู่กับกรอบในโปรเจกต์นี้ — ตรวจรูปแบบรหัสกรอบหรือเลขข้อ')
        frame_tags = report.get('frame_tags') or {}
        laid_out = 0
        layout_failed = 0
        for p in pages:
            im = None
            for b in p.bubbles:
                if b.id not in changes:
                    continue
                if im is None:
                    im = core.load_image(p.path)
                b.translation = changes[b.id]
                if b.id in frame_tags:
                    b.frame_type = frame_tags[b.id]
                    b.frame_type_locked = True
                # Reset stale manual box so newly imported Thai text centers cleanly
                b.text_layer = {}
                b.custom_size = 0
                b.custom_width = 0
                b.text_offset = []
                b.custom_rotation = None
                try:
                    text_update(im, p, b, {'translation': b.translation})
                    laid_out += 1
                except Exception:
                    layout_failed += 1
        save(key, '', pages, masks)
        note = f'นำเข้าคำแปลสำเร็จ {len(changes)}/{report["total_bubbles"]} บอลลูน'
        if layout_failed:
            note += f' (จัดวางอัตโนมัติไม่สำเร็จ {layout_failed})'
        return dict(
            pages=[summary(p) for p in pages],
            report=report,
            applied=len(changes),
            laid_out=laid_out,
            layout_failed=layout_failed,
            note=note,
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


def _unique_path(folder: Path, stem: str, suffix: str) -> Path:
    candidate = folder / f'{stem}{suffix}'
    idx = 1
    while candidate.exists():
        candidate = folder / f'{stem}_{idx}{suffix}'
        idx += 1
    return candidate


@app.post('/api/{key}/export')
def export(key: str, d: dict):
    from photoshop_export import export_psd

    pages, _ = load(key)
    fmt = d.get('format', 'psd')
    if fmt not in ('psd', 'mtp', 'all') or d.get('backend') not in ('photoshop', 'photopea'):
        raise ValueError('ตัวเลือกส่งออกไม่ถูกต้อง')
    job = uuid.uuid4().hex
    dest_dir = str(d.get('destination') or '').strip()
    if dest_dir:
        folder = Path(dest_dir).resolve()
        folder.mkdir(parents=True, exist_ok=True)
    else:
        folder = ROOT / key / 'exports' / job
        folder.mkdir(parents=True)
    stem = Path(project_name(key)).stem or 'project'
    progress = jobs[job] = dict(done=False, value=0, files=[], paths=[], folder=str(folder))

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
                    out = _unique_path(folder, f'{i:02}', '.psd')
                    export_psd(core.load_image(p.path), visible, out, backend=d['backend'])
                    files.append(out)
                progress['value'] = int(i * 80 / len(pages))
            if fmt != 'psd':
                out = _unique_path(folder, stem if dest_dir else 'project', '.mtp')
                core.write_mtp(out, pages, {}, cancel, lambda _: None)
                files.append(out)
            progress['files'] = [f.name for f in files]
            progress['paths'] = [str(f.resolve()) for f in files]
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
            global main_window
            import webview

            main_window = webview.create_window('Manhwa OCR Editor 4.0', url, width=1440, height=900, maximized=True)
            webview.start()
        except Exception:
            main_window = None
            print(url, flush=True)
            webbrowser.open(url)
            thread.join()
        finally:
            main_window = None
            server.should_exit = True
            thread.join(5)


if __name__ == '__main__':
    main()
