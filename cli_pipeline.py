"""Manhwa OCR Editor 4.1 batch entry point for an interactive Windows login."""
import argparse
from contextlib import nullcontext
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import threading
from time import perf_counter
from urllib import error as urllib_error, request as urllib_request

from PIL import Image
import manhwa_ocr as app
from bubble_cleaner import auto_region, Cleaner, compose_page, encode_patch, encode_region
from clean_quality import clean_checked
from gemini_translator import DEFAULT_GEMINI_MODEL, translate_pages_with_gemini
from photoshop_export import DEFAULT_PSD_BACKEND, PhotopeaSession, export_psd as _real_export_psd
from text_layout import (classify_bubble_type, composite_text_crop, detect_and_parse_translations,
                         export_source_script, layout_text, load_font_presets, resolve_font)

export_psd = _real_export_psd
POLL_INTERVAL_SECONDS = 15.0
DISCORD_COLOR_INFO = 0x5865F2
DISCORD_COLOR_SUCCESS = 0x2ECC71
DISCORD_COLOR_WARNING = 0xF39C12
DISCORD_COLOR_ERROR = 0xE74C3C


def boolean(value):
    if value.lower() in ('true', 'false'):
        return value.lower() == 'true'
    raise argparse.ArgumentTypeError('expected true or false')


def send_discord_notification(
    title,
    description,
    *,
    color=DISCORD_COLOR_INFO,
    fields=None,
    url=None,
    webhook_url=None,
    timeout=15,
):
    """Send a Discord webhook embed notification. Silently no-ops if webhook is unset."""
    target = (webhook_url if webhook_url is not None else os.environ.get('DISCORD_WEBHOOK_URL', '')).strip()
    if not target:
        return False
    embed = {
        'title': str(title)[:256],
        'description': str(description)[:4000],
        'color': int(color),
    }
    if url:
        embed['url'] = str(url)
    if fields:
        embed['fields'] = [
            {
                'name': str(f['name'])[:256],
                'value': str(f['value'])[:1024],
                'inline': bool(f.get('inline', False)),
            }
            for f in fields[:25]
        ]
    payload = json.dumps({'embeds': [embed]}, ensure_ascii=False).encode('utf-8')
    req = urllib_request.Request(
        target,
        data=payload,
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'ManhwaOCR-Pipeline/4.1',
        },
        method='POST',
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', None) or resp.getcode()
            return 200 <= int(status or 200) < 300
    except (urllib_error.URLError, OSError, ValueError) as exc:
        print(f'Discord notification skipped ({exc})', flush=True)
        return False


