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

                                # Test TXT Modal (T): switch format
                                tab.keyboard.press('t')
                                tab.locator('#txtArea').wait_for()
                                tab.locator('#txtMode').select_option('numbered')
                                tab.wait_for_timeout(250)
                                self.assertIn('1.', tab.locator('#txtArea').input_value())
                                tab.locator('#close').click()

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

                                # Test V-key Move/Transform (8 handles, resize, Shift+Arrow, color, PSD+MTP export)
                                tab.keyboard.press('v')
                                self.assertEqual(tab.locator('.handle').count(), 8)
                                handle = tab.locator('[data-handle=se]').bounding_box()
                                x, y = handle['x'] + 4, handle['y'] + 4
                                old = int(tab.locator('#size').input_value())
                                tab.mouse.move(x, y)
                                tab.mouse.down()
                                tab.mouse.move(x + 20, y + 10)
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
