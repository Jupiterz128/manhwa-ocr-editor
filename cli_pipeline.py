"""Manhwa OCR Editor 3.1 batch entry point for an interactive Windows login."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import threading

from PIL import Image
import manhwa_ocr as app
from bubble_cleaner import Cleaner, compose_page, encode_patch, encode_region, prepare_region
from clean_quality import clean_checked
from gemini_translator import DEFAULT_GEMINI_MODEL, translate_pages_with_gemini
from photoshop_export import DEFAULT_PSD_BACKEND, PhotopeaSession, export_psd as _real_export_psd
from text_layout import (classify_bubble_type, composite_text_crop, detect_and_parse_translations,
                         export_source_script, layout_text, load_font_presets, resolve_font)

export_psd = _real_export_psd


def boolean(value):
    if value.lower() in ('true', 'false'):
        return value.lower() == 'true'
    raise argparse.ArgumentTypeError('expected true or false')


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
    p.add_argument('--export-format', choices=['psd','mtp','png','all'], default='psd')
    p.add_argument('--psd-backend', choices=['photoshop','photopea'],
                   default=os.environ.get('MANHWA_PSD_BACKEND', DEFAULT_PSD_BACKEND))
    p.add_argument('--auto-translate-gemini', '--auto-translate', dest='auto_translate_gemini',
                   nargs='?', const=True, default=False, type=boolean)
    p.add_argument('--gemini-model', default=None)
    args=p.parse_args()
    if bool(args.drive_link)!=bool(args.input_mode):
        p.error('--drive-link and --input-mode must be used together')
    if args.translation_link and args.input_mode!='image_folder':
        p.error('--translation-link requires image_folder')
    if args.drive_link and (args.translation or args.output_dir):
        p.error('Use --translation-link and the configured Drive output folders for Drive jobs')
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


def run(args):
    drive_first=args.input_mode=='image_folder' and not args.translation_link
    if args.drive_link:
        import gdrive_sync as drive
        from tempfile import mkdtemp
        source_id=drive.drive_id(args.drive_link)
        jobs=drive.ROOT/'runner_jobs'
        jobs.mkdir(parents=True,exist_ok=True)
        job=Path(mkdtemp(prefix=source_id+'-',dir=jobs))
        cache=drive.ROOT/'runner_cache'/source_id
        args.output_dir=job/'output'
        args.export_format='psd'
        if args.input_mode=='mtp':
            args.input,name=drive.download_drive_file(source_id,job/'input')
            if args.input.suffix.lower()!='.mtp':raise ValueError('Expected .mtp input')
            episode=Path(name).stem
            args.smart_stitch=args.skip_clean=True
            args.stitch_height,args.stitch_width=15000,720
        elif drive_first:
            args.input=job/'images'
            episode,_=drive.download_drive_folder_images(source_id,args.input)
            args.skip_clean=False
            if not args.auto_translate_gemini:
                args.smart_stitch=False
        else:
            args.input=cache/'project_cleaned.json'
            if not args.input.is_file() or (cache/'pending').exists():
                raise ValueError('No completed clean cache for this Drive folder; finish the initial image_folder job first')
            episode=json.loads(args.input.read_text('utf-8'))['drive_folder_name']
            args.translation,_=drive.download_drive_file(args.translation_link,job/'translation')
            if args.translation.suffix.lower()!='.txt':raise ValueError('Expected .txt translation')
            args.skip_clean=True
    source = args.input.resolve(strict=True)
    if not 800 <= args.stitch_height <= 29400 or not 200 <= args.stitch_width <= 6000:
        raise ValueError('Stitch height must be 800..29400 and width 200..6000')
    output = (args.output_dir or source.parent/(source.stem+'_output')).resolve()
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

    def reader(for_detection=False):
        nonlocal pipeline
        if pipeline is None:
            pipeline = app.LocalPipeline(app.OCRCache(), mode='rapid' if drive_first else 'auto', lang=args.lang, gpu=gpu,
                                         neural=for_detection, type_scan=for_detection, prefer_rapid=True, log=print)
        return pipeline

    try:
        if source.is_dir():
            images = sorted((p for p in source.iterdir() if p.is_file() and p.suffix.lower() in app.IMAGE_EXTS),
                            key=lambda p:app.natural_sort_key(p.name))
            for i,path in enumerate(images,1):
                page = app.Page(str(path))
                pages.append(page)
                page.bubbles = reader(True).scan(app.load_image(path))
                page.processed = True
                print(f'OCR {100*i/len(images):.0f}% {path.name}', flush=True)
        else:
            data = json.loads(source.read_text('utf-8-sig')) if source.suffix.lower()=='.json' else {}
            if source.suffix.lower()=='.mtp' or 'files' in data:
                pages,_,_ = app.read_mtp(source,cancel,emit,output/'source_mtp_pages')
            elif data.get('format')=='manhwa-ocr-project' and data.get('version') in (2,3,4,5):
                pages,drafts,missing = app.read_project_pages(data,source)
                if missing:raise FileNotFoundError(missing[0].path)
            else:
                raise ValueError('Unsupported project format')
        if not pages:raise ValueError('No pages in input')
        if args.auto_translate_gemini and not args.translation:
            tr_report = translate_pages_with_gemini(
                pages,
                source_lang=args.lang,
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
                (output/'translation_gemini.txt').write_text(
                    export_source_script(pages,'id_template'),encoding='utf-8'
                )
        if drive_first:
            cache.mkdir(parents=True,exist_ok=True)
            (cache/'pending').write_text(job.name,encoding='utf-8')
            text_folder=drive.create_job_subfolder(drive.TEXT_EXPORT_FOLDER_ID,episode)
            for mode,suffix in [('id_template','_ocr.txt'),('numbered','_ocr_numbered.txt')]:
                script=job/(drive.local_name(episode)+suffix)
                script.write_text(export_source_script(pages,mode),encoding='utf-8')
                drive.upload_file_to_folder(script,text_folder)
            if args.auto_translate_gemini:
                print('แปลภาษาไทยด้วย Gemini และส่งสคริปต์แล้ว กำลังสร้าง Mask, Clean และส่งออก PSD ต่อในรอบเดียว',flush=True)
            else:
                print('ส่งต้นฉบับพร้อมแปลแล้ว กำลังสร้าง Mask และ Clean ต่อ',flush=True)
            for page in pages:
                raw=app.load_image(page.path)
                for b in page.bubbles:
                    drafts[(page.id,b.id)]=prepare_region(raw,b)
        if args.translation:
            changes,report = detect_and_parse_translations(args.translation.read_text('utf-8-sig'),pages)
            print(f"Translations: {report['matched_count']}/{report['total_bubbles']}", *report['warnings'],flush=True)
            if not changes:raise ValueError('No translations matched')
            for page in pages:
                for b in page.bubbles:
                    if b.id in changes:b.translation=changes[b.id]
                    if b.id in report['frame_tags']:
                        b.frame_type=report['frame_tags'][b.id]
                        b.frame_type_locked=True
        if args.smart_stitch:
            pages,drafts,_ = app.stitch_pages(pages,drafts,args.stitch_height,'User Customized Width',
                args.stitch_width,'Smart Pixel Comparison',90,5,0,output/'stitched_pages',cancel,emit,strict=True)
        fonts = load_font_presets()[args.preset]
        cleaner = Cleaner()
        for i,page in enumerate(pages,1):
            raw = app.load_image(page.path)
            work = compose_page(raw,page.clean_patches)
            existing = {p.get('bubble_id') for p in page.clean_patches}
            for b in page.bubbles:
                key = (page.id,b.id)
                if b.id in existing:
                    stats['existing']+=1
                elif not args.skip_clean:
                    try:
                        region = drafts.get(key) or prepare_region(work,b)
                        drafts[key] = region
                        def verify(result):
                            pipe=reader()
                            engine=next(pipe.iter_engines())
                            lines=pipe.read(engine,result,'cli-clean-verify')
                            return [dict(box=[l.box[0]+region.box[0],l.box[1]+region.box[1],
                                              l.box[2]+region.box[0],l.box[3]+region.box[1]],text=l.text) for l in lines]
                        repaired,mask,method,quality=clean_checked(cleaner,region,cancel=cancel,reader=verify)
                        patch=encode_patch(region.box,repaired,mask,method,b.id)
                        patch['quality']=quality
                        page.clean_patches.append(patch)
                        work.paste(repaired,region.box[:2],Image.fromarray(mask))
                        if quality['needs_review']:
                            region.mask=mask
                            stats['review']+=1
                        else:
                            drafts.pop(key,None)
                            stats['verified']+=1
                    except Exception as exc:
                        stats['failures'].append(dict(page=i,bubble=b.id,error=str(exc)))
                        print(f'CLEAN failed: {i}/{b.id}: {exc}',flush=True)
                if b.translation.strip():
                    b.frame_type=classify_bubble_type(raw,b,gpu=gpu)
                    b.font_choice=fonts[b.frame_type]
                    try:resolve_font(b.font_choice)
                    except RuntimeError:
                        print(f'Font unavailable: {b.font_choice}; using {app.DEFAULT_FONT_CHOICE}',flush=True)
                        b.font_choice=app.DEFAULT_FONT_CHOICE
                    b.custom_size=b.custom_width=0
                    b.custom_rotation=None
                    b.text_offset=[]
                    b.text_layer=layout_text(work,b)
            print(f'CLEAN / layout {100*i/len(pages):.0f}%',flush=True)
        checkpoint(output/'project.json',pages,drafts,args.preset)
        if drive_first and not stats['failures']:
            checkpoint(cache/'project_cleaned.json',pages,drafts,args.preset,drive_folder_name=episode)
            (cache/'pending').unlink()
        psd_backend = getattr(args, 'psd_backend', DEFAULT_PSD_BACKEND)
        use_pp_session = (
            args.export_format in ('psd','all')
            and psd_backend=='photopea'
            and export_psd is _real_export_psd
        )
        pp_session = PhotopeaSession() if use_pp_session else None
        try:
            if pp_session is not None:
                pp_session.start()
            for i,page in enumerate(pages,1):
                image=app.load_image(page.path)
                if args.export_format in ('png','all'):
                    composite_text_crop(compose_page(image,page.clean_patches),page.bubbles,(0,0)).save(output/f'{i:02}.png')
                if args.export_format in ('psd','all'):
                    if export_psd is _real_export_psd:
                        report=export_psd(image,page,output/f'{i:02}.psd',
                                          backend=psd_backend,session=pp_session)
                    else:
                        report=export_psd(image,page,output/f'{i:02}.psd')
                    for warning in report.get('warnings',[]):print(warning,flush=True)
                print(f'Export pages {100*i/len(pages):.0f}%',flush=True)
        finally:
            if pp_session is not None:
                pp_session.close()
        if args.drive_link and not stats['failures']:
            dest=drive.create_job_subfolder(drive.PSD_OUTPUT_FOLDER_ID,episode)
            for i in range(1,len(pages)+1):
                drive.upload_file_to_folder(output/f'{i:02}.psd',dest)
        if args.export_format in ('mtp','all'):
            app.write_mtp(output/'project.mtp',pages,fonts,cancel,emit)
    finally:
        checkpoint(output/'project.json',pages,drafts,args.preset)
        app.atomic_json(output/'clean_report.json',stats)
        print('CLEAN:',json.dumps(stats,ensure_ascii=False),flush=True)
    return 1 if stats['failures'] else 0


if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):stream.reconfigure(encoding='utf-8')
    try:sys.exit(run(options()))
    except Exception as exc:sys.exit(f'Pipeline failed: {exc}')