def append_step_summary(markdown_text):
    """Append markdown content to $GITHUB_STEP_SUMMARY when running under GitHub Actions."""
    summary_path = (os.environ.get('GITHUB_STEP_SUMMARY') or '').strip()
    if not summary_path:
        return False
    try:
        path = Path(summary_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(markdown_text.rstrip() + '\n\n')
        return True
    except OSError as exc:
        print(f'Step summary write skipped ({exc})', flush=True)
        return False


def _format_minutes(minutes):
    val = float(minutes)
    return str(int(val)) if val.is_integer() else f'{val:g}'


def options():
    p = argparse.ArgumentParser(description=__doc__)
    inputs=p.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--input',type=Path)
    inputs.add_argument('--drive-link')
    p.add_argument('--input-mode',choices=['mtp','image_folder'])
    p.add_argument('--translation-link')
    p.add_argument('--translation', type=Path)
    p.add_argument('--output-dir', type=Path)
    p.add_argument('--lang', choices=['ko+en','en','zh+en','zh_tw+en'], default='ko+en')
    p.add_argument('--preset', choices=list(load_font_presets()), default=None)
    p.add_argument('--smart-stitch', type=boolean, default=False)
    p.add_argument('--stitch-height', type=int, default=15000)
    p.add_argument('--stitch-width', type=int, default=720)
    p.add_argument('--skip-clean', type=boolean, default=False)
    p.add_argument('--auto-stroke', action='store_true')
    p.add_argument('--auto-gradient', action='store_true')
    p.add_argument('--export-format', choices=['psd','mtp','png','psd+mtp','all'], default='psd')
    p.add_argument('--psd-backend', choices=['photoshop','photopea'],
                   default=os.environ.get('MANHWA_PSD_BACKEND', DEFAULT_PSD_BACKEND))
    p.add_argument('--translation-mode', choices=['manual_drive', 'gemini_auto'], default=None)
    p.add_argument('--wait-timeout-minutes', type=float, default=60.0)
    p.add_argument('--auto-translate-gemini', '--auto-translate', dest='auto_translate_gemini',
                   nargs='?', const=True, default=False, type=boolean)
    p.add_argument('--translation-note', type=str, default='')
    p.add_argument('--gemini-model', default=None)
    args=p.parse_args()
    if bool(args.drive_link)!=bool(args.input_mode):
        p.error('--drive-link and --input-mode must be used together')
    if args.translation_link and args.input_mode!='image_folder':
        p.error('--translation-link requires image_folder')
    if args.drive_link and (args.translation or args.output_dir):
        p.error('Use --translation-link and the configured Drive output folders for Drive jobs')
    if args.wait_timeout_minutes < 0:
        p.error('--wait-timeout-minutes must be >= 0')
    if args.translation_mode == 'gemini_auto':
        args.auto_translate_gemini = True
    elif args.auto_translate_gemini and not args.translation_mode:
        args.translation_mode = 'gemini_auto'
    if not args.preset:
        args.preset='2. การ์ตูนต่อสู้' if args.input_mode=='mtp' else '1. มังฮวาเกาหลี'
    return args


def emit(event):
    kind, value = event
    print(f'{value:.0f}%' if kind=='progress' else value, flush=True)


def checkpoint(path, pages, drafts, preset, **metadata):
    records = []
    for page in pages:
        record = asdict(page)
        try:
            record['path'] = os.path.relpath(page.path, path.parent)
        except ValueError:
            pass
        record['clean_drafts'] = {b.id:encode_region(drafts[(page.id,b.id)]) for b in page.bubbles
                                  if (page.id,b.id) in drafts}
        records.append(record)
    app.atomic_json(path, dict(format='manhwa-ocr-project', version=5, font_preset=preset, pages=records, **metadata))


def _apply_translations(translation_path, pages):
    changes, report = detect_and_parse_translations(Path(translation_path).read_text('utf-8-sig'), pages)
    print(f"Translations: {report['matched_count']}/{report['total_bubbles']}", *report['warnings'], flush=True)
    if not changes:
        raise ValueError('No translations matched')
    for page in pages:
        for b in page.bubbles:
            if b.id in changes:
                b.translation = changes[b.id]
            if b.id in report['frame_tags']:
                b.frame_type = report['frame_tags'][b.id]
                b.frame_type_locked = True
    return report


def _layout_translated_pages(pages, fonts, gpu, auto_stroke, auto_gradient):
    for i, page in enumerate(pages, 1):
        raw = app.load_image(page.path)
        work = compose_page(raw, page.clean_patches)
        for b in page.bubbles:
            if b.translation.strip():
                b.frame_type = classify_bubble_type(raw, b, gpu=gpu)
                b.font_choice = fonts[b.frame_type]
                try:
                    resolve_font(b.font_choice)
                except RuntimeError:
                    print(f'Font unavailable: {b.font_choice}; using {app.DEFAULT_FONT_CHOICE}', flush=True)
                    b.font_choice = app.DEFAULT_FONT_CHOICE
                b.custom_size = b.custom_width = 0
                b.custom_rotation = None
                b.text_offset = []
                b.text_layer = layout_text(
                    work,
                    b,
                    page_bubbles=page.bubbles,
                    original=raw,
                    cleaned=work if page.clean_patches else None,
                    auto_stroke=auto_stroke,
                    auto_gradient=auto_gradient,
                )
        print(f'Layout {100*i/len(pages):.0f}%', flush=True)


def _find_existing_translation(drive, folder_ids, dest_path):
    seen = set()
    for fid in folder_ids:
        if not fid or fid in seen:
            continue
        seen.add(fid)
        record = drive.find_translation_file(fid)
        if not record:
            continue
        try:
            path, text = drive.download_text_record(record, dest_path)
        except Exception:
            continue
        if text.strip():
            return path, record, text
    return None


def _notify_manual_drive_waiting(episode, total_bubbles, folder_link, raw_file_link, timeout_minutes):
    timeout_label = _format_minutes(timeout_minutes)
    if timeout_minutes > 0:
        instruction = (
            f'นำ `01_raw_text.txt` ไปแปล แล้วอัปโหลด/วางไฟล์ชื่อ `02_translated.txt` '
            f'ลงโฟลเดอร์ Drive เดิมได้เลย (กำลังรอตรวจอัตโนมัติทุก {int(POLL_INTERVAL_SECONDS)} วินาที สูงสุด {timeout_label} นาที)'
        )
    else:
        instruction = (
            'นำ `01_raw_text.txt` ไปแปล แล้วอัปโหลด/วางไฟล์ชื่อ `02_translated.txt` '
            'ลงโฟลเดอร์ Drive เดิม จากนั้นกด Run Workflow เดิมอีกรอบ โปรแกรมจะข้ามขั้นตอนคลีนแล้วส่งออก PSD ให้ทันที '
            '(โหมดไม่รอ: wait_timeout_minutes = 0)'
        )
    print(f'พร้อมแปลตอน "{episode}" ({total_bubbles} บับเบิล)', flush=True)
    print(f'โฟลเดอร์ Drive: {folder_link}', flush=True)
    print(f'ไฟล์ต้นฉบับ 01_raw_text.txt: {raw_file_link}', flush=True)
    print(instruction, flush=True)

    append_step_summary(
        f'## 📝 พร้อมแปลข้อความ — {episode}\n\n'
        f'- **จำนวนบับเบิล:** {total_bubbles}\n'
        f'- **โฟลเดอร์ Google Drive:** [{episode}]({folder_link})\n'
        f'- **ไฟล์ต้นฉบับ:** [01_raw_text.txt]({raw_file_link})\n\n'
        f'> {instruction}'
    )
    send_discord_notification(
        title=f'📝 พร้อมแปลแล้ว: {episode}',
        description=instruction,
        color=DISCORD_COLOR_INFO,
        url=folder_link,
        fields=[
            {'name': 'ชื่อตอน', 'value': episode, 'inline': True},
            {'name': 'จำนวนบับเบิล', 'value': f'{total_bubbles} บับเบิล', 'inline': True},
            {'name': '📁 โฟลเดอร์ Drive', 'value': f'[เปิดโฟลเดอร์ Drive]({folder_link})', 'inline': False},
            {'name': '📄 ไฟล์ข้อความต้นฉบับ', 'value': f'[เปิด 01_raw_text.txt]({raw_file_link})', 'inline': False},
        ],
    )


def _notify_manual_drive_timeout(episode, folder_link, timeout_minutes):
    timeout_label = _format_minutes(timeout_minutes)
    msg = (
        '⏳ หมดเวลารอแล้ว — เมื่อวาง `02_translated.txt` เสร็จ '
        'ให้กด Run Workflow เดิมอีกรอบ โปรแกรมจะข้ามขั้นตอนคลีนแล้วส่งออก PSD ให้ทันที'
    )
    print(f'{msg} (รอครบ {timeout_label} นาที) — {folder_link}', flush=True)
    append_step_summary(
        f'## ⏳ หมดเวลารอคำแปล ({timeout_label} นาที) — {episode}\n\n'
        f'{msg}\n\n'
        f'- **โฟลเดอร์ Google Drive:** [{episode}]({folder_link})'
    )
    send_discord_notification(
        title=f'⏳ หมดเวลารอคำแปล: {episode}',
        description=msg,
        color=DISCORD_COLOR_WARNING,
        url=folder_link,
        fields=[
            {'name': '📁 โฟลเดอร์ Drive', 'value': f'[เปิดโฟลเดอร์ Drive]({folder_link})', 'inline': False},
        ],
    )


def _notify_export_complete(episode, dest_link, exported_names):
    summary_files = ', '.join(exported_names[:6])
    if len(exported_names) > 6:
        summary_files += f' (+อีก {len(exported_names) - 6} ไฟล์)'
    print(f'✅ เสร็จเรียบร้อย! ส่งออก PSD ขึ้น Drive แล้ว: {dest_link}', flush=True)
    append_step_summary(
        f'## ✅ เสร็จเรียบร้อย! ส่งออก PSD ขึ้น Drive แล้ว — {episode}\n\n'
        f'- **โฟลเดอร์ผลลัพธ์:** [{episode}]({dest_link})\n'
        f'- **ไฟล์ที่อัปโหลด ({len(exported_names)} ไฟล์):** {summary_files or "-"}'
    )
    send_discord_notification(
        title='✅ เสร็จเรียบร้อย! ส่งออก PSD ขึ้น Drive แล้ว',
        description=f'ตอน **{episode}** ส่งออกและอัปโหลดขึ้น Google Drive เรียบร้อยแล้ว ({len(exported_names)} ไฟล์)',
        color=DISCORD_COLOR_SUCCESS,
        url=dest_link,
        fields=[
            {'name': 'ชื่อตอน', 'value': episode, 'inline': True},
            {'name': 'จำนวนไฟล์', 'value': f'{len(exported_names)} ไฟล์', 'inline': True},
            {'name': '📁 โฟลเดอร์ผลลัพธ์', 'value': f'[เปิดโฟลเดอร์ PSD บน Drive]({dest_link})', 'inline': False},
        ],
    )


def _notify_error(episode, exc_message):
    short_err = str(exc_message).strip() or 'Unknown error'
    if len(short_err) > 500:
        short_err = short_err[:497] + '...'
    title = f'❌ เกิดข้อผิดพลาด: {episode}' if episode else '❌ เกิดข้อผิดพลาดระหว่างรัน Pipeline'
    send_discord_notification(
        title=title,
        description=f'```\n{short_err}\n```',
        color=DISCORD_COLOR_ERROR,
    )


def run(args):
    translation_mode = getattr(args, 'translation_mode', None)
    wait_timeout_minutes = float(getattr(args, 'wait_timeout_minutes', 60.0))
    is_manual_drive = bool(
        args.drive_link
        and args.input_mode == 'image_folder'
        and translation_mode == 'manual_drive'
        and not args.translation_link
    )
    drive_first = args.input_mode == 'image_folder' and not args.translation_link and not is_manual_drive
    want_smart_stitch = bool(args.smart_stitch)
    episode = ''
    text_folder = None
    existing_manual_translation = None
    manual_cache_hit = False
    drive = None
    source_id = None
    job = None
    cache = None

    try:
        if args.drive_link:
            import gdrive_sync as drive
            from tempfile import mkdtemp
            source_id = drive.drive_id(args.drive_link)
            jobs = drive.ROOT / 'runner_jobs'
            jobs.mkdir(parents=True, exist_ok=True)
            job = Path(mkdtemp(prefix=source_id + '-', dir=jobs))
            cache = drive.ROOT / 'runner_cache' / source_id
            args.output_dir = job / 'output'
            if args.input_mode == 'mtp':
                args.input, name = drive.download_drive_file(source_id, job / 'input')
                if args.input.suffix.lower() != '.mtp':
                    raise ValueError('Expected .mtp input')
                episode = Path(name).stem
                args.smart_stitch = args.skip_clean = True
                args.stitch_height, args.stitch_width = 15000, 720
            elif is_manual_drive:
                cache_project = cache / 'project_cleaned.json'
                cache_ready = cache_project.is_file() and not (cache / 'pending').exists()
                if cache_ready:
                    cached_meta = json.loads(cache_project.read_text('utf-8-sig'))
                    episode = cached_meta['drive_folder_name']
                    text_folder = cached_meta.get('drive_text_folder_id') or drive.find_subfolder(
                        drive.TEXT_EXPORT_FOLDER_ID, episode
                    )
                else:
                    folder_meta = drive.metadata(source_id)
                    if folder_meta.get('mimeType') != drive.FOLDER:
                        raise ValueError('Expected a Google Drive folder')
                    episode = folder_meta['name']
                    text_folder = drive.find_subfolder(drive.TEXT_EXPORT_FOLDER_ID, episode)
                existing_manual_translation = _find_existing_translation(
                    drive,
                    (text_folder, source_id),
                    job / 'translation' / drive.TRANSLATED_TEXT_FILENAME,
                )
                if cache_ready:
                    manual_cache_hit = True
                    args.input = cache_project
                    args.skip_clean = True
                    if existing_manual_translation is not None:
                        args.translation = existing_manual_translation[0]
                        args.smart_stitch = want_smart_stitch
                        print(
                            f'พบ 02_translated.txt และแคชคลีนของตอน "{episode}" แล้ว '
                            '— ข้าม OCR/Clean และข้ามการรอ ไปจัดฟอนต์และส่งออก PSD ทันที',
                            flush=True,
                        )
                    else:
                        args.smart_stitch = False
                        print(
                            f'พบแคชคลีนของตอน "{episode}" แล้ว — ข้าม OCR/Clean และเตรียมรอไฟล์ 02_translated.txt',
                            flush=True,
                        )
                else:
                    args.input = job / 'images'
                    episode, _ = drive.download_drive_folder_images(source_id, args.input)
                    args.skip_clean = False
                    args.smart_stitch = False
            elif drive_first:
                args.input = job / 'images'
                episode, _ = drive.download_drive_folder_images(source_id, args.input)
                args.skip_clean = False
                if not args.auto_translate_gemini:
                    args.smart_stitch = False
            else:
                args.input = cache / 'project_cleaned.json'
                if not args.input.is_file() or (cache / 'pending').exists():
                    raise ValueError('No completed clean cache for this Drive folder; finish the initial image_folder job first')
                episode = json.loads(args.input.read_text('utf-8'))['drive_folder_name']
                args.translation, _ = drive.download_drive_file(args.translation_link, job / 'translation')
                if args.translation.suffix.lower() != '.txt':
                    raise ValueError('Expected .txt translation')
                args.skip_clean = True

        source = args.input.resolve(strict=True)
        if not 800 <= args.stitch_height <= 29400 or not 200 <= args.stitch_width <= 6000:
            raise ValueError('Stitch height must be 800..29400 and width 200..6000')
        output = (args.output_dir or source.parent / (source.stem + '_output')).resolve()
        if output == source:
            raise ValueError('Choose a separate output folder')
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            from tempfile import mkdtemp
            output = Path(mkdtemp(prefix='run-', dir=output))
        print(f'Output: {output}', flush=True)
        pages, drafts = [], {}
        stats = dict(verified=0, review=0, existing=0, failures=[])
        try:
            import torch
            gpu = torch.cuda.is_available()
        except ImportError:
            gpu = False
        print(f'0% CUDA (PyTorch): {gpu}', flush=True)
        cancel = threading.Event()
        pipeline = None

        use_rapid = drive_first or (is_manual_drive and not manual_cache_hit)

        def reader(for_detection=False):
            nonlocal pipeline
            if pipeline is None:
                pipeline = app.LocalPipeline(
                    app.OCRCache(),
                    mode='rapid' if use_rapid else 'auto',
                    lang=args.lang,
                    gpu=gpu,
                    neural=for_detection,
                    type_scan=for_detection,
                    prefer_rapid=True,
                    log=print,
                )
            return pipeline

        try:
            if source.is_dir():
                images = sorted(
                    (p for p in source.iterdir() if p.is_file() and p.suffix.lower() in app.IMAGE_EXTS),
                    key=lambda p: app.natural_sort_key(p.name),
                )
                for i, path in enumerate(images, 1):
                    page = app.Page(str(path))
                    pages.append(page)
                    page.bubbles = reader(True).scan(app.load_image(path))
                    page.processed = True
                    print(f'OCR {100*i/len(images):.0f}% {path.name}', flush=True)
            else:
                data = json.loads(source.read_text('utf-8-sig')) if source.suffix.lower() == '.json' else {}
                if source.suffix.lower() == '.mtp' or 'files' in data:
                    pages, _, _ = app.read_mtp(source, cancel, emit, output / 'source_mtp_pages')
                elif data.get('format') == 'manhwa-ocr-project' and data.get('version') in (2, 3, 4, 5):
                    pages, drafts, missing = app.read_project_pages(data, source)
                    if missing:
                        raise FileNotFoundError(missing[0].path)
                else:
                    raise ValueError('Unsupported project format')
            if not pages:
                raise ValueError('No pages in input')

            if args.auto_translate_gemini and not args.translation and not is_manual_drive:
                tr_report = translate_pages_with_gemini(
                    pages,
                    source_lang=args.lang,
                    translation_note=args.translation_note,
                    model=args.gemini_model or DEFAULT_GEMINI_MODEL,
                    log=print,
                )
                stats['translation'] = {
                    'provider': 'google-genai',
                    'model': tr_report['model'],
                    'translated': tr_report['translated_count'],
                    'candidates': tr_report['total_candidates'],
                    'batches': tr_report['batches'],
                }
                if tr_report['translated_count']:
                    (output / 'translation_gemini.txt').write_text(
                        export_source_script(pages, 'id_template'), encoding='utf-8'
                    )

            if drive_first:
                cache.mkdir(parents=True, exist_ok=True)
                (cache / 'pending').write_text(job.name, encoding='utf-8')
                text_folder = drive.create_job_subfolder(drive.TEXT_EXPORT_FOLDER_ID, episode)
                for mode, suffix in [('id_template', '_ocr.txt'), ('numbered', '_ocr_numbered.txt')]:
                    script = job / (drive.local_name(episode) + suffix)
                    script.write_text(export_source_script(pages, mode), encoding='utf-8')
                    drive.upload_file_to_folder(script, text_folder)
                if args.auto_translate_gemini:
                    print('แปลภาษาไทยด้วย Gemini และส่งสคริปต์แล้ว กำลังสร้าง Mask, Clean และส่งออก PSD ต่อในรอบเดียว', flush=True)
                else:
                    print('ส่งต้นฉบับพร้อมแปลแล้ว กำลังสร้าง Mask และ Clean ต่อ', flush=True)
                for page in pages:
                    raw = app.load_image(page.path)
                    for b in page.bubbles:
                        region = auto_region(raw, b)
                        if region is not None:
                            drafts[(page.id, b.id)] = region
            elif is_manual_drive and not manual_cache_hit:
                cache.mkdir(parents=True, exist_ok=True)
                (cache / 'pending').write_text(job.name, encoding='utf-8')
                print('OCR เสร็จแล้ว กำลังสร้าง Mask และ Clean ก่อนส่งออก 01_raw_text.txt', flush=True)
                for page in pages:
                    raw = app.load_image(page.path)
                    for b in page.bubbles:
                        region = auto_region(raw, b)
                        if region is not None:
                            drafts[(page.id, b.id)] = region

            if args.translation:
                _apply_translations(args.translation, pages)
            if args.smart_stitch:
                pages, drafts, _ = app.stitch_pages(
                    pages, drafts, args.stitch_height, 'User Customized Width',
                    args.stitch_width, 'Smart Pixel Comparison', 90, 5, 0,
                    output / 'stitched_pages', cancel, emit, strict=True,
                )

            fonts = load_font_presets()[args.preset]
            cleaner = Cleaner()
            for i, page in enumerate(pages, 1):
                raw = app.load_image(page.path)
                work = compose_page(raw, page.clean_patches)
                existing = {p.get('bubble_id') for p in page.clean_patches}
                for b in page.bubbles:
                    key = (page.id, b.id)
                    if b.id in existing:
                        stats['existing'] += 1
                    elif not args.skip_clean:
                        try:
                            region = auto_region(work, b, drafts.get(key))
                            if region is not None:
                                drafts[key] = region
                                def verify(result):
                                    pipe = reader()
                                    engine = next(pipe.iter_engines())
                                    lines = pipe.read(engine, result, 'cli-clean-verify')
                                    return [
                                        dict(
                                            box=[
                                                l.box[0] + region.box[0],
                                                l.box[1] + region.box[1],
                                                l.box[2] + region.box[0],
                                                l.box[3] + region.box[1],
                                            ],
                                            text=l.text,
                                        )
                                        for l in lines
                                    ]
                                repaired, mask, method, quality = clean_checked(
                                    cleaner, region, cancel=cancel, reader=verify
                                )
                                patch = encode_patch(region.box, repaired, mask, method, b.id)
                                patch['quality'] = quality
                                page.clean_patches.append(patch)
                                work.paste(repaired, region.box[:2], Image.fromarray(mask))
                                if quality['needs_review']:
                                    region.mask = mask
                                    stats['review'] += 1
                                else:
                                    drafts.pop(key, None)
                                    stats['verified'] += 1
                        except Exception as exc:
                            stats['failures'].append(dict(page=i, bubble=b.id, error=str(exc)))
                            print(f'CLEAN failed: {i}/{b.id}: {exc}', flush=True)
                    if b.translation.strip():
                        b.frame_type = classify_bubble_type(raw, b, gpu=gpu)
                        b.font_choice = fonts[b.frame_type]
                        try:
                            resolve_font(b.font_choice)
                        except RuntimeError:
                            print(f'Font unavailable: {b.font_choice}; using {app.DEFAULT_FONT_CHOICE}', flush=True)
                            b.font_choice = app.DEFAULT_FONT_CHOICE
                        b.custom_size = b.custom_width = 0
                        b.custom_rotation = None
                        b.text_offset = []
                        b.text_layer = layout_text(
                            work,
                            b,
                            page_bubbles=page.bubbles,
                            original=raw,
                            cleaned=work if page.clean_patches else None,
                            auto_stroke=args.auto_stroke,
                            auto_gradient=args.auto_gradient,
                        )
                print(f'CLEAN / layout {100*i/len(pages):.0f}%', flush=True)

            checkpoint(output / 'project.json', pages, drafts, args.preset)
            if drive_first and not stats['failures']:
                checkpoint(cache / 'project_cleaned.json', pages, drafts, args.preset, drive_folder_name=episode)
                (cache / 'pending').unlink(missing_ok=True)

            if is_manual_drive:
                if stats['failures']:
                    _notify_error(episode, f"CLEAN failed for {len(stats['failures'])} bubble(s)")
                    return 1
                if not text_folder:
                    text_folder = drive.get_or_create_subfolder(drive.TEXT_EXPORT_FOLDER_ID, episode)
                if not manual_cache_hit:
                    checkpoint(
                        cache / 'project_cleaned.json',
                        pages,
                        drafts,
                        args.preset,
                        drive_folder_name=episode,
                        drive_text_folder_id=text_folder,
                    )
                    (cache / 'pending').unlink(missing_ok=True)
                if not args.translation:
                    total_bubbles = sum(len(p.bubbles) for p in pages)
                    if existing_manual_translation is not None:
                        tr_path = existing_manual_translation[0]
                    elif total_bubbles == 0:
                        tr_path = None
                        print('ไม่พบบับเบิลข้อความในตอนนี้ ข้ามขั้นตอนรอคำแปลและส่งออกไฟล์คลีนทันที', flush=True)
                    else:
                        raw_script = job / drive.RAW_TEXT_FILENAME
                        raw_script.write_text(export_source_script(pages, 'id_template'), encoding='utf-8')
                        raw_file_id = drive.upsert_file_to_folder(
                            raw_script,
                            text_folder,
                            remote_name=drive.RAW_TEXT_FILENAME,
                            mimetype='text/plain',
                        )
                        folder_link = drive.folder_url(text_folder)
                        raw_file_link = drive.file_url(raw_file_id)
                        _notify_manual_drive_waiting(
                            episode, total_bubbles, folder_link, raw_file_link, wait_timeout_minutes
                        )
                        if wait_timeout_minutes <= 0:
                            print('อัปโหลด 01_raw_text.txt เรียบร้อยแล้ว (wait_timeout_minutes=0 ไม่รอคำแปล)', flush=True)
                            return 0
                        waited = drive.wait_for_translation_file(
                            text_folder,
                            job / 'translation' / drive.TRANSLATED_TEXT_FILENAME,
                            filename=drive.TRANSLATED_TEXT_FILENAME,
                            timeout_minutes=wait_timeout_minutes,
                            poll_interval=POLL_INTERVAL_SECONDS,
                            extra_folder_ids=(source_id,),
                            log=print,
                        )
                        if waited is None:
                            _notify_manual_drive_timeout(episode, folder_link, wait_timeout_minutes)
                            return 0
                        tr_path = waited[0]
                        print(f'พบไฟล์คำแปลแล้ว ({waited[1].get("name")}) — กำลังนำเข้า จัดฟอนต์ และส่งออก PSD', flush=True)
                    if tr_path is not None:
                        _apply_translations(tr_path, pages)
                        if want_smart_stitch:
                            pages, drafts, _ = app.stitch_pages(
                                pages, drafts, args.stitch_height, 'User Customized Width',
                                args.stitch_width, 'Smart Pixel Comparison', 90, 5, 0,
                                output / 'stitched_pages', cancel, emit, strict=True,
                            )
                        _layout_translated_pages(pages, fonts, gpu, args.auto_stroke, args.auto_gradient)
                        checkpoint(output / 'project.json', pages, drafts, args.preset)

            psd_backend = getattr(args, 'psd_backend', DEFAULT_PSD_BACKEND)
            files_to_upload = []
            write_psd = args.export_format in ('psd', 'psd+mtp', 'all')
            write_png = args.export_format == 'png' or (not args.drive_link and args.export_format == 'all')
            use_pp_session = (
                write_psd
                and psd_backend == 'photopea'
                and export_psd is _real_export_psd
            )
            with (PhotopeaSession() if use_pp_session else nullcontext()) as pp_session:
                for i, page in enumerate(pages, 1):
                    if not (write_png or write_psd):
                        break
                    image = app.load_image(page.path)
                    if write_png:
                        composite_text_crop(compose_page(image, page.clean_patches), page.bubbles, (0, 0)).save(output / f'{i:02}.png')
                        files_to_upload.append(output / f'{i:02}.png')
                    if write_psd:
                        started = perf_counter()
                        if export_psd is _real_export_psd:
                            report = export_psd(
                                image, page, output / f'{i:02}.psd',
                                backend=psd_backend, session=pp_session,
                            )
                        else:
                            report = export_psd(image, page, output / f'{i:02}.psd')
                        print(f'Exported {i:02}.psd ({perf_counter()-started:.2f}s)', flush=True)
                        files_to_upload.append(output / f'{i:02}.psd')
                        for warning in report.get('warnings', []):
                            print(warning, flush=True)
                    print(f'Export pages {100*i/len(pages):.0f}%', flush=True)
            if args.export_format in ('mtp', 'psd+mtp', 'all'):
                mtp_path = output / (drive.local_name(episode) + '.mtp' if args.drive_link else 'project.mtp')
                app.write_mtp(mtp_path, pages, fonts, cancel, emit)
                files_to_upload.append(mtp_path)
            if args.drive_link and not stats['failures']:
                dest = drive.create_job_subfolder(drive.PSD_OUTPUT_FOLDER_ID, episode)
                for path in files_to_upload:
                    drive.upload_file_to_folder(path, dest)
                dest_link = drive.folder_url(dest)
                if is_manual_drive or os.environ.get('DISCORD_WEBHOOK_URL') or os.environ.get('GITHUB_STEP_SUMMARY'):
                    _notify_export_complete(episode, dest_link, [p.name for p in files_to_upload])
        finally:
            checkpoint(output / 'project.json', pages, drafts, args.preset)
            app.atomic_json(output / 'clean_report.json', stats)
            print('CLEAN:', json.dumps(stats, ensure_ascii=False), flush=True)
        if stats['failures']:
            _notify_error(episode, f"CLEAN failed for {len(stats['failures'])} bubble(s)")
            return 1
        return 0
    except Exception as exc:
        _notify_error(episode, exc)
        raise


if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):stream.reconfigure(encoding='utf-8')
    try:sys.exit(run(options()))
    except Exception as exc:sys.exit(f'Pipeline failed: {exc}')
