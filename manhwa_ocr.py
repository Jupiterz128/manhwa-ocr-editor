#!/usr/bin/env python3
"""Manhwa OCR Editor 3.1 — local OCR, CLEAN, MTP exchange and lossless SmartStitch.
Run: python manhwa_ocr.py. See README_TH.md for installation and model sources.
OCR packages load lazily; opening/editing a project never needs a cloud key.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from contextlib import closing
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from statistics import median
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

import numpy as np
from PIL import Image, ImageOps, ImageEnhance, ImageTk
from bubble_cleaner import (Cleaner, CleanRegion, prepare_region, encode_patch, decode_patch,
                            compose_page, export_cleaned, encode_region, decode_region, lama_model_path)
from frame_geometry import box_quad, frame_quad, valid_quad, quad_bounds, move_line, translation, ocr_crop, psd_stem, outline_read, auto_tilt, quad_angle

APP_VERSION = '3.1'
REVIEW_THRESHOLD = 0.90
DEFAULT_FONT_CHOICE = 'TH Sarabun New Bold'
DATA_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ManhwaOCREditor'
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}
DETECTOR_ID = 'ogkalu/comic-text-and-bubble-detector'
DRAG_DEADZONE_PX = 7.0
SNAP_COLOR = '#ff2ec4'

MTP_TYPE_TO_FRAME = {
    'normal': 'normal',
    'shout': 'shout',
    'scream': 'shout',
    'think': 'thought',
    'thought': 'thought',
    'system': 'system',
    'narr_in': 'narration',
    'narr_out': 'narration',
    'narration': 'narration',
    'small': 'floating',
    'sfx': 'floating',
    'floating': 'floating',
}


def _auto_gpu_default():
    if 'unittest' in sys.modules:
        return False
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def verify_clean_components():
    import importlib
    problems=[]
    for name in ('bubble_cleaner','clean_editor','clean_quality','clean_diagnostics','text_layout','text_editor','windows_text','photoshop_export','app_ui','sound_effects','frame_geometry'):
        module=importlib.import_module(name)
        actual=getattr(module,'COMPONENT_VERSION','เก่า/ไม่ระบุ')
        if actual!=APP_VERSION:problems.append(name+'.py: '+str(actual))
    if problems:raise RuntimeError('ไฟล์โปรแกรมคนละรุ่น ต้องวางไฟล์ทั้งหมดจาก ZIP 3.1 แล้วเปิด START_3_1.bat\n'+'\n'.join(problems))


def uid():
    return uuid.uuid4().hex


def natural_sort_key(name):
    return [int(s) if s.isdigit() else s.lower() for s in re.split(r'(\d+)', str(name))]


def one_line(text):
    return re.sub(r'\s+', ' ', text).strip()


def _is_chinese_char(ch):
    return ('\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'
            or ch in '，。！？；：、“”‘’（）【】《》…—')


def join_lines(lines):
    parts = [one_line(s) for s in lines if s and s.strip()]
    if not parts:
        return ''
    out = parts[0]
    for part in parts[1:]:
        sep = '' if (_is_chinese_char(out[-1]) and _is_chinese_char(part[0])) else ' '
        out += sep + part
    return one_line(out)


def _is_suspect_mismatch(bubble):
    src = re.sub(r'[\s\W_]+', '', getattr(bubble, 'text', '') or '', flags=re.UNICODE)
    trn = re.sub(r'[\s\W_]+', '', getattr(bubble, 'translation', '') or '', flags=re.UNICODE)
    if not src or not trn:
        return False
    cjk_src = sum(1 for c in src if '\u4e00' <= c <= '\u9fff' or '\uac00' <= c <= '\ud7a3')
    if cjk_src > 0:
        if len(src) <= 1 and len(trn) >= 14:
            return True
        if len(src) <= 2 and len(trn) >= 26:
            return True
        if len(src) >= 12 and len(trn) <= 2:
            return True
    else:
        if len(src) <= 2 and len(trn) >= 18:
            return True
        if len(src) >= 22 and len(trn) <= 2:
            return True
    return False


def _snap_rotation_deg(raw_deg, shift_held=False, alt_held=False):
    ang = float(np.clip(raw_deg, -85.0, 85.0))
    if alt_held:
        return round(ang, 1), False
    if shift_held:
        return round(ang / 5.0) * 5.0, True
    for target in (0.0, -15.0, 15.0, -30.0, 30.0, -45.0, 45.0, -60.0, 60.0, -75.0, 75.0):
        if abs(ang - target) <= 2.2:
            return target, True
    return round(ang, 1), False


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uid() + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_image(path):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ('RGBA', 'LA') or 'transparency' in im.info:
            rgba = im.convert('RGBA')
            bg = Image.new('RGBA', rgba.size, 'white')
            im = Image.alpha_composite(bg, rgba)
        return im.convert('RGB')


def area(b):
    return max(0, b[2]-b[0]) * max(0, b[3]-b[1])


def intersection(a, b):
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def iou(a, b):
    n = intersection(a, b)
    return n / max(1, area(a) + area(b) - n)


def union_box(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def clip_box(b, w, h, pad=0):
    return [max(0, int(b[0])-pad), max(0, int(b[1])-pad),
            min(w, int(np.ceil(b[2]))+pad), min(h, int(np.ceil(b[3]))+pad)]


def polygon_box(poly):
    p = np.asarray(poly, dtype=float).reshape(-1, 2)
    return [float(p[:,0].min()), float(p[:,1].min()), float(p[:,0].max()), float(p[:,1].max())]


def _decode_data_url(data_url):
    raw = str(data_url or '').strip()
    if ',' in raw and raw.startswith('data:'):
        raw = raw.split(',', 1)[1]
    return base64.b64decode(raw)


def _encode_data_url(image, fmt='JPEG', quality=92):
    buf = io.BytesIO()
    if fmt.upper() == 'PNG':
        image.save(buf, format='PNG', compress_level=1)
        mime = 'image/png'
    else:
        image.convert('RGB').save(buf, format='JPEG', quality=quality)
        mime = 'image/jpeg'
    return f'data:{mime};base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


def _mtp_page_lookup(mapping, page_name, index):
    if not isinstance(mapping, dict) or not mapping:
        return []
    direct = mapping.get(page_name)
    if isinstance(direct, list) and direct:
        return direct
    stem = Path(page_name).stem
    digits = re.findall(r'\d+', stem)
    target_num = int(digits[-1]) if digits else (index + 1)
    best_empty = direct if isinstance(direct, list) else []
    for k, val in mapping.items():
        if not isinstance(val, list):
            continue
        kd = re.findall(r'\d+', Path(str(k)).stem)
        if kd and int(kd[-1]) == target_num:
            if val:
                return val
            best_empty = val
    return best_empty


def _read_strip_rows_gray(strips, y0, y1, margin_x=0):
    total_w = strips[0][2].width
    h_span = max(1, y1 - y0)
    out = np.full((h_span, total_w), 255, dtype=np.uint8)
    for sy, sh, im in strips:
        ey = sy + sh
        if ey <= y0 or sy >= y1:
            continue
        iy0 = max(y0, sy); iy1 = min(y1, ey)
        crop = im.crop((0, iy0 - sy, total_w, iy1 - sy)).convert('L')
        out[iy0 - y0:iy1 - y0, :] = np.asarray(crop)
    if margin_x > 0 and margin_x * 2 + 16 < total_w:
        return out[:, margin_x:total_w - margin_x]
    return out


def _crop_stitched_slice_rgb(strips, y0, y1):
    total_w = strips[0][2].width
    canvas = Image.new('RGB', (total_w, max(1, y1 - y0)), (255, 255, 255))
    for sy, sh, im in strips:
        ey = sy + sh
        if ey <= y0 or sy >= y1:
            continue
        iy0 = max(y0, sy); iy1 = min(y1, ey)
        part = im.crop((0, iy0 - sy, total_w, iy1 - sy))
        canvas.paste(part, (0, iy0 - y0))
    return canvas


def _find_smart_cuts(strips, total_h, rough_height, detector_type='Smart Pixel Comparison',
                     sensitivity=90, scan_step=5, ignore_margin=0, protected_spans=()):
    hard_psd_cap = 29400
    rough = max(800, min(hard_psd_cap, int(rough_height)))
    step = max(1, min(25, int(scan_step)))
    sens = max(1, min(100, int(sensitivity)))
    max_diff = int(round(255.0 * (1.0 - sens / 100.0)))

    merged_prot = []
    for a, b in sorted((int(np.floor(u)), int(np.ceil(v))) for u, v in protected_spans if 0 < v - u < rough * 0.70):
        if merged_prot and a <= merged_prot[-1][1] + 2:
            merged_prot[-1][1] = max(merged_prot[-1][1], b)
        else:
            merged_prot.append([a, b])

    def in_protected(y_val):
        for a, b in merged_prot:
            if a > y_val:
                return False
            if a <= y_val <= b:
                return True
        return False

    cuts = [0]
    cur = 0
    while total_h - cur > max(rough + 200, int(rough * 1.14)) or (total_h - cur > hard_psd_cap):
        ideal = min(cur + rough, cur + hard_psd_cap - 200)
        if total_h - ideal < 240:
            break
        min_y = max(cur + max(400, int(rough * 0.35)), ideal - int(rough * 0.50))
        max_y = min(total_h - 200, cur + hard_psd_cap, ideal + int(rough * 0.45))
        if max_y <= min_y:
            cuts.append(min(total_h - 64, cur + hard_psd_cap))
            cur = cuts[-1]
            continue

        if detector_type == 'Direct Slicing':
            chosen_y = ideal
            if in_protected(chosen_y):
                for delta in range(step, max(ideal - min_y, max_y - ideal) + step, step):
                    cand_up = ideal - delta
                    if cand_up >= min_y and not in_protected(cand_up):
                        chosen_y = cand_up; break
                    cand_dn = ideal + delta
                    if cand_dn <= max_y and not in_protected(cand_dn):
                        chosen_y = cand_dn; break
            cuts.append(int(chosen_y))
            cur = cuts[-1]
            continue

        band = _read_strip_rows_gray(strips, min_y, max_y + 1, margin_x=max(0, int(ignore_margin)))
        diffs = np.abs(np.diff(band.astype(np.int16), axis=1))
        row_max_step = diffs.max(axis=1) if diffs.shape[1] > 0 else np.zeros(band.shape[0], dtype=np.int16)
        row_ptp = np.ptp(band.astype(np.int16), axis=1)
        row_p95 = np.percentile(diffs, 95, axis=1) if diffs.shape[1] > 0 else np.zeros(band.shape[0])

        offsets = [0]
        max_off = max(ideal - min_y, max_y - ideal)
        for d in range(step, max_off + step, step):
            if ideal - d >= min_y:
                offsets.append(-d)
            if ideal + d <= max_y:
                offsets.append(d)

        chosen_y = None
        best_safe = None
        best_any = None
        ptp_limit = max(16, int(round(max_diff * 1.35)))
        for off in offsets:
            y_cand = ideal + off
            r_idx = y_cand - min_y
            safe_here = not in_protected(y_cand)
            is_flat = (int(row_max_step[r_idx]) <= max_diff and int(row_ptp[r_idx]) <= ptp_limit)
            if safe_here and is_flat:
                chosen_y = y_cand
                break
            cost = float(row_p95[r_idx]) + 0.25 * float(row_ptp[r_idx]) + 0.004 * abs(off)
            if safe_here and (best_safe is None or cost < best_safe[0]):
                best_safe = (cost, y_cand)
            if best_any is None or cost < best_any[0]:
                best_any = (cost, y_cand)

        if chosen_y is None:
            chosen_y = best_safe[1] if best_safe is not None else best_any[1]
        cuts.append(int(chosen_y))
        cur = cuts[-1]

    cuts.append(int(total_h))
    return cuts


@dataclass
class Line:
    text: str
    box: list
    score: float = 0.0
    engine: str = ''
    quad: list = field(default_factory=list)


@dataclass
class Bubble:
    box: list
    text: str = ''
    translation: str = ''
    lines: list = field(default_factory=list)
    score: float = 0.0
    engine: str = ''
    candidates: list = field(default_factory=list)
    reviewed: bool = False
    id: str = field(default_factory=uid)

    text_layer: dict = field(default_factory=dict)
    quad: list = field(default_factory=list)
    font_choice: str = DEFAULT_FONT_CHOICE
    custom_size: int = 0
    custom_width: int = 0
    text_offset: list = field(default_factory=list)
    frame_type: str = ''
    custom_rotation: float | None = None
    detector_class: int = -1
    frame_type_locked: bool = False
    detected_frame_type: str = ''


@dataclass
class Page:
    path: str
    bubbles: list = field(default_factory=list)
    error: str = ''
    processed: bool = False
    id: str = field(default_factory=uid)
    clean_patches: list = field(default_factory=list)


def format_output(pages):
    return '\n\n'.join(
        f'คำต้นฉบับ: {one_line(b.text)}\nคำแปลไทย: {one_line(b.translation)}'
        for p in pages for b in p.bubbles if b.text.strip()
    )


def order_lines(lines):
    if len(lines) >= 2 and any(_is_chinese_char(c) for l in lines for c in l.text):
        vertical = sum((l.box[3]-l.box[1]) > max(1, l.box[2]-l.box[0]) * 1.65 for l in lines)
        if vertical >= len(lines) * .70:
            cols = []
            for line in sorted(lines, key=lambda x: (-(x.box[0]+x.box[2])/2, x.box[1])):
                cx = (line.box[0]+line.box[2])/2
                width = max(1, line.box[2]-line.box[0])
                found = None
                for col in reversed(cols[-3:]):
                    c = col[0]
                    if abs(cx-(c.box[0]+c.box[2])/2) < .45*min(width, max(1, c.box[2]-c.box[0])):
                        found = col
                        break
                if found is None:
                    cols.append([line])
                else:
                    found.append(line)
            return [line for col in cols for line in sorted(col, key=lambda x: x.box[1])]
    rows = []
    for line in sorted(lines, key=lambda x: (x.box[1], x.box[0])):
        cy = (line.box[1]+line.box[3])/2
        height = max(1, line.box[3]-line.box[1])
        found = None
        for row in reversed(rows[-3:]):
            r = row[0]
            if abs(cy-(r.box[1]+r.box[3])/2) < .45*min(height, r.box[3]-r.box[1]):
                found = row
                break
        if found is None:
            rows.append([line])
        else:
            found.append(line)
    return [line for row in rows for line in sorted(row, key=lambda x: x.box[0])]


def dedupe_lines(lines):
    kept = []
    for line in sorted(lines, key=lambda x: x.score, reverse=True):
        if not line.text.strip() or area(line.box) <= 0:
            continue
        if any(iou(line.box, other.box) > .60 or
               (one_line(line.text).casefold() == one_line(other.text).casefold()
                and intersection(line.box, other.box)/max(1, min(area(line.box), area(other.box))) > .75)
               for other in kept):
            continue
        kept.append(line)
    return order_lines(kept)


def _container_region_ids(regions, lines=()):
    containers = set()
    clean_lines = dedupe_lines(lines) if lines else []
    for i, outer in enumerate(regions):
        outer_area = area(outer)
        if outer_area <= 0:
            continue
        children = [j for j, r in enumerate(regions)
                    if i != j and 0 < area(r) < outer_area * .75
                    and intersection(r, outer) / max(1, area(r)) >= .85]
        if not children:
            continue
        distinct = []
        for j in sorted(children, key=lambda k: area(regions[k]), reverse=True):
            if not any(iou(regions[j], regions[d]) >= .35 for d in distinct):
                distinct.append(j)
        if len(distinct) >= 2:
            containers.add(i)
            continue
        if clean_lines:
            child = regions[distinct[0]]
            in_child = any(intersection(l.box, child) / max(1, area(l.box)) > .8 for l in clean_lines)
            outside_child = any(intersection(l.box, outer) / max(1, area(l.box)) > .8
                                and intersection(l.box, child) / max(1, area(l.box)) <= .2
                                for l in clean_lines)
            if in_child and outside_child:
                containers.add(i)
    return containers


def group_lines(lines, regions=(), page_size=None, detector_labels=None):
    containers = _container_region_ids(regions, lines) if regions else set()
    groups = []
    for line in order_lines(dedupe_lines(lines)):
        matches = [(area(r), i) for i, r in enumerate(regions)
                   if i not in containers and intersection(line.box, r)/max(1, area(line.box)) > .8]
        region = min(matches)[1] if matches else None
        target = None
        for group in groups:
            if region is not None and group['region'] == region:
                target = group
                break
            if region is not None or group['region'] is not None:
                continue
            previous=group['lines'][-1]
            last = previous.box
            b = line.box
            heights = [x.box[3]-x.box[1] for x in group['lines']] + [b[3]-b[1]]
            angle=quad_angle(line.quad);other=quad_angle(previous.quad)
            if abs(angle-other)>10:continue
            if abs(angle)>=3 and valid_quad(previous.quad):
                r=np.radians((angle+other)/2)
                basis=np.array([[np.cos(r),-np.sin(r)],[np.sin(r),np.cos(r)]])
                last=quad_bounds(np.asarray(previous.quad)@basis)
                b=quad_bounds(np.asarray(line.quad)@basis)
                heights=[last[3]-last[1],b[3]-b[1]]
                if b[1]<last[1]:last,b=b,last
            h = max(1, median(heights))
            overlap = max(0, min(last[2], b[2])-max(last[0], b[0]))
            width = max(1, min(last[2]-last[0], b[2]-b[0]))
            gap = b[1]-last[3]
            same_row = abs((b[1]+b[3]-last[1]-last[3])/2) < .4*h
            hgap = b[0]-last[2]
            stacked = -.25*h <= gap <= .95*h and overlap/width >= .40
            adjacent = same_row and 0 <= hgap <= .8*h
            if stacked or adjacent:
                target = group
                break
        if target is None:
            target = {'region': region, 'lines': []}
            groups.append(target)
        target['lines'].append(line)
    bubbles = []
    for group in groups:
        ls = order_lines(group['lines'])
        box = list(regions[group['region']]) if group['region'] is not None else union_box([l.box for l in ls])
        bubble=auto_tilt(Bubble(box=box, text=join_lines([l.text for l in ls]),
                              lines=[asdict(l) for l in ls], score=min(l.score for l in ls),
                              engine='+'.join(sorted(set(l.engine for l in ls)))),page_size)
        if detector_labels:
            source_label=detector_labels.get(tuple(box),-1)
            if source_label==-1:
                source_label=next((2 for rect,label in detector_labels.items() if label==2 and
                                   intersection(rect,box)/max(1,min(area(rect),area(box)))>.8),-1)
            bubble.detector_class=source_label
        order_tilted_bubble(bubble)
        bubbles.append(bubble)
    return sorted(bubbles, key=lambda b: (b.box[1], b.box[0]))


def order_tilted_bubble(bubble):
    if not bubble.quad:return
    r=np.radians(quad_angle(bubble.quad))
    basis=np.array([[np.cos(r),-np.sin(r)],[np.sin(r),np.cos(r)]])
    local=[]
    for record in bubble.lines:
        item=Line(**record)
        item.box=quad_bounds(np.asarray(item.quad or box_quad(item.box))@basis)
        local.append(item)
    lookup={id(item):record for item,record in zip(local,bubble.lines)}
    bubble.lines=[lookup[id(item)] for item in order_lines(local)]
    bubble.text=join_lines([record['text'] for record in bubble.lines])


def tiles(image, height=1800, overlap=260):
    y = 0
    while y < image.height:
        bottom = min(image.height, y+height)
        yield y, image.crop((0, y, image.width, bottom))
        if bottom == image.height:
            break
        y = bottom-overlap


def deglow_crop(image):
    import cv2
    arr = np.asarray(image.convert('RGB'))
    if min(arr.shape[:2]) < 8:
        return None
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    if float(np.median(gray)) >= 165.0:
        return None
    achrom = np.min(arr, axis=2)
    p35 = float(np.percentile(achrom, 35))
    p98 = float(np.percentile(achrom, 98))
    if p98 < 130.0 or (p98 - p35) < 40.0:
        return None
    h, w = achrom.shape
    scale = 2 if max(h, w) < 1000 and h < 240 else 1
    work = cv2.resize(arr, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC) if scale > 1 else arr
    w_min = np.min(work, axis=2).astype(np.float32)
    kw = max(25, min(65, (max(work.shape[:2]) // 4) * 2 + 1))
    bg = cv2.morphologyEx(w_min, cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kw, kw)))
    bg = cv2.GaussianBlur(bg, (0, 0), max(2.0, kw * .18))
    lift = np.clip(w_min - bg - 10.0, 0.0, 255.0)
    hi = float(np.percentile(lift, 99))
    if hi < 22.0:
        return None
    norm = np.clip(lift * (255.0 / hi), 0.0, 255.0)
    sharp = np.clip(((norm / 255.0) ** 1.18) * 255.0, 0.0, 255.0).astype(np.uint8)
    inverted = 255 - sharp
    return Image.fromarray(np.stack([inverted, inverted, inverted], axis=2)), scale


def _is_system_text_region(tile, box):
    import cv2
    b = clip_box(box, tile.width, tile.height)
    bw, bh = b[2] - b[0], b[3] - b[1]
    if bw < 26 or bh < 12 or bw / max(1, bh) < 0.50:
        return False
    arr = np.asarray(tile.crop(b).convert('RGB'))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    if float(np.percentile(gray, 95) - np.percentile(gray, 5)) < 35.0:
        return False
    if float(np.median(gray)) >= 165.0:
        return False
    achrom = np.min(arr, axis=2)
    max_ch = np.max(arr, axis=2)
    sat = max_ch.astype(np.int16) - achrom.astype(np.int16)
    kw = max(15, min(45, (max(bh, 30) // 2) * 2 + 1))
    bg = cv2.morphologyEx(achrom, cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kw, kw)))
    lift = achrom.astype(np.int16) - bg.astype(np.int16)
    cores = ((lift >= 35) & (achrom >= 120) & (sat <= np.maximum(80, (max_ch * .55)))).astype(np.uint8) * 255
    core_px = int(np.count_nonzero(cores))
    if core_px < 16 or float(np.percentile(achrom, 95) - np.percentile(achrom, 35)) < 40.0:
        return False
    if min(bh, bw) >= 28:
        thick = cv2.erode(cores, np.ones((19, 19), np.uint8))
        if np.count_nonzero(thick) > core_px * .22:
            return False
    n, _, stats, _ = cv2.connectedComponentsWithStats(cores)
    glyphs = [stats[i] for i in range(1, n)
              if stats[i, cv2.CC_STAT_AREA] >= 5
              and 4 <= stats[i, cv2.CC_STAT_HEIGHT] <= min(80, max(20, round(bh * .95)))
              and 1 <= stats[i, cv2.CC_STAT_WIDTH] <= max(45, round(stats[i, cv2.CC_STAT_HEIGHT] * 3.2))]
    return len(glyphs) >= 2


def _valid_extra_ocr(lines):
    if not lines:
        return False
    text = ''.join(str(l.text) for l in lines)
    alnum = sum(c.isalnum() for c in text)
    alpha = sum(c.isalpha() for c in text)
    top_score = max((float(l.score) for l in lines), default=0.0)
    return alnum >= 2 and (alpha >= 2 or alnum >= 4) and top_score >= 0.30


class OCRCache:
    def __init__(self, path=None):
        self.path = Path(path or DATA_DIR / 'ocr_cache.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    @staticmethod
    def key(image, signature):
        h = hashlib.sha256(signature.encode())
        h.update(str(image.size).encode())
        h.update(image.tobytes())
        return h.hexdigest()

    def get(self, key):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute('SELECT value FROM cache WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR REPLACE INTO cache VALUES (?, ?)', (key, json.dumps(value, ensure_ascii=False)))


def package_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return 'missing'


class EasyEngine:
    name = 'EasyOCR'

    def __init__(self, lang='ko+en', gpu=False):
        import torch
        use_gpu = bool(gpu and torch.cuda.is_available())
        if not use_gpu:
            torch.set_num_threads(max(1, min(6, os.cpu_count() or 1)))
        import easyocr
        self.fast = True
        if lang == 'en':
            langs = ['en']
        elif lang == 'zh+en':
            langs = ['ch_sim', 'en']
        elif lang == 'zh_tw+en':
            langs = ['ch_tra', 'en']
        else:
            langs = ['ko', 'en']
        try:
            self.reader = easyocr.Reader(langs, gpu=use_gpu, verbose=False)
        except Exception:
            if not use_gpu:
                raise
            use_gpu = False
            self.reader = easyocr.Reader(langs, gpu=False, verbose=False)
        self.signature = f'easy:{package_version("easyocr")}:{langs}:{use_gpu}:v3'

    def read(self, image):
        return self._read(image, self.fast)

    def read_detailed(self, image):
        return self._read(image, False)

    def _read(self, image, fast):
        result = self.reader.readtext(np.asarray(image), detail=1, paragraph=False,
                    decoder='greedy', beamWidth=5,
                    rotation_info=None, workers=0,
                    text_threshold=.55, low_text=.3, link_threshold=.35,
                    contrast_ths=.1 if fast else .2, adjust_contrast=.7,
                    canvas_size=2560 if fast else 1600, mag_ratio=1.0 if fast else 1.2)
        return [Line(str(text), polygon_box(box), float(score), self.name, np.asarray(box).tolist())
                for box, text, score in result if str(text).strip()]


class PaddleEngine:
    name = 'PaddleOCR'

    def __init__(self, lang='ko+en', gpu=False):
        from paddleocr import PaddleOCR
        major = int(package_version('paddleocr').split('.')[0])
        self.v3 = major >= 3
        if lang == 'en':
            language = 'en'
            rec_name = 'en_PP-OCRv5_mobile_rec'
        elif lang in ('zh+en', 'zh_tw+en'):
            language = 'ch' if lang == 'zh+en' else 'chinese_cht'
            rec_name = 'PP-OCRv5_mobile_rec'
        else:
            language = 'korean'
            rec_name = 'korean_PP-OCRv5_mobile_rec'
        use_gpu = bool(gpu)
        if self.v3:
            model_root = Path(os.environ.get('MANHWA_PADDLE_MODEL_DIR', str(DATA_DIR/'paddle_models')))
            local_models = {}
            for option, name in [('text_detection_model_dir', 'PP-OCRv5_mobile_det'),
                                 ('text_recognition_model_dir', rec_name)]:
                folder = model_root/name
                if (folder/'inference.pdiparams').is_file() and (folder/'inference.yml').is_file():
                    local_models[option] = str(folder)
            try:
                self.reader = PaddleOCR(
                    text_detection_model_name='PP-OCRv5_mobile_det',
                    text_recognition_model_name=rec_name,
                    **local_models,
                    use_doc_orientation_classify=False, use_doc_unwarping=False,
                    use_textline_orientation=False, device='gpu:0' if use_gpu else 'cpu',
                    enable_mkldnn=False)
            except Exception:
                if not use_gpu:
                    raise
                use_gpu = False
                self.reader = PaddleOCR(
                    text_detection_model_name='PP-OCRv5_mobile_det',
                    text_recognition_model_name=rec_name,
                    **local_models,
                    use_doc_orientation_classify=False, use_doc_unwarping=False,
                    use_textline_orientation=False, device='cpu',
                    enable_mkldnn=False)
        else:
            self.reader = PaddleOCR(lang=language, use_angle_cls=True,
                                    use_gpu=use_gpu, show_log=False)
        self.signature = f'paddle:{package_version("paddleocr")}:{language}:{use_gpu}:v3-upright'

    def read(self, image):
        arr = np.asarray(image)[:, :, ::-1].copy()
        lines = []
        if self.v3:
            for result in self.reader.predict(arr):
                data = result.json
                if callable(data):
                    data = data()
                if isinstance(data, str):
                    data = json.loads(data)
                data = data.get('res', data)
                for box, text, score in zip(data.get('rec_polys', []),
                        data.get('rec_texts', []), data.get('rec_scores', [])):
                    if str(text).strip():
                        lines.append(Line(str(text), polygon_box(box), float(score), self.name, np.asarray(box).tolist()))
        else:
            for page in self.reader.ocr(arr, cls=True) or []:
                for box, (text, score) in page or []:
                    if str(text).strip():
                        lines.append(Line(str(text), polygon_box(box), float(score), self.name, np.asarray(box).tolist()))
        return lines


class RapidEngine:
    name = 'RapidOCR (PP-OCRv5)'

    def __init__(self, lang='ko+en', gpu=False):
        try:
            from rapidocr import RapidOCR, EngineType, LangDet, LangRec, ModelType, OCRVersion
            import onnxruntime
        except ImportError as exc:
            raise RuntimeError('ยังไม่ได้ติดตั้ง RapidOCR ให้รัน install_rapid_windows.bat ก่อน') from exc
        if lang == 'en':
            language = LangRec.EN
        elif lang in ('zh+en', 'zh_tw+en'):
            language = getattr(LangRec, 'CH', 'ch')
        else:
            language = LangRec.KOREAN
        model_root = DATA_DIR / 'rapid_models'
        model_root.mkdir(parents=True, exist_ok=True)
        self.reader = RapidOCR(params={
            'Global.model_root_dir': str(model_root),
            'Global.use_cls': False,
            'Global.log_level': 'warning',
            'EngineConfig.onnxruntime.intra_op_num_threads': max(2, min(8, os.cpu_count() or 4)),
            'EngineConfig.onnxruntime.inter_op_num_threads': 1,
            'EngineConfig.onnxruntime.use_cuda': False,
            'Det.engine_type': EngineType.ONNXRUNTIME,
            'Det.lang_type': LangDet.CH,
            'Det.model_type': ModelType.MOBILE,
            'Det.ocr_version': OCRVersion.PPOCRV5,
            'Det.limit_type': 'max',
            'Det.limit_side_len': 1280,
            'Rec.engine_type': EngineType.ONNXRUNTIME,
            'Rec.lang_type': language,
            'Rec.model_type': ModelType.MOBILE,
            'Rec.ocr_version': OCRVersion.PPOCRV5,
            'Cls.engine_type': EngineType.ONNXRUNTIME,
            'Cls.lang_type': LangDet.CH,
            'Cls.model_type': ModelType.MOBILE,
            'Cls.ocr_version': OCRVersion.PPOCRV4,
        })
        self.signature = f'rapid:{package_version("rapidocr")}:{package_version("onnxruntime")}:{lang}:ppv5-mobile:max1280:no-cls:v2'

    def read(self, image):
        result = self.reader(image.convert('RGB'), use_det=True, use_cls=False, use_rec=True)
        if result is None or result.boxes is None or result.txts is None or result.scores is None:
            return []
        return [Line(str(text), polygon_box(box), float(score), self.name, np.asarray(box).tolist())
                for box, text, score in zip(result.boxes, result.txts, result.scores)
                if str(text).strip()]


class ComicDetector:
    def __init__(self, gpu=False):
        import torch
        if not gpu:
            torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        from transformers import RTDetrImageProcessor, RTDetrV2ForObjectDetection
        self.torch = torch
        self.device = 'cuda' if gpu and torch.cuda.is_available() else 'cpu'
        self.processor = RTDetrImageProcessor.from_pretrained(DETECTOR_ID)
        self.model = RTDetrV2ForObjectDetection.from_pretrained(
            DETECTOR_ID, use_safetensors=True, trust_remote_code=False).to(self.device).eval()

    def detect(self, image):
        inputs = self.processor(images=image, return_tensors='pt').to(self.device)
        with self.torch.inference_mode():
            output = self.model(**inputs)
        result = self.processor.post_process_object_detection(output, threshold=.45,
            target_sizes=self.torch.tensor([[image.height, image.width]], device=self.device))[0]
        records = []
        for score, label, box in zip(result['scores'], result['labels'], result['boxes']):
            b = clip_box(box.tolist(), image.width, image.height)
            if area(b) > 25:
                records.append((int(label), float(score), b))
        return records


def contour_regions(image):
    import cv2
    gray = np.asarray(image.convert('L'))
    mask = cv2.threshold(gray, 215, 255, cv2.THRESH_BINARY)[1]
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3,3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if x <= 1 or y <= 1 or x+w >= image.width-1 or y+h >= image.height-1:
            continue
        if w < 35 or h < 30 or w*h > image.width*image.height*.22:
            continue
        if .2 < w/h < 7 and cv2.contourArea(contour)/(w*h) > .55:
            out.append([x,y,x+w,y+h])
    return out


class Cancelled(Exception):
    pass


def _pixel_sample(image,box=None):
    return np.asarray(image.resize((32,32),Image.Resampling.NEAREST,box=box).convert('L'))


def _merge_tile_regions(tile,det,typed):
    from text_layout import _MODEL_TYPES
    speech=[box for label,_,box in det if label==0]
    candidates=[]
    sfx=[]
    for name,score,box in sorted(typed,key=lambda row:-row[1]):
        if area(box)<=0:continue
        if name=='SFX':
            if score>=.45:sfx.append(box)
            continue
        extra=name in ('OT','ST')
        threshold=.48 if extra else (.35 if name=='System Panel' else .38)
        if name not in _MODEL_TYPES or score<threshold:continue
        if extra and np.ptp(_pixel_sample(tile,tuple(box)))<24:continue
        candidates.append((box,_MODEL_TYPES[name],extra))
    protected=speech+[box for box,kind,extra in candidates if not extra]
    rows=[];blocked=[]
    for label,score,box in det:
        if label==2 and any(iou(box,r)>=.55 for r in sfx) and not any(intersection(box,r)>0 for r in protected):
            blocked.append(box)
            continue
        matches=[(iou(box,r),kind,extra) for r,kind,extra in candidates if iou(box,r)>=.50]
        _,kind,extra=max(matches,default=(0,'',False))
        rows.append((label,score,box,kind,extra))
    for box,kind,extra in candidates:
        if all(iou(box,row[2])<.50 for row in rows):
            rows.append((-1,1.,box,kind,extra))
    return rows,blocked


class LocalPipeline:
    def __init__(self, cache, mode='auto', lang='ko+en', gpu=False,
                 neural=True, retry=False, log=lambda s: None, cancel=None,
                 fast=True, bubble_only=True, type_scan=False, prefer_rapid=False):
        self.type_scan=type_scan
        self.cache, self.log = cache, log
        self.cancel = cancel or threading.Event()
        self.mode, self.lang, self.gpu = mode, lang, gpu
        self.retry = retry
        self.fast, self.bubble_only = fast, bubble_only
        self.on_progress = lambda fraction: None
        self.on_partial = lambda bubbles: None
        self.engines = []
        self.detector = None
        self.hits = 0
        choices = [('paddle', PaddleEngine), ('easy', EasyEngine)]
        if lang == 'ko+en':
            choices.reverse()
        if mode == 'auto' and (prefer_rapid or 'unittest' not in sys.modules):
            choices = [('rapid', RapidEngine)] + choices
        elif mode == 'rapid':
            choices = [('rapid', RapidEngine)]
        self.pending = [(key, factory) for key, factory in choices if mode == 'auto' or mode == key]
        failures = []
        while self.pending and not self.engines:
            key, factory = self.pending.pop(0)
            self.check()
            try:
                self.log(f'กำลังโหลด {factory.name} (ครั้งแรกอาจดาวน์โหลดโมเดล)…')
                engine = factory(lang, gpu)
                engine.fast = fast
                self.engines.append(engine)
            except Exception as exc:
                failures.append(f'{factory.name}: {type(exc).__name__}: {exc}')
        for reason in failures:
            self.log(reason)
        if not self.engines:
            raise RuntimeError('ไม่พบ OCR ในเครื่องที่พร้อมใช้ ให้ติดตั้ง requirements.txt\n'+'\n'.join(failures))
        if neural:
            try:
                self.log('กำลังโหลดโมเดลกรอบการ์ตูน RT-DETR-v2…')
                self.detector = ComicDetector(gpu)
            except Exception as exc:
                self.log(f'โมเดลกรอบการ์ตูนไม่พร้อม: {type(exc).__name__}: {exc}\nใช้กรอบพื้นขาว/ระยะบรรทัดแทน')
        self.log('OCR ในเครื่อง: ' + ', '.join(e.name for e in self.engines))

    def iter_engines(self):
        yield from self.engines
        while self.pending:
            self.check()
            key, factory = self.pending.pop(0)
            try:
                self.log(f'กำลังโหลดโมเดลสำรอง {factory.name}…')
                engine = factory(self.lang, self.gpu)
                engine.fast = self.fast
                self.engines.append(engine)
            except Exception as exc:
                self.log(f'{factory.name} ไม่พร้อม: {exc}')
                continue
            yield engine

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def read(self, engine, image, variant='original'):
        self.check()
        detailed = variant in ('original-crop', 'contrast-2x', 'clean-detail-v2', 'deglow-crop-v2', 'clean-deglow-v2')
        key = self.cache.key(image, engine.signature+':'+variant+f':fast={self.fast}:detail={detailed}:quad-v1')
        cached = self.cache.get(key)
        if cached is not None:
            self.hits += 1
            return [Line(**l) for l in cached]
        result = engine.read_detailed(image) if detailed and hasattr(engine, 'read_detailed') else engine.read(image)
        self.cache.put(key, [asdict(l) for l in result])
        return result

    def _tile_regions(self,tile):
        from concurrent.futures import ThreadPoolExecutor
        from text_layout import _BUBBLE_TYPE_DETECTOR as detector
        signature=detector.signature() if getattr(self,'type_scan',False) else None
        key=self.cache.key(tile,'bubble-regions:v4:merged:'+DETECTOR_ID+str(signature))
        hit=self.cache.get(key)
        if hit is not None:
            self.hits+=1
            return hit
        if signature:
            with ThreadPoolExecutor(max_workers=2) as pool:
                primary=pool.submit(self.detector.detect,tile)
                secondary=pool.submit(detector.detect_tile,tile,self.gpu)
                det=primary.result()
                try:typed=secondary.result()
                except Exception as exc:
                    self.log(f'ใช้ผล RT-DETR สำหรับช่วงนี้: {exc}')
                    return _merge_tile_regions(tile,det,[])
        else:
            det=self.detector.detect(tile)
            typed=[]
        result=_merge_tile_regions(tile,det,typed)
        self.cache.put(key,result)
        return result

    def scan(self, image):
        if self.bubble_only:
            return self.scan_bubbles(image)
        lines, regions, text_regions = [], [], []
        detector_labels={}
        typed_regions={}
        primary = self.engines[0]
        total = max(1, 1 + (max(0, image.height-1800)+1539)//1540)
        for tile_index, (y, tile) in enumerate(tiles(image)):
            self.check()
            self.log(f'อ่านช่วง {tile_index+1}/{total} y={y}–{y+tile.height} ด้วย {primary.name}')
            found,blocked = [],[]
            if self.detector:
                self.check()
                self.log(f'ตรวจกรอบช่วง {tile_index+1}/{total}')
                try:
                    found,blocked = self._tile_regions(tile)
                except Cancelled:
                    raise
                except Exception as exc:
                    self.log(f'ตรวจกรอบด้วยโมเดลไม่สำเร็จ ใช้กรอบพื้นขาวแทน: {exc}')
            read_tile=tile
            if blocked:
                read_tile=tile.copy()
                for rect in blocked:
                    read_tile.paste('white',tuple(rect))
            tile_lines = None
            failures = []
            for engine in self.iter_engines():
                try:
                    tile_lines = self.read(engine, read_tile)
                    break
                except Cancelled:
                    raise
                except Exception as exc:
                    failures.append(f'{engine.name}: {exc}')
                    self.log(f'{engine.name} อ่านช่วงนี้ไม่สำเร็จ ลองโมเดลในเครื่องถัดไป: {exc}')
            if tile_lines is None:
                raise RuntimeError('OCR ในเครื่องทุกตัวอ่านช่วงนี้ไม่สำเร็จ: ' + '; '.join(failures))
            if not tile_lines and self.retry:
                rescue = self.engines[-1]
                adjusted = ImageOps.autocontrast(read_tile.convert('L')).convert('RGB')
                try:
                    tile_lines = self.read(rescue, adjusted, 'empty-rescue')
                except Cancelled:
                    raise
                except Exception as exc:
                    self.log(f'อ่านช่วงว่างซ้ำไม่สำเร็จ: {exc}')
            for l in tile_lines:
                move_line(l,translation(y=y))
            lines.extend(tile_lines)
            if found:
                containers=_container_region_ids([row[2] for row in found]) if any(row[3]=='system' for row in found) else set()
                for idx,(label, score, b, kind, extra) in enumerate(found):
                    if kind=='system' and idx in containers:continue
                    local_b = list(b)
                    b = [b[0], b[1]+y, b[2], b[3]+y]
                    detector_labels[tuple(b)]=int(label)
                    if kind:
                        relevant=[l for l in tile_lines if intersection(l.box,b)/max(1,area(l.box))>.65]
                        if label==-1 and not relevant:
                            recovered=self.read(engine,tile.crop(local_b),'bubble-fast' if self.fast else 'bubble-full')
                            for line in recovered:
                                move_line(line,translation(x=b[0],y=b[1]))
                            relevant=recovered
                            lines.extend(recovered)
                        if extra and not _valid_extra_ocr(relevant):
                            rejected={id(line) for line in relevant}
                            lines=[line for line in lines if id(line) not in rejected]
                            continue
                        typed_regions[tuple(b)]=kind
                    if label == 0 or kind:
                        regions.append(b)
                        if label==-1 and kind and not extra:text_regions.append(b)
                    elif label in (1, 2):
                        text_regions.append(b)
                        if _is_system_text_region(tile, local_b) and np.median(_pixel_sample(tile,tuple(local_b)))<160:
                            deglow = deglow_crop(tile.crop(local_b))
                            if deglow is not None:
                                dg_img, dg_scale = deglow
                                try:
                                    dg_ls = self.read(primary, dg_img, 'tile-deglow-v2')
                                    if _valid_extra_ocr(dg_ls):
                                        for dl in dg_ls:
                                            move_line(dl, translation(x=local_b[0], y=local_b[1]+y, sx=1.0/dg_scale, sy=1.0/dg_scale))
                                            lines.append(dl)
                                except Cancelled:
                                    raise
                                except Exception:
                                    pass
            else:
                for b in contour_regions(tile):
                    regions.append([b[0], b[1]+y, b[2], b[3]+y])
            self.on_partial(group_lines(copy.deepcopy(lines), regions,image.size,detector_labels))
            self.on_progress((tile_index+1)/total*(.8 if self.retry else .99))
        clean_regions = []
        for b in sorted(regions, key=area, reverse=True):
            if not any(iou(b, r) > .55 for r in clean_regions):
                clean_regions.append(b)
        bubbles = group_lines(lines, clean_regions,image.size,detector_labels)
        for b in sorted(text_regions, key=area):
            if not any(intersection(b, v.box)/max(1, min(area(b), area(v.box))) > .5 for v in bubbles):
                bubbles.append(Bubble(box=b, engine='detected / unread',detector_class=detector_labels.get(tuple(b),-1)))
        for b in bubbles:
            b.detected_frame_type=typed_regions.get(tuple(b.box),'')
            if b.detected_frame_type:b.frame_type=b.detected_frame_type
        if self.retry:
            for i, b in enumerate(bubbles):
                self.check()
                self.log(f'ตรวจซ้ำกรอบ {i+1}/{len(bubbles)}')
                if b.score < REVIEW_THRESHOLD or not b.text:
                    b.candidates = self.candidates(image, b)
                    if not b.text and b.candidates:
                        c = b.candidates[0]
                        b.text, b.score, b.engine = c['text'], c['score'], c['engine']
                self.on_progress(.8+.19*(i+1)/max(1,len(bubbles)))
        return sorted(bubbles, key=lambda b: (b.box[1], b.box[0]))

    def scan_bubbles(self, image):
        total = max(1, 1 + (max(0, image.height-1800)+1539)//1540)
        raw_items = []
        region_labels={}
        for i, (y, tile) in enumerate(tiles(image)):
            self.check()
            self.log(f'หาบับเบิ้ล/กรอบระบบ ช่วง {i+1}/{total} (ยังไม่อ่านตัวอักษร)')
            from text_layout import _BUBBLE_TYPE_DETECTOR
            enriched=self.detector and getattr(self,'type_scan',False) and _BUBBLE_TYPE_DETECTOR.signature()
            legacy_key=self.cache.key(tile,'bubble-regions:v4:'+DETECTOR_ID) if self.detector and not enriched else None
            previous=self.cache.get(legacy_key) if legacy_key else None
            if previous is not None:
                self.hits+=1
                found=[(item[0],item[1],item[2] if len(item)>2 else -1,'') for item in previous]
            elif self.detector:
                try:
                    if enriched:
                        rows,_=self._tile_regions(tile)
                    else:
                        rows=[(label,score,box,'',False) for label,score,box in self.detector.detect(tile)]
                    speech=[box for label,_,box,_,_ in rows if label==0]
                    found=[]
                    for label,score,box,kind,extra in rows:
                        if label==0 or kind:
                            found.append((box,extra,label,kind))
                        elif label in (1,2) and score>=.50 and not any(intersection(box,r)/max(1,area(box))>.50 for r in speech):
                            if _is_system_text_region(tile,box):
                                found.append((clip_box(box,tile.width,tile.height,pad=4),True,label,''))
                    if legacy_key:
                        self.cache.put(legacy_key,[[box,extra,label] for box,extra,label,_ in found])
                except Cancelled:
                    raise
                except Exception as exc:
                    self.log(f'โมเดลกรอบไม่พร้อม ใช้กรอบพื้นขาว: {exc}')
                    found=[(b,False,-1,'') for b in contour_regions(tile)]
            else:
                key=self.cache.key(tile,'bubble-regions:v4:contour')
                boxes=self.cache.get(key)
                if boxes is None:
                    boxes=[[b,False] for b in contour_regions(tile)]
                    self.cache.put(key,boxes)
                else:self.hits+=1
                found=[(b,False,-1,'') for b,_ in boxes]
            for b,is_extra,label,kind in found:
                box=[b[0],b[1]+y,b[2],b[3]+y]
                region_labels[tuple(box)]=(label,kind)
                raw_items.append((box,is_extra))
            self.on_progress(.25*(i+1)/total)
        deduped = []
        for b, is_extra in sorted(raw_items, key=lambda x: (not x[1], area(x[0])), reverse=True):
            if not any(iou(b, r) > .55 for r, _ in deduped):
                deduped.append((b, is_extra))
        containers = _container_region_ids([b for b, _ in deduped])
        filtered = [(b, is_extra) for i, (b, is_extra) in enumerate(deduped) if i not in containers]
        unique = []
        for b, is_extra in sorted(filtered, key=lambda x: area(x[0]), reverse=True):
            if not any(intersection(b, r)/max(1, min(area(b), area(r))) > .65 for r, _ in unique):
                unique.append((b, is_extra))
        unique.sort(key=lambda x: (x[0][1], x[0][0]))
        if not unique:
            self.log('ไม่พบบับเบิ้ล — ลากเพิ่มกรอบเอง หรือปิด “เฉพาะบับเบิ้ล” เพื่ออ่านทั้งหน้า')
            return []
        bubbles = []
        for i, (region, is_extra) in enumerate(unique):
            self.check()
            self.log(f'อ่านบับเบิ้ล {i+1}/{len(unique)}')
            box = clip_box(region, image.width, image.height)
            label,kind=region_labels[tuple(region)]
            crop = image.crop(box)
            ls, errors = None, []
            for engine in self.iter_engines():
                try:
                    ls = []
                    for offset, part in tiles(crop):
                        part_lines = self.read(engine, part, 'bubble-fast' if self.fast else 'bubble-full')
                        deglow = deglow_crop(part) if kind in ('system','terrible') or np.median(_pixel_sample(part))<160 else None
                        if deglow is not None:
                            dg_img, dg_scale = deglow
                            dg_lines = self.read(engine, dg_img, 'bubble-deglow-v2')
                            for dl in dg_lines:
                                move_line(dl, translation(sx=1.0/dg_scale, sy=1.0/dg_scale))
                            part_lines = dedupe_lines(part_lines + dg_lines)
                        for line in part_lines:
                            move_line(line,translation(y=offset))
                            ls.append(line)
                    break
                except Cancelled:
                    raise
                except Exception as exc:
                    ls = None
                    errors.append(str(exc))
                    self.log(f'{engine.name} อ่านกรอบไม่สำเร็จ: {exc}')
            if ls is None:
                raise RuntimeError('OCR ในเครื่องอ่านกรอบไม่สำเร็จ: '+'; '.join(errors))
            ls = order_lines(dedupe_lines(ls))
            if is_extra and not _valid_extra_ocr(ls):
                self.on_progress(.25+.74*(i+1)/len(unique))
                continue
            for line in ls:
                move_line(line,translation(box[0],box[1]))
            bubble = Bubble(box=box, detector_class=label, frame_type=kind, detected_frame_type=kind, text=join_lines([l.text for l in ls]),
                            score=min((l.score for l in ls),default=0),
                            engine=engine.name if ls else 'detected / unread',
                            lines=[asdict(l) for l in ls])
            auto_tilt(bubble,image.size)
            order_tilted_bubble(bubble)
            bubbles.append(bubble)
            self.on_partial(copy.deepcopy(bubbles))
            if self.retry and (bubble.score < REVIEW_THRESHOLD or not bubble.text):
                self.log(f'ตรวจซ้ำบับเบิ้ล {i+1}/{len(unique)}')
                bubble.candidates = self.candidates(image, bubble)
                if not bubble.text and bubble.candidates:
                    c = bubble.candidates[0]
                    b.text, bubble.score, bubble.engine = c['text'],c['score'],c['engine']
            self.on_progress(.25+.74*(i+1)/len(unique))
        return bubbles

    def candidates(self, image, bubble):
        crop,_ = ocr_crop(image,bubble,pad=10)
        if min(crop.size) < 2:
            return []
        factor = min(2.0, 2200/max(crop.size))
        scaled = crop.resize((max(1, round(crop.width*factor)), max(1, round(crop.height*factor))), Image.Resampling.LANCZOS)
        variants = [('original-crop', crop), ('contrast-2x',
            ImageEnhance.Contrast(ImageOps.autocontrast(scaled.convert('L'))).enhance(1.35).convert('RGB'))]
        deglow = deglow_crop(crop)
        if deglow is not None:
            variants.append(('deglow-crop-v2', deglow[0]))
        candidates, seen = [], {one_line(bubble.text)}
        for eng in self.iter_engines():
            for name, img in variants:
                try:
                    ls = order_lines(self.read(eng, img, name))
                except Cancelled:
                    raise
                except Exception as exc:
                    self.log(f'{eng.name} อ่านกรอบซ้ำไม่สำเร็จ: {exc}')
                    continue
                text = join_lines([l.text for l in ls])
                if text and text not in seen:
                    seen.add(text)
                    candidates.append({'text': text, 'score': min(l.score for l in ls),
                                       'engine': eng.name+'/'+name})
        return candidates


class GoogleVisionOCR:
    ENDPOINT = 'https://vision.googleapis.com/v1/images:annotate'

    def __init__(self, cache):
        self.cache = cache
        self.calls = 0
        self.hits = 0
        self.limit = 10

    @staticmethod
    def parse(data):
        lines = []
        for page in data.get('fullTextAnnotation', {}).get('pages', []):
            for block in page.get('blocks', []):
                for para in block.get('paragraphs', []):
                    text = ''
                    for word in para.get('words', []):
                        for symbol in word.get('symbols', []):
                            br = symbol.get('property', {}).get('detectedBreak', {})
                            kind = br.get('type', '')
                            sep = '\n' if kind in ('EOL_SURE_SPACE', 'LINE_BREAK') else ' ' if kind in ('SPACE', 'SURE_SPACE') else '-' if kind == 'HYPHEN' else ''
                            if br.get('isPrefix'):
                                text += sep
                            text += symbol.get('text', '')
                            if not br.get('isPrefix'):
                                text += sep
                    vs = para.get('boundingBox', block.get('boundingBox', {})).get('vertices', [])
                    if text.strip() and vs:
                        poly = [[v.get('x',0), v.get('y',0)] for v in vs]
                        lines.append(Line(one_line(text), polygon_box(poly),
                                          float(para.get('confidence',0)), 'Google Vision', poly))
        return lines

    def read(self, image, credential, lang='ko+en'):
        key = self.cache.key(image, 'google-document:v2:'+lang)
        cached = self.cache.get(key)
        if cached is not None:
            self.hits += 1
            return [Line(**l) for l in cached]
        if self.calls >= self.limit:
            raise RuntimeError(f'ถึงเพดาน Google Vision {self.limit} ครั้งต่อการเปิดโปรแกรมแล้ว')
        if not credential.strip():
            raise RuntimeError('ใส่ API key หรือเลือก Service Account JSON ก่อน')
        import requests
        value = credential.strip().strip('"').strip("'")
        headers, params = {}, {}
        if value.lower().endswith('.json') and Path(value).is_file():
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request
            creds = service_account.Credentials.from_service_account_file(value,
                scopes=['https://www.googleapis.com/auth/cloud-platform'])
            creds.refresh(Request())
            headers['Authorization'] = 'Bearer '+creds.token
        else:
            params['key'] = value
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        if len(buf.getvalue()) > 7_000_000:
            raise RuntimeError('กรอบใหญ่เกินไป กรุณาลากกรอบให้เล็กลงก่อนใช้ Google Vision')
        hints = (['en'] if lang == 'en' else
                 ['zh-Hans', 'en'] if lang == 'zh+en' else
                 ['zh-Hant', 'en'] if lang == 'zh_tw+en' else
                 ['ko', 'en'])
        payload = {'requests': [{'image': {'content': base64.b64encode(buf.getvalue()).decode()},
            'features': [{'type': 'DOCUMENT_TEXT_DETECTION'}],
            'imageContext': {'languageHints': hints}}]}
        self.calls += 1
        try:
            response = requests.post(self.ENDPOINT, params=params, headers=headers, json=payload, timeout=60)
        except requests.RequestException:
            raise RuntimeError('ติดต่อ Google Vision ไม่สำเร็จ (นับเป็น 1 ครั้งที่พยายามเรียก)') from None
        if response.status_code != 200:
            raise RuntimeError(f'Google Vision HTTP {response.status_code}; ตรวจ key/สิทธิ์/โควตา')
        data = response.json().get('responses', [{}])[0]
        if 'error' in data:
            raise RuntimeError(f'Google Vision error code {data["error"].get("code", "unknown")}')
        result = self.parse(data)
        self.cache.put(key, [asdict(l) for l in result])
        return result


def read_mtp(mtp_file, cancel, emit, extract_dir=None):
    from text_layout import fast_direct_layer,normalize_font_choice,resolve_font
    emit(('log',f'กำลังอ่านไฟล์ .mtp: {mtp_file.name}…'))
    data=json.loads(mtp_file.read_text(encoding='utf-8-sig'))
    files_list=data.get('files')
    if not isinstance(files_list,list) or not files_list:
        raise ValueError('ไม่พบรายการรูปภาพ (files) ในไฟล์ .mtp นี้')

    safe_stem=re.sub(r'[<>:"/\\|?*]+','_',mtp_file.stem).strip() or 'mtp_project'
    extract_dir=extract_dir or mtp_file.parent/(safe_stem+'_mtp_pages')
    try:
        extract_dir.mkdir(parents=True,exist_ok=True)
    except OSError:
        h_tag=hashlib.md5(str(mtp_file).encode('utf-8',errors='ignore')).hexdigest()[:10]
        extract_dir=DATA_DIR/'mtp_pages'/(safe_stem[:40]+'_'+h_tag)
        extract_dir.mkdir(parents=True,exist_ok=True)

    font_reg=data.get('fontRegistry') if isinstance(data.get('fontRegistry'),dict) else {}
    ts_all=data.get('tsData') if isinstance(data.get('tsData'),dict) else {}
    ocr_all=data.get('ocrData') if isinstance(data.get('ocrData'),dict) else {}
    tl_all=data.get('textLayersData') if isinstance(data.get('textLayersData'),dict) else {}

    def safe_font(raw_name):
        cand=normalize_font_choice(raw_name)
        try:
            resolve_font(cand)
            return cand
        except Exception:
            return DEFAULT_FONT_CHOICE

    pages_out=[]
    total_bubbles=0;total_patches=0;total_laid=0
    for idx,f_rec in enumerate(files_list):
        if cancel.is_set():raise Cancelled()
        raw_name=str(f_rec.get('name') or f'{idx+1:02d}.jpg')
        clean_name=Path(raw_name).name
        if Path(clean_name).suffix.lower() not in IMAGE_EXTS:
            clean_name+=('.png' if 'png' in str(f_rec.get('type','')).lower() else '.jpg')
        img_dest=extract_dir/clean_name
        emit(('log',f'แกะข้อมูล .mtp หน้า {idx+1}/{len(files_list)}: {clean_name}'))
        img_bytes=_decode_data_url(f_rec.get('dataUrl',''))
        if not img_dest.is_file() or img_dest.stat().st_size!=len(img_bytes):
            tmp_img=img_dest.with_name(img_dest.name+'.'+uid()+'.tmp')
            try:
                tmp_img.write_bytes(img_bytes)
                os.replace(tmp_img,img_dest)
            finally:
                tmp_img.unlink(missing_ok=True)

        im=load_image(img_dest)
        W,H=im.size
        ts_page=_mtp_page_lookup(ts_all,raw_name,idx)
        ocr_page=_mtp_page_lookup(ocr_all,raw_name,idx)
        tl_page=_mtp_page_lookup(tl_all,raw_name,idx)
        tl_by_id={str(t.get('id')):t for t in tl_page if isinstance(t,dict) and t.get('id') is not None}
        used_tl_ids=set()

        text_entries=[it for it in ts_page if isinstance(it,dict) and it.get('type')=='text' and it.get('visible',True) is not False]
        if not text_entries:
            text_entries=[it for it in ocr_page if isinstance(it,dict)]

        page_bubbles=[]
        layer_hints={}
        mtp_id_to_bid={}
        for t_it in text_entries:
            raw_b=t_it.get('box')
            q=[]
            if isinstance(raw_b,list) and len(raw_b)==4 and isinstance(raw_b[0],(list,tuple)):
                bbox=clip_box(polygon_box(raw_b),W,H)
                if valid_quad(raw_b) and abs(quad_angle(raw_b))>=1.5:
                    q=[[float(pt[0]),float(pt[1])] for pt in raw_b]
            elif isinstance(raw_b,list) and len(raw_b)==4:
                bbox=clip_box(raw_b,W,H)
            else:
                continue
            if area(bbox)<=16:continue

            tl=tl_by_id.get(str(t_it.get('targetLayerId','')))
            if tl is not None:
                used_tl_ids.add(str(tl.get('id')))
            src_txt=str(t_it.get('originalText') or t_it.get('text') or '').strip()
            trn_txt=str(t_it.get('translation') or (tl.get('text') if tl else '') or '').strip()
            ft_raw=str(t_it.get('fontType') or (tl.get('fontType') if tl else '') or 'normal').lower()
            ftype=MTP_TYPE_TO_FRAME.get(ft_raw,'normal')
            raw_font=(tl.get('fontFamily') if tl else None) or font_reg.get(ft_raw) or DEFAULT_FONT_CHOICE
            f_choice=safe_font(raw_font)

            b_obj=Bubble(
                box=bbox,
                text=src_txt,
                translation=trn_txt,
                lines=[asdict(Line(src_txt or trn_txt,bbox,0.99,'MTP',q))],
                score=0.99,
                engine='MTP',
                reviewed=True,
                quad=q,
                font_choice=f_choice,
                frame_type=ftype,
            )
            if tl is not None:
                pref_sz=int(round(float(tl.get('fontSize') or tl.get('size') or 0)))
                pref_xy=(float(tl['x']),float(tl['y'])) if ('x' in tl and 'y' in tl) else None
                raw_rot=float(tl.get('rot') or 0.0)
                pref_rot=raw_rot if abs(raw_rot)>=0.2 else None
                layer_hints[b_obj.id]=(pref_sz,pref_xy,pref_rot)
            page_bubbles.append(b_obj)
            if t_it.get('id') is not None:
                mtp_id_to_bid[str(t_it.get('id'))]=b_obj.id

        for tl in tl_page:
            if not isinstance(tl,dict) or tl.get('visible',True) is False:continue
            tid=str(tl.get('id',''))
            if tid in used_tl_ids:continue
            trn_txt=str(tl.get('text') or '').strip()
            if not trn_txt:continue
            cx=float(tl.get('x',W/2.0));cy=float(tl.get('y',H/2.0))
            bw_l=max(24.0,float(tl.get('boxWidth') or 140.0))
            bh_l=max(24.0,float(tl.get('boxHeight') or 80.0))
            bbox=clip_box([cx-bw_l/2.0,cy-bh_l/2.0,cx+bw_l/2.0,cy+bh_l/2.0],W,H)
            if area(bbox)<=16:continue
            ft_raw=str(tl.get('fontType') or 'normal').lower()
            b_obj=Bubble(
                box=bbox,
                text='',
                translation=trn_txt,
                lines=[],
                score=0.99,
                engine='MTP',
                reviewed=True,
                font_choice=safe_font(tl.get('fontFamily') or DEFAULT_FONT_CHOICE),
                frame_type=MTP_TYPE_TO_FRAME.get(ft_raw,'normal'),
            )
            pref_sz=int(round(float(tl.get('fontSize') or tl.get('size') or 0)))
            raw_rot=float(tl.get('rot') or 0.0)
            pref_rot=raw_rot if abs(raw_rot)>=0.2 else None
            layer_hints[b_obj.id]=(pref_sz,(cx,cy),pref_rot)
            page_bubbles.append(b_obj)

        page_bubbles.sort(key=lambda b:(b.box[1],b.box[0]))

        patches_out=[]
        for p_it in ts_page:
            if not isinstance(p_it,dict) or p_it.get('type')!='inpaint':continue
            if p_it.get('inpaintVisible',True) is False or p_it.get('visible',True) is False:continue
            purl=str(p_it.get('patchUrl') or '')
            if not purl.startswith('data:image/'):continue
            try:
                p_bytes=_decode_data_url(purl)
                with Image.open(io.BytesIO(p_bytes)) as pim:
                    p_rgba=pim.convert('RGBA')
                pw,ph=p_rgba.size
                pr=p_it.get('patchRect') or [0,0,pw,ph]
                px0=max(0,min(W-2,int(round(float(pr[0])))))
                py0=max(0,min(H-2,int(round(float(pr[1])))))
                cw_p=min(pw,W-px0);ch_p=min(ph,H-py0)
                if cw_p<2 or ch_p<2:continue
                if (cw_p,ch_p)!=(pw,ph):
                    p_rgba=p_rgba.crop((0,0,cw_p,ch_p))
                pbox=[px0,py0,px0+cw_p,py0+ch_p]
                alpha_np=np.asarray(p_rgba.getchannel('A'))
                pmask=((alpha_np>16).astype(np.uint8)*255) if int(alpha_np.min())<250 else np.full((ch_p,cw_p),255,dtype=np.uint8)
                linked_bid=mtp_id_to_bid.get(str(p_it.get('targetLayerId','')),'')
                method_p=str(p_it.get('inpaintModel') or 'lama')
                patch_rec=encode_patch(pbox,p_rgba.convert('RGB'),pmask,method_p,linked_bid)
                patch_rec['quality']={'passes':1,'remaining':[],'verification_error':'','visual_checked':True,'visual_residual_pixels':0,'needs_review':False}
                patches_out.append(patch_rec)
            except Exception as exc:
                emit(('log',f'ข้ามแผ่นคลีนบางจุดใน {clean_name}: {exc}'))

        work_im=compose_page(im,patches_out) if patches_out else im
        for b_obj in page_bubbles:
            if b_obj.translation.strip():
                pref_sz,pref_xy,pref_rot=layer_hints.get(b_obj.id,(0,None,None))
                try:
                    b_obj.text_layer=fast_direct_layer(
                        work_im,b_obj,b_obj.translation,
                        font_choice=b_obj.font_choice,
                        preferred_size=pref_sz,center_xy=pref_xy,preferred_rot=pref_rot,
                    )
                    if b_obj.text_layer.get('font_choice'):
                        b_obj.font_choice=b_obj.text_layer['font_choice']
                    total_laid+=1
                except Exception as exc:
                    emit(('log',f'จัด Text {clean_name}: {exc}'))

        p_obj=Page(str(img_dest.resolve()),page_bubbles,'',True,uid(),patches_out)
        pages_out.append(p_obj)
        total_bubbles+=len(page_bubbles);total_patches+=len(patches_out)
        emit(('progress',(idx+1)/len(files_list)*100))

    suspect_total=sum(1 for pg in pages_out for bb in pg.bubbles if _is_suspect_mismatch(bb))
    warn_tail=f' • ⚠ พบ {suspect_total} กรอบที่อาจติดเสียง SFX แทรกคิว' if suspect_total else ''
    summary=(f'เปิดไฟล์ .mtp สำเร็จ: {len(pages_out)} หน้า • กรอบข้อความ {total_bubbles} ช่อง • '
             f'แผ่นคลีน {total_patches} จุด • วางคำแปลไทย {total_laid} ช่อง{warn_tail}')
    return pages_out,str(mtp_file),summary


def write_mtp(path, tasks, preset_map, cancel, emit):
    files_arr=[];ocr_dict={};ts_dict={};tl_dict={}
    for idx,p in enumerate(tasks):
        if cancel.is_set():raise Cancelled()
        name=Path(p.path).name
        emit(('log',f'แพ็กข้อมูล .mtp หน้า {idx+1}/{len(tasks)}: {name}'))
        im=load_image(p.path)
        W,H=im.size
        files_arr.append({
            'name':name,'type':'image/jpeg','width':W,'height':H,
            'dataUrl':_encode_data_url(im,'JPEG',92),
        })
        ocr_list=[];ts_list=[];tl_list=[]
        patch_by_bid={pt.get('bubble_id'):pt for pt in p.clean_patches if pt.get('bubble_id')}
        used_patches=set()
        for b_i,b in enumerate(p.bubbles,1):
            tid_num=int(time.time()*1000)+idx*1000+b_i
            layer_id=f'ts_layer_{idx+1:02d}_{b_i}'
            q=frame_quad(b)
            ft=getattr(b,'frame_type','') or 'normal'
            mtp_ft={'thought':'think','narration':'narr_in','floating':'small','terrible':'scream','happy':'normal','phone':'normal'}.get(ft,ft)
            ocr_list.append({
                'id':tid_num,'box':q,'text':b.text,'originalText':b.text,
                'translation':b.translation,'visible':True,'selected':False,'fontType':mtp_ft,
            })
            pt=patch_by_bid.get(b.id)
            if pt is not None:
                used_patches.add(id(pt))
                rgba_p=decode_patch(pt)
                ts_list.append({
                    'id':f'inpaint_{idx+1:02d}_{b_i}','type':'inpaint','targetLayerId':tid_num,
                    'inpaintModel':pt.get('method') or 'lama',
                    'patchUrl':_encode_data_url(rgba_p,'PNG'),
                    'patchExpansion':0,'patchRect':list(pt['box']),
                    'inpaintVisible':True,'visible':True,'selected':False,
                    'box':q,'text':'','originalText':'','translation':'','isExternalEdit':False,
                })
            ts_list.append({
                'id':tid_num,'box':q,'text':b.text,'originalText':b.text,
                'translation':b.translation,'visible':True,'selected':False,
                'type':'text','fontType':mtp_ft,'targetLayerId':layer_id,
                'inpaintModel':None,'patchUrl':None,'patchExpansion':0,
                'patchRect':list(b.box),'inpaintVisible':True,
            })
            if b.translation.strip():
                tb=(b.text_layer.get('box') if b.text_layer else None) or b.box
                fsz=int(round(b.text_layer.get('font_size',24))) if b.text_layer else 24
                col='#FFFFFF' if (b.text_layer and b.text_layer.get('color')==[255,255,255]) else '#000000'
                tl_list.append({
                    'id':layer_id,'type':'text','number':b_i,
                    'text':(b.text_layer.get('text') if b.text_layer else None) or b.translation,
                    'x':int(round((tb[0]+tb[2])/2.0)),'y':int(round((tb[1]+tb[3])/2.0)),
                    'boxWidth':max(20,int(round(b.box[2]-b.box[0]))),
                    'boxHeight':max(20,int(round(b.box[3]-b.box[1]))),
                    'size':fsz,'fontSize':fsz,'fontType':mtp_ft,
                    'fontFamily':getattr(b,'font_choice',DEFAULT_FONT_CHOICE),
                    'weight':'bold','style':'normal','color':col,
                    'rot':float(b.text_layer.get('rotation',0) if b.text_layer else 0),
                    'align':'center','textAlign':'center','lineHeight':1.2,
                    'letterSpacing':0,'opacity':1,'visible':True,'selected':False,
                })
        for u_i,pt in enumerate(p.clean_patches,1):
            if id(pt) in used_patches:continue
            rgba_p=decode_patch(pt);pb=list(pt['box'])
            ts_list.insert(0,{
                'id':f'inpaint_free_{idx+1:02d}_{u_i}','type':'inpaint','targetLayerId':None,
                'inpaintModel':pt.get('method') or 'lama',
                'patchUrl':_encode_data_url(rgba_p,'PNG'),
                'patchExpansion':0,'patchRect':pb,'inpaintVisible':True,
                'visible':True,'selected':False,'box':box_quad(pb),
                'text':'','originalText':'','translation':'','isExternalEdit':False,
            })
        ocr_dict[name]=ocr_list;ts_dict[name]=ts_list;tl_dict[name]=tl_list
        emit(('progress',(idx+1)/len(tasks)*100))

    mtp_obj={
        'version':'1.0.0',
        'savedAt':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        'currentMode':'ts',
        'currentFileIndex':0,
        'appSettings':{'readingDirection':'ttb'},
        'fontRegistry':{
            'normal':preset_map.get('normal',DEFAULT_FONT_CHOICE),
            'shout':preset_map.get('shout',DEFAULT_FONT_CHOICE),
            'think':preset_map.get('thought',DEFAULT_FONT_CHOICE),
            'thought':preset_map.get('thought',DEFAULT_FONT_CHOICE),
            'narr_in':preset_map.get('narration',DEFAULT_FONT_CHOICE),
            'narr_out':preset_map.get('narration',DEFAULT_FONT_CHOICE),
            'narration':preset_map.get('narration',DEFAULT_FONT_CHOICE),
            'system':preset_map.get('system',DEFAULT_FONT_CHOICE),
            'small':preset_map.get('floating',DEFAULT_FONT_CHOICE),
            'scream':preset_map.get('shout',DEFAULT_FONT_CHOICE),
            'sfx':preset_map.get('floating',DEFAULT_FONT_CHOICE),
        },
        'fontRealNameMap':{},'fontPresetStyles':{},'fontFilesData':{},
        'files':files_arr,'bannerData':{'top':None,'bottom':None},
        'ocrData':ocr_dict,'tsData':ts_dict,'textLayersData':tl_dict,
        'textGroupsData':{},'detectedBubbles':{},'paintData':{},
        'originalImages':{},'originalTsData':{},'originalOcrData':{},
    }
    atomic_json(path,mtp_obj)
    emit(('job_summary',f'ส่งออกไฟล์ .mtp สำเร็จ ({len(tasks)} หน้า): {path}'))
    return None


def stitch_pages(src_pages, src_drafts, rough_height, width_mode, custom_width, detector_type, sensitivity, scan_step, ignore_margin, out_dir, cancel, emit, strict=False):
    from text_layout import scale_text_layer,fast_direct_layer
    emit(('log',f'SmartStitch: กำลังโหลดและปรับขนาดภาพต้นฉบับ {len(src_pages)} หน้า…'))
    loaded=[]
    for p in src_pages:
        if cancel.is_set():raise Cancelled()
        loaded.append((p,load_image(p.path)))

    if width_mode=='User Customized Width':
        target_w=int(custom_width)
    elif width_mode=='Automatic Uniform Width':
        target_w=min(im.width for _,im in loaded)
    else:
        target_w=loaded[0][1].width

    strips=[]
    global_bubbles=[]
    global_patches=[]
    global_drafts=[]
    protected_spans=[]
    y_cursor=0

    for idx,(p,im) in enumerate(loaded):
        if cancel.is_set():raise Cancelled()
        w0,h0=im.size
        sx=float(target_w)/max(1.0,float(w0)) if (width_mode!='No Enforcement' or w0!=target_w) else 1.0
        nw=int(target_w)
        nh=max(16,int(round(h0*sx)))
        sy=float(nh)/max(1.0,float(h0))
        im_scaled=im.resize((nw,nh),Image.Resampling.LANCZOS) if (nw,nh)!=(w0,h0) else im
        strips.append((y_cursor,nh,im_scaled))

        for b in p.bubbles:
            nb=copy.deepcopy(b)
            old_layer=copy.deepcopy(b.text_layer) if b.text_layer else None
            lx0=max(0.0,min(float(nw-2),float(b.box[0])*sx))
            ly0=max(0.0,min(float(nh-2),float(b.box[1])*sy))
            lx1=max(lx0+4.0,min(float(nw),float(b.box[2])*sx))
            ly1=max(ly0+4.0,min(float(nh),float(b.box[3])*sy))
            nb.box=[lx0,ly0+y_cursor,lx1,ly1+y_cursor]
            if b.quad and valid_quad(b.quad):
                nb.quad=[[max(0.0,min(float(nw),float(qx)*sx)),
                          max(0.0,min(float(nh),float(qy)*sy))+y_cursor] for qx,qy in b.quad]
                if valid_quad(nb.quad):
                    nb.box=quad_bounds(nb.quad)
                else:
                    nb.quad=[]
            new_lines=[]
            for rec in b.lines:
                lb=rec.get('box')
                if not lb or len(lb)!=4:continue
                rlx0=max(0.0,min(float(nw-2),float(lb[0])*sx))
                rly0=max(0.0,min(float(nh-2),float(lb[1])*sy))
                rlx1=max(rlx0+2.0,min(float(nw),float(lb[2])*sx))
                rly1=max(rly0+2.0,min(float(nh),float(lb[3])*sy))
                nrec=dict(rec,box=[rlx0,rly0+y_cursor,rlx1,rly1+y_cursor])
                if rec.get('quad') and valid_quad(rec['quad']):
                    nrec['quad']=[[float(qx)*sx,float(qy)*sy+y_cursor] for qx,qy in rec['quad']]
                new_lines.append(nrec)
            nb.lines=new_lines
            if getattr(b,'custom_size',0)>0:
                nb.custom_size=max(6,int(round(b.custom_size*min(sx,sy))))
            if getattr(b,'custom_width',0)>0:
                nb.custom_width=max(24,int(round(b.custom_width*sx)))
            if getattr(b,'text_offset',[]) and len(b.text_offset)==2:
                nb.text_offset=[round(b.text_offset[0]*sx,2),round(b.text_offset[1]*sy,2)]
            nb.text_layer={}
            global_bubbles.append((nb,old_layer,sx,sy,y_cursor))
            protected_spans.append((nb.box[1]-4,nb.box[3]+4))

        for pt in p.clean_patches:
            rgba=decode_patch(pt)
            pb=pt['box']
            px0=max(0,min(nw-2,int(round(pb[0]*sx))))
            py0=max(0,min(nh-2,int(round(pb[1]*sy))))
            px1=max(px0+2,min(nw,int(round(pb[2]*sx))))
            py1=max(py0+2,min(nh,int(round(pb[3]*sy))))
            pw_s=px1-px0;ph_s=py1-py0
            if (pw_s,ph_s)!=rgba.size:
                rgb_s=rgba.convert('RGB').resize((pw_s,ph_s),Image.Resampling.LANCZOS)
                a_s=rgba.getchannel('A').resize((pw_s,ph_s),Image.Resampling.BILINEAR)
                a_np=(np.asarray(a_s)>96).astype(np.uint8)*255
            else:
                rgb_s=rgba.convert('RGB')
                a_np=(np.asarray(rgba.getchannel('A'))>0).astype(np.uint8)*255
            global_patches.append({
                'gbox':[px0,py0+y_cursor,px1,py1+y_cursor],
                'rgb':rgb_s,'mask':a_np,
                'method':pt.get('method','lama'),
                'bubble_id':pt.get('bubble_id',''),
                'quality':pt.get('quality',{}),
            })
            protected_spans.append((py0+y_cursor-2,py1+y_cursor+2))

        for b in p.bubbles:
            reg=src_drafts.get((p.id,b.id))
            if reg is None:continue
            rb=reg.box
            rx0=max(0,min(nw-2,int(round(rb[0]*sx))))
            ry0=max(0,min(nh-2,int(round(rb[1]*sy))))
            rx1=max(rx0+2,min(nw,int(round(rb[2]*sx))))
            ry1=max(ry0+2,min(nh,int(round(rb[3]*sy))))
            rw_s=rx1-rx0;rh_s=ry1-ry0
            r_img=reg.image.resize((rw_s,rh_s),Image.Resampling.LANCZOS)
            r_mask=(np.asarray(Image.fromarray(reg.mask).resize((rw_s,rh_s),Image.Resampling.NEAREST))>0).astype(np.uint8)*255
            r_safe=(np.asarray(Image.fromarray(reg.safe).resize((rw_s,rh_s),Image.Resampling.NEAREST))>0).astype(np.uint8)*255
            global_drafts.append((b.id,[rx0,ry0+y_cursor,rx1,ry1+y_cursor],r_img,r_mask,r_safe,reg))

        y_cursor+=nh
        emit(('progress',(idx+1)/len(loaded)*40))

    total_h=y_cursor
    emit(('log',f'SmartStitch: ความสูงรวม {total_h:,} px (กว้าง {target_w} px) — กำลังหาจุดตัดหลบบับเบิ้ล…'))
    cuts=_find_smart_cuts(
        strips,total_h,rough_height,
        detector_type=detector_type,sensitivity=sensitivity,
        scan_step=scan_step,ignore_margin=ignore_margin,
        protected_spans=protected_spans,
    )
    if strict:
        # Protect long spans as well, even if this requires exceeding the target height.
        intervals=sorted((max(1,a),min(total_h-1,b)) for a,b in protected_spans)
        safe=[0]
        for cut in cuts[1:-1]:
            for lo,hi in intervals:
                if lo<=cut<=hi:cut=hi+1
            if safe[-1]<cut<total_h:safe.append(cut)
        cuts=safe+[total_h]
        if max(b-a for a,b in zip(cuts,cuts[1:]))>29400:
            raise ValueError('Cannot split within the PSD height limit without cutting a protected region')
    num_slices=len(cuts)-1
    out_dir.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%H%M%S')
    use_suffix=any((out_dir/f'{i+1:02d}.png').exists() for i in range(num_slices))

    new_pages=[];new_drafts={};laid_count=0
    for s_idx in range(num_slices):
        if cancel.is_set():raise Cancelled()
        cy0,cy1=cuts[s_idx],cuts[s_idx+1]
        sh=cy1-cy0
        fname=f'{s_idx+1:02d}_{stamp}.png' if use_suffix else f'{s_idx+1:02d}.png'
        dest_img=out_dir/fname
        emit(('log',f'SmartStitch: บันทึกหน้าใหม่ {s_idx+1}/{num_slices} ({target_w}×{sh} px) → {fname}'))
        slice_im=_crop_stitched_slice_rgb(strips,cy0,cy1)
        slice_im.save(dest_img,format='PNG',compress_level=1)

        page_bs_meta=[]
        for gb,old_layer,b_sx,b_sy,b_ycur in global_bubbles:
            bcy=(gb.box[1]+gb.box[3])*0.5
            in_slice=(cy0<=bcy<cy1) or (s_idx==num_slices-1 and bcy>=cy0)
            if not in_slice:continue
            lb_copy=copy.deepcopy(gb)
            lb_copy.box=clip_box([gb.box[0],gb.box[1]-cy0,gb.box[2],gb.box[3]-cy0],target_w,sh)
            if lb_copy.quad:
                lb_copy.quad=[[qx,max(0.0,min(float(sh),qy-cy0))] for qx,qy in lb_copy.quad]
                if valid_quad(lb_copy.quad):
                    lb_copy.box=quad_bounds(lb_copy.quad)
                else:
                    lb_copy.quad=[]
            shifted_ls=[]
            for rec in lb_copy.lines:
                rb=rec['box']
                srec=dict(rec,box=clip_box([rb[0],rb[1]-cy0,rb[2],rb[3]-cy0],target_w,sh))
                if rec.get('quad'):
                    srec['quad']=[[qx,max(0.0,min(float(sh),qy-cy0))] for qx,qy in rec['quad']]
                shifted_ls.append(srec)
            lb_copy.lines=shifted_ls
            if area(lb_copy.box)>16:
                page_bs_meta.append((lb_copy,old_layer,b_sx,b_sy,b_ycur-cy0))
        page_bs_meta.sort(key=lambda item:(item[0].box[1],item[0].box[0]))
        page_bs=[item[0] for item in page_bs_meta]
        page_bids={b.id for b in page_bs}

        page_pts=[]
        for gp in global_patches:
            gx0,gy0,gx1,gy1=gp['gbox']
            iy0=max(cy0,gy0);iy1=min(cy1,gy1)
            if iy1-iy0<2:continue
            top_cut=iy0-gy0;bot_cut=iy1-gy0
            sub_rgb=gp['rgb'].crop((0,top_cut,gx1-gx0,bot_cut))
            sub_mask=gp['mask'][top_cut:bot_cut,:]
            if not np.any(sub_mask):continue
            local_box=[gx0,iy0-cy0,gx1,iy1-cy0]
            bid_ref=gp['bubble_id'] if gp['bubble_id'] in page_bids else ''
            prec=encode_patch(local_box,sub_rgb,sub_mask,gp['method'],bid_ref)
            if gp.get('quality'):prec['quality']=dict(gp['quality'])
            page_pts.append(prec)

        p_new=Page(str(dest_img.resolve()),page_bs,'',True,uid(),page_pts)

        for bid,gbox,r_img,r_mask,r_safe,orig_reg in global_drafts:
            if bid not in page_bids:continue
            gx0,gy0,gx1,gy1=gbox
            iy0=max(cy0,gy0);iy1=min(cy1,gy1)
            if iy1-iy0<8:continue
            tc=iy0-gy0;bc=iy1-gy0
            c_im=r_img.crop((0,tc,gx1-gx0,bc))
            new_drafts[(p_new.id,bid)]=CleanRegion(
                [gx0,iy0-cy0,gx1,iy1-cy0],c_im,r_mask[tc:bc,:].copy(),r_safe[tc:bc,:].copy(),
                orig_reg.boundary_found,orig_reg.note,orig_reg.manual_frame,orig_reg.manual_edited,
            )

        work_slice=compose_page(slice_im,page_pts) if page_pts else slice_im
        for b_item,old_layer,b_sx,b_sy,shift_y in page_bs_meta:
            if not b_item.translation.strip():continue
            try:
                if old_layer:
                    b_item.text_layer=scale_text_layer(work_slice,b_item,old_layer,b_sx,b_sy,shift_y)
                else:
                    b_item.text_layer=fast_direct_layer(
                        work_slice,b_item,b_item.translation,
                        font_choice=getattr(b_item,'font_choice',DEFAULT_FONT_CHOICE),
                    )
                if b_item.text_layer.get('font_choice'):
                    b_item.font_choice=b_item.text_layer['font_choice']
                laid_count+=1
            except Exception as exc:
                emit(('log',f'ปรับ Text ({fname}): {exc}'))

        new_pages.append(p_new)
        emit(('progress',40+60*(s_idx+1)/num_slices))

    summary=(f'SmartStitch สำเร็จรวดเร็ว! รวมจาก {len(src_pages)} หน้า → เหลือ {len(new_pages)} หน้า '
             f'(กว้าง {target_w}px, สูง ~{rough_height}px) • กรอบ {sum(len(p.bubbles) for p in new_pages)} ช่อง • '
             f'แผ่นคลีน {sum(len(p.clean_patches) for p in new_pages)} จุด • Text {laid_count} ช่องครบ 100% (Ctrl+Z ย้อนกลับได้)')
    return new_pages,new_drafts,summary


def read_project_pages(data,path):
    pages=[];drafts={}
    missing=[]
    seen_ids=set()
    for record in data['pages']:
        bs=[Bubble(**b) for b in record['bubbles']]
        for b in bs:
            if not isinstance(b.id,str) or not b.id or b.id in seen_ids:
                raise ValueError('พบรหัสกรอบซ้ำหรือไม่ถูกต้องในโปรเจกต์')
            seen_ids.add(b.id)
            if b.quad:
                if not valid_quad(b.quad):raise ValueError('พบมุมกรอบเอียงที่ไม่ถูกต้องในโปรเจกต์')
                b.box=quad_bounds(b.quad)
            if b.text_layer:
                from text_layout import validate_layer,normalize_font_choice,_normalize_rotation
                validate_layer(b.text_layer)
                b.font_choice=normalize_font_choice(b.text_layer.get('font_choice',b.font_choice))
                b.custom_size=int(b.text_layer.get('custom_size',b.custom_size) or 0)
                b.custom_width=int(b.text_layer.get('custom_width',b.custom_width) or 0)
                b.text_offset=list(b.text_layer.get('text_offset',b.text_offset) or [])
                if 'custom_rotation' in b.text_layer:
                    b.custom_rotation=_normalize_rotation(b.text_layer.get('custom_rotation'))
            if len(b.box)!=4 or not all(isinstance(v,(int,float)) and np.isfinite(v) for v in b.box) or area(b.box)<=0:
                raise ValueError('พบพิกัดกรอบที่ไม่ถูกต้องในโปรเจกต์')
        image_path=Path(record['path'])
        if not image_path.is_absolute():
            image_path=Path(path).parent/image_path
        p=Page(str(image_path.resolve()),bs,record.get('error',''),record.get('processed',False),record.get('id',uid()))
        p.clean_patches=record.get('clean_patches',[])
        for patch in p.clean_patches:decode_patch(patch)
        for bid,draft in record.get('clean_drafts',{}).items():
            if bid not in {b.id for b in bs}:raise ValueError('มาสก์อ้างถึงกรอบที่ไม่มีในโปรเจกต์')
            drafts[(p.id,bid)]=decode_region(draft)
        pages.append(p)
        if not image_path.is_file():
            missing.append(p)
    return pages,drafts,missing


class ManhwaOCRApp:
    def __init__(self, root):
        self.root = root
        verify_clean_components()
        root.title('Manhwa OCR Editor 3.1 — OCR / CLEAN / MTP / SmartStitch')
        width=min(1880,root.winfo_screenwidth()-40)
        height=min(990,root.winfo_screenheight()-90)
        root.geometry(f'{width}x{height}+20+20')
        root.minsize(min(1000,width),min(600,height))
        self.pages = []
        self.page_index = -1
        self.current_bubble = None
        self.image = None
        self.photo = None
        self.scale = .56
        self.view_mode = 'reading'
        self._wheel_job=None
        self._wheel_pending=0.0
        self._view_cache=None
        self._clean_view_dirty=False
        self._last_clean_refresh=0.0
        self.project_path = None
        self.dirty = False
        self.loading_editor = False
        self.busy = False
        self.events = queue.Queue()
        from sound_effects import SoundEffects
        self.sounds=SoundEffects(log=lambda message:self.events.put(('sound_log',message)))
        self.cancel = threading.Event()
        self.pipeline = None
        self.pipeline_settings = None
        self.rapid_pipeline = None
        self.rapid_lang = None
        self.cleaner=Cleaner()
        self.lama_note=tk.StringVar(value='LaMa: พบไฟล์ แต่ยังไม่ได้ทดสอบ' if lama_model_path().is_file() else 'LaMa: ยังไม่มีน้ำหนัก — ติดตั้งก่อนคลีนพื้นซับซ้อน')
        self.clean_reader=None;self.clean_reader_settings=None
        self.clean_prepared={};self.mask_preview=None
        self.show_masks=tk.BooleanVar(value=False)
        self.edit_frame=tk.BooleanVar(value=False)
        self.snap_guides=tk.BooleanVar(value=True)
        self.clean_verify=tk.BooleanVar(value=True)
        self.cleaned_preview=None
        self.show_cleaned=tk.BooleanVar(value=False)
        self.show_text=tk.BooleanVar(value=True)
        self.selected_font=tk.StringVar(value=DEFAULT_FONT_CHOICE)
        self.font_size_var=tk.IntVar(value=24)
        from text_layout import load_font_presets
        self.font_presets_map=load_font_presets()
        self.font_preset=tk.StringVar(value='1. มังฮวาเกาหลี')
        self.stitch_height=tk.IntVar(value=15000)
        self.stitch_width_mode=tk.StringVar(value='User Customized Width')
        self.stitch_custom_width=tk.IntVar(value=720)
        self.stitch_detector=tk.StringVar(value='Smart Pixel Comparison')
        self.stitch_sensitivity=tk.IntVar(value=90)
        self.stitch_scan_step=tk.IntVar(value=5)
        self.stitch_ignore_margin=tk.IntVar(value=0)
        self.completion_note=''
        self.cache = OCRCache()
        self.google = GoogleVisionOCR(self.cache)
        self.undo_stack = []
        self.mode = tk.StringVar(value='auto')
        self.lang = tk.StringVar(value='ko+en')
        self.gpu = tk.BooleanVar(value=_auto_gpu_default())
        self.neural = tk.BooleanVar(value=True)
        self.retry = tk.BooleanVar(value=False)
        self.type_scan = tk.BooleanVar(value=False)
        self.fast = tk.BooleanVar(value=True)
        self.bubble_only = tk.BooleanVar(value=True)
        self.job_started = 0
        self.stage_started = 0
        self.stage_text = ''
        self.job_failed = False
        self.draw_mode = tk.BooleanVar(value=False)
        self.credential = tk.StringVar(value=os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', ''))
        self.status = tk.StringVar(value='เลือกภาพ หรือกด "เปิดโปรเจกต์" เพื่อเปิดไฟล์ .json / .mtp ได้ทันที')
        self.stats = tk.StringVar(value='Google Vision: 0 / 10 ครั้ง | แคช: 0')
        self._build_ui()
        self.poll_id = self.root.after(100, self._poll)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Control-s>', lambda e: self.save_project())
        root.bind('<Control-z>', lambda e: self.undo() if not isinstance(e.widget, tk.Text) else None)
        root.bind('<Escape>', lambda e: self.cancel_active_drag())

    def _build_ui(self):
        from app_ui import build_ui
        build_ui(self)

    def page(self):
        return self.pages[self.page_index] if 0 <= self.page_index < len(self.pages) else None

    def guard(self):
        if self.busy:
            self.status.set('รอให้งาน OCR เสร็จ หรือกดหยุดก่อนแก้ไข')
            return False
        return True

    def log(self, text):
        self.log_text.configure(state='normal')
        self.log_text.insert('end',str(text)+'\n')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def snapshot(self):
        self.commit_editor()
        self.undo_stack.append(copy.deepcopy(self.pages))
        self.undo_stack = self.undo_stack[-20:]

    def undo(self):
        if self.guard() and self.undo_stack:
            self.current_bubble = None
            self.pages = self.undo_stack.pop()
            self.clean_prepared={};self.mask_preview=None
            self.dirty = True
            self.page_index = min(self.page_index,len(self.pages)-1)
            self.refresh_pages()
            if self.pages:
                self.select_page(self.page_index)
            else:
                self.image = None
                self.cleaned_preview=None
                self.fill_editor(None)
                self.refresh_tree()
                self.refresh_output()
                self.render()

    def refresh_pages(self):
        self.page_combo['values'] = [f'{i+1}. {Path(p.path).name}' for i,p in enumerate(self.pages)]
        if not self.pages:
            self.page_combo.set('')
        if self.pages and self.page_index >= 0:
            self.page_combo.current(self.page_index)

    def add_paths(self, paths):
        if not self.guard():
            return
        known = {p.path for p in self.pages}
        ordered = sorted({str(Path(p).resolve()) for p in paths},key=natural_sort_key)
        if not ordered:
            return
        self.snapshot()
        for path in ordered:
            if path not in known and Path(path).suffix.lower() in IMAGE_EXTS:
                self.pages.append(Page(path))
        self.dirty = True
        self.refresh_pages()
        if self.pages and self.page_index < 0:
            self.select_page(0)

    def add_images(self):
        if self.guard():
            self.add_paths(filedialog.askopenfilenames(title='เลือกภาพการ์ตูน',
                filetypes=[('Images','*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff')]))

    def add_folder(self):
        if not self.guard():
            return
        folder = filedialog.askdirectory(title='เลือกโฟลเดอร์ภาพ')
        if folder:
            self.add_paths([p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS])

    def select_page(self, index):
        self.commit_editor()
        if index < 0 or index >= len(self.pages):
            return
        self.cancel_wheel()
        self.current_bubble = None
        self.page_index = index
        self.refresh_pages()
        try:
            self.image = load_image(self.page().path)
        except Exception as exc:
            self.image = None
            self.log(f'เปิดภาพไม่ได้: {self.page().path}: {exc}')
        self.refresh_clean_view()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.refresh_tree()
        self.fill_editor(None)
        self.fit()
        self.refresh_output()
        p = self.page()
        dims = f' ({self.image.width}×{self.image.height}px)' if self.image else ''
        suspect_n = sum(1 for b in p.bubbles if _is_suspect_mismatch(b))
        warn_s = f' | ⚠ พบ {suspect_n} กรอบคำแปลอาจเลื่อนคิว' if suspect_n else ''
        self.page_note.configure(text=p.error or ('ไม่พบข้อความ — ลากเพิ่มกรอบแล้วอ่านซ้ำได้' if p.processed and not p.bubbles else f'{len(p.bubbles)} กรอบ{dims}{warn_s}'))

    def navigate(self, delta):
        self.select_page(self.page_index+delta)

    def refresh_tree(self, selected=None):
        self.tree.delete(*self.tree.get_children())
        p = self.page()
        if p:
            for i,b in enumerate(p.bubbles):
                suspect = _is_suspect_mismatch(b)
                mark = '⚠เช็ก' if suspect else ('✓' if b.reviewed else f'{b.score:.0%}')
                row_tags = ('suspect',) if suspect else (('low',) if b.score<REVIEW_THRESHOLD and not b.reviewed else ())
                label_txt = one_line(b.text) or ('[แปล] '+one_line(b.translation)[:24] if b.translation.strip() else '(ยังอ่านไม่ได้)')
                self.tree.insert('', 'end', iid=b.id,values=(i+1,label_txt,mark),tags=row_tags)
            if selected:
                items = [selected] if isinstance(selected, str) else [s for s in selected if self.tree.exists(s)]
                valid = [s for s in items if self.tree.exists(s)]
                if valid:
                    self.tree.selection_set(valid)
                    self.tree.see(valid[0])

    def select_all_bubbles(self):
        if not self.guard() or not self.page() or not self.page().bubbles:
            return
        self.commit_editor()
        self.tree.selection_set([b.id for b in self.page().bubbles])

    def select_bubble(self, event=None):
        ids = self.tree.selection()
        if not ids:
            self.commit_editor()
            self.fill_editor(None)
            self.render()
            return
        self.commit_editor()
        selected_bubbles = [b for b in self.page().bubbles if b.id in set(ids)]
        bubble = next((b for b in self.page().bubbles if b.id==ids[-1]), selected_bubbles[0] if selected_bubbles else None)
        self.fill_editor(bubble, selected_count=len(selected_bubbles), selected_bubbles=selected_bubbles)
        self.render()
        if len(selected_bubbles) == 1 and bubble and self.image and not getattr(self, '_suppress_autoscroll', False):
            y = max(0,bubble.box[1]*self.scale-40)
            self.canvas.yview_moveto(y/max(1,self.image.height*self.scale))
            self.render()

    def fill_editor(self, bubble, selected_count=None, selected_bubbles=None):
        self.loading_editor = True
        self.current_bubble = bubble
        for widget,text in [(self.source,bubble.text if bubble else ''),(self.thai,bubble.translation if bubble else '')]:
            widget.configure(state='normal')
            widget.delete('1.0','end')
            widget.insert('1.0',text)
            widget.edit_modified(False)
            widget.edit_reset()
            widget.configure(state='disabled' if self.busy or bubble is None else 'normal')
        self.reviewed.set(bubble.reviewed if bubble else False)
        self.candidate_combo['values'] = [f'{i+1}. {c["engine"]}: {c["text"]}' for i,c in enumerate(bubble.candidates)] if bubble else []
        self.candidate_combo.set('')
        count = selected_count if selected_count is not None else (1 if bubble else 0)
        if bubble:
            cur_sz = int(round(bubble.text_layer.get('font_size', 0))) if bubble.text_layer else int(getattr(bubble, 'custom_size', 0) or 24)
            if cur_sz >= 6:
                self.font_size_var.set(cur_sz)
        if selected_bubbles and len(selected_bubbles) > 1:
            fonts = {getattr(b, 'font_choice', '') or DEFAULT_FONT_CHOICE for b in selected_bubbles}
            if len(fonts) == 1:
                self.selected_font.set(next(iter(fonts)))
            font_summary = next(iter(fonts)) if len(fonts) == 1 else f'ผสม {len(fonts)} ฟอนต์'
            self.bubble_note.configure(text=f'เลือกอยู่ {count} กรอบ (ฟอนต์: {font_summary}) — กดสลับคำแปล ⇄ หรือเลื่อนคำแปลในช่วงที่เลือกได้')
        elif bubble:
            from text_layout import FRAME_TYPE_LABELS
            b_font = getattr(bubble, 'font_choice', '') or DEFAULT_FONT_CHOICE
            self.selected_font.set(b_font)
            ftype = getattr(bubble, 'frame_type', '')
            ft_tag = f' [{FRAME_TYPE_LABELS[ftype]}]' if ftype in FRAME_TYPE_LABELS else ''
            sz_note = f' | ขนาด {int(round(bubble.text_layer["font_size"]))}px' if bubble.text_layer else ''
            rot_val = float(bubble.text_layer.get('rotation', 0) or 0.0) if bubble.text_layer else (getattr(bubble, 'custom_rotation', None) or 0.0)
            if abs(rot_val) >= 0.1:
                sz_note += f' • หมุน {rot_val:+.1f}°'
            if getattr(bubble, 'custom_size', 0) > 0 or getattr(bubble, 'text_offset', []) or getattr(bubble, 'custom_rotation', None) is not None:
                sz_note += ' (จัดเอง)'
            warn_prefix = '⚠ อาจเป็นเสียง SFX ที่แทรกคิว (กด "เลื่อนคำแปล ↓" แล้ว "ลบกรอบ+คืนภาพ") | ' if _is_suspect_mismatch(bubble) else ''
            self.bubble_note.configure(text=f'{warn_prefix}{bubble.engine}{ft_tag} | {b_font}{sz_note}')
        else:
            self.bubble_note.configure(text='เลือกกรอบในภาพหรือรายการ (Ctrl+คลิกเลือกหลายกรอบได้)')
        self.loading_editor = False

    def commit_editor(self):
        if self.current_bubble and not self.loading_editor and not self.busy:
            b = self.current_bubble
            source = self.source.get('1.0','end-1c')
            thai = self.thai.get('1.0','end-1c')
            from text_layout import extract_frame_tag
            tag,clean=extract_frame_tag(thai)
            if tag:
                self.thai.delete('1.0',f'1.0+{len(thai)-len(clean)}c')
                b.frame_type=tag;b.frame_type_locked=True
                thai=clean;self.dirty=True
            if source != b.text or thai != b.translation:
                b.text, b.translation = source, thai
                if not thai.strip():b.text_layer={}
                b.reviewed = False
                self.reviewed.set(False)
                self.dirty = True
                if self.tree.exists(b.id):
                    suspect = _is_suspect_mismatch(b)
                    values = list(self.tree.item(b.id,'values'))
                    values[1] = one_line(source) or ('[แปล] '+one_line(thai)[:24] if thai.strip() else '(ยังอ่านไม่ได้)')
                    values[2] = '⚠เช็ก' if suspect else f'{b.score:.0%}'
                    self.tree.item(b.id,values=values,tags=('suspect',) if suspect else (('low',) if b.score<REVIEW_THRESHOLD else ()))

    def edited(self,event):
        widget = event.widget
        if widget.edit_modified():
            if not self.loading_editor and not self.busy:
                self.commit_editor()
                self.refresh_output()
                if self.current_bubble and self.current_bubble.text_layer:self.render()
            widget.edit_modified(False)

    def mark_reviewed(self):
        if self.guard() and self.current_bubble:
            value = self.reviewed.get()
            self.commit_editor()
            self.current_bubble.reviewed = value
            self.reviewed.set(value)
            self.dirty = True
            self.refresh_tree(self.tree.selection() or self.current_bubble.id)

    def refresh_output(self):
        self.output.configure(state='normal')
        self.output.delete('1.0','end')
        self.output.insert('1.0',format_output(self.pages))
        self.output.configure(state='disabled')

    def fit(self,mode='reading'):
        self.cancel_wheel()
        self.view_mode=mode
        if self.image:
            available=max(40,self.canvas.winfo_width()-48)
            self.scale=.56 if mode=='reading' else max(.01,min(4,available/self.image.width))
        self.render()

    def resize_view(self,event=None):
        if self.view_mode in ('reading','width'):self.fit(self.view_mode)
        else:self.render()

    def zoom(self,factor):
        if not self.image:return
        self.cancel_wheel()
        width=self.canvas.winfo_width();height=self.canvas.winfo_height()
        center=((self.canvas.canvasx(width/2))/self.scale,(self.canvas.canvasy(height/2))/self.scale)
        self.view_mode='manual'
        self.scale = min(4,max(.01,self.scale*factor))
        self.render()
        x0,y0,x1,y1=map(float,self.canvas.cget('scrollregion').split())
        self.canvas.xview_moveto((center[0]*self.scale-width/2-x0)/max(1,x1-x0))
        self.canvas.yview_moveto((center[1]*self.scale-height/2-y0)/max(1,y1-y0))
        self.render()

    def scroll_y(self,*args):
        self.canvas.yview(*args)
        self.render()

    def scroll_x(self,*args):
        self.canvas.xview(*args)
        self.render()

    def wheel(self,event):
        if event.state & 0x4:
            self.zoom(1.15 if event.delta>0 else 1/1.15)
        else:
            self._wheel_pending-=event.delta/120*60
            if self._wheel_job is None:self._wheel_job=self.canvas.after_idle(self.flush_wheel)
        return 'break'

    def cancel_wheel(self):
        if self._wheel_job is not None:
            self.canvas.after_cancel(self._wheel_job);self._wheel_job=None
        self._wheel_pending=0.0

    def flush_wheel(self):
        self._wheel_job=None
        pixels=int(self._wheel_pending)
        self._wheel_pending-=pixels
        if pixels:self.scroll_y('scroll',pixels,'units')

    @staticmethod
    def _outer_handle_coords(bubble, scale):
        bx0, by0, bx1, by1 = [v * scale for v in bubble.box]
        bmx = (bx0 + bx1) / 2.0
        bmy = (by0 + by1) / 2.0
        return bx0, by0, bx1, by1, bmx, bmy

    def _compute_smart_snap(self, bubble, tb, raw_dx_img, raw_dy_img, state=0):
        """Photoshop-style Smart Guides: snap text box to bubble center, neighbor layers & page axes."""
        shift_held = bool(state & 0x0001)
        alt_held = bool(state & 0x20000 or state & 0x0008)
        dx_img, dy_img = float(raw_dx_img), float(raw_dy_img)
        if shift_held:
            if abs(dx_img) >= abs(dy_img):
                dy_img = 0.0
            else:
                dx_img = 0.0
        if not self.snap_guides.get() or alt_held or not self.image:
            return dx_img, dy_img, []

        tol = max(2.0, 7.5 / max(0.15, float(self.scale)))
        tcx0 = (float(tb[0]) + float(tb[2])) * 0.5
        tcy0 = (float(tb[1]) + float(tb[3])) * 0.5
        thw = max(4.0, (float(tb[2]) - float(tb[0])) * 0.5)
        thh = max(4.0, (float(tb[3]) - float(tb[1])) * 0.5)
        cand_cx = tcx0 + dx_img
        cand_cy = tcy0 + dy_img

        bx0, by0, bx1, by1 = [float(v) for v in bubble.box]
        bcx = (bx0 + bx1) * 0.5
        bcy = (by0 + by1) * 0.5
        cur_off = getattr(bubble, 'text_offset', None) or []
        off_x = float(cur_off[0]) if len(cur_off) == 2 else 0.0
        off_y = float(cur_off[1]) if len(cur_off) == 2 else 0.0
        auto_cx = tcx0 - off_x
        auto_cy = tcy0 - off_y

        x_targets = [
            (bcx, bcx, by0 - 28.0, by1 + 28.0, 'กึ่งกลางกรอบ'),
        ]
        if abs(auto_cx - bcx) >= 2.0:
            x_targets.append((auto_cx, auto_cx, by0 - 28.0, by1 + 28.0, 'กึ่งกลางบับเบิ้ล'))
        if abs(tcx0 - bcx) >= 2.0 and abs(tcx0 - auto_cx) >= 2.0:
            x_targets.append((tcx0, tcx0, min(by0, tcy0 - thh) - 24.0, max(by1, tcy0 + thh) + 24.0, 'แนวตั้งเดิม'))
        page_cx = float(self.image.width) * 0.5
        x_targets.append((page_cx, page_cx, max(0.0, cand_cy - 320.0), min(float(self.image.height), cand_cy + 320.0), 'กึ่งกลางหน้า'))

        y_targets = [
            (bcy, bcy, bx0 - 28.0, bx1 + 28.0, 'กึ่งกลางกรอบ'),
        ]
        if abs(auto_cy - bcy) >= 2.0:
            y_targets.append((auto_cy, auto_cy, bx0 - 28.0, bx1 + 28.0, 'กึ่งกลางบับเบิ้ล'))
        if abs(tcy0 - bcy) >= 2.0 and abs(tcy0 - auto_cy) >= 2.0:
            y_targets.append((tcy0, tcy0, min(bx0, tcx0 - thw) - 24.0, max(bx1, tcx0 + thw) + 24.0, 'แนวนอนเดิม'))

        p = self.page()
        if p:
            for idx_o, ob in enumerate(p.bubbles, 1):
                if ob.id == bubble.id or not ob.text_layer:
                    continue
                otb = ob.text_layer.get('box')
                if not otb or len(otb) != 4:
                    continue
                ox0, oy0, ox1, oy1 = [float(v) for v in otb]
                ocx = (ox0 + ox1) * 0.5
                ocy = (oy0 + oy1) * 0.5
                if abs(ocy - cand_cy) <= 1800.0:
                    y_lo = min(oy0, cand_cy - thh) - 18.0
                    y_hi = max(oy1, cand_cy + thh) + 18.0
                    x_targets.append((ocx, ocx, y_lo, y_hi, f'ตรงกลาง #{idx_o}'))
                    x_targets.append((ox0 + thw, ox0, y_lo, y_hi, f'ชิดซ้าย #{idx_o}'))
                    x_targets.append((ox1 - thw, ox1, y_lo, y_hi, f'ชิดขวา #{idx_o}'))
                if abs(ocy - cand_cy) <= 1100.0:
                    x_lo = min(ox0, cand_cx - thw) - 18.0
                    x_hi = max(ox1, cand_cx + thw) + 18.0
                    y_targets.append((ocy, ocy, x_lo, x_hi, f'ระดับเดียว #{idx_o}'))
                    y_targets.append((oy0 + thh, oy0, x_lo, x_hi, f'ขอบบน #{idx_o}'))
                    y_targets.append((oy1 - thh, oy1, x_lo, x_hi, f'ขอบล่าง #{idx_o}'))

        guides = []
        best_x = min(x_targets, key=lambda item: abs(cand_cx - item[0]), default=None)
        if best_x is not None and abs(cand_cx - best_x[0]) <= tol:
            dx_img = best_x[0] - tcx0
            cand_cx = best_x[0]
            guides.append(('v', best_x[1], best_x[2], best_x[3], best_x[4]))

        best_y = min(y_targets, key=lambda item: abs(cand_cy - item[0]), default=None)
        if best_y is not None and abs(cand_cy - best_y[0]) <= tol:
            dy_img = best_y[0] - tcy0
            cand_cy = best_y[0]
            guides.append(('h', best_y[1], best_y[2], best_y[3], best_y[4]))

        return dx_img, dy_img, guides

    def cancel_active_drag(self, event=None):
        cancelled = False
        for attr in ('text_drag', 'frame_drag', 'drag_start'):
            if hasattr(self, attr):
                delattr(self, attr)
                cancelled = True
        if cancelled:
            self.canvas.delete('selection')
            self.render()
            self.status.set('ยกเลิกการลากแล้ว — ตำแหน่งและขนาดเดิมไม่ถูกเปลี่ยน')
            return 'break'

    def render(self):
        self.canvas.delete('bubble')
        if not self.image:
            self.canvas.delete('page_image');self._view_cache=None;self.photo=None
            self._view_scrollregion=None
            self.canvas.configure(scrollregion=(0,0,1,1))
            from app_ui import draw_workspace_grid
            draw_workspace_grid(self.canvas,viewport=True)
            return
        w,h = self.image.size
        s = self.scale
        padx=max(0,(self.canvas.winfo_width()-w*s)/2)
        pady=max(0,(self.canvas.winfo_height()-h*s)/2)
        region=(-padx,-pady,w*s+padx,h*s+pady)
        if getattr(self,'_view_scrollregion',None)!=region:
            self.canvas.configure(scrollregion=region);self._view_scrollregion=region
        from app_ui import draw_workspace_grid
        draw_workspace_grid(self.canvas,viewport=True)
        self.zoom_note.set(f'{s:.0%}')
        x0=max(0,int(self.canvas.canvasx(0)/s))
        y0=max(0,int(self.canvas.canvasy(0)/s))
        x1=min(w,int((self.canvas.canvasx(0)+self.canvas.winfo_width())/s)+2)
        y1=min(h,int((self.canvas.canvasy(0)+self.canvas.winfo_height())/s)+2)
        if x1>x0 and y1>y0:
            display=self.cleaned_preview if self.show_cleaned.get() and self.cleaned_preview is not None else self.image
            if self.show_masks.get() and self.mask_preview is not None:display=self.mask_preview
            layers=tuple((b.id,tuple(b.box),repr(b.quad),b.translation,getattr(b,'font_choice',DEFAULT_FONT_CHOICE),
                          getattr(b,'custom_size',0),getattr(b,'custom_width',0),
                          tuple(getattr(b,'text_offset',[]) or ()),getattr(b,'custom_rotation',None),repr(b.text_layer))
                         for b in self.page().bubbles if b.text_layer) if self.show_text.get() and self.page() else ()
            key=(id(display),s,layers);cached=self._view_cache
            if not cached or cached[0]!=key or not (cached[1][0]<=x0 and cached[1][1]<=y0 and cached[1][2]>=x1 and cached[1][3]>=y1):
                px=max(16,round((x1-x0)*.1));py=max(32,round((y1-y0)*.25))
                bounds=(max(0,x0-px),max(0,y0-py),min(w,x1+px),min(h,y1+py))
                cx,cy,cx1,cy1=bounds;crop=display.crop(bounds)
                if layers:
                    try:
                        from text_layout import composite_text_crop
                        crop=composite_text_crop(crop,self.page().bubbles,(cx,cy))
                    except Exception as exc:
                        self.show_text.set(False);self.log('แสดง Text ไม่สำเร็จ: '+str(exc))
                crop=crop.resize((max(1,round((cx1-cx)*s)),max(1,round((cy1-cy)*s))),Image.Resampling.BILINEAR)
                self.photo=ImageTk.PhotoImage(crop)
                self.canvas.delete('page_image')
                self.canvas.create_image(cx*s,cy*s,anchor='nw',image=self.photo,tags='page_image')
                self.canvas.tag_raise('page_image','workspace_grid')
                self._view_cache=(key,bounds,display)
        if self.page():
            selected=set(self.tree.selection())
            for i,b in enumerate(self.page().bubbles):
                if b.box[3]<y0 or b.box[1]>y1:
                    continue
                suspect=_is_suspect_mismatch(b)
                color='#00c8ff' if b.id in selected else ('#ff6b6b' if suspect else ('#2dd4a0' if b.reviewed else '#f6ad36'))
                coords=[v*s for v in b.box]
                points=frame_quad(b)
                self.canvas.create_polygon(*[v*s for point in points for v in point],outline=color,fill='',width=3 if (b.id in selected or suspect) else 2,tags='bubble')
                if self.edit_frame.get() and b.id in selected:
                    for px,py in points:
                        self.canvas.create_rectangle(px*s-5,py*s-5,px*s+5,py*s+5,fill=color,outline='white',tags='bubble')
                elif self.show_text.get() and not self.draw_mode.get() and len(selected)==1 and b.id in selected and b.text_layer and b.translation.strip():
                    tb=b.text_layer.get('box')
                    if tb and len(tb)==4:
                        bx0,by0,bx1,by1,bmx,bmy=self._outer_handle_coords(b,s)
                        # Top-center cyan handle (`+`) = Move Text (with Magenta Smart Guides)
                        self.canvas.create_oval(bmx-8,by0-8,bmx+8,by0+8,fill='#00c8ff',outline='white',width=2,tags='bubble')
                        self.canvas.create_line(bmx-4,by0,bmx+4,by0,fill='#081218',width=2,tags='bubble')
                        self.canvas.create_line(bmx,by0-4,bmx,by0+4,fill='#081218',width=2,tags='bubble')
                        # Top-right orange-gold handle (`↻`) = Rotate Text
                        self.canvas.create_oval(bx1-8,by0-8,bx1+8,by0+8,fill='#ff9f43',outline='white',width=2,tags='bubble')
                        self.canvas.create_arc(bx1-5,by0-5,bx1+5,by0+5,start=35,extent=275,style='arc',outline='#081218',width=2,tags='bubble')
                        rot_deg=float(b.text_layer.get('rotation',0) or 0.0)
                        if abs(rot_deg)>=0.2:
                            self.canvas.create_text(bx1+10,by0-6,anchor='sw',text=f'{rot_deg:+.1f}°',fill='#ff9f43',font=('Segoe UI',10,'bold'),tags='bubble')
                        # Right-middle green handle = Wrap Width
                        self.canvas.create_rectangle(bx1-5,bmy-9,bx1+5,bmy+9,fill='#98e55b',outline='white',width=1,tags='bubble')
                        # Bottom-right purple handle = Font Size
                        self.canvas.create_rectangle(bx1-6,by1-6,bx1+6,by1+6,fill='#aa83f3',outline='white',width=2,tags='bubble')
                        fsz=int(round(b.text_layer.get('font_size',24)))
                        self.canvas.create_text(bx1+8,by1+4,anchor='nw',text=f'{fsz}px',fill='#c084fc',font=('Segoe UI',10,'bold'),tags='bubble')
                badge=f'{i+1}⚠' if suspect else str(i+1)
                self.canvas.create_text(coords[0]+3,coords[1]+2,anchor='nw',text=badge,fill=color,font=('Segoe UI',12,'bold'),tags='bubble')

    def canvas_hover(self,event):
        if not self.image or self.busy or self.draw_mode.get() or self.edit_frame.get() or not self.show_text.get():
            if self.canvas.cget('cursor')!='':self.canvas.configure(cursor='')
            return
        x,y=self.canvas.canvasx(event.x),self.canvas.canvasy(event.y)
        cursor=''
        sel=self.tree.selection()
        if len(sel)==1 and self.current_bubble and self.current_bubble.id==sel[0] and self.current_bubble.text_layer:
            tb=self.current_bubble.text_layer.get('box')
            if tb and len(tb)==4:
                bx0,by0,bx1,by1,bmx,bmy=self._outer_handle_coords(self.current_bubble,self.scale)
                if abs(bx1-x)<=12 and abs(by0-y)<=12:cursor='exchange'
                elif abs(bx1-x)<=12 and abs(by1-y)<=12:cursor='size_nw_se'
                elif abs(bx1-x)<=12 and abs(bmy-y)<=14:cursor='sb_h_double_arrow'
                elif abs(bmx-x)<=12 and abs(by0-y)<=12:cursor='fleur'
        if self.canvas.cget('cursor')!=cursor:
            self.canvas.configure(cursor=cursor)

    def canvas_down(self,event):
        if not self.image:
            return
        x,y=self.canvas.canvasx(event.x),self.canvas.canvasy(event.y)
        state=getattr(event,'state',0)
        multi=bool(state&0x0005)
        alt_held=bool(state&0x20000 or state&0x0008)
        if self.edit_frame.get() and self.guard():
            for b in self.page().bubbles:
                if b.id not in self.tree.selection():continue
                points=frame_quad(b)
                for index,(px,py) in enumerate(points):
                    if abs(px*self.scale-x)<=10 and abs(py*self.scale-y)<=10:
                        self.frame_drag=(b,index,copy.deepcopy(points))
                        return
        if self.show_text.get() and not self.edit_frame.get() and not self.draw_mode.get() and not multi and self.guard():
            sel=self.tree.selection()
            if len(sel)==1 and self.current_bubble and self.current_bubble.id==sel[0] and self.current_bubble.text_layer:
                tb=self.current_bubble.text_layer.get('box')
                if tb and len(tb)==4:
                    bx0,by0,bx1,by1,bmx,bmy=self._outer_handle_coords(self.current_bubble,self.scale)
                    base_sz=float(self.current_bubble.text_layer.get('font_size',24))
                    base_w=float(getattr(self.current_bubble,'custom_width',0) or max(28.0,tb[2]-tb[0]))
                    base_rot=float(self.current_bubble.text_layer.get('rotation',0) or 0.0)
                    if abs(bx1-x)<=12 and abs(by0-y)<=12:
                        self.text_drag=('rotate',self.current_bubble,list(tb),base_sz,base_w,x,y,False,base_rot)
                        return
                    if abs(bx1-x)<=12 and abs(by1-y)<=12:
                        self.text_drag=('size',self.current_bubble,list(tb),base_sz,base_w,x,y,False,base_rot)
                        return
                    if abs(bx1-x)<=12 and abs(bmy-y)<=14:
                        self.text_drag=('width',self.current_bubble,list(tb),base_sz,base_w,x,y,False,base_rot)
                        return
                    if abs(bmx-x)<=12 and abs(by0-y)<=12:
                        self.text_drag=('move',self.current_bubble,list(tb),base_sz,base_w,x,y,False,base_rot)
                        return
                    if alt_held and bx0<=x<=bx1 and by0<=y<=by1:
                        self.text_drag=('move',self.current_bubble,list(tb),base_sz,base_w,x,y,False,base_rot)
                        return
        if self.draw_mode.get() and self.guard():
            self.drag_start=(x,y)
            self.canvas.delete('selection')
            self.canvas.create_rectangle(x,y,x,y,outline='#00c8ff',width=2,tags='selection')
        else:
            import cv2
            text_hits=[]
            if self.show_text.get() and not multi:
                s=self.scale
                for b in self.page().bubbles:
                    tb=b.text_layer.get('box') if b.text_layer else None
                    if tb and len(tb)==4 and tb[0]*s<=x<=tb[2]*s and tb[1]*s<=y<=tb[3]*s:
                        text_hits.append(b)
            hits=[b for b in self.page().bubbles if cv2.pointPolygonTest(np.asarray(frame_quad(b),np.float32),(x/self.scale,y/self.scale),False)>=0]
            chosen_b=min(hits,key=lambda b:area(b.box)) if hits else (min(text_hits,key=lambda b:area(b.box)) if text_hits else None)
            if chosen_b:
                if multi:
                    if chosen_b.id in self.tree.selection():
                        self.tree.selection_remove(chosen_b.id)
                    else:
                        self.tree.selection_add(chosen_b.id)
                else:
                    self._suppress_autoscroll=True
                    try:
                        self.tree.selection_set(chosen_b.id)
                    finally:
                        self._suppress_autoscroll=False

    def canvas_drag(self,event):
        if hasattr(self,'text_drag'):
            mode_d,b,tb,base_sz,base_w,sx0,sy0,moved,base_rot=self.text_drag
            ex,ey=self.canvas.canvasx(event.x),self.canvas.canvasy(event.y)
            if not moved and float(np.hypot(ex-sx0,ey-sy0))<DRAG_DEADZONE_PX:
                return
            self.text_drag=(mode_d,b,tb,base_sz,base_w,sx0,sy0,True,base_rot)
            s=self.scale
            tcx=(tb[0]+tb[2])/2.0*s;tcy=(tb[1]+tb[3])/2.0*s
            bx0,by0,bx1,by1,bmx,bmy=self._outer_handle_coords(b,s)
            state=getattr(event,'state',0)
            self.canvas.delete('selection')
            if mode_d=='move':
                raw_dx_img=(ex-sx0)/s;raw_dy_img=(ey-sy0)/s
                snap_dx_img,snap_dy_img,guides=self._compute_smart_snap(b,tb,raw_dx_img,raw_dy_img,state)
                dx=snap_dx_img*s;dy=snap_dy_img*s
                nx0,ny0,nx1,ny1=tb[0]*s+dx,tb[1]*s+dy,tb[2]*s+dx,tb[3]*s+dy
                ncx,ncy=(nx0+nx1)/2.0,(ny0+ny1)/2.0
                for axis,pos_i,sp0_i,sp1_i,lbl in guides:
                    if axis=='v':
                        gx=pos_i*s;gy0=sp0_i*s;gy1=sp1_i*s
                        self.canvas.create_line(gx,gy0,gx,gy1,fill=SNAP_COLOR,width=2,tags='selection')
                        self.canvas.create_text(gx+5,min(ny0-4,gy0+14),anchor='nw',text=lbl,fill=SNAP_COLOR,font=('Segoe UI',9,'bold'),tags='selection')
                    else:
                        gy=pos_i*s;gx0=sp0_i*s;gx1=sp1_i*s
                        self.canvas.create_line(gx0,gy,gx1,gy,fill=SNAP_COLOR,width=2,tags='selection')
                        self.canvas.create_text(min(nx0,gx0)+4,gy-14,anchor='nw',text=lbl,fill=SNAP_COLOR,font=('Segoe UI',9,'bold'),tags='selection')
                outline_col=SNAP_COLOR if guides else '#00c8ff'
                self.canvas.create_rectangle(nx0,ny0,nx1,ny1,outline=outline_col,width=2,dash=(4,2),tags='selection')
                self.canvas.create_line(ncx-7,ncy,ncx+7,ncy,fill=outline_col,width=2,tags='selection')
                self.canvas.create_line(ncx,ncy-7,ncx,ncy+7,fill=outline_col,width=2,tags='selection')
                hint='ล็อกเส้นไกด์ (กด Alt ค้าง=ขยับอิสระ)' if guides else 'ปล่อยเพื่อวาง (Alt=อิสระ • Esc=ยกเลิก)'
                self.canvas.create_text(nx1+6,ny0,anchor='nw',text=hint,fill=outline_col,font=('Segoe UI',10,'bold'),tags='selection')
            elif mode_d=='rotate':
                a0=float(np.degrees(np.arctan2(sy0-tcy,sx0-tcx)))
                a1=float(np.degrees(np.arctan2(ey-tcy,ex-tcx)))
                delta=(a1-a0+180.0)%360.0-180.0
                shift_h=bool(state&0x0001)
                alt_h=bool(state&0x20000 or state&0x0008)
                new_rot,snapped=_snap_rotation_deg(base_rot+delta,shift_held=shift_h,alt_held=alt_h)
                ts=b.text_layer.get('text_size') if b.text_layer else None
                hw=(float(ts[0])*0.5*s) if (ts and len(ts)==2) else max(14.0,(tb[2]-tb[0])*0.5*s)
                hh=(float(ts[1])*0.5*s) if (ts and len(ts)==2) else max(12.0,(tb[3]-tb[1])*0.5*s)
                rad=np.radians(new_rot)
                c_r,s_r=float(np.cos(rad)),float(np.sin(rad))
                pts=[]
                for lx,ly in ((-hw,-hh),(hw,-hh),(hw,hh),(-hw,hh)):
                    pts.extend([tcx+lx*c_r-ly*s_r,tcy+lx*s_r+ly*c_r])
                col_r=SNAP_COLOR if snapped else '#ff9f43'
                self.canvas.create_polygon(*pts,outline=col_r,fill='',width=2,dash=(4,2),tags='selection')
                self.canvas.create_line(tcx,tcy,ex,ey,fill=col_r,width=1,dash=(2,2),tags='selection')
                snap_tag=' [ล็อกมุม]' if snapped else ''
                self.canvas.create_text(ex+10,ey-8,anchor='nw',text=f'หมุน: {new_rot:+.1f}°{snap_tag}',fill=col_r,font=('Segoe UI',11,'bold'),tags='selection')
            elif mode_d=='size':
                orig_d=max(16.0,float(np.hypot(bx1-bmx,by1-bmy)))
                cur_d=max(8.0,float(np.hypot(ex-bmx,ey-bmy)))
                ratio=cur_d/orig_d
                new_sz=int(np.clip(round(base_sz*ratio),6,180))
                self.font_size_var.set(new_sz)
                hw=max(10.0,(tb[2]-tb[0])/2.0*s*ratio);hh=max(10.0,(tb[3]-tb[1])/2.0*s*ratio)
                self.canvas.create_rectangle(tcx-hw,tcy-hh,tcx+hw,tcy+hh,outline='#c084fc',width=2,dash=(4,2),tags='selection')
                self.canvas.create_text(bx1+8,ey,anchor='w',text=f'ขนาดฟอนต์: {new_sz} px',fill='#c084fc',font=('Segoe UI',11,'bold'),tags='selection')
            else:
                dx_img=(ex-sx0)/s
                new_w=int(np.clip(round(base_w+dx_img*2.0),28,self.image.width-8))
                half_w=new_w*s/2.0
                hh=max(12.0,(tb[3]-tb[1])/2.0*s)
                self.canvas.create_rectangle(tcx-half_w,tcy-hh,tcx+half_w,tcy+hh,outline='#98e55b',width=2,dash=(4,2),tags='selection')
                self.canvas.create_text(tcx+half_w+8,tcy,anchor='w',text=f'ความกว้างบรรทัด: {new_w} px',fill='#98e55b',font=('Segoe UI',11,'bold'),tags='selection')
            return
        if hasattr(self,'frame_drag'):
            b,index,points=self.frame_drag
            points[index]=[max(0,min(self.image.width,self.canvas.canvasx(event.x)/self.scale)),
                           max(0,min(self.image.height,self.canvas.canvasy(event.y)/self.scale))]
            self.canvas.delete('selection')
            self.canvas.create_polygon(*[v*self.scale for point in points for v in point],fill='',outline='#00c8ff' if valid_quad(points) else '#ff6666',width=3,tags='selection')
            return
        if self.draw_mode.get() and hasattr(self,'drag_start'):
            self.canvas.coords('selection',*self.drag_start,self.canvas.canvasx(event.x),self.canvas.canvasy(event.y))

    def canvas_up(self,event):
        if hasattr(self,'text_drag'):
            mode_d,b,tb,base_sz,base_w,sx0,sy0,moved,base_rot=self.text_drag;del self.text_drag
            self.canvas.delete('selection')
            if not moved:
                return
            ex,ey=self.canvas.canvasx(event.x),self.canvas.canvasy(event.y)
            s=self.scale
            state=getattr(event,'state',0)
            bx0,by0,bx1,by1,bmx,bmy=self._outer_handle_coords(b,s)
            if mode_d=='move':
                raw_dx_img=(ex-sx0)/s;raw_dy_img=(ey-sy0)/s
                dx_img,dy_img,_=self._compute_smart_snap(b,tb,raw_dx_img,raw_dy_img,state)
                if abs(dx_img)>=0.5 or abs(dy_img)>=0.5:
                    self._move_bubble_text(b,dx_img,dy_img)
            elif mode_d=='rotate':
                tcx=(tb[0]+tb[2])/2.0*s;tcy=(tb[1]+tb[3])/2.0*s
                a0=float(np.degrees(np.arctan2(sy0-tcy,sx0-tcx)))
                a1=float(np.degrees(np.arctan2(ey-tcy,ex-tcx)))
                delta=(a1-a0+180.0)%360.0-180.0
                shift_h=bool(state&0x0001)
                alt_h=bool(state&0x20000 or state&0x0008)
                new_rot,_=_snap_rotation_deg(base_rot+delta,shift_held=shift_h,alt_held=alt_h)
                self._rotate_bubble_text(b,new_rot)
            elif mode_d=='size':
                orig_d=max(16.0,float(np.hypot(bx1-bmx,by1-bmy)))
                cur_d=max(8.0,float(np.hypot(ex-bmx,ey-bmy)))
                new_sz=int(np.clip(round(base_sz*(cur_d/orig_d)),6,180))
                self.font_size_var.set(new_sz)
                self._apply_text_style_to_targets([b],custom_size=new_sz)
            else:
                dx_img=(ex-sx0)/s
                new_w=int(np.clip(round(base_w+dx_img*2.0),28,self.image.width-8))
                keep_sz=int(round(b.text_layer.get('font_size',base_sz)))
                self._apply_text_style_to_targets([b],custom_size=keep_sz,custom_width=new_w)
            return
        if hasattr(self,'frame_drag'):
            self.canvas_drag(event)
            b,index,points=self.frame_drag;del self.frame_drag
            self.canvas.delete('selection')
            self.set_frame(b,points)
            return
        if not hasattr(self,'drag_start'):
            return
        x,y=self.drag_start
        del self.drag_start
        ex,ey=self.canvas.canvasx(event.x),self.canvas.canvasy(event.y)
        self.canvas.delete('selection')
        box=clip_box([min(x,ex)/self.scale,min(y,ey)/self.scale,max(x,ex)/self.scale,max(y,ey)/self.scale],*self.image.size)
        if area(box)>100 and self.guard():
            self.snapshot()
            b=Bubble(box=box,engine='manual')
            self.page().bubbles.append(b)
            self.page().bubbles.sort(key=lambda b:(b.box[1],b.box[0]))
            self.dirty=True
            self.refresh_tree(b.id)
            self.render()
            self.status.set('เพิ่มกรอบแล้ว — กด RapidOCR กรอบนี้ หรือเลื่อนคำแปลเข้ามาใส่ได้เลย')

    def _move_bubble_text(self,bubble,dx_img,dy_img):
        if not self.guard() or not self.image:return
        self.snapshot()
        from text_layout import move_text_layer
        work_im=self.cleaned_preview if self.cleaned_preview is not None else self.image
        try:
            bubble.text_layer=move_text_layer(work_im,bubble,(dx_img,dy_img))
            self.dirty=True
            self._view_cache=None
            self.fill_editor(bubble)
            self.render()
            self.status.set('ย้ายตำแหน่ง Text แล้ว (ปุ่ม "ออโต้" รีเซ็ตกลับกลางบับเบิ้ล • Ctrl+Z ย้อนกลับ)')
        except Exception as exc:
            messagebox.showwarning('ย้ายตำแหน่ง Text',str(exc))

    def _rotate_bubble_text(self,bubble,new_angle):
        if not self.guard() or not self.image:return
        self.snapshot()
        from text_layout import rotate_text_layer
        work_im=self.cleaned_preview if self.cleaned_preview is not None else self.image
        try:
            bubble.text_layer=rotate_text_layer(work_im,bubble,new_angle)
            self.dirty=True
            self._view_cache=None
            self.fill_editor(bubble)
            self.render()
            self.status.set(f'หมุนประโยคเป็น {new_angle:+.1f}° แล้ว (กดปุ่ม "ออโต้" เพื่อตั้งตรง/คืนค่า • Ctrl+Z ย้อนกลับ)')
        except Exception as exc:
            messagebox.showwarning('หมุนประโยค Text',str(exc))

    def toggle_frame_edit(self):
        if self.edit_frame.get():self.draw_mode.set(False)
        self.render()
        self.status.set('เลือกกรอบ แล้วลากจุดมุมทั้ง 4 ให้ครอบคำเอียง จากนั้นสร้าง Mask ใหม่')

    def set_frame(self,bubble,points):
        if not self.guard() or not valid_quad(points):
            self.status.set('มุมกรอบไขว้กันหรือเล็กเกินไป — ยังไม่ได้เปลี่ยนกรอบ');return
        if any(x<0 or y<0 or x>self.image.width or y>self.image.height for x,y in points):
            self.status.set('กรอบเลยขอบภาพ กรุณาลดมุมหมุนหรือปรับจุดมุม');return
        if np.allclose(points,frame_quad(bubble)):return
        self.commit_editor();self.snapshot()
        bubble.quad=copy.deepcopy(points);bubble.box=quad_bounds(points)
        bubble.lines=[];bubble.text_layer={};bubble.text_offset=[];bubble.frame_type=bubble.frame_type if bubble.frame_type_locked else '';bubble.custom_rotation=None
        self.clean_prepared.pop((self.page().id,bubble.id),None)
        self.dirty=True;self.refresh_tree(bubble.id);self.refresh_clean_view()
        self.status.set('ปรับกรอบแล้ว — อ่านคำซ้ำ / สร้าง Mask ใหม่ก่อนคลีน และจัด Text ใหม่ (Undo ย้อนกลับได้)')

    def rotate_frame(self):
        if not self.guard() or not self.current_bubble:return
        angle=simpledialog.askfloat('หมุนกรอบ','หมุนเพิ่มกี่องศา? ค่าบวกหมุนตามเข็มนาฬิกา\nจากนั้นลากจุดมุมเพื่อปรับให้พอดีได้',initialvalue=0,minvalue=-90,maxvalue=90,parent=self.root)
        if angle is None:return
        q=np.asarray(frame_quad(self.current_bubble),float);center=q.mean(axis=0)
        rad=np.deg2rad(angle);rotation=np.array([[np.cos(rad),-np.sin(rad)],[np.sin(rad),np.cos(rad)]])
        self.edit_frame.set(True);self.draw_mode.set(False)
        self.set_frame(self.current_bubble,((q-center)@rotation.T+center).tolist());self.render()

    def merge(self):
        if not self.guard() or not self.page():
            return
        ids=set(self.tree.selection())
        bs=[b for b in self.page().bubbles if b.id in ids]
        if len(bs)<2:
            self.status.set('กด Ctrl แล้วเลือกอย่างน้อย 2 รายการเพื่อรวม')
            return
        self.snapshot()
        b=Bubble(box=union_box([b.box for b in bs]),text=join_lines([b.text for b in bs]),
                 translation=join_lines([b.translation for b in bs]),lines=[l for b in bs for l in b.lines],
                 score=min(b.score for b in bs),engine='manual merge',
                 font_choice=getattr(bs[0],'font_choice',DEFAULT_FONT_CHOICE))
        first=self.page().bubbles.index(bs[0])
        self.page().bubbles=[v for v in self.page().bubbles if v.id not in ids]
        self.page().bubbles.insert(first,b)
        self.changed(b.id)

    def split(self):
        if not self.guard() or not self.current_bubble:
            return
        self.commit_editor()
        b=self.current_bubble
        texts=[t.strip() for t in b.text.splitlines() if t.strip()]
        if len(texts)<2:
            self.status.set('กด Enter แบ่งข้อความในช่องคำต้นฉบับก่อน แล้วกดแยกบรรทัด')
            return
        translations=b.translation.splitlines()
        if b.translation.strip() and len(translations)!=len(texts):
            messagebox.showinfo('แยกคำแปลด้วย','แบ่งคำแปลให้มีจำนวนบรรทัดตรงกับต้นฉบับก่อน เพื่อเก็บคู่แปลให้ถูกต้อง')
            return
        self.snapshot()
        pos=self.page().bubbles.index(b)
        new=[Bubble(box=list(b.box),text=t,translation=translations[i] if i<len(translations) else '',
                    engine='manual split',quad=copy.deepcopy(b.quad),
                    font_choice=getattr(b,'font_choice',DEFAULT_FONT_CHOICE)) for i,t in enumerate(texts)]
        self.page().bubbles[pos:pos+1]=new
        self.changed(new[0].id)

    def delete(self):
        if self.guard() and self.page() and self.tree.selection():
            self.snapshot()
            ids=set(self.tree.selection())
            self.page().bubbles=[b for b in self.page().bubbles if b.id not in ids]
            self.changed()

    def delete_and_restore_art(self):
        if not self.guard() or not self.page():
            return
        ids=set(self.tree.selection())
        if not ids and self.current_bubble:
            ids={self.current_bubble.id}
        if not ids:
            messagebox.showinfo('ลบกรอบ+คืนภาพ','คลิกเลือกกรอบเสียงเอฟเฟกต์หรือกรอบที่เกินมาก่อนกดปุ่มนี้');return
        self.commit_editor()
        p=self.page()
        removed_bs=[b for b in p.bubbles if b.id in ids]
        if not removed_bs:
            return
        self.snapshot()
        kept_patches=[]
        restored_n=0
        for pt in p.clean_patches:
            if pt.get('bubble_id') in ids:
                restored_n+=1
                continue
            pb=pt.get('box')
            if pb and len(pb)==4 and any(intersection(pb,rb.box)/max(1.0,float(min(area(pb),area(rb.box))))>=0.55 for rb in removed_bs):
                restored_n+=1
                continue
            kept_patches.append(pt)
        p.clean_patches=kept_patches
        for bid in ids:
            self.clean_prepared.pop((p.id,bid),None)
        p.bubbles=[b for b in p.bubbles if b.id not in ids]
        self.current_bubble=None
        self.dirty=True
        self.fill_editor(None)
        self.refresh_tree()
        self.refresh_output()
        self.refresh_clean_view()
        self.status.set(f'ลบกรอบ {len(removed_bs)} ช่อง และคืนภาพต้นฉบับ {restored_n} จุดเรียบร้อย (Ctrl+Z ย้อนกลับได้)')

    def _relayout_changed_bubbles(self,changed_pairs,status_note):
        from text_layout import layout_text
        to_layout=[]
        for p_obj,b_obj in changed_pairs:
            b_obj.text_offset=[]
            if not b_obj.translation.strip():
                b_obj.text_layer={}
            else:
                to_layout.append((p_obj,b_obj))
        self.dirty=True
        if not to_layout:
            self._view_cache=None
            self.refresh_tree(self.tree.selection())
            self.refresh_output()
            self.refresh_clean_view()
            self.status.set(status_note)
            return
        cur_p=self.page()
        if len(to_layout)<=10 and cur_p and all(p_obj.id==cur_p.id for p_obj,_ in to_layout) and self.image is not None:
            work_im=self.cleaned_preview if self.cleaned_preview is not None else self.image
            for _,b_obj in to_layout:
                chosen=getattr(b_obj,'font_choice',None) or DEFAULT_FONT_CHOICE
                try:
                    b_obj.text_layer=layout_text(work_im,b_obj,b_obj.translation,font_choice=chosen,
                                                 custom_size=getattr(b_obj,'custom_size',0),
                                                 custom_width=getattr(b_obj,'custom_width',0),
                                                 custom_rotation=getattr(b_obj,'custom_rotation',None))
                except Exception as exc:
                    self.log(f'จัด Text กรอบ {b_obj.id[:6]}: {exc}')
            self._view_cache=None
            self.show_text.set(True)
            keep_sel=list(self.tree.selection())
            self.refresh_tree(keep_sel)
            self.refresh_output()
            self.refresh_clean_view()
            self._suppress_autoscroll=True
            try:self.select_bubble()
            finally:self._suppress_autoscroll=False
            self.status.set(status_note)
        else:
            keep_sel=list(self.tree.selection())
            self.refresh_tree(keep_sel)
            self.refresh_output()
            tasks=[(p_obj.id,p_obj.path,copy.deepcopy(b_obj),b_obj.translation,
                    getattr(b_obj,'font_choice',None) or DEFAULT_FONT_CHOICE) for p_obj,b_obj in to_layout]
            self._run_text_layout_tasks(tasks,summary_prefix=status_note)

    def shift_translations(self,direction=1):
        if not self.guard() or not self.page() or not self.page().bubbles:
            return
        self.commit_editor()
        cur_p=self.page()
        sel_ids=list(self.tree.selection())
        if not sel_ids and self.current_bubble:
            sel_ids=[self.current_bubble.id]
        if not sel_ids:
            messagebox.showinfo('เลื่อนลำดับคำแปล','คลิกเลือกกรอบจุดเริ่มต้นที่คำแปลเริ่มรวนก่อนกดเลื่อนคำแปล');return

        id_set=set(sel_ids)
        page_indices=[i for i,b in enumerate(cur_p.bubbles) if b.id in id_set]
        if not page_indices:
            return

        if len(page_indices)>=2:
            chain=[(cur_p,cur_p.bubbles[i]) for i in range(min(page_indices),max(page_indices)+1)]
        else:
            start_i=page_indices[0]
            chain=[(cur_p,b) for b in cur_p.bubbles[start_i:]]
            later_pages=[p for p in self.pages[self.page_index+1:] if p.bubbles]
            if later_pages:
                need_ask=(direction>0 and bool(chain[-1][1].translation.strip())) or (direction<0 and len(chain)==1)
                if need_ask:
                    ans=messagebox.askyesnocancel(
                        'ขอบเขตการเลื่อนคำแปล',
                        'ต้องการเลื่อนคิวคำแปลต่อเนื่องไปจนถึงหน้าท้ายสุดของตอนนี้เลยหรือไม่?\n\n'
                        '• Yes = เลื่อนต่อเนื่องไปทุกหน้าถัดไปทั้งตอน\n'
                        '• No = เลื่อนเฉพาะในหน้าปัจจุบันเท่านั้น',
                    )
                    if ans is None:
                        return
                    if ans is True:
                        for lp in later_pages:
                            chain.extend((lp,lb) for lb in lp.bubbles)

        if len(chain)<2:
            messagebox.showinfo('เลื่อนลำดับคำแปล','ต้องมีอย่างน้อย 2 กรอบในช่วงที่จะเลื่อนคำแปล');return

        self.snapshot()
        old_trns=[b.translation for _,b in chain]
        if direction>0:
            new_trns=['']+old_trns[:-1]
            tail_overflow=old_trns[-1].strip()
            if tail_overflow:
                new_trns[-1]=(new_trns[-1]+'\n'+tail_overflow).strip() if new_trns[-1].strip() else tail_overflow
            note=f'เลื่อนคำแปลลง ↓ {len(chain)} กรอบแล้ว (Ctrl+Z ย้อนกลับได้)'
        else:
            new_trns=old_trns[1:]+['']
            note=f'ดึงคำแปลขึ้น ↑ {len(chain)} กรอบแล้ว (Ctrl+Z ย้อนกลับได้)'

        changed=[]
        for (p_obj,b_obj),nt in zip(chain,new_trns):
            if b_obj.translation!=nt:
                b_obj.translation=nt
                changed.append((p_obj,b_obj))
        self._relayout_changed_bubbles(changed,note)

    def swap_translations(self):
        if not self.guard() or not self.page() or len(self.page().bubbles)<2:
            return
        self.commit_editor()
        cur_p=self.page()
        sel_ids=set(self.tree.selection())
        chosen=[b for b in cur_p.bubbles if b.id in sel_ids]
        if len(chosen)==1:
            idx=cur_p.bubbles.index(chosen[0])
            other_idx=idx+1 if idx+1<len(cur_p.bubbles) else idx-1
            chosen=[cur_p.bubbles[idx],cur_p.bubbles[other_idx]]
        elif len(chosen)!=2:
            messagebox.showinfo('สลับคำแปล ⇄','กด Ctrl+คลิก เลือก 2 กรอบที่ต้องการสลับคำแปล (หรือคลิก 1 กรอบเพื่อสลับกับกรอบถัดไป)');return
        b1,b2=chosen[0],chosen[1]
        self.snapshot()
        b1.translation,b2.translation=b2.translation,b1.translation
        self._relayout_changed_bubbles([(cur_p,b1),(cur_p,b2)],'สลับคำแปล 2 กรอบเรียบร้อย (Ctrl+Z ย้อนกลับได้)')

    def move(self,delta):
        if not self.guard() or not self.current_bubble:
            return
        bs=self.page().bubbles
        pos=bs.index(self.current_bubble)
        if 0<=pos+delta<len(bs):
            self.snapshot()
            bs[pos],bs[pos+delta]=bs[pos+delta],bs[pos]
            self.changed(bs[pos+delta].id)

    def changed(self,selected=None):
        self.current_bubble=None
        self.dirty=True
        self.fill_editor(None)
        self.refresh_tree(selected)
        self.refresh_output()
        self.render()

    def use_candidate(self):
        if not self.guard() or not self.current_bubble:
            return
        index=self.candidate_combo.current()
        if index<0:
            return
        self.snapshot()
        b=self.current_bubble
        c=b.candidates[index]
        old={'text':b.text,'score':b.score,'engine':b.engine}
        b.text,b.score,b.engine=c['text'],c['score'],c['engine']
        b.candidates=[old]+[v for i,v in enumerate(b.candidates) if i!=index]
        b.reviewed=False
        self.dirty=True
        self.fill_editor(b)
        self.refresh_tree(b.id)
        self.refresh_output()

    def choose_credential(self):
        path=filedialog.askopenfilename(title='เลือก Service Account JSON',filetypes=[('JSON','*.json')])
        if path:
            self.credential.set(path)

    def settings(self):
        return (self.mode.get(),self.lang.get(),self.gpu.get(),self.neural.get(),self.retry.get(),self.fast.get(),self.bubble_only.get(),self.type_scan.get())

    def get_pipeline(self,settings):
        if self.pipeline is None or settings!=self.pipeline_settings:
            self.pipeline=None
            self.pipeline=LocalPipeline(self.cache,*settings[:5],fast=settings[5],bubble_only=settings[6],type_scan=len(settings)>7 and settings[7],
                log=lambda s:self.events.put(('log',s)),cancel=self.cancel)
            self.pipeline_settings=settings
        return self.pipeline

    def launch(self,work,completion_sound=None):
        if not self.guard():
            return
        self.commit_editor()
        self.busy=True
        self.job_started=self.stage_started=time.monotonic()
        self.stage_text='เริ่มงาน / เตรียมโมเดล'
        self.job_failed=False
        self.completion_note=''
        self.cancel.clear()
        self.source.configure(state='disabled')
        self.thai.configure(state='disabled')
        self.progress['value']=0
        def run():
            completed=False
            try:
                completed=work() is not False
            except Cancelled:
                self.events.put(('log','หยุดแล้ว เก็บผลที่อ่านเสร็จรวมถึงหน้าที่ยังอ่านไม่ครบไว้'))
            except Exception as exc:
                self.events.put(('error',f'{type(exc).__name__}: {exc}'))
            finally:
                self.events.put(('done',completion_sound if completed else None))
        threading.Thread(target=run,daemon=True).start()

    def start_scan(self,all_pages=False):
        if not self.guard() or not self.page():
            return
        target=list(self.pages) if all_pages else [self.page()]
        if any(p.bubbles for p in target) and not messagebox.askyesno('อ่านใหม่','OCR ใหม่จะแทนข้อความและคำแปลของหน้าที่เลือก ต้องการอ่านใหม่หรือไม่? (ย้อนกลับได้)'):
            return
        self.snapshot()
        settings=self.settings()
        tasks=[(p.id,p.path) for p in target]
        def work():
            pipeline=self.get_pipeline(settings)
            for index,(pid,path) in enumerate(tasks):
                pipeline.check()
                self.events.put(('log',f'หน้า {index+1}/{len(tasks)}: {Path(path).name}'))
                pipeline.on_progress=lambda fraction, idx=index:self.events.put(('progress',(idx+fraction)/len(tasks)*100))
                pipeline.on_partial=lambda bs, page_id=pid:self.events.put(('partial',(page_id,bs,'ผลบางส่วน — ยังอ่านไม่ครบหน้า')))
                try:
                    image=load_image(path)
                    if image.width < 400:
                        self.events.put(('log',f'ภาพกว้างเพียง {image.width} px — แนะนำภาพต้นฉบับความละเอียดเต็ม; OCR อาจอ่านผิด'))
                    bs=pipeline.scan(image)
                    self.events.put(('page',(pid,bs,'' if bs else 'ไม่พบข้อความ — ลากเพิ่มกรอบแล้วอ่านซ้ำในเครื่องได้')))
                except Cancelled:
                    raise
                except Exception as exc:
                    self.events.put(('page_error',(pid,str(exc))))
                self.events.put(('progress',(index+1)/len(tasks)*100))
            pipeline.on_partial=lambda bs:None
            pipeline.on_progress=lambda fraction:None
        self.launch(work,completion_sound='ocr_all' if all_pages else None)

    def retry_bubble(self):
        if not self.guard() or not self.current_bubble or not self.image:
            return
        self.snapshot()
        b=copy.deepcopy(self.current_bubble)
        pid=self.page().id
        path=self.page().path
        settings=self.settings()
        def work():
            pipeline=self.get_pipeline(settings)
            pipeline.on_partial=lambda bs:None
            pipeline.on_progress=lambda fraction:None
            candidates=pipeline.candidates(load_image(path),b)
            self.events.put(('candidates',(pid,b.id,candidates)))
        self.launch(work)

    def rapid_bubble(self):
        if not self.guard() or not self.current_bubble or not self.image:
            return
        self.snapshot()
        b=copy.deepcopy(self.current_bubble)
        pid, path, lang=self.page().id, self.page().path, self.lang.get()
        def work():
            if self.rapid_pipeline is None or self.rapid_lang != lang:
                self.rapid_pipeline=None
                self.rapid_pipeline=LocalPipeline(self.cache,mode='rapid',lang=lang,
                    neural=False,retry=False,log=lambda s:self.events.put(('log',s)),cancel=self.cancel)
                self.rapid_lang=lang
            self.events.put(('log','RapidOCR อ่านเฉพาะกรอบที่เลือกด้วย CPU…'))
            candidates=self.rapid_pipeline.candidates(load_image(path),b)
            self.events.put(('candidates',(pid,b.id,candidates)))
        self.launch(work)

    def google_bubble(self):
        if not self.guard() or not self.current_bubble or not self.image:
            return
        self.snapshot()
        b=copy.deepcopy(self.current_bubble)
        pid=self.page().id
        crop,_=ocr_crop(self.image,b,pad=8)
        key,lang=self.credential.get(),self.lang.get()
        def work():
            lines=self.google.read(crop,key,lang)
            text=join_lines([l.text for l in order_lines(lines)])
            candidates=[{'text':text,'score':min(l.score for l in lines),'engine':'Google Vision'}] if text else []
            self.events.put(('candidates',(pid,b.id,candidates)))
        self.launch(work)

    def stop(self):
        if self.busy:
            self.cancel.set()
            self.status.set('กำลังหยุดหลังโมเดลจบคำสั่งปัจจุบัน…')

    def _throttled_clean_refresh(self):
        now=time.monotonic()
        if not self.busy or (now-self._last_clean_refresh)>=0.35:
            self._last_clean_refresh=now
            self._clean_view_dirty=False
            self.refresh_clean_view()
        else:
            self._clean_view_dirty=True

    def _poll(self):
        try:
            while True:
                kind,data=self.events.get_nowait()
                if kind=='log':
                    self.log(data)
                    self.stage_text=str(data).splitlines()[0][:150]
                    self.stage_started=time.monotonic()
                    self.status.set(self.stage_text)
                elif kind=='sound_log':
                    self.log(data)
                elif kind=='error':
                    self.job_failed=True
                    self.log(data)
                    self.status.set('เกิดข้อผิดพลาด — ดูรายละเอียดใน Log')
                    messagebox.showerror('OCR',data)
                elif kind=='progress':
                    self.progress['value']=data
                elif kind=='clean_mask':
                    pid,bid,region=data
                    self.clean_prepared[(pid,bid)]=region
                    self.dirty=True
                    if self.page() and pid==self.page().id:
                        self.show_masks.set(True);self._throttled_clean_refresh()
                elif kind=='clean_patch':
                    pid,patch=data
                    p=next((v for v in self.pages if v.id==pid),None)
                    if p:
                        p.clean_patches=[old for old in p.clean_patches if not patch.get('bubble_id') or old.get('bubble_id')!=patch['bubble_id']]
                        p.clean_patches.append(patch);self.dirty=True
                        if not patch.get('quality',{}).get('needs_review'):
                            self.clean_prepared.pop((pid,patch.get('bubble_id')),None)
                        if p is self.page():
                            self.show_cleaned.set(True);self._throttled_clean_refresh()
                elif kind in ('clean_summary','job_summary'):
                    self.completion_note=data
                    self.log(data)
                elif kind=='psd_error':
                    messagebox.showwarning('ส่งออก PSD ไม่สำเร็จบางหน้า',str(data))
                elif kind=='preset_warning':
                    messagebox.showwarning('บางฟอนต์ในโปรไฟล์ยังไม่ได้ติดตั้งในเครื่อง',str(data))
                elif kind=='mtp_loaded':
                    pages_new,mtp_src,summary=data
                    self.current_bubble=None
                    self.pages=pages_new
                    self.clean_prepared={};self.mask_preview=None
                    self.project_path=None
                    self.undo_stack=[]
                    self.dirty=True
                    self.show_cleaned.set(True);self.show_text.set(True)
                    self.page_index=-1
                    self.refresh_pages()
                    if pages_new:
                        self.select_page(0)
                    else:
                        self.image=None;self.fill_editor(None);self.refresh_tree();self.refresh_output();self.render()
                    self.completion_note=summary
                    self.log(summary)
                elif kind=='stitch_done':
                    pages_new,drafts_new,summary=data
                    self.current_bubble=None
                    self.pages=pages_new
                    self.clean_prepared=drafts_new;self.mask_preview=None
                    self.dirty=True
                    self.show_cleaned.set(True);self.show_text.set(True)
                    self.page_index=-1
                    self.refresh_pages()
                    if pages_new:
                        self.select_page(0)
                    self.completion_note=summary
                    self.log(summary)
                elif kind=='text_results':
                    for record in data:
                        pid,bid,translation,layer,error=record[:5]
                        font_used=record[5] if len(record)>5 else None
                        ftype_used=record[6] if len(record)>6 else None
                        reset_auto_c=bool(record[7]) if len(record)>7 else False
                        p=next((v for v in self.pages if v.id==pid),None)
                        b=next((v for v in p.bubbles if v.id==bid),None) if p else None
                        if b:
                            b.translation=translation;self.dirty=True
                            if reset_auto_c:
                                b.custom_size=0;b.custom_width=0;b.text_offset=[];b.custom_rotation=None
                            if len(record)>8 and record[8]:b.frame_type_locked=True
                            if ftype_used is not None:b.frame_type=ftype_used
                            if font_used:b.font_choice=font_used
                            if layer:
                                b.text_layer=layer
                                if not font_used and layer.get('font_choice'):b.font_choice=layer['font_choice']
                                b.text_offset=list(layer.get('text_offset',[]))
                                if 'custom_rotation' in layer:b.custom_rotation=layer['custom_rotation']
                            if error:self.log('Text '+bid+': '+error)
                    self._view_cache=None
                    self.show_text.set(True);self.show_cleaned.set(True)
                    self.refresh_output();self.refresh_clean_view()
                    if self.page() and self.tree.selection():
                        self._suppress_autoscroll=True
                        try:self.select_bubble()
                        finally:self._suppress_autoscroll=False
                elif kind=='clean_readiness':
                    self.lama_note.set(data['message'])
                    self.log(data['message'])
                elif kind=='seg_readiness':
                    self.log(data['message'])
                    messagebox.showinfo('AI จับทรงบับเบิ้ล (ONNX)',data['message'])
                elif kind=='clean_export':
                    self.completion_note=data
                    self.log(data)
                    messagebox.showinfo('ส่งออกภาพคลีน',data)
                elif kind in ('page','page_error','partial'):
                    pid=data[0]
                    p=next((p for p in self.pages if p.id==pid),None)
                    if p:
                        if kind in ('page','partial'):
                            p.bubbles,p.error,p.processed=data[1],data[2],kind=='page'
                            self.clean_prepared={key:value for key,value in self.clean_prepared.items() if key[0]!=pid}
                        else:
                            self.job_failed=True
                            p.processed=False
                            p.error='OCR ไม่สำเร็จ: '+data[1]
                            self.log(p.error)
                        self.dirty=True
                        if p is self.page():
                            self.current_bubble=None
                            self.fill_editor(None)
                            self.refresh_tree()
                            self.render()
                            self.page_note.configure(text=p.error or f'{len(p.bubbles)} กรอบ')
                        self.refresh_output()
                elif kind=='candidates':
                    pid,bid,cs=data
                    p=next((p for p in self.pages if p.id==pid),None)
                    b=next((b for b in p.bubbles if b.id==bid),None) if p else None
                    if b:
                        seen={c['text'] for c in b.candidates}
                        b.candidates.extend(c for c in cs if c['text'] not in seen and c['text']!=b.text)
                        if not b.text and cs:
                            c=cs[0]
                            b.text,b.score,b.engine=c['text'],c['score'],c['engine']
                            b.candidates=[v for v in b.candidates if v['text']!=b.text]
                        self.dirty=True
                        self.log('อ่านกรอบเสร็จแล้ว ตรวจผลในข้อความทางเลือก' if cs else 'ไม่พบผลอ่านใหม่ ลองแก้กรอบหรือพิมพ์ข้อความ')
                        if p is self.page():
                            self.refresh_tree(b.id)
                            self.fill_editor(b)
                        self.refresh_output()
                elif kind=='done':
                    self.busy=False
                    if self._clean_view_dirty:
                        self._clean_view_dirty=False
                        self.refresh_clean_view()
                    if self.page() and len(self.tree.selection()) > 1:
                        selected_bs=[b for b in self.page().bubbles if b.id in set(self.tree.selection())]
                        self.fill_editor(self.current_bubble, selected_count=len(selected_bs), selected_bubbles=selected_bs)
                    else:
                        self.fill_editor(self.current_bubble)
                    hits=self.google.hits+(self.pipeline.hits if self.pipeline else 0)+(self.rapid_pipeline.hits if self.rapid_pipeline else 0)
                    self.stats.set(f'Google Vision: {self.google.calls} / {self.google.limit} ครั้ง | แคช: {hits}')
                    elapsed=int(time.monotonic()-self.job_started)
                    if self.job_failed:
                        self.status.set(f'จบงานโดยมีข้อผิดพลาด ({elapsed} วินาที) — ตรวจ Log; ผลบางหน้าอาจยังไม่ครบ')
                    elif self.cancel.is_set():
                        self.status.set(f'หยุดแล้ว ({elapsed} วินาที) — '+(self.completion_note or 'เก็บผลบางส่วนไว้ ตรวจแก้หรือบันทึกได้'))
                    else:
                        self.progress['value']=100
                        self.status.set(self.completion_note or f'เสร็จแล้ว ({elapsed} วินาที) — พร้อมตรวจแก้และส่งออก')
                        if data:self.sounds.play(data)
        except queue.Empty:
            pass
        if self.busy:
            now=time.monotonic()
            elapsed=int(now-self.job_started)
            stage=int(now-self.stage_started)
            prefix='กำลังหยุดหลังคำสั่งปัจจุบัน' if self.cancel.is_set() else self.stage_text
            self.status.set(f'{prefix} | รวม {elapsed//60}:{elapsed%60:02d} | ขั้นนี้ {stage} วินาที')
        self.poll_id = self.root.after(100,self._poll)

    def refresh_clean_view(self):
        self.cleaned_preview=None;self.mask_preview=None;self._view_cache=None
        p=self.page()
        if self.image is not None and p:
            if self.show_cleaned.get() and p.clean_patches:
                try:self.cleaned_preview=compose_page(self.image,p.clean_patches)
                except Exception as exc:self.log('เปิดผลคลีนไม่ได้: '+str(exc))
            if self.show_masks.get():
                self.mask_preview=(self.cleaned_preview or self.image).copy()
                for (pid,bid),region in self.clean_prepared.items():
                    if pid!=p.id or not any(b.id==bid for b in p.bubbles):continue
                    box=region.box;crop=np.array(self.mask_preview.crop(box))
                    marked=region.mask>0
                    crop[marked]=(crop[marked]*.4+np.array([255,35,35])*.6).astype(np.uint8)
                    self.mask_preview.paste(Image.fromarray(crop),(box[0],box[1]))
        self.render()

    def check_clean_readiness(self):
        if not self.guard():return
        def work():
            self.events.put(('log','กำลังโหลดและทดสอบ LaMa ในเครื่อง…'))
            status=self.cleaner.check_lama(force=True)
            self.events.put(('clean_readiness',status))
            self.events.put(('clean_summary',status['message']))
        self.launch(work)

    def prepare_bubble_seg_ai(self):
        if not self.guard():return
        from text_layout import BUBBLE_SEGMENTER, bubble_seg_model_path
        has_file=bubble_seg_model_path().is_file() or bubble_seg_model_path().with_name('manga109-640.ts').is_file()
        if not has_file:
            if not messagebox.askyesno('โหลดโมเดล AI จับทรงบับเบิ้ล (YOLOv8m-seg ONNX)',
                'ต้องการดาวน์โหลดโมเดล AI จับทรงบับเบิ้ล (~104 MB) เก็บไว้ในเครื่องหรือไม่?\n\n'
                '• โหลดครั้งเดียว ใช้งานแบบ Offline ได้ตลอดไป\n'
                '• รันผ่าน ONNX Runtime ที่มีในเครื่องแล้ว ไม่ต้องลง pip เพิ่ม\n'
                '• ช่วยให้วางคำแปลไทยตรงกลางลูกโป่งคำพูดแม้กรอบส้มเบี้ยวหรือเป็นบับเบิ้ลเส้นแฉก'):
                return
        def work():
            if not bubble_seg_model_path().is_file():
                status=BUBBLE_SEGMENTER.download(progress=lambda m:self.events.put(('log',m)),cancel=self.cancel)
            else:
                self.events.put(('log','กำลังตรวจสอบและทดสอบโมเดล AI จับทรงบับเบิ้ล (YOLOv8m-seg ONNX)…'))
                status=BUBBLE_SEGMENTER.check(force=True)
            self.events.put(('seg_readiness',status))
            self.events.put(('job_summary',status['message']))
        self.launch(work)

    def clean_help(self):
        messagebox.showinfo('วิธี CLEAN',
            '1. สร้างมาสก์ แล้วตรวจสีแดงให้ครอบคำและเงา โดยไม่ทับเส้นกรอบ\n'
            '2. ตรวจความพร้อม LaMa ก่อนงานจำนวนมาก แล้วกดคลีนมาสก์\n'
            '3. ดูผลและจำนวนที่ต้องตรวจเพิ่ม ก่อนส่งออก PNG\n\n'
            'auto: เลือกสีล้วน / ไล่สี / LaMa ตามตัวอย่างพื้น\n'
            'solid: ดูดสีพื้นแล้วเติม หรือใช้สีที่เลือกเอง\n'
            'gradient: ประมาณพื้นไล่สีจากบริเวณรอบมาสก์\n'
            'lama: ซ่อมพื้นภาพ/ลวดลาย ต้องโหลดและทดสอบโมเดลผ่าน\n'
            'inpaint: OpenCV Telea ใช้เมื่อเลือกเอง พื้นกว้างอาจเป็นปื้น')

    def open_clean_logs(self):
        folder=DATA_DIR/'clean_logs';folder.mkdir(parents=True,exist_ok=True)
        if os.name=='nt':os.startfile(str(folder))
        else:messagebox.showinfo('รายงาน CLEAN',str(folder))

    def restore_clean_draft(self):
        if not self.guard() or not self.page() or not self.current_bubble or self.image is None:return
        path=filedialog.askopenfilename(title='เปิดมาสก์จากรายงานให้กรอบที่เลือก',filetypes=[('CLEAN draft','region-*.json')])
        if not path:return
        try:
            region=decode_region(json.loads(Path(path).read_text(encoding='utf-8')))
            working=compose_page(self.image,self.page().clean_patches)
            if region.box[2]>self.image.width or region.box[3]>self.image.height:
                raise ValueError('มาสก์อยู่นอกภาพที่เปิดอยู่')
            sources=[self.image.crop(region.box),working.crop(region.box)]
            if not any(im.tobytes()==region.image.tobytes() for im in sources):
                raise ValueError('ภาพตั้งต้นของมาสก์ไม่ตรงกับหน้านี้ ให้เปิดภาพต้นฉบับหรือโปรเจกต์ที่ตรงกัน')
            b=self.current_bubble.box
            if not (region.box[0]<=(b[0]+b[2])/2<region.box[2] and region.box[1]<=(b[1]+b[3])/2<region.box[3]):
                raise ValueError('มาสก์ไม่ตรงกับตำแหน่งกรอบที่เลือก')
            self.clean_prepared[(self.page().id,self.current_bubble.id)]=region
            self.dirty=True;self.show_masks.set(True);self.refresh_clean_view()
            self.status.set('เปิดมาสก์จากรายงานแล้ว — ตรวจสีแดงก่อนคลีนใหม่')
        except Exception as exc:messagebox.showerror('เปิดมาสก์ไม่สำเร็จ',str(exc))

    def clean_menu(self):
        if not self.guard():return
        menu=tk.Menu(self.root,tearoff=False)
        menu.add_command(label='วิธีใช้ CLEAN และความหมายแต่ละโหมด',command=self.clean_help)
        menu.add_command(label='ตรวจความพร้อม LaMa (โหลด + คลีนทดสอบ)',command=self.check_clean_readiness)
        menu.add_command(label='เปิดโฟลเดอร์รายงาน CLEAN',command=self.open_clean_logs)
        menu.add_command(label='เปิดมาสก์จากรายงานให้กรอบที่เลือก',command=self.restore_clean_draft)
        menu.add_command(label='แก้มาสก์ / คลีนกรอบที่เลือก',command=self.open_clean_editor)
        menu.add_separator()
        menu.add_command(label='1. อ่านคำซ้ำ + สร้างมาสก์กรอบที่เลือก',command=lambda:self.prepare_clean_masks(selected=True))
        menu.add_command(label='1. อ่านคำซ้ำ + สร้างมาสก์หน้านี้',command=self.prepare_clean_masks)
        menu.add_command(label='1. อ่านคำซ้ำ + สร้างมาสก์ทุกหน้า',command=lambda:self.prepare_clean_masks(True))
        menu.add_command(label='2. คลีนมาสก์กรอบที่เลือก',command=lambda:self.start_clean(selected=True))
        menu.add_command(label='2. คลีนมาสก์หน้านี้',command=self.start_clean)
        menu.add_command(label='2. คลีนมาสก์ทุกหน้า',command=lambda:self.start_clean(True))
        menu.add_checkbutton(label='ตรวจละเอียด + คลีนซ้ำสูงสุด 4 รอบ',variable=self.clean_verify)
        menu.add_separator()
        menu.add_command(label='ส่งออกภาพคลีน → cleaned_pages',command=self.export_clean_images)
        menu.add_command(label='ล้างงานคลีนของหน้านี้ (ย้อนกลับได้)',command=self.clear_clean_page)
        try:menu.tk_popup(self.root.winfo_pointerx(),self.root.winfo_pointery())
        finally:menu.grab_release()

    def open_clean_editor(self):
        if not self.guard():return
        if not self.page() or not self.image or not self.current_bubble:
            messagebox.showinfo('CLEAN','เลือกบับเบิ้ลในรายการก่อน หากยังไม่มีกรอบ ให้อ่าน OCR หรือลากเพิ่มกรอบ');return
        self.commit_editor()
        from clean_editor import CleanEditor
        working=self.image.copy()
        draft=self.clean_prepared.get((self.page().id,self.current_bubble.id))
        return CleanEditor(self,self.page(),self.current_bubble,working,draft)

    def cleaning_lines(self,image,bubble,settings,cancel):
        crop,to_page=ocr_crop(image,bubble)
        arr_c=np.asarray(crop.convert('RGB'))
        ch,cw=arr_c.shape[:2]
        if ch>=4 and cw>=4:
            iy=min(8,ch//5);ix=min(8,cw//5)
            probe=arr_c[iy:ch-iy,ix:cw-ix] if (ch>20 and cw>20) else arr_c
            if int(probe.max())-int(probe.min())<=6:
                return []
        mode,lang,gpu=settings[:3];key=(mode,lang,gpu)
        if self.pipeline is not None and self.pipeline_settings is not None and self.pipeline_settings[:3]==key:
            reader=self.pipeline;reader.cancel=cancel
        else:
            if self.clean_reader is None or self.clean_reader_settings!=key:
                self.clean_reader=LocalPipeline(self.cache,mode=mode,lang=lang,gpu=gpu,
                    neural=False,retry=False,fast=False,bubble_only=False,cancel=cancel,
                    log=lambda note:self.events.put(('log',note)))
                self.clean_reader_settings=key
            reader=self.clean_reader;reader.cancel=cancel
        found=[];failures=[];successful=False
        for engine in reader.iter_engines():
            try:
                found=reader.read(engine,crop,'original-crop');successful=True;break
            except Cancelled:raise
            except Exception as exc:failures.append(str(exc))
        if not successful:raise RuntimeError('อ่านคำซ้ำไม่สำเร็จ: '+'; '.join(failures))
        need_detail=(not found or min((l.score for l in found),default=0.0)<0.78 or max(crop.size)<220)
        if need_detail:
            scale=min(2.0,max(1.0,1600/max(crop.size)))
            enlarged=crop.resize((round(crop.width*scale),round(crop.height*scale)),Image.Resampling.LANCZOS)
            contrast=ImageOps.autocontrast(enlarged.convert('L')).convert('RGB')
            for line in reader.read(engine,contrast,'clean-detail-v2'):
                found.append(move_line(line,translation(sx=crop.width/contrast.width,sy=crop.height/contrast.height)))
        deglow=deglow_crop(crop)
        if deglow is not None:
            dg_img,dg_scale=deglow
            for line in reader.read(engine,dg_img,'clean-deglow-v2'):
                found.append(move_line(line,translation(sx=1.0/dg_scale,sy=1.0/dg_scale)))
        found=dedupe_lines(found)
        output=[]
        for line in found:
            if not line.text.strip():continue
            local=clip_box(line.box,*crop.size)
            if area(local)<=0:continue
            record=asdict(move_line(line,to_page))
            if outline_read(record,bubble.box):continue
            output.append(record)
        return output

    def clean_targets(self,all_pages=False,selected=False):
        pages=list(self.pages) if all_pages else ([self.page()] if self.page() else [])
        ids=set(self.tree.selection()) if selected else None
        return [(p,copy.deepcopy(b)) for p in pages for b in p.bubbles if ids is None or b.id in ids]

    def prepare_clean_masks(self,all_pages=False,selected=False):
        if not self.guard():return
        targets=self.clean_targets(all_pages,selected)
        if not targets:
            messagebox.showinfo('ยังไม่มีกรอบ','เลือกบับเบิ้ลหรือเพิ่มกรอบก่อนสร้างมาสก์');return
        settings=self.settings()
        tasks=[(p.id,p.path,copy.deepcopy(p.clean_patches),b) for p,b in targets]
        def work():
            prepared=failed=ocr_failed=0;image=None;last_pid=None
            self.events.put(('log','ตรวจความพร้อม LaMa ก่อนสร้างมาสก์…'))
            self.events.put(('clean_readiness',self.cleaner.check_lama()))
            for i,(pid,path,patches,b) in enumerate(tasks):
                if self.cancel.is_set():raise Cancelled()
                try:
                    if pid!=last_pid:image=load_image(path);last_pid=pid
                    warning=''
                    has_solid_lines=bool(b.lines and float(getattr(b,'score',0.0))>=0.82
                                         and all(isinstance(r.get('box'),list) and len(r['box'])==4 for r in b.lines))
                    if not has_solid_lines:
                        self.events.put(('log',f'สร้างมาสก์ {i+1}/{len(tasks)} — อ่านคำเพิ่มเติมเฉพาะกรอบไม่มั่นใจ'))
                        try:b.lines+=self.cleaning_lines(image,b,settings,self.cancel)
                        except Cancelled:raise
                        except Exception as exc:
                            ocr_failed+=1;warning=' | OCR ซ้ำไม่พร้อม: '+str(exc)
                            self.events.put(('log',f'กรอบ {i+1}'+warning+' — ใช้พิกัดเดิม/ความต่างสีแทน'))
                    region=prepare_region(image,b);region.note+=warning
                    self.events.put(('clean_mask',(pid,b.id,region)));prepared+=1
                    if not np.any(region.mask):self.events.put(('log',f'กรอบ {i+1}: มาสก์ว่าง ต้องระบายก่อนคลีน'))
                except Cancelled:raise
                except Exception as exc:
                    failed+=1;self.events.put(('log',f'สร้างมาสก์กรอบ {i+1} ไม่สำเร็จ: {exc}'))
                self.events.put(('progress',(i+1)/len(tasks)*100))
            self.events.put(('clean_summary',f'สร้างมาสก์ {prepared}/{len(tasks)} กรอบ • ผิดพลาด {failed} • OCR ซ้ำไม่พร้อม {ocr_failed} — ตรวจสีแดงแล้วกด 2. คลีนมาสก์ (ยังไม่ได้แก้ภาพ)'))
            return failed==0
        self.launch(work,completion_sound='mask_all' if all_pages else None)

    def apply_clean_patch(self,pid,patch):
        if not self.guard():return
        p=next((v for v in self.pages if v.id==pid),None)
        if not p:return
        decode_patch(patch)
        self.snapshot()
        p.clean_patches=[old for old in p.clean_patches if not patch.get('bubble_id') or old.get('bubble_id')!=patch['bubble_id']]
        p.clean_patches.append(patch);self.dirty=True
        self.clean_prepared.pop((pid,patch.get('bubble_id')),None)
        self.show_cleaned.set(True);self.refresh_clean_view()
        self.status.set('ใช้ผลคลีนแล้ว — Ctrl+Z ย้อนกลับ / บันทึกโปรเจกต์หรือส่งออกภาพคลีน')

    def clear_clean_page(self):
        if not self.guard() or not self.page() or not self.page().clean_patches:return
        self.snapshot();self.page().clean_patches=[];self.dirty=True
        self.clean_prepared={key:value for key,value in self.clean_prepared.items() if key[0]!=self.page().id}
        self.refresh_clean_view()
        self.status.set('ล้างงานคลีนหน้านี้แล้ว — Ctrl+Z ย้อนกลับได้')

    def start_clean(self,all_pages=False,selected=False):
        if not self.guard():return
        targets=self.clean_targets(all_pages,selected)
        if not targets:return
        missing=[b for p,b in targets if (p.id,b.id) not in self.clean_prepared]
        if missing:
            messagebox.showinfo('สร้างมาสก์ก่อน',f'ยังไม่ได้สร้างมาสก์ {len(missing)} กรอบ กด 1. อ่านคำซ้ำ + สร้างมาสก์ แล้วตรวจสีแดงก่อนคลีน');return
        tasks=[(p.id,p.path,copy.deepcopy(p.clean_patches),b,copy.deepcopy(self.clean_prepared[(p.id,b.id)])) for p,b in targets]
        settings=self.settings();verify=self.clean_verify.get()
        self.snapshot()
        def work():
            from clean_quality import clean_checked,quality_note
            from clean_diagnostics import CleanRun,counts,summary_text
            self.events.put(('log','ตรวจความพร้อม LaMa ก่อนคลีนทั้งชุด…'))
            readiness=self.cleaner.check_lama()
            self.events.put(('clean_readiness',readiness))
            records=[];run=None;page_image=None;last_pid=None
            try:
                run=CleanRun(DATA_DIR/'clean_logs',len(tasks),readiness)
                self.events.put(('log','รายงาน CLEAN: '+str(run.folder)))
            except Exception as exc:self.events.put(('log','บันทึกรายงานลงดิสก์ไม่ได้: '+str(exc)))
            for index,(pid,path,patches,b,region) in enumerate(tasks):
                if self.cancel.is_set():break
                record={'index':index+1,'page_id':pid,'bubble_id':b.id,'source_path':str(path),
                        'bubble_box':list(b.box),'region_box':list(region.box),'status':'failed'}
                self.cleaner.last_diagnostics={}
                self.events.put(('log',f'คลีนละเอียด {index+1}/{len(tasks)} — ประมวลผลทุกกรอบที่เลือก'))
                try:
                    if pid!=last_pid:page_image=compose_page(load_image(path),patches);last_pid=pid
                    def read_result(result):
                        ub=clip_box(union_box([region.box,b.box]),page_image.width,page_image.height,pad=16)
                        rx0,ry0=ub[0],ub[1]
                        local_page=page_image.crop(ub)
                        local_page.paste(result,(region.box[0]-rx0,region.box[1]-ry0))
                        local_b=copy.deepcopy(b)
                        local_b.box=[b.box[0]-rx0,b.box[1]-ry0,b.box[2]-rx0,b.box[3]-ry0]
                        if local_b.quad:
                            local_b.quad=[[px-rx0,py-ry0] for px,py in local_b.quad]
                        raw_ls=self.cleaning_lines(local_page,local_b,settings,self.cancel)
                        shifted=[]
                        for rec in raw_ls:
                            rb=rec['box']
                            item=dict(rec,box=[rb[0]+rx0,rb[1]+ry0,rb[2]+rx0,rb[3]+ry0])
                            if rec.get('quad'):
                                item['quad']=[[qx+rx0,qy+ry0] for qx,qy in rec['quad']]
                            shifted.append(item)
                        return shifted
                    out,mask,method,report=clean_checked(self.cleaner,region,cancel=self.cancel,
                        reader=read_result if verify else None,max_passes=4 if verify else 1,
                        progress=lambda s:None)
                    patch=encode_patch(region.box,out,mask,method,b.id);patch['quality']=report
                    region.mask=mask.copy()
                    if report['needs_review']:self.events.put(('clean_mask',(pid,b.id,copy.deepcopy(region))))
                    self.events.put(('clean_patch',(pid,patch)))
                    page_image.paste(out,(region.box[0],region.box[1]),Image.fromarray(mask))
                    record.update(status='review' if report['needs_review'] else 'success',method=method,quality=report)
                    self.events.put(('log',f'กรอบ {index+1}: '+quality_note(report)))
                except (InterruptedError,Cancelled) as exc:
                    record.update(status='cancelled',error=str(exc),error_type=type(exc).__name__)
                    self.cancel.set()
                except Exception as exc:
                    record.update(error=str(exc),error_type=type(exc).__name__)
                    self.events.put(('log',f'คลีนกรอบ {index+1} ไม่สำเร็จ: {exc} — เก็บมาสก์ไว้ให้แก้'))
                if record['status'] in ('failed','cancelled'):
                    self.events.put(('clean_mask',(pid,b.id,copy.deepcopy(region))))
                record['background']=dict(self.cleaner.last_diagnostics)
                records.append(record)
                if run is not None:
                    try:run.append(record,region if record['status']!='success' else None)
                    except Exception as exc:self.events.put(('log',f'เก็บรายงานกรอบ {index+1} ไม่สำเร็จ: {exc} — มาสก์ยังอยู่ในโปรเจกต์ กรุณาบันทึก'))
                self.events.put(('progress',(index+1)/len(tasks)*100))
            summary=summary_text(counts(records,len(tasks)))
            if run is not None:
                try:
                    run.write_json('summary.json',counts(records,len(tasks)))
                    self.events.put(('log','รายงานรายกรอบและมาสก์: '+str(run.folder)))
                except Exception as exc:self.events.put(('log','บันทึกสรุปไม่ได้: '+str(exc)))
            self.events.put(('clean_summary',summary))
            return len(records)==len(tasks) and all(r['status'] in ('success','review') for r in records)
        self.launch(work,completion_sound='clean_all' if all_pages else None)

    def export_clean_images(self):
        if not self.guard():return
        tasks=copy.deepcopy([p for p in self.pages if p.clean_patches])
        if not tasks:
            messagebox.showinfo('ยังไม่มีงานคลีน','คลีนแล้วกดใช้ผลนี้ก่อนส่งออกภาพ');return
        parent=filedialog.askdirectory(title='เลือกโฟลเดอร์หลัก (จะสร้าง cleaned_pages ข้างใน)')
        if not parent:return
        folder=Path(parent)/'cleaned_pages'
        def work():
            folder.mkdir(parents=True,exist_ok=True)
            count=0
            for i,p in enumerate(tasks):
                if self.cancel.is_set():raise Cancelled()
                stem=Path(p.path).stem+'_clean';destination=folder/(stem+'.png');suffix=1
                image=load_image(p.path)
                while True:
                    try:
                        export_cleaned(image,p.clean_patches,destination);break
                    except FileExistsError:
                        destination=folder/f'{stem}_{suffix}.png';suffix+=1
                count+=1;self.events.put(('log','บันทึกภาพคลีน: '+str(destination)))
                self.events.put(('progress',(i+1)/len(tasks)*100))
            self.events.put(('clean_export',f'ส่งออก {count} ภาพแล้ว: {folder}'))
        self.launch(work)

    def save_project(self):
        if not self.guard():
            return False
        self.commit_editor()
        path=self.project_path or filedialog.asksaveasfilename(title='บันทึกโปรเจกต์',
            defaultextension='.json',initialfile='manhwa_project.json',filetypes=[('OCR project','*.json')])
        if not path:
            return False
        try:
            data={'format':'manhwa-ocr-project','version':5,'font_preset':self.font_preset.get(),'pages':[]}
            for p in self.pages:
                record=asdict(p)
                record['clean_drafts']={b.id:encode_region(self.clean_prepared[(p.id,b.id)])
                    for b in p.bubbles if (p.id,b.id) in self.clean_prepared}
                try:
                    record['path']=os.path.relpath(p.path,Path(path).parent)
                except ValueError:
                    record['path']=p.path
                data['pages'].append(record)
            atomic_json(path,data)
            self.project_path=path
            self.dirty=False
            self.status.set('บันทึกโปรเจกต์แล้ว: '+str(path))
            return True
        except Exception as exc:
            messagebox.showerror('บันทึกไม่สำเร็จ',str(exc))
            return False

    def may_discard(self):
        self.commit_editor()
        if not self.dirty:
            return True
        answer=messagebox.askyesnocancel('บันทึกงาน','มีงานที่ยังไม่ได้บันทึก ต้องการบันทึกโปรเจกต์ก่อนหรือไม่?')
        return self.save_project() if answer is True else answer is False

    def open_project(self):
        if not self.guard() or not self.may_discard():
            return
        path=filedialog.askopenfilename(title='เปิดโปรเจกต์ (.json หรือ .mtp)',
            filetypes=[('โปรเจกต์ทั้งหมด (*.json, *.mtp)','*.json *.mtp'),
                       ('Manhwa OCR Project','*.json'),
                       ('Manga Translation Project (.mtp)','*.mtp')])
        if not path:
            return
        if Path(path).suffix.lower()=='.mtp':
            self._open_mtp_file(path)
            return
        try:
            data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            if isinstance(data,dict) and isinstance(data.get('files'),list) and ('tsData' in data or 'ocrData' in data):
                self._open_mtp_file(path)
                return
            if data.get('format')!='manhwa-ocr-project' or data.get('version') not in (2,3,4,5):
                raise ValueError('ไฟล์นี้ไม่ใช่โปรเจกต์ Manhwa OCR Editor หรือไฟล์ .mtp ที่รองรับ')
            if data.get('font_preset') in self.font_presets_map:
                self.font_preset.set(data['font_preset'])
            pages,drafts,missing=read_project_pages(data,path)
            if missing:
                folder=filedialog.askdirectory(title='ไม่พบภาพต้นฉบับ เลือกโฟลเดอร์ภาพเพื่อเชื่อมใหม่ (ยกเลิกเพื่อเปิดเฉพาะข้อความ)')
                for p in missing:
                    candidate=Path(folder)/Path(p.path).name if folder else None
                    if candidate and candidate.is_file():
                        p.path=str(candidate.resolve())
                    else:
                        p.error='ไม่พบภาพต้นฉบับ: '+p.path
            self.current_bubble=None
            self.pages=pages
            self.clean_prepared=drafts;self.mask_preview=None
            self.project_path=path
            self.undo_stack=[]
            self.dirty=bool(missing)
            self.page_index=-1
            self.refresh_pages()
            if pages:
                self.select_page(0)
            else:
                self.image=None
                self.fill_editor(None)
                self.refresh_tree()
                self.refresh_output()
                self.render()
            self.status.set('เปิดโปรเจกต์แล้ว: '+path)
        except Exception as exc:
            messagebox.showerror('เปิดไม่สำเร็จ',str(exc))

    def _open_mtp_file(self,mtp_path):
        mtp_file=Path(mtp_path)
        def work():
            self.events.put(('mtp_loaded',read_mtp(mtp_file,self.cancel,self.events.put)))
            return True
        self.launch(work)

    def export_mtp_project(self):
        if not self.guard() or not self.pages:
            messagebox.showinfo('ส่งออก .mtp','เปิดภาพหรือโปรเจกต์ก่อนส่งออกเป็นไฟล์ .mtp');return
        self.commit_editor()
        path=filedialog.asksaveasfilename(title='ส่งออกเป็นไฟล์ .mtp',
            defaultextension='.mtp',initialfile='manga_project.mtp',
            filetypes=[('Manga Translation Project','*.mtp')])
        if not path:return
        tasks=copy.deepcopy(self.pages)
        preset_map=dict(self.font_presets_map.get(self.font_preset.get(),{}))
        def work():
            write_mtp(path,tasks,preset_map,self.cancel,self.events.put)
            return True
        self.launch(work)

    def open_smart_stitch_dialog(self):
        if not self.guard():return
        if not self.pages:
            messagebox.showinfo('SmartStitch','เปิดภาพการ์ตูน หรือกด "เปิดโปรเจกต์" เลือกไฟล์ .mtp ก่อนรวมหน้า');return
        self.commit_editor()
        win=tk.Toplevel(self.root)
        win.title('SmartStitch — รวม/ตัดความสูงหน้าแบบข้อมูลอยู่ครบ 100%')
        win.configure(bg='#0b1218')
        win.transient(self.root);win.grab_set()
        pad=ttk.Frame(win,padding=14);pad.pack(fill='both',expand=True)

        ttk.Label(pad,text='รวมและตัดแบ่งหน้าใหม่ความเร็วสูง พร้อมย้าย กรอบ OCR + แผ่นคลีน + คำแปลไทย + ฟอนต์ ตามไปครบ 100%',
                  foreground='#98e55b',wraplength=540).pack(anchor='w',pady=(0,10))

        first_p=Path(self.pages[0].path)
        default_out=str(first_p.parent.parent/(first_p.parent.name+'_stitched')) if first_p.parent.name.endswith('_mtp_pages') else str(first_p.parent/'stitched_pages')
        out_var=tk.StringVar(value=default_out)

        form=ttk.Frame(pad);form.pack(fill='x')
        form.columnconfigure(1,weight=1)

        ttk.Label(form,text='Rough Output Height [px]:').grid(row=0,column=0,sticky='w',padx=(0,10),pady=4)
        ttk.Spinbox(form,from_=1000,to=29400,increment=500,textvariable=self.stitch_height,width=12).grid(row=0,column=1,sticky='ew',pady=4)

        ttk.Label(form,text='Width Enforcement Type:').grid(row=1,column=0,sticky='w',padx=(0,10),pady=4)
        w_combo=ttk.Combobox(form,textvariable=self.stitch_width_mode,
                             values=['User Customized Width','Automatic Uniform Width','No Enforcement'],
                             state='readonly',width=26)
        w_combo.grid(row=1,column=1,sticky='ew',pady=4)

        ttk.Label(form,text='Manual Custom Width [in pixels]:').grid(row=2,column=0,sticky='w',padx=(0,10),pady=4)
        w_spin=ttk.Spinbox(form,from_=200,to=4000,increment=20,textvariable=self.stitch_custom_width,width=12)
        w_spin.grid(row=2,column=1,sticky='ew',pady=4)

        ttk.Label(form,text='Detector Type:').grid(row=3,column=0,sticky='w',padx=(0,10),pady=4)
        ttk.Combobox(form,textvariable=self.stitch_detector,
                     values=['Smart Pixel Comparison','Direct Slicing'],
                     state='readonly',width=26).grid(row=3,column=1,sticky='ew',pady=4)

        ttk.Label(form,text='Object Detection Sensitivity [1-100%]:').grid(row=4,column=0,sticky='w',padx=(0,10),pady=4)
        ttk.Spinbox(form,from_=1,to=100,textvariable=self.stitch_sensitivity,width=12).grid(row=4,column=1,sticky='ew',pady=4)

        ttk.Label(form,text='Scan Line Step [1-25 px]:').grid(row=5,column=0,sticky='w',padx=(0,10),pady=4)
        ttk.Spinbox(form,from_=1,to=25,textvariable=self.stitch_scan_step,width=12).grid(row=5,column=1,sticky='ew',pady=4)

        ttk.Label(form,text='โฟลเดอร์เก็บภาพที่รวมแล้ว:').grid(row=6,column=0,sticky='w',padx=(0,10),pady=4)
        out_row=ttk.Frame(form);out_row.grid(row=6,column=1,sticky='ew',pady=4)
        out_row.columnconfigure(0,weight=1)
        ttk.Entry(out_row,textvariable=out_var).grid(row=0,column=0,sticky='ew',padx=(0,4))
        def browse_out():
            chosen=filedialog.askdirectory(parent=win,title='เลือกโฟลเดอร์บันทึกภาพ SmartStitch')
            if chosen:out_var.set(chosen)
        ttk.Button(out_row,text='เลือก…',command=browse_out).grid(row=0,column=1)

        def sync_w_state(*_):
            w_spin.configure(state='normal' if self.stitch_width_mode.get()=='User Customized Width' else 'disabled')
        w_combo.bind('<<ComboboxSelected>>',sync_w_state)
        sync_w_state()

        def reset_preset():
            self.stitch_height.set(15000)
            self.stitch_width_mode.set('User Customized Width')
            self.stitch_custom_width.set(720)
            self.stitch_detector.set('Smart Pixel Comparison')
            self.stitch_sensitivity.set(90)
            self.stitch_scan_step.set(5)
            self.stitch_ignore_margin.set(0)
            sync_w_state()

        def run_now():
            try:
                rh=max(800,min(29400,int(self.stitch_height.get())))
                cw=max(200,min(6000,int(self.stitch_custom_width.get())))
                sens=max(1,min(100,int(self.stitch_sensitivity.get())))
                step=max(1,min(25,int(self.stitch_scan_step.get())))
            except (tk.TclError,ValueError):
                messagebox.showerror('ค่าไม่ถูกต้อง','กรุณากรอกตัวเลขความสูงและความกว้างให้ถูกต้อง',parent=win);return
            w_mode=self.stitch_width_mode.get()
            det_mode=self.stitch_detector.get()
            dest_folder=out_var.get().strip()
            if not dest_folder:
                messagebox.showerror('เลือกโฟลเดอร์','กรุณาระบุโฟลเดอร์สำหรับบันทึกภาพที่รวมแล้ว',parent=win);return
            win.destroy()
            self._run_smart_stitch(rh,w_mode,cw,det_mode,sens,step,0,Path(dest_folder))

        btns=ttk.Frame(pad);btns.pack(fill='x',pady=(14,0))
        ttk.Button(btns,text='คืนค่า Preset (15000 / 720px)',command=reset_preset).pack(side='left')
        ttk.Button(btns,text='เริ่มรวมหน้า (Start Process)',command=run_now,style='batch.TButton').pack(side='right',padx=(6,0))
        ttk.Button(btns,text='ยกเลิก',command=win.destroy).pack(side='right')

    def _run_smart_stitch(self,rough_height,width_mode,custom_width,detector_type,sensitivity,scan_step,ignore_margin,out_dir):
        if not self.guard() or not self.pages:return
        self.snapshot()
        src_pages=copy.deepcopy(self.pages)
        src_drafts=copy.deepcopy(self.clean_prepared)

        def work():
            self.events.put(('stitch_done',stitch_pages(src_pages,src_drafts,rough_height,width_mode,custom_width,detector_type,sensitivity,scan_step,ignore_margin,out_dir,self.cancel,self.events.put)))
            return True

        self.launch(work)

    def add_text(self):
        if not self.guard() or not self.pages:return
        self.commit_editor()
        from text_editor import TextImportDialog
        return TextImportDialog(self)

    def open_preset_dialog(self):
        if not self.guard():return
        from text_layout import FRAME_TYPES, FONT_CHOICES, DEFAULT_FONT_PRESETS, save_font_presets
        win=tk.Toplevel(self.root)
        win.title('ตั้งค่าโปรไฟล์ฟอนต์ตามประเภทกรอบ')
        win.configure(bg='#0b1218')
        win.transient(self.root);win.grab_set()
        pad=ttk.Frame(win,padding=14);pad.pack(fill='both',expand=True)

        profiles=copy.deepcopy(self.font_presets_map)
        active=self.font_preset.get()
        p_var=tk.StringVar(value=active)
        top_row=ttk.Frame(pad);top_row.pack(fill='x',pady=(0,10))
        ttk.Label(top_row,text='เลือกโปรไฟล์:',foreground='#98e55b').pack(side='left',padx=(0,8))
        p_combo=ttk.Combobox(top_row,textvariable=p_var,values=list(self.font_presets_map.keys()),state='readonly',width=22)
        p_combo.pack(side='left',fill='x',expand=True)

        grid_f=ttk.Frame(pad);grid_f.pack(fill='x',pady=4)
        grid_f.columnconfigure(1,weight=1)
        row_vars={}
        for r_idx,(key,label) in enumerate(FRAME_TYPES):
            ttk.Label(grid_f,text=f'{label}:').grid(row=r_idx,column=0,sticky='w',padx=(0,12),pady=4)
            v=tk.StringVar()
            cb=ttk.Combobox(grid_f,textvariable=v,values=FONT_CHOICES,state='readonly',width=24)
            cb.grid(row=r_idx,column=1,sticky='ew',pady=4)
            row_vars[key]=v

        def load_current_into_vars(*_):
            nonlocal active
            if active in profiles and row_vars['normal'].get():
                profiles[active]={k:v.get() for k,v in row_vars.items()}
            active=p_var.get()
            mapping=profiles.get(active,DEFAULT_FONT_PRESETS['1. มังฮวาเกาหลี'])
            for k,_ in FRAME_TYPES:
                row_vars[k].set(mapping.get(k,DEFAULT_FONT_CHOICE))

        p_combo.bind('<<ComboboxSelected>>',load_current_into_vars)
        load_current_into_vars()

        def reset_default():
            name=p_var.get()
            base=DEFAULT_FONT_PRESETS.get(name,DEFAULT_FONT_PRESETS['1. มังฮวาเกาหลี'])
            for k,_ in FRAME_TYPES:
                row_vars[k].set(base.get(k,DEFAULT_FONT_CHOICE))

        def create_profile():
            name=simpledialog.askstring('สร้างโปรไฟล์ใหม่','ชื่อโปรไฟล์',parent=win)
            if not name:return
            name=name.strip()
            if not name or name in profiles:
                messagebox.showwarning('ชื่อโปรไฟล์','กรุณาระบุชื่อใหม่ที่ยังไม่มีในรายการ',parent=win)
                return
            profiles[name]={k:v.get() for k,v in row_vars.items()}
            p_combo.configure(values=list(profiles))
            p_var.set(name)
            load_current_into_vars()

        def delete_profile():
            name=p_var.get()
            if name in DEFAULT_FONT_PRESETS:
                messagebox.showinfo('ลบโปรไฟล์','ลบได้เฉพาะโปรไฟล์ที่สร้างเพิ่ม',parent=win)
                return
            if messagebox.askyesno('ลบโปรไฟล์',f'ลบโปรไฟล์ {name}?',parent=win):
                del profiles[name]
                p_combo.configure(values=list(profiles))
                p_var.set(next(iter(profiles)))
                load_current_into_vars()

        manage=ttk.Frame(pad)
        manage.pack(fill='x')
        ttk.Button(manage,text='สร้างโปรไฟล์ใหม่…',command=create_profile).pack(side='left')
        ttk.Button(manage,text='ลบโปรไฟล์',command=delete_profile).pack(side='left')
        def save_and_close():
            name=p_var.get()
            profiles[name]={k:v.get() for k,v in row_vars.items()}
            try:save_font_presets(profiles)
            except OSError as exc:
                messagebox.showerror('บันทึกโปรไฟล์ไม่สำเร็จ',str(exc),parent=win)
                return
            self.font_presets_map=profiles
            self.preset_combo.configure(values=list(profiles))
            self.font_preset.set(name)
            self.status.set(f'บันทึกชุดฟอนต์โปรไฟล์ "{name}" แล้ว — กดปุ่มจัดฟอนต์ตามกรอบเพื่อใช้งาน')
            win.destroy()

        btns=ttk.Frame(pad);btns.pack(fill='x',pady=(12,0))
        ttk.Button(btns,text='คืนค่าเริ่มต้นโปรไฟล์นี้',command=reset_default).pack(side='left')
        ttk.Button(btns,text='บันทึก',command=save_and_close,style='text.TButton').pack(side='right',padx=(6,0))
        ttk.Button(btns,text='ปิด',command=win.destroy).pack(side='right')

    def apply_preset_fonts(self,all_pages=False,quick_type=None):
        """Re-center every bubble cleanly using our oval/breathing-room layout + apply preset fonts."""
        if not self.guard():return
        self.commit_editor()
        if not self.pages or (not all_pages and not self.page()):
            messagebox.showinfo('ยังไม่มีหน้าการ์ตูน','เปิดภาพการ์ตูนและอ่าน OCR ก่อนจัดฟอนต์ตามกรอบ');return
        from text_layout import (classify_bubble_type, layout_text, resolve_font,
                                 normalize_font_choice, FRAME_TYPE_LABELS, DEFAULT_FONT_PRESETS)
        preset_name=self.font_preset.get()
        raw_map=self.font_presets_map.get(preset_name) or DEFAULT_FONT_PRESETS['1. มังฮวาเกาหลี']

        fallback_font=DEFAULT_FONT_CHOICE
        normal_candidate=normalize_font_choice(raw_map.get('normal',DEFAULT_FONT_CHOICE))
        try:
            resolve_font(normal_candidate)
            fallback_font=normal_candidate
        except Exception:
            pass

        resolved_map={};missing_fonts=set()
        for fkey,font_wanted in raw_map.items():
            canon=normalize_font_choice(font_wanted)
            try:
                resolve_font(canon)
                resolved_map[fkey]=canon
            except Exception:
                missing_fonts.add(canon)
                resolved_map[fkey]=fallback_font

        if all_pages:
            page_targets=[(p,list(p.bubbles)) for p in self.pages if p.bubbles]
        else:
            sel_ids=set(self.tree.selection())
            bs=[b for b in self.page().bubbles if b.id in sel_ids] if quick_type or len(sel_ids)>=2 else list(self.page().bubbles)
            page_targets=[(self.page(),bs)] if bs else []

        total_bubbles=sum(len(bs) for _,bs in page_targets)
        if total_bubbles==0:
            messagebox.showinfo('ยังไม่มีกรอบ','ยังไม่มีกรอบข้อความในหน้าที่เลือก');return

        self.snapshot()
        task_pages=[(p.id,p.path,[copy.deepcopy(b) for b in bs],copy.deepcopy(p.clean_patches)) for p,bs in page_targets]

        def work():
            results=[];type_counts={};laid_out=0;failed=0
            for p_idx,(pid,path,bs,patches) in enumerate(task_pages):
                if self.cancel.is_set():break
                raw_im=load_image(path)
                work_im=compose_page(raw_im,patches) if patches else raw_im
                for b_idx,b in enumerate(bs):
                    if self.cancel.is_set():break
                    ftype=quick_type or classify_bubble_type(raw_im,b)
                    type_counts[ftype]=type_counts.get(ftype,0)+1
                    chosen_font=resolved_map.get(ftype,fallback_font)
                    b.frame_type=ftype
                    b.font_choice=chosen_font
                    b.custom_size=0;b.custom_width=0;b.text_offset=[];b.custom_rotation=None
                    layer={};err=''
                    if b.translation.strip():
                        self.events.put(('log',f'[{preset_name}] {Path(path).name} กรอบ {b_idx+1}: [{FRAME_TYPE_LABELS.get(ftype,ftype)}] → จัดกึ่งกลางใหม่ด้วย {chosen_font}'))
                        try:
                            layer=layout_text(raw_im if b.engine=="MTP" else work_im,b,b.translation,font_choice=chosen_font,
                                              custom_size=0,custom_width=0,text_offset=[],custom_rotation=None)
                            laid_out+=1
                        except Exception as exc:
                            err=str(exc);failed+=1
                    results.append((pid,b.id,b.translation,layer,err,chosen_font,ftype,True,bool(quick_type or b.frame_type_locked)))
                self.events.put(('progress',(p_idx+1)/len(task_pages)*100))

            self.events.put(('text_results',results))
            breakdown=' • '.join(f'{FRAME_TYPE_LABELS.get(k,k)} {v}' for k,v in type_counts.items())
            summary=f'จัดกึ่งกลางบับเบิ้ลใหม่ + ฟอนต์ [{preset_name}] {len(results)} กรอบ ({breakdown}) • สำเร็จ {laid_out} กรอบ'
            if failed:summary+=f' • ล้มเหลว {failed}'
            self.events.put(('job_summary',summary))
            if missing_fonts:
                warn_list=', '.join(sorted(missing_fonts))
                self.events.put(('preset_warning',
                    f'ยังไม่พบฟอนต์ต่อไปนี้ใน Windows Fonts:\n• {warn_list}\n\n'
                    f'ระบบใช้ฟอนต์ "{fallback_font}" แทนชั่วคราวสำหรับหมวดดังกล่าว (ติดตั้งฟอนต์แล้วกดจัดฟอนต์ซ้ำได้ทันที)'))
            return failed==0

        self.launch(work)

    def _selected_page_bubbles(self):
        if not self.page():return []
        ids=set(self.tree.selection())
        targets=[b for b in self.page().bubbles if b.id in ids]
        if not targets and self.current_bubble:
            targets=[self.current_bubble]
        return targets

    def _apply_text_style_to_targets(self,targets,font_choice=None,custom_size=None,custom_width=None,reset_auto=False):
        if not self.guard() or not self.page() or not targets:return
        self.commit_editor()
        from text_layout import layout_text,resolve_font,normalize_font_choice
        if font_choice is not None:
            font_choice=normalize_font_choice(font_choice)
            try:resolve_font(font_choice)
            except Exception as exc:
                messagebox.showerror('ไม่พบฟอนต์ในเครื่อง',str(exc));return
        self.snapshot()
        for b in targets:
            if font_choice is not None:b.font_choice=font_choice
            if reset_auto:
                b.custom_size=0;b.custom_width=0;b.text_offset=[];b.custom_rotation=None
            else:
                if custom_size is not None:b.custom_size=int(np.clip(custom_size,6,180))
                if custom_width is not None:b.custom_width=int(max(24,custom_width))
        self.dirty=True
        translated=[b for b in targets if b.translation.strip()]
        if not translated:
            self.select_bubble()
            self.status.set('บันทึกค่าฟอนต์/ขนาดไว้แล้ว — เมื่อใส่คำแปลไทยจะแสดงผลตามค่านี้ทันที')
            return
        if self.image is not None and len(translated)<=4:
            errors=[]
            work_im=self.cleaned_preview if self.cleaned_preview is not None else self.image
            for b in translated:
                chosen=getattr(b,'font_choice',None) or DEFAULT_FONT_CHOICE
                try:
                    b.text_layer=layout_text(work_im,b,b.translation,font_choice=chosen,
                                             custom_size=b.custom_size,custom_width=b.custom_width,
                                             text_offset=getattr(b,'text_offset',[]),
                                             custom_rotation=getattr(b,'custom_rotation',None))
                except Exception as exc:
                    errors.append(str(exc))
            self._view_cache=None
            self.show_text.set(True)
            self.refresh_output();self.refresh_clean_view()
            self._suppress_autoscroll=True
            try:self.select_bubble()
            finally:self._suppress_autoscroll=False
            if errors:messagebox.showwarning('จัดวาง Text',errors[0])
            else:self.status.set('อัปเดต Text แล้ว (กด Ctrl+Z เพื่อย้อนกลับได้)')
            return
        tasks=[(self.page().id,self.page().path,copy.deepcopy(b),b.translation,
                getattr(b,'font_choice',None) or DEFAULT_FONT_CHOICE) for b in translated]
        self._run_text_layout_tasks(tasks,summary_prefix='อัปเดต Text')

    def apply_editor_text_now(self):
        if not self.guard() or not self.page():return
        self.commit_editor()
        targets=self._selected_page_bubbles()
        if not targets:
            messagebox.showinfo('เลือกกรอบก่อน','คลิกเลือกกรอบที่มีคำแปลไทยก่อนอัปเดตบรรทัด');return
        self._apply_text_style_to_targets(targets)

    def step_font_size(self,delta):
        if not self.guard() or not self.page():return
        self.commit_editor()
        targets=self._selected_page_bubbles()
        if not targets:
            messagebox.showinfo('เลือกกรอบก่อน','คลิกเลือกกรอบคำพูดก่อนปรับขนาดฟอนต์');return
        ref=targets[-1]
        cur=int(round(ref.text_layer.get('font_size',0))) if ref.text_layer else int(getattr(ref,'custom_size',0) or self.font_size_var.get() or 24)
        new_sz=int(np.clip((cur if cur>=6 else 24)+int(delta),6,180))
        self.font_size_var.set(new_sz)
        self._apply_text_style_to_targets(targets,custom_size=new_sz)

    def apply_size_to_bubbles(self):
        if not self.guard() or not self.page():return
        self.commit_editor()
        targets=self._selected_page_bubbles()
        if not targets:
            messagebox.showinfo('เลือกกรอบก่อน','คลิกเลือกกรอบคำพูดก่อนกำหนดขนาดฟอนต์');return
        try:new_sz=int(np.clip(int(self.font_size_var.get()),6,180))
        except (tk.TclError,ValueError):return
        self.font_size_var.set(new_sz)
        self._apply_text_style_to_targets(targets,custom_size=new_sz)

    def step_wrap_width(self,factor):
        if not self.guard() or not self.page() or not self.image:return
        self.commit_editor()
        targets=self._selected_page_bubbles()
        if not targets:
            messagebox.showinfo('เลือกกรอบก่อน','คลิกเลือกกรอบคำพูดก่อนปรับความกว้างบรรทัด');return
        self.snapshot()
        from text_layout import layout_text
        errors=[]
        work_im=self.cleaned_preview if self.cleaned_preview is not None else self.image
        for b in targets:
            if not b.translation.strip():continue
            cur_sz=int(round(b.text_layer.get('font_size',24))) if b.text_layer else int(getattr(b,'custom_size',0) or 24)
            cur_w=int(getattr(b,'custom_width',0)) or (int(round(b.text_layer['box'][2]-b.text_layer['box'][0])) if b.text_layer else int(round((b.box[2]-b.box[0])*.9)))
            b.custom_size=max(6,cur_sz)
            b.custom_width=int(np.clip(round(max(28,cur_w)*float(factor)),28,self.image.width-8))
            chosen=getattr(b,'font_choice',None) or DEFAULT_FONT_CHOICE
            try:
                b.text_layer=layout_text(work_im,b,b.translation,font_choice=chosen,
                                         custom_size=b.custom_size,custom_width=b.custom_width,
                                         text_offset=getattr(b,'text_offset',[]),
                                         custom_rotation=getattr(b,'custom_rotation',None))
            except Exception as exc:
                errors.append(str(exc))
        self.dirty=True;self._view_cache=None;self.show_text.set(True)
        self.refresh_output();self.refresh_clean_view()
        self._suppress_autoscroll=True
        try:self.select_bubble()
        finally:self._suppress_autoscroll=False
        if errors:messagebox.showwarning('ปรับบรรทัด',errors[0])

    def reset_text_auto(self):
        if not self.guard() or not self.page():return
        targets=self._selected_page_bubbles()
        if not targets:return
        self._apply_text_style_to_targets(targets,reset_auto=True)

    def apply_font_to_bubbles(self,all_in_page=False):
        if not self.guard() or not self.page():return
        self.commit_editor()
        from text_layout import normalize_font_choice
        chosen=normalize_font_choice(self.selected_font.get())
        self.selected_font.set(chosen)
        targets=list(self.page().bubbles) if all_in_page else self._selected_page_bubbles()
        if not targets:
            messagebox.showinfo('เลือกกรอบก่อน','คลิกเลือกกรอบคำพูดก่อน (กด Ctrl+คลิก หรือ Shift+คลิก บนภาพหรือในรายการเพื่อเลือกหลายกรอบพร้อมกัน)');return
        self._apply_text_style_to_targets(targets,font_choice=chosen)

    def start_text_layout(self,changes=None,frame_tags=None):
        if not self.guard():return
        self.commit_editor()
        tasks=[(p.id,p.path,copy.deepcopy(b),changes[b.id] if changes is not None else b.translation,
                getattr(b,'font_choice',None) or DEFAULT_FONT_CHOICE)
               for p in self.pages for b in p.bubbles
               if (b.id in changes if changes is not None else bool(b.translation.strip()))]
        if not tasks:
            messagebox.showinfo('เพิ่ม Text','กรอกคำแปลไทยก่อน แล้วกดเพิ่ม Text เพื่อจัดวาง');return
        self.snapshot()
        self._run_text_layout_tasks(tasks,summary_prefix='จัด Text',frame_tags=frame_tags)

    def _run_text_layout_tasks(self,tasks,summary_prefix='จัด Text',frame_tags=None):
        patches_by_pid={p.id:copy.deepcopy(p.clean_patches) for p in self.pages if p.clean_patches}
        def work():
            from text_layout import layout_text
            results=[];successful=failed=review=0;image=None;last_key=None
            try:
                for index,(pid,path,b,translation,font_choice) in enumerate(tasks):
                    if self.cancel.is_set():break
                    self.events.put(('log',f'{summary_prefix} {index+1}/{len(tasks)} ({font_choice}) — {Path(path).name}'))
                    try:
                        if (pid,path)!=last_key:
                            raw_im=load_image(path)
                            pts=patches_by_pid.get(pid)
                            image=compose_page(raw_im,pts) if pts else raw_im
                            last_key=(pid,path)
                        layer=layout_text(image,b,translation,font_choice=font_choice,
                                          custom_size=getattr(b,'custom_size',0),
                                          custom_width=getattr(b,'custom_width',0),
                                          text_offset=getattr(b,'text_offset',[]),
                                          custom_rotation=getattr(b,'custom_rotation',None))
                        results.append((pid,b.id,translation,layer,'',font_choice,(frame_tags or {}).get(b.id),False,b.id in (frame_tags or {})));successful+=1
                        if layer['needs_review']:review+=1
                    except Exception as exc:
                        results.append((pid,b.id,translation,{},str(exc),font_choice,(frame_tags or {}).get(b.id),False,b.id in (frame_tags or {})));failed+=1
                    self.events.put(('progress',(index+1)/len(tasks)*100))
            finally:
                self.events.put(('text_results',results))
                self.events.put(('job_summary',f'{summary_prefix} {len(results)}/{len(tasks)} กรอบ • สำเร็จ {successful} • ล้มเหลว {failed} • ต้องตรวจเพิ่ม {review} — ดู Text แล้วส่งออก PSD ได้'))
        self.launch(work)

    def psd_menu(self):
        if not self.guard():return
        menu=tk.Menu(self.root,tearoff=False)
        menu.add_command(label='PSD หน้านี้ — Background + CLEAN + Text',command=self.export_psd_pages)
        menu.add_command(label='PSD ทุกหน้า — Background + CLEAN + Text',command=lambda:self.export_psd_pages(True))
        menu.add_separator()
        menu.add_command(label='PSD หน้านี้ — เฉพาะ Background + CLEAN',command=lambda:self.export_psd_pages(False,False))
        menu.add_command(label='PSD ทุกหน้า — เฉพาะ Background + CLEAN',command=lambda:self.export_psd_pages(True,False))
        menu.add_separator()
        menu.add_command(label='ต้องมี Photoshop Desktop และติดตั้งฟอนต์ที่เลือกใน Windows',state='disabled')
        try:menu.tk_popup(self.root.winfo_pointerx(),self.root.winfo_pointery())
        finally:menu.grab_release()

    def export_psd_pages(self,all_pages=False,include_text=True):
        if not self.guard():return
        self.commit_editor()
        targets=self.pages if all_pages else ([self.page()] if self.page() else [])
        if not targets:return
        from photoshop_export import photoshop_progid,export_psd
        from text_layout import is_current,layout_text
        try:photoshop_progid()
        except Exception as exc:messagebox.showerror('ส่งออก PSD',str(exc));return
        parent=filedialog.askdirectory(title='เลือกโฟลเดอร์ส่งออก PSD (แยกหนึ่งไฟล์ต่อหน้า)')
        if not parent:return
        tasks=copy.deepcopy(targets);folder=Path(parent)/'psd_pages'
        page_numbers={p.id:i+1 for i,p in enumerate(self.pages)}
        def work():
            succeeded=failed=attempted=0;first_error='';auto_layouts=[]
            for index,p in enumerate(tasks):
                if self.cancel.is_set():break
                attempted+=1
                self.events.put(('log',f'สร้าง PSD {index+1}/{len(tasks)} ผ่าน Photoshop — {Path(p.path).name}'))
                stem=psd_stem(p.path,page_numbers[p.id]);destination=folder/(stem+'.psd');suffix=1
                while destination.exists():destination=folder/f'{stem}_{suffix}.psd';suffix+=1
                try:
                    image=load_image(p.path)
                    work_im=compose_page(image,p.clean_patches) if p.clean_patches else image
                    if include_text:
                        for b in p.bubbles:
                            if b.translation.strip() and not is_current(b):
                                chosen=getattr(b,'font_choice',None) or DEFAULT_FONT_CHOICE
                                b.text_layer=layout_text(work_im,b,b.translation,font_choice=chosen,
                                                         custom_size=getattr(b,'custom_size',0),
                                                         custom_width=getattr(b,'custom_width',0),
                                                         text_offset=getattr(b,'text_offset',[]),
                                                         custom_rotation=getattr(b,'custom_rotation',None))
                                auto_layouts.append((p.id,b.id,b.translation,b.text_layer,'',chosen))
                    report=export_psd(image,p,destination,include_text,self.cancel)
                    succeeded+=1;self.events.put(('log',f'บันทึก {destination} • CLEAN {report["clean_layers"]} • Text {len(report["text_layers"])}'))
                    for warning in report.get('warnings',[]):self.events.put(('log','คำเตือนฟอนต์: '+warning))
                except InterruptedError:self.cancel.set();break
                except Exception as exc:
                    failed+=1
                    if not first_error:first_error=f'{Path(p.path).name}: {exc}'
                    self.events.put(('log',f'PSD {Path(p.path).name} ไม่สำเร็จ: {exc}'))
                self.events.put(('progress',(index+1)/len(tasks)*100))
            if auto_layouts:
                self.events.put(('text_results',auto_layouts))
            tail=f' | สาเหตุ: {first_error}' if first_error else f' — {folder}'
            self.events.put(('job_summary',f'PSD พยายาม {attempted}/{len(tasks)} หน้า • สำเร็จ {succeeded} • ล้มเหลว {failed} • หยุดกลางหน้า {attempted-succeeded-failed}{tail}'))
            if first_error:
                self.events.put(('psd_error',first_error))
            return succeeded==len(tasks) and failed==0
        self.launch(work,completion_sound='psd')

    def export(self):
        self.commit_editor()
        text=format_output(self.pages)
        if not text:
            messagebox.showinfo('ยังไม่มีข้อความ','อ่านภาพหรือเพิ่มข้อความก่อนส่งออก')
            return
        path=filedialog.asksaveasfilename(title='ส่งออกคู่ต้นฉบับ/คำแปล',defaultextension='.txt',
            initialfile='manhwa_translation.txt',filetypes=[('Text','*.txt')])
        if path:
            try:
                Path(path).write_text(text+'\n',encoding='utf-8')
                self.status.set('ส่งออกแล้ว: '+path)
            except OSError as exc:
                messagebox.showerror('ส่งออกไม่สำเร็จ',str(exc))

    def copy_output(self):
        self.commit_editor()
        self.root.clipboard_clear()
        self.root.clipboard_append(format_output(self.pages))
        self.status.set('คัดลอกคู่ต้นฉบับ/คำแปลทั้งหมดแล้ว')

    def help(self):
        messagebox.showinfo('วิธีใช้',
            '1. กด "เปิดโปรเจกต์" เพื่อเปิดไฟล์ .mtp หรือ .json\n'
            '2. ปุ่มบนขอบกรอบข้อความเมื่อคลิกเลือก:\n'
            '   • วงกลมสีฟ้าขอบบน (+) = ย้ายตำแหน่ง Text พร้อมเส้นล็อกสีม่วงสไตล์ Photoshop (กด Alt ค้างเพื่อขยับอิสระ)\n'
            '   • วงกลมสีส้มมุมขวาบน (↻) = หมุนเอียงประโยค (ดูดเข้ามุมหลักอัตโนมัติ)\n'
            '   • แถบสีเขียวขอบขวา = บีบ/ขยายบรรทัด  |  จุดสีม่วงมุมขวาล่าง = ปรับขนาดฟอนต์\n'
            '3. กดปุ่ม "จัดฟอนต์ตามกรอบ (ทุกหน้า)" เพื่อคำนวณจัดกึ่งกลางบับเบิ้ลใหม่ทั้งหมดด้วยสูตรของคุณ\n'
            '4. ส่งออกเป็น PSD แท้ผ่าน Photoshop Desktop หรือส่งออกกลับเป็นไฟล์ .mtp ในเมนูเพิ่มเติม')

    def close(self):
        if self.busy:
            self.stop()
            messagebox.showinfo('กำลังหยุด','รอคำสั่ง OCR ปัจจุบันจบ แล้วปิดโปรแกรมอีกครั้งเพื่อบันทึกงาน')
            return
        if self.may_discard():
            self.cancel_wheel()
            self.root.after_cancel(self.poll_id)
            self.sounds.close()
            self.root.destroy()


def main():
    root=tk.Tk()
    try:ManhwaOCRApp(root)
    except Exception as exc:
        root.withdraw();messagebox.showerror('เปิดโปรแกรมไม่ได้',str(exc));root.destroy();return
    root.mainloop()


if __name__=='__main__':
    main()
