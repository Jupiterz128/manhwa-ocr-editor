import hashlib
import io
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import web_api as studio
import test_text_effects_io as fixtures
from bubble_cleaner import decode_region
from manhwa_ocr import Bubble, Line, Page, read_mtp, write_mtp
from text_layout import is_current


class StudioPersistenceTests(unittest.TestCase):
    def test_transform_paint_undo_and_mtp(self):
        im, b = fixtures.styled_fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp)):
            source = Path(tmp) / '01.png'
            im.save(source)
            before = hashlib.sha256(source.read_bytes()).digest()
            page = Page(str(source), [b])
            studio.save('chapter', 'Chapter', [page], {})
            style = {
                'id': b.id,
                'box': [140, 100, 420, 210],
                'rotation': 12,
                'font_size': 36,
                'color': [1, 48, 230],
                'alignment': 'right',
            }
            studio.action('chapter', 0, 'edit', style)
            studio.action('chapter', 0, 'paint', {'id': b.id, 'points': [[200, 140], [220, 144]], 'radius': 8})
            pages, masks = studio.load('chapter')
            self.assertTrue(masks[b.id]['manual_edited'])
            studio.action('chapter', 0, 'undo', {'id': b.id})
            pages, masks = studio.load('chapter')
            self.assertNotIn(b.id, masks)
            path = Path(tmp) / 'export.mtp'
            write_mtp(path, pages, {}, threading.Event(), lambda _: None)
            restored = read_mtp(path, threading.Event(), lambda _: None)[0][0].bubbles[0]
            self.assertTrue(is_current(restored))
            for field in ['box', 'editor_box', 'font_size', 'rotation', 'color', 'alignment', 'auto_leading', 'stroke', 'gradient']:
                self.assertEqual(restored.text_layer[field], pages[0].bubbles[0].text_layer[field])
            self.assertTrue(restored.text_layer['auto_leading'])
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)

    def test_clean_mode_mask_edit_and_clean_patch(self):
        im = Image.new('RGB', (400, 300), '#9fb3c8')
        d = ImageDraw.Draw(im)
        d.ellipse((40, 40, 360, 260), fill='white', outline='#111111', width=4)
        d.rectangle((140, 125, 260, 175), fill='black')
        b = fixtures.styled_fixture()[1]
        b.box = [40, 40, 360, 260]
        b.lines = [{'box': [140, 125, 260, 175], 'text': 'TEST'}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp)):
            source = Path(tmp) / '01.png'
            im.save(source)
            studio.save('cleanjob', 'Clean', [Page(str(source), [b])], {})
            studio.action('cleanjob', 0, 'paint', {'id': b.id, 'points': [[145, 150], [255, 150]], 'radius': 24, 'erase': False})
            _, masks = studio.load('cleanjob')
            reg = decode_region(masks[b.id])
            self.assertTrue(reg.manual_edited)
            self.assertEqual(int(np.count_nonzero((reg.mask > 0) & (reg.safe == 0))), 0)
            res = studio.action('cleanjob', 0, 'clean', {'id': b.id})
            self.assertIn('คลีน 1 กรอบ', res['note'])
            self.assertTrue(res['page']['clean'])
            cleaned_resp = studio.image('cleanjob', 0, view='thai')
            cleaned_arr = np.asarray(Image.open(io.BytesIO(cleaned_resp.body)).convert('RGB'))
            self.assertGreater(int(cleaned_arr[135:165, 150:250].min()), 240)
            self.assertEqual(tuple(cleaned_arr[10, 10]), (159, 179, 200))

    def test_add_delete_bubble_and_txt_export_import(self):
        im, b = fixtures.styled_fixture()
        b.text = 'Hello world'
        b.translation = ''
        b.text_layer = {}
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp)):
            source = Path(tmp) / 'ep01.png'
            im.save(source)
            studio.save('txtjob', 'ep01.png', [Page(str(source), [b])], {})

            added = studio.action('txtjob', 0, 'add_bubble', {'box': [30, 30, 140, 110]})
            new_id = added['id']
            self.assertEqual(len(added['page']['bubbles']), 2)
            self.assertEqual(added['page']['bubbles'][-1]['box'], [30, 30, 140, 110])

            # Resize/move the newly created untranslated bubble via bubble_box
            moved = studio.action('txtjob', 0, 'edit', {'id': new_id, 'bubble_box': [45, 50, 190, 145]})
            self.assertEqual(moved['page']['bubbles'][-1]['box'], [45, 50, 190, 145])

            exp_id = studio.export_txt('txtjob', 'id_template')
            self.assertIn(b.id, exp_id['text'])
            self.assertEqual(exp_id['filename'], 'ep01_ocr.txt')
            exp_num = studio.export_txt('txtjob', 'numbered')
            self.assertIn('1. Hello world', exp_num['text'])

            imp = studio.import_txt('txtjob', {'text': f'รหัส: {b.id}\nคำต้นฉบับ: Hello world\nคำแปลไทย: สวัสดีชาวโลก'})
            self.assertEqual(imp['applied'], 1)
            pages, _ = studio.load('txtjob')
            self.assertEqual(pages[0].bubbles[0].translation, 'สวัสดีชาวโลก')
            self.assertTrue(pages[0].bubbles[0].text_layer['auto_leading'])

            deleted = studio.action('txtjob', 0, 'delete_bubble', {'id': new_id})
            self.assertEqual(len(deleted['page']['bubbles']), 1)
            self.assertEqual(deleted['page']['bubbles'][0]['id'], b.id)

    def test_ocr_options_and_single_bubble_rapid_ocr(self):
        im = Image.new('RGB', (400, 300), 'white')
        b_existing = Bubble(box=[20, 20, 160, 100], text='KEEP ME', translation='เก็บไว้', score=0.98)
        b_new = Bubble(box=[180, 120, 360, 220], text='', translation='', score=1.0)
        calls = []

        class FakePipe:
            def candidates(self, image, bubble):
                return [
                    {'text': 'HELLO', 'score': 0.88, 'engine': 'RapidOCR (PP-OCRv5)/original-crop'},
                    {'text': 'HELLO!', 'score': 0.94, 'engine': 'RapidOCR (PP-OCRv5)/contrast-2x'},
                ]

            def scan(self, image):
                return [
                    Bubble(box=[10, 10, 100, 80], text='SCANNED', score=0.95, engine='RapidOCR (PP-OCRv5)'),
                    Bubble(box=[150, 20, 240, 120], text='', score=0.0, engine='detected / unread'),
                    Bubble(box=[250, 20, 340, 120], text='   ', score=0.0, engine='detected / unread'),
                ]

        def fake_reader(lang='ko+en', engine='auto', neural=True, type_scan=False):
            calls.append((lang, engine, neural, type_scan))
            return FakePipe()

        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp)), patch.object(studio, 'reader', side_effect=fake_reader):
            source = Path(tmp) / '01.png'
            im.save(source)
            studio.save('ocrjob', 'OCR', [Page(str(source), [b_existing, b_new])], {})

            # Single-bubble RapidOCR delegates to LocalPipeline.candidates, updates ONLY b_new
            res = studio.action('ocrjob', 0, 'ocr_bubble', {'id': b_new.id, 'lang': 'en'})
            self.assertEqual(res['text'], 'HELLO!')
            self.assertAlmostEqual(res['score'], 0.94, places=2)
            self.assertEqual(calls[-1], ('en', 'rapid', False, False))
            pages, _ = studio.load('ocrjob')
            self.assertEqual(pages[0].bubbles[0].text, 'KEEP ME')
            self.assertEqual(pages[0].bubbles[0].translation, 'เก็บไว้')
            self.assertEqual(pages[0].bubbles[1].text, 'HELLO!')
            self.assertEqual(len(pages[0].bubbles[1].candidates), 2)

            # Batch OCR uses legacy type_scan=False and filters out empty / OCR 0% false positives
            bres = studio.batch_action('ocrjob', 'ocr', {'replace': True, 'lang': 'zh+en', 'engine': 'paddle'})
            self.assertEqual(calls[-1], ('zh+en', 'paddle', True, False))
            self.assertIn('paddle', bres['note'])
            self.assertIn('zh+en', bres['note'])
            self.assertEqual(len(bres['pages'][0]['bubbles']), 1)
            self.assertEqual(bres['pages'][0]['bubbles'][0]['text'], 'SCANNED')

            # Manually created frames via + กรอบ are kept even when empty
            added = studio.action('ocrjob', 0, 'add_bubble', {'box': [20, 140, 150, 220]})
            self.assertEqual(len(added['page']['bubbles']), 2)
            self.assertEqual(added['page']['bubbles'][-1]['text'], '')

    def test_mask_edit_preview_cancel_and_apply(self):
        im = Image.new('RGB', (400, 300), '#9fb3c8')
        d = ImageDraw.Draw(im)
        d.ellipse((40, 40, 360, 260), fill='white', outline='#111111', width=4)
        d.rectangle((140, 125, 260, 175), fill='black')
        b = Bubble(box=[40, 40, 360, 260], text='TEST', lines=[{'box': [140, 125, 260, 175], 'text': 'TEST'}], score=0.95)
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp)):
            source = Path(tmp) / '01.png'
            im.save(source)
            studio.save('maskjob', 'Mask', [Page(str(source), [b])], {})

            studio.action('maskjob', 0, 'mask_start', {'id': b.id})
            studio.action('maskjob', 0, 'mask_paint', {'id': b.id, 'points': [[145, 150], [255, 150]], 'radius': 24, 'erase': False})
            prev = studio.action('maskjob', 0, 'mask_preview', {'id': b.id})
            self.assertTrue(prev['has_preview'])
            preview_resp = studio.image('maskjob', 0, view='mask_preview', bid=b.id)
            preview_arr = np.asarray(Image.open(io.BytesIO(preview_resp.body)).convert('RGB'))
            self.assertGreater(int(preview_arr[135:165, 150:250].min()), 240)
            pages_mid, masks_mid = studio.load('maskjob')
            self.assertEqual(pages_mid[0].clean_patches, [])
            self.assertNotIn(b.id, masks_mid)

            studio.action('maskjob', 0, 'mask_cancel', {'id': b.id})
            self.assertNotIn(('maskjob', 0, b.id), studio.mask_sessions)
            pages_after_cancel, masks_after_cancel = studio.load('maskjob')
            self.assertEqual(pages_after_cancel[0].clean_patches, [])
            self.assertNotIn(b.id, masks_after_cancel)

            studio.action('maskjob', 0, 'mask_start', {'id': b.id})
            studio.action('maskjob', 0, 'mask_paint', {'id': b.id, 'points': [[145, 150], [255, 150]], 'radius': 24, 'erase': False})
            studio.action('maskjob', 0, 'mask_undo', {'id': b.id})
            studio.action('maskjob', 0, 'mask_paint', {'id': b.id, 'points': [[145, 150], [255, 150]], 'radius': 24, 'erase': False})
            studio.action('maskjob', 0, 'mask_preview', {'id': b.id})
            applied = studio.action('maskjob', 0, 'mask_apply', {'id': b.id})
            self.assertTrue(applied['page']['clean'])
            pages_done, masks_done = studio.load('maskjob')
            self.assertEqual(len(pages_done[0].clean_patches), 1)
            self.assertTrue(masks_done[b.id]['manual_edited'])
            clean_resp = studio.image('maskjob', 0, view='clean')
            clean_arr = np.asarray(Image.open(io.BytesIO(clean_resp.body)).convert('RGB'))
            self.assertGreater(int(clean_arr[135:165, 150:250].min()), 240)

    def test_batch_mask_and_clean_all_pages(self):
        im = Image.new('RGB', (400, 300), '#9fb3c8')
        d = ImageDraw.Draw(im)
        d.ellipse((40, 40, 360, 260), fill='white', outline='#111111', width=4)
        d.rectangle((140, 125, 260, 175), fill='black')
        b1 = Bubble(box=[40, 40, 360, 260], text='A', lines=[{'box': [140, 125, 260, 175], 'text': 'A'}], score=0.96)
        b2 = Bubble(box=[40, 40, 360, 260], text='B', lines=[{'box': [140, 125, 260, 175], 'text': 'B'}], score=0.96)
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp)):
            p1 = Path(tmp) / '01.png'
            p2 = Path(tmp) / '02.png'
            im.save(p1)
            im.save(p2)
            studio.save('batchjob', 'Batch', [Page(str(p1), [b1]), Page(str(p2), [b2])], {})
            rm = studio.batch_action('batchjob', 'mask', {})
            self.assertIn('2 หน้า', rm['note'])
            _, masks = studio.load('batchjob')
            self.assertIn(b1.id, masks)
            self.assertIn(b2.id, masks)
            with patch.object(studio, '_make_verifier', return_value=lambda _: []):
                rc = studio.batch_action('batchjob', 'clean', {})
            self.assertIn('2 หน้า', rc['note'])
            self.assertTrue(all(pg['clean'] for pg in rc['pages']))

    def test_json_save_reload_and_txt_save_as_and_export_destination(self):
        from fastapi.testclient import TestClient

        im, b = fixtures.styled_fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'store'):
            root = Path(tmp)
            source = root / 'ep01.png'
            im.save(source)
            studio.save('savejob', 'ep01.png', [Page(str(source), [b])], {})
            studio.action('savejob', 0, 'edit', {
                'id': b.id, 'box': [120, 90, 410, 200], 'font_size': 38, 'rotation': 7,
                'color': [12, 180, 99], 'alignment': 'left',
            })
            studio.action('savejob', 0, 'paint', {'id': b.id, 'points': [[180, 130], [220, 140]], 'radius': 10})
            studio.action('savejob', 0, 'clean', {'id': b.id})

            # 1) First Ctrl+S opens Save As dialog and remembers json_path
            json1 = root / 'saved' / 'ep01.json'
            with patch.object(studio, '_pick_save_path', return_value=json1) as picker:
                r1 = studio.save_json_project('savejob', {'save_as': False})
            picker.assert_called_once()
            self.assertFalse(r1['cancelled'])
            self.assertTrue(json1.is_file())
            self.assertEqual(studio.state('savejob')['json_path'], str(json1.resolve()))

            # 2) Second Ctrl+S overwrites directly without re-opening Save As dialog
            studio.action('savejob', 0, 'edit', {'id': b.id, 'translation': 'คำแปลอัปเดตรอบสอง'})
            with patch.object(studio, '_pick_save_path', side_effect=AssertionError('Must not re-prompt')) as picker2:
                r2 = studio.save_json_project('savejob', {'save_as': False})
            picker2.assert_not_called()
            self.assertIn('ep01.json', r2['note'])

            # 3) Ctrl+Shift+S (Save As) opens dialog for a new file path
            json2 = root / 'saved' / 'ep01_copy.json'
            with patch.object(studio, '_pick_save_path', return_value=json2) as picker3:
                r3 = studio.save_json_project('savejob', {'save_as': True})
            picker3.assert_called_once()
            self.assertTrue(json2.is_file())
            self.assertEqual(studio.state('savejob')['json_path'], str(json2.resolve()))

            # 4) Reopen JSON via open_json_project -> verify ALL fields restored
            opened = studio.open_json_project({'path': str(json2)})
            self.assertFalse(opened['cancelled'])
            reloaded_pages, reloaded_masks = studio.load(opened['key'])
            rb = reloaded_pages[0].bubbles[0]
            self.assertEqual(rb.translation, 'คำแปลอัปเดตรอบสอง')
            self.assertEqual(rb.text_layer['color'], [12, 180, 99])
            self.assertEqual(rb.text_layer['font_size'], 38)
            self.assertEqual(rb.text_layer['rotation'], 7)
            self.assertEqual(rb.text_layer['alignment'], 'left')
            self.assertTrue(rb.text_layer['stroke']['enabled'])
            self.assertTrue(rb.text_layer['gradient']['enabled'])
            self.assertEqual(len(reloaded_pages[0].clean_patches), 1)
            self.assertTrue(reloaded_masks[rb.id]['manual_edited'])

            # 5) Reopen JSON via file upload (/api/projects) too
            client = TestClient(studio.app)
            hdr = {'x-studio': 'editor'}
            with json2.open('rb') as f:
                up = client.post('/api/projects', files=[('files', ('ep01_copy.json', f, 'application/json'))], headers=hdr)
            self.assertEqual(up.status_code, 200, up.text)
            up_pages, _ = studio.load(up.json())
            self.assertEqual(up_pages[0].bubbles[0].translation, 'คำแปลอัปเดตรอบสอง')

            # 6) Save _ocr.txt via Save As dialog
            txt_out = root / 'exports' / 'ep01_ocr.txt'
            with patch.object(studio, '_pick_save_path', return_value=txt_out) as txt_picker:
                rt = studio.save_txt_file('savejob', {'mode': 'id_template'})
            txt_picker.assert_called_once()
            self.assertFalse(rt['cancelled'])
            self.assertTrue(txt_out.is_file())
            self.assertIn(b.id, txt_out.read_text(encoding='utf-8'))

            # 7) Export MTP to custom chosen destination folder
            exp_dir = root / 'custom_export'
            job_id = studio.export('savejob', {'backend': 'photopea', 'format': 'mtp', 'destination': str(exp_dir)})
            for _ in range(100):
                prog = studio.progress(job_id)
                if prog['done']:
                    break
                time.sleep(0.05)
            self.assertTrue(prog['done'])
            self.assertNotIn('error', prog)
            self.assertTrue((exp_dir / 'ep01.mtp').is_file())
            self.assertEqual(prog['paths'], [str((exp_dir / 'ep01.mtp').resolve())])

            # 8) Reveal in Windows Explorer uses /select,<path>
            with patch('web_api.subprocess.Popen') as popen:
                rv = studio.reveal_path({'path': str(txt_out)})
            self.assertTrue(rv['ok'])
            if os.name == 'nt':
                popen.assert_called_once_with(['explorer', f'/select,{str(txt_out.resolve())}'])

    def test_font_presets_crud_apply_and_open_txt(self):
        import text_layout as tl

        im, b = fixtures.styled_fixture()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(studio, 'ROOT', Path(tmp) / 'store'), \
             patch.object(tl, 'presets_config_path', return_value=Path(tmp) / 'presets.json'):
            root = Path(tmp)
            source = root / '01.png'
            im.save(source)
            studio.save('pjob', '01.png', [Page(str(source), [b])], {})

            # Open TXT via native path helper
            txt_file = root / 'in.txt'
            txt_file.write_text('1. สวัสดีจากไฟล์', encoding='utf-8')
            opened = studio.open_txt_file({'path': str(txt_file)})
            self.assertFalse(opened['cancelled'])
            self.assertIn('สวัสดีจากไฟล์', opened['text'])

            # Font presets list, create new profile, apply profile
            listed = studio.get_font_presets()
            self.assertIn('1. มังฮวาเกาหลี', listed['presets'])
            self.assertEqual(len(listed['frame_types']), 9)
            created = studio.update_font_presets({
                'name': '3. ทดสอบโปรไฟล์',
                'mapping': {ft['key']: 'TH Sarabun New Bold' for ft in listed['frame_types']},
            })
            self.assertIn('3. ทดสอบโปรไฟล์', created['presets'])
            applied = studio.apply_font_preset('pjob', {'preset': '3. ทดสอบโปรไฟล์', 'all_pages': True})
            self.assertEqual(applied['laid_out'], 1)
            self.assertIn('3. ทดสอบโปรไฟล์', applied['note'])
            pages, _ = studio.load('pjob')
            self.assertEqual(pages[0].bubbles[0].font_choice, 'TH Sarabun New Bold')
            self.assertTrue(pages[0].bubbles[0].text_layer['auto_leading'])

    @unittest.skipUnless(os.environ.get('TEST_STUDIO_BROWSER') == '1', 'opt-in browser and Photopea integration')
    def test_browser_transform_and_export_at_desktop_scaling(self):
        import uvicorn
        from playwright.sync_api import sync_playwright

        im, b = fixtures.styled_fixture()
        # Draw a crisp white bubble with clear English text in lower region for live RapidOCR single-bubble test
        draw_im = im.copy()
        dr = ImageDraw.Draw(draw_im)
        dr.rectangle((35, 305, 315, 425), fill='white', outline='#111111', width=3)
        fpath = 'C:/Windows/Fonts/arialbd.ttf'
        font = ImageFont.truetype(fpath, 36) if Path(fpath).is_file() else ImageFont.load_default()
        dr.text((90, 342), 'HELLO', font=font, fill='black')
        b_untr = Bubble(box=[340, 310, 520, 420], text='WAIT', translation='', score=0.96, lines=[])
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'store'):
            root = Path(tmp)
            path = root / 'page.png'
            draw_im.save(path)
            source = root / 'chapter.mtp'
            write_mtp(source, [Page(str(path), [b, b_untr])], {}, threading.Event(), lambda _: None)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                server = uvicorn.Server(uvicorn.Config(studio.app, log_level='warning'))
                thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]})
                thread.start()
                try:
                    for _ in range(300):
                        if server.started:
                            break
                        time.sleep(0.02)
                    self.assertTrue(server.started)
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch()
                        for ratio in (1, 1.25):
                            with self.subTest(scale=ratio):
                                vw, vh = round(1920 / ratio), round(1080 / ratio)
                                context = browser.new_context(viewport={'width': vw, 'height': vh}, device_scale_factor=ratio)
                                tab = context.new_page()
                                errors = []
                                tab.on('pageerror', lambda error: errors.append(str(error)))
                                tab.goto(f'http://127.0.0.1:{sock.getsockname()[1]}')
                                tab.locator('#files').set_input_files(source)
                                tab.locator('#bid').wait_for()

                                # No window scrollbars & all top/bottom toolbar buttons unclipped
                                self.assertTrue(
                                    tab.evaluate('document.documentElement.scrollWidth===innerWidth && document.documentElement.scrollHeight===innerHeight')
                                )
                                clipped = tab.evaluate(
                                    """() => {
                                      const bad = [];
                                      for (const sel of ['header button', '#topBar button', '.tools button']) {
                                        for (const el of document.querySelectorAll(sel)) {
                                          if (el.offsetParent === null) continue;
                                          const r = el.getBoundingClientRect();
                                          if (r.width < 10 || r.left < 0 || r.right > window.innerWidth + 0.5 || r.top < 0 || r.bottom > window.innerHeight + 0.5)
                                            bad.push({text: el.textContent.trim(), r: {l:r.left, r:r.right, t:r.top, b:r.bottom, w:r.width}});
                                        }
                                      }
                                      return bad;
                                    }"""
                                )
                                self.assertEqual(clipped, [], f'Clipped buttons at scale {ratio}: {clipped}')
                                self.assertIn('Photopea Backend', tab.locator('footer').text_content())
                                self.assertEqual(tab.locator('#bid').text_content(), 'B01')
                                self.assertTrue(tab.locator('#frameBadge').is_visible())

                                # Verify Bubbles/Layers shows B01 + text + status pill cleanly (not crushed to 0 width)
                                layer_metrics = tab.evaluate(
                                    """() => {
                                      const row = document.querySelector('#layers .layer');
                                      return {
                                        bid: row.querySelector('.bid-tag')?.getBoundingClientRect().width || 0,
                                        txt: row.querySelector('.layer-txt')?.getBoundingClientRect().width || 0,
                                        pill: row.querySelector('.st-pill')?.getBoundingClientRect().width || 0,
                                        bidText: row.querySelector('.bid-tag')?.textContent || '',
                                      };
                                    }"""
                                )
                                self.assertEqual(layer_metrics['bidText'], 'B01')
                                self.assertGreater(layer_metrics['bid'], 12, f'B01 crushed: {layer_metrics}')
                                self.assertGreater(layer_metrics['txt'], 20, f'Layer text crushed: {layer_metrics}')
                                self.assertGreater(layer_metrics['pill'], 16, f'Status pill crushed: {layer_metrics}')

                                # Verify Font Profile panel is visible under Bubbles/Layers with all controls
                                self.assertTrue(tab.locator('#profilePanel').is_visible())
                                self.assertTrue(tab.locator('#presetSelect').is_visible())
                                self.assertTrue(tab.locator('[data-cmd=applyPreset]').is_visible())
                                self.assertTrue(tab.locator('[data-cmd=editPreset]').is_visible())
                                self.assertTrue(tab.locator('[data-cmd=newPreset]').is_visible())

                                # Verify all 9 bilingual frame type labels in Inspector dropdown
                                frame_labels = tab.locator('#frame option').all_text_contents()
                                self.assertEqual(len(frame_labels), 9)
                                self.assertIn('normal — ปกติ (คำพูดทั่วไป)', frame_labels)
                                self.assertIn('terrible — น่ากลัว / กดดัน / สยองขวัญ', frame_labels)

                                # Test OCR Options Modal (key '1') opens & remembers lang/engine in session
                                tab.keyboard.press('1')
                                tab.locator('#ocrLang').wait_for(state='visible')
                                self.assertEqual(tab.locator('#ocrLang').input_value(), 'ko+en')
                                self.assertEqual(tab.locator('#ocrEngine').input_value(), 'rapid')
                                tab.locator('#ocrLang').select_option('en')
                                tab.locator('#ocrEngine').select_option('paddle')
                                tab.locator('#modal button', has_text='ยกเลิก').click()
                                tab.keyboard.press('1')
                                tab.locator('#ocrLang').wait_for(state='visible')
                                self.assertEqual(tab.locator('#ocrLang').input_value(), 'en')
                                self.assertEqual(tab.locator('#ocrEngine').input_value(), 'paddle')
                                tab.locator('#ocrEngine').select_option('rapid')
                                tab.locator('#modal button', has_text='ยกเลิก').click()

                                # Verify view buttons (ต้นฉบับ / คลีน / Text), heatmap bubbles & badges
                                self.assertEqual(tab.locator('#views button').all_text_contents(), ['ต้นฉบับ', 'คลีน', 'Text'])
                                self.assertGreaterEqual(tab.locator('.bubble').count(), 2)
                                self.assertEqual(tab.locator('.bubble .badge').count(), tab.locator('.bubble').count())

                                # Draw new frame around 'HELLO' (x=35..315, y=305..425) -> auto V mode (8 handles!) -> resize -> RapidOCR single bubble -> delete
                                count_before = tab.locator('.bubble').count()
                                tab.keyboard.press('n')
                                stage_bb = tab.locator('#stage').bounding_box()
                                z = stage_bb['width'] / draw_im.width
                                tab.mouse.move(stage_bb['x'] + 40 * z, stage_bb['y'] + 310 * z)
                                tab.mouse.down()
                                tab.mouse.move(stage_bb['x'] + 280 * z, stage_bb['y'] + 410 * z, steps=5)
                                tab.mouse.up()
                                tab.wait_for_function(f"() => document.querySelectorAll('.bubble').length === {count_before + 1}")
                                # Immediately enters Move [V] with 8 handles on the new frame
                                self.assertEqual(tab.locator('.handle').count(), 8)
                                se_new = tab.locator('[data-handle=se]').bounding_box()
                                tab.mouse.move(se_new['x'] + 4, se_new['y'] + 4)
                                tab.mouse.down()
                                tab.mouse.move(se_new['x'] + 24, se_new['y'] + 12, steps=4)
                                tab.mouse.up()
                                tab.wait_for_timeout(450)
                                # Click RapidOCR ช่องนี้ (real RapidOCR reads 'HELLO' only in this frame!)
                                tab.locator('#inspector [data-cmd=rapidBubble]').click()
                                tab.wait_for_function("() => document.getElementById('source').value.trim().length > 0", timeout=60000)
                                self.assertIn('HELLO', tab.locator('#source').input_value().upper())
                                tab.keyboard.press('Delete')
                                tab.wait_for_function(f"() => document.querySelectorAll('.bubble').length === {count_before}")

                                # Test Ctrl+K Command Palette has JSON save shortcut & Ctrl+S saves JSON + shows Toast with เปิดโฟลเดอร์
                                tab.keyboard.press('Control+k')
                                tab.locator('#modal .pal-item').first.wait_for(state='visible')
                                self.assertIn('บันทึกโปรเจกต์ JSON', tab.locator('#modal').text_content())
                                tab.locator('#close').click()
                                json_out = root / f'saved_{int(ratio * 100)}.json'
                                with patch.object(studio, '_pick_save_path', return_value=json_out):
                                    tab.keyboard.press('Control+s')
                                    tab.wait_for_function("() => document.getElementById('notice').textContent.includes('บันทึก JSON แล้ว')", timeout=15000)
                                self.assertTrue(json_out.is_file())
                                self.assertTrue(tab.locator('#notice .btn-reveal').is_visible())

                                # Test TXT Modal (T): switch format, Save _ocr.txt via Save As, and Import translation back!
                                tab.keyboard.press('t')
                                tab.locator('#txtArea').wait_for()
                                tab.locator('#txtMode').select_option('numbered')
                                tab.wait_for_timeout(250)
                                self.assertIn('1.', tab.locator('#txtArea').input_value())
                                txt_saved = root / f'saved_{int(ratio * 100)}_ocr.txt'
                                with patch.object(studio, '_pick_save_path', return_value=txt_saved):
                                    tab.locator('#modal button', has_text='บันทึก _ocr.txt').click()
                                    tab.wait_for_function("() => document.getElementById('notice').textContent.includes('บันทึก TXT แล้ว')", timeout=15000)
                                self.assertTrue(txt_saved.is_file())
                                tab.locator('#txtArea').fill('1. คำแปลนำเข้าทดสอบ\n2. รอด้วยนะ')
                                tab.locator('#txtImportBtn').click()
                                tab.locator('#dialog').wait_for(state='hidden', timeout=20000)
                                tab.wait_for_function("() => document.getElementById('notice').textContent.includes('นำเข้าคำแปลสำเร็จ')", timeout=15000)
                                self.assertEqual(tab.locator('#thai').input_value(), 'คำแปลนำเข้าทดสอบ')
                                # In Text view, NO status badges or blue coord HUD should obstruct Thai text on canvas
                                self.assertEqual(tab.locator('.bubble .badge').count(), 0)
                                self.assertEqual(tab.locator('.bubble .hud').count(), 0)
                                self.assertTrue(tab.locator('#coordInfo').text_content().strip())

                                # Test Mask Edit Mode (M) -> paint -> Preview -> ตกลง
                                tab.locator('#layers .layer button').first.click()
                                tab.keyboard.press('m')
                                tab.locator('#cleanPanel').wait_for(state='visible')
                                sel_bb = tab.locator('.bubble.selected').bounding_box()
                                tab.mouse.move(sel_bb['x'] + sel_bb['width'] * 0.3, sel_bb['y'] + sel_bb['height'] * 0.5)
                                tab.mouse.down()
                                tab.mouse.move(sel_bb['x'] + sel_bb['width'] * 0.7, sel_bb['y'] + sel_bb['height'] * 0.5, steps=4)
                                tab.mouse.up()
                                tab.wait_for_timeout(400)
                                tab.locator('[data-cmd=maskPreview]').click()
                                tab.locator('#maskToggle').wait_for(state='visible', timeout=30000)
                                tab.locator('[data-cmd=maskApply]').click()
                                tab.locator('#cleanPanel').wait_for(state='hidden', timeout=30000)

                                # Test V-key Move/Transform (full bubble frame, 8 handles, 4 rotate zones, Ctrl+Z/Ctrl+Y, PSD+MTP export)
                                tab.keyboard.press('v')
                                self.assertEqual(tab.locator('.handle').count(), 8)
                                self.assertEqual(tab.locator('.rot-zone').count(), 4)
                                self.assertEqual(tab.locator('.bubble .badge').count(), 0)
                                self.assertEqual(tab.locator('.bubble .hud').count(), 0)

                                # Verify full-bubble frame encloses Thai text span without cutting through glyphs
                                box_check = tab.evaluate(
                                    """() => {
                                      const b = document.querySelector('.bubble.selected');
                                      const s = b.querySelector('span');
                                      const br = b.getBoundingClientRect(), sr = s.getBoundingClientRect();
                                      const rotCursor = getComputedStyle(b.querySelector('.rot-zone')).cursor;
                                      return {
                                        bw: br.width, bh: br.height, sw: sr.width, sh: sr.height,
                                        enclosed: sr.top >= br.top - 1 && sr.bottom <= br.bottom + 1,
                                        rotCursor
                                      };
                                    }"""
                                )
                                self.assertTrue(box_check['enclosed'], f'Frame cuts through Thai text: {box_check}')
                                self.assertIn('data:image/svg+xml', box_check['rotCursor'])

                                # Test drag-rotate via outer corner .rot-zone -> persists -> Ctrl+Z undo -> Ctrl+Y redo
                                rot_bb = tab.locator('[data-rot=ne]').bounding_box()
                                rx, ry = rot_bb['x'] + rot_bb['width'] / 2, rot_bb['y'] + rot_bb['height'] / 2
                                tab.mouse.move(rx, ry)
                                tab.mouse.down()
                                tab.mouse.move(rx + 20, ry + 55, steps=6)
                                tab.mouse.up()
                                tab.wait_for_timeout(450)
                                rotated_val = float(tab.locator('#rotation').input_value())
                                self.assertNotEqual(rotated_val, 0.0, 'Corner rotate zone did not rotate bubble')

                                # Undo rotation with Ctrl+Z
                                tab.keyboard.press('Control+z')
                                tab.wait_for_function("() => document.getElementById('notice').textContent.includes('Undo:')", timeout=10000)
                                self.assertEqual(float(tab.locator('#rotation').input_value()), 0.0)

                                # Redo rotation with Ctrl+Y
                                tab.keyboard.press('Control+y')
                                tab.wait_for_function("() => document.getElementById('notice').textContent.includes('Redo:')", timeout=10000)
                                self.assertEqual(float(tab.locator('#rotation').input_value()), rotated_val)

                                # Undo back to 0° cleanly before corner-scale + export test
                                tab.keyboard.press('Control+z')
                                tab.wait_for_timeout(350)
                                self.assertEqual(float(tab.locator('#rotation').input_value()), 0.0)

                                handle = tab.locator('[data-handle=se]').bounding_box()
                                x, y = handle['x'] + 4, handle['y'] + 4
                                old = int(tab.locator('#size').input_value())
                                tab.mouse.move(x, y)
                                tab.mouse.down()
                                tab.mouse.move(x + 36, y + 18, steps=4)
                                tab.mouse.up()
                                self.assertGreater(int(tab.locator('#size').input_value()), old)
                                tab.keyboard.press('Shift+ArrowRight')
                                tab.locator('#ink').fill('#991155')
                                tab.locator('[data-cmd=export]').click()
                                tab.locator('#format').select_option('all')
                                tab.locator('#modal button').last.click()
                                tab.locator('#modal a').first.wait_for(timeout=120000)
                                key = tab.evaluate("localStorage.getItem('studio-project')")
                                files = list((studio.ROOT / key / 'exports').glob('*/*'))
                                with next(p for p in files if p.suffix == '.psd').open('rb') as f:
                                    self.assertEqual(f.read(4), b'8BPS')
                                restored = read_mtp(next(p for p in files if p.suffix == '.mtp'), threading.Event(), lambda _: None)[0][0].bubbles[0]
                                self.assertEqual(restored.text_layer['color'], [153, 17, 85])
                                self.assertGreater(restored.text_layer['font_size'], old)
                                self.assertTrue(restored.text_layer['auto_leading'])
                                self.assertTrue(is_current(restored))
                                self.assertEqual(errors, [])
                                context.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(10)
