import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw
import web_api as studio
from manhwa_ocr import Bubble, Page, read_mtp, write_mtp
from text_layout import layout_text
from text_style_sampler import effects, styled_tile


class GradientOverlayUnitTests(unittest.TestCase):
    def test_effects_multi_stop_and_legacy_backwards_compatibility(self):
        # Legacy 2-color payload auto-populates stops + Photoshop Classic defaults
        legacy = effects({
            'gradient': {
                'enabled': True,
                'start_color': [10, 20, 30],
                'end_color': [200, 210, 220],
                'angle': 45,
            }
        })['gradient']
        self.assertTrue(legacy['enabled'])
        self.assertEqual(legacy['stops'], [
            {'pos': 0, 'color': '#0A141E'},
            {'pos': 100, 'color': '#C8D2DC'},
        ])
        self.assertEqual(legacy['angle'], 45)
        self.assertEqual(legacy['scale'], 100)
        self.assertEqual(legacy['opacity'], 100)
        self.assertFalse(legacy['reverse'])
        self.assertEqual(legacy['style'], 'linear')
        self.assertEqual(legacy['blend_mode'], 'normal')
        self.assertTrue(legacy['align_with_layer'])

        # Full 3-stop Photoshop Classic payload normalizes, sorts stops, and syncs start/end_color
        rich = effects({
            'gradient': {
                'enabled': True,
                'stops': [
                    {'pos': 100, 'color': '#b4ba97'},
                    {'pos': 0, 'color': '#000000'},
                    {'pos': 50, 'color': '#556644'},
                ],
                'angle': -135,
                'scale': 125,
                'opacity': 80,
                'reverse': True,
                'style': 'reflected',
                'blend_mode': 'multiply',
                'align_with_layer': False,
            }
        })['gradient']
        self.assertEqual(rich['stops'], [
            {'pos': 0, 'color': '#000000'},
            {'pos': 50, 'color': '#556644'},
            {'pos': 100, 'color': '#B4BA97'},
        ])
        self.assertEqual(rich['start_color'], [0, 0, 0])
        self.assertEqual(rich['end_color'], [180, 186, 151])
        self.assertEqual(rich['angle'], -135)
        self.assertEqual(rich['scale'], 125)
        self.assertEqual(rich['opacity'], 80)
        self.assertTrue(rich['reverse'])
        self.assertEqual(rich['style'], 'reflected')
        self.assertEqual(rich['blend_mode'], 'multiply')
        self.assertFalse(rich['align_with_layer'])

    def test_styled_tile_and_persistence_roundtrip_sqlite_json_mtp(self):
        im = Image.new('RGB', (480, 320), 'white')
        b = Bubble(box=[40, 40, 440, 260], text='GRAD', translation='ไล่สีสามจุดทดสอบ', score=0.98)
        b.text_layer = layout_text(im, b)
        grad_cfg = {
            'enabled': True,
            'stops': [
                {'pos': 0, 'color': '#000000'},
                {'pos': 50, 'color': '#556644'},
                {'pos': 100, 'color': '#B4BA97'},
            ],
            'angle': -45,
            'scale': 120,
            'opacity': 90,
            'reverse': True,
            'style': 'radial',
            'blend_mode': 'overlay',
            'align_with_layer': False,
        }
        # PNG preview tile renders cleanly for all 3 gradient styles
        tile = Image.new('RGBA', (120, 60), (20, 20, 20, 255))
        for st in ('linear', 'radial', 'reflected'):
            out = styled_tile(tile, {'gradient': {**grad_cfg, 'style': st}})
            arr = np.asarray(out)
            self.assertEqual(arr.shape, (60, 120, 4))
            self.assertGreater(int(np.ptp(arr[:, :, :3])), 20)

        # Roundtrip across SQLite, JSON, and MTP
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'store'):
            root = Path(tmp)
            src = root / '01.png'
            im.save(src)
            studio.save('gjob', '01.png', [Page(str(src), [b])], {})
            studio.action('gjob', 0, 'edit', {'id': b.id, 'style': {'gradient': grad_cfg}})
            pages_sql, _ = studio.load('gjob')
            saved_g = pages_sql[0].bubbles[0].text_layer['gradient']
            self.assertEqual(len(saved_g['stops']), 3)
            self.assertEqual(saved_g['stops'][1], {'pos': 50, 'color': '#556644'})
            self.assertEqual(saved_g['angle'], -45)
            self.assertEqual(saved_g['scale'], 120)
            self.assertEqual(saved_g['opacity'], 90)
            self.assertTrue(saved_g['reverse'])
            self.assertEqual(saved_g['style'], 'radial')
            self.assertEqual(saved_g['blend_mode'], 'overlay')
            self.assertFalse(saved_g['align_with_layer'])

            # JSON roundtrip
            jpath = root / 'gproj.json'
            studio.save_json_project('gjob', {'path': str(jpath)})
            opened = studio.open_json_project({'path': str(jpath)})
            pages_json, _ = studio.load(opened['key'])
            self.assertEqual(pages_json[0].bubbles[0].text_layer['gradient'], saved_g)

            # MTP roundtrip
            mtp = root / 'gproj.mtp'
            write_mtp(mtp, pages_sql, {}, threading.Event(), lambda _: None)
            pages_mtp, _, _ = read_mtp(mtp, threading.Event(), lambda _: None, root / 'unpacked')
            self.assertEqual(pages_mtp[0].bubbles[0].text_layer['gradient'], saved_g)


@unittest.skipUnless(os.getenv('TEST_STUDIO_BROWSER') == '1', 'requires Chromium and Photopea')
class GradientOverlayBrowserTests(unittest.TestCase):
    def test_photoshop_gradient_modal_60fps_multistop_del_and_photopea_psd_export(self):
        import uvicorn
        from playwright.sync_api import sync_playwright
        from psd_tools import PSDImage

        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'studio'):
            im = Image.new('RGB', (640, 480), '#f8fafc')
            dr = ImageDraw.Draw(im)
            dr.rounded_rectangle((60, 60, 560, 240), radius=24, fill='white', outline='#111111', width=4)
            path = Path(tmp) / 'grad_page.png'
            im.save(path)

            b1 = Bubble(box=[60, 60, 560, 240], text='MAGIC!', translation='พลังเวทย์สามสี!', score=0.98)
            b1.text_layer = layout_text(im, b1)
            b2 = Bubble(box=[80, 290, 420, 410], text='SECOND', translation='บอลลูนสำรอง', score=0.96)
            b2.text_layer = layout_text(im, b2)

            key = 'gradfx'
            studio.save(key, 'grad_page.png', [Page(str(path), [b1, b2])], {})

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
                        ctx = browser.new_context(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1)
                        tab = ctx.new_page()
                        errors = []
                        tab.on('pageerror', lambda err: errors.append(str(err)))
                        tab.add_init_script(f"localStorage.setItem('studio-project','{key}')")
                        tab.goto(f'http://127.0.0.1:{sock.getsockname()[1]}')
                        tab.locator('#bid').wait_for()

                        # Open Photoshop Classic Gradient Overlay window from Inspector
                        tab.locator('#openGradBtn').click()
                        modal = tab.locator('#gradModal')
                        modal.wait_for(state='visible')

                        # 1) Drag window header smoothly with translate3d at 60fps
                        head_bb = tab.locator('#gradHeader').bounding_box()
                        before_modal = modal.bounding_box()
                        tab.mouse.move(head_bb['x'] + 40, head_bb['y'] + 12)
                        tab.mouse.down()
                        win_fps = tab.evaluate("""() => new Promise(resolve => {
                          let first = 0, count = 0;
                          function step(t) {
                            first ||= t;
                            gradHeader.dispatchEvent(new PointerEvent('pointermove', {
                              pointerId: gradWinDrag.id, clientX: gradWinDrag.dx + 420 + count, clientY: gradWinDrag.dy + 90, buttons: 1
                            }));
                            if (++count === 61) resolve(60000 / (t - first)); else requestAnimationFrame(step);
                          }
                          requestAnimationFrame(step);
                        })""")
                        tab.mouse.up()
                        after_modal = modal.bounding_box()
                        self.assertGreater(abs(after_modal['x'] - before_modal['x']), 20)

                        # 2) Verify 60fps smooth drag with ZERO DOM recreation on Opacity, Angle, Scale, Angle Dial, and Color Stop pin
                        drag_metrics = tab.evaluate("""() => new Promise(resolve => {
                          const opEl = document.getElementById('gmOpacity');
                          const angEl = document.getElementById('gmAngle');
                          const sclEl = document.getElementById('gmScale');
                          const dial = document.getElementById('gmDial');
                          const pin0 = document.querySelector('#gmStops .gm-stop');
                          const dialRect = dial.getBoundingClientRect();
                          const cx = dialRect.left + dialRect.width / 2, cy = dialRect.top + dialRect.height / 2;

                          dial.dispatchEvent(new PointerEvent('pointerdown', { pointerId: 7, button: 0, clientX: cx + 15, clientY: cy }));
                          let first = 0, count = 0;
                          function step(t) {
                            first ||= t;
                            opEl.value = String(40 + (count % 60)); opEl.dispatchEvent(new Event('input'));
                            sclEl.value = String(60 + (count % 80)); sclEl.dispatchEvent(new Event('input'));
                            angEl.value = String(-180 + count * 6); angEl.dispatchEvent(new Event('input'));
                            const rad = (count * 6) * Math.PI / 180;
                            dial.dispatchEvent(new PointerEvent('pointermove', { pointerId: 7, clientX: cx + Math.cos(rad) * 16, clientY: cy - Math.sin(rad) * 16, buttons: 1 }));
                            if (++count === 61) {
                              dial.dispatchEvent(new PointerEvent('pointerup', { pointerId: 7 }));
                              resolve({
                                fps: 60000 / (t - first),
                                sameOpacityNode: opEl === document.getElementById('gmOpacity'),
                                sameAngleNode: angEl === document.getElementById('gmAngle'),
                                sameScaleNode: sclEl === document.getElementById('gmScale'),
                                samePinNode: pin0 === document.querySelector('#gmStops .gm-stop'),
                                liveBg: document.querySelector('.bubble.selected span').style.backgroundImage,
                              });
                            } else requestAnimationFrame(step);
                          }
                          requestAnimationFrame(step);
                        })""")
                        self.assertTrue(drag_metrics['sameOpacityNode'])
                        self.assertTrue(drag_metrics['sameAngleNode'])
                        self.assertTrue(drag_metrics['sameScaleNode'])
                        self.assertTrue(drag_metrics['samePinNode'])
                        self.assertIn('gradient(', drag_metrics['liveBg'])

                        # Test Cancel restores original state (gradient disabled) without backend save
                        tab.locator('#gmCancel').click()
                        modal.wait_for(state='hidden')
                        self.assertFalse(tab.locator('#gradient').is_checked())

                        # 3) Re-open, add 3rd color stop by clicking bar, drag stop smoothly without DOM recreation, then delete via Delete key (without deleting balloon!)
                        tab.locator('#openGradBtn').click()
                        modal.wait_for(state='visible')
                        self.assertEqual(tab.locator('#gmStops .gm-stop').count(), 2)
                        bar_bb = tab.locator('#gmBar').bounding_box()
                        tab.mouse.click(bar_bb['x'] + bar_bb['width'] * 0.5, bar_bb['y'] + bar_bb['height'] * 0.5)
                        self.assertEqual(tab.locator('#gmStops .gm-stop').count(), 3)

                        # Drag middle stop from ~50% to ~70% and verify pin stays mounted during drag
                        mid_pin = tab.locator('#gmStops .gm-stop').nth(1)
                        mid_bb = mid_pin.bounding_box()
                        tab.mouse.move(mid_bb['x'] + mid_bb['width'] / 2, mid_bb['y'] + mid_bb['height'] / 2)
                        tab.mouse.down()
                        tab.mouse.move(bar_bb['x'] + bar_bb['width'] * 0.72, mid_bb['y'] + mid_bb['height'] / 2, steps=6)
                        moved_pos = int(tab.locator('#gmStopPos').input_value())
                        self.assertGreaterEqual(moved_pos, 66)
                        tab.mouse.up()

                        # Press Delete key -> deletes 3rd stop, NEVER deletes balloon behind modal!
                        bubbles_before = tab.locator('.bubble').count()
                        tab.keyboard.press('Delete')
                        self.assertEqual(tab.locator('#gmStops .gm-stop').count(), 2)
                        self.assertEqual(tab.locator('.bubble').count(), bubbles_before)

                        # Pressing Delete again with only 2 stops must keep 2 stops & keep all balloons!
                        tab.keyboard.press('Delete')
                        self.assertEqual(tab.locator('#gmStops .gm-stop').count(), 2)
                        self.assertEqual(tab.locator('.bubble').count(), bubbles_before)

                        # 4) Configure exact 3-stop Photoshop Classic gradient & click OK
                        tab.locator('#gmStops .gm-stop').nth(0).click()
                        tab.locator('#gmStopColor').fill('#000000')
                        tab.locator('#gmStops .gm-stop').nth(1).click()
                        tab.locator('#gmStopColor').fill('#b4ba97')
                        tab.mouse.click(bar_bb['x'] + bar_bb['width'] * 0.5, bar_bb['y'] + bar_bb['height'] * 0.5)
                        self.assertEqual(tab.locator('#gmStops .gm-stop').count(), 3)
                        tab.locator('#gmStopColor').fill('#556644')
                        tab.locator('#gmStopPos').fill('50')
                        tab.locator('#gmStopPos').dispatch_event('change')

                        tab.locator('#gmBlend').select_option('multiply')
                        tab.locator('#gmOpacity').fill('85')
                        tab.locator('#gmOpacity').dispatch_event('input')
                        tab.locator('#gmReverse').check()
                        tab.locator('#gmStyle').select_option('linear')
                        tab.locator('#gmAngleNum').fill('30')
                        tab.locator('#gmAngleNum').dispatch_event('input')
                        tab.locator('#gmScaleNum').fill('120')
                        tab.locator('#gmScaleNum').dispatch_event('input')
                        tab.locator('#gmAlign').uncheck()

                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#gmOk').click()
                        modal.wait_for(state='hidden')
                        self.assertTrue(tab.locator('#gradient').is_checked())

                        # Verify persisted in SQLite
                        saved_pages, _ = studio.load(key)
                        g_saved = saved_pages[0].bubbles[0].text_layer['gradient']
                        self.assertTrue(g_saved['enabled'])
                        self.assertEqual(len(g_saved['stops']), 3)
                        self.assertEqual(g_saved['angle'], 30)
                        self.assertEqual(g_saved['scale'], 120)
                        self.assertEqual(g_saved['opacity'], 85)
                        self.assertTrue(g_saved['reverse'])
                        self.assertEqual(g_saved['blend_mode'], 'multiply')
                        self.assertFalse(g_saved['align_with_layer'])

                        # 5) Export PSD via Photopea from Studio UI and inspect binary GrFl descriptor
                        tab.locator('[data-cmd=export]').click()
                        tab.locator('#format').select_option('psd')
                        tab.locator('#modal button.primary').click()
                        tab.locator('#exportStatus.export-ok').wait_for(timeout=120000)

                        psd_path = next((studio.ROOT / key / 'exports').glob('*/*.psd'))
                        doc = PSDImage.open(psd_path)
                        type_layer = next(l for l in doc.descendants() if l.kind == 'type' and b1.id in l.name)
                        self.assertIn('GradientOverlay', {e.name for e in type_layer.effects if e.enabled})
                        fx = type_layer.tagged_blocks.get_data(b'lfx2')
                        grfl = fx[b'GrFl']
                        clrs = grfl[b'Grad'][b'Clrs']
                        self.assertEqual(len(clrs), 3, f'Expected 3 gradient stops in PSD GrFl, got {len(clrs)}')
                        locs = [int(s[b'Lctn']) for s in clrs]
                        self.assertEqual(locs[0], 0)
                        self.assertAlmostEqual(locs[1], 2048, delta=60)
                        self.assertEqual(locs[2], 4096)
                        self.assertAlmostEqual(float(grfl[b'Angl']), 30.0, delta=0.5)
                        self.assertAlmostEqual(float(grfl[b'Scl ']), 120.0, delta=0.5)
                        self.assertAlmostEqual(float(grfl[b'Opct']), 85.0, delta=0.5)
                        self.assertTrue(bool(grfl[b'Rvrs']))
                        self.assertFalse(bool(grfl[b'Algn']))

                        print(
                            f'Gradient Overlay verified: win_drag={win_fps:.1f}fps, sliders_dial={drag_metrics["fps"]:.1f}fps, PSD GrFl stops={len(clrs)}',
                            flush=True,
                        )
                        self.assertEqual(errors, [], f'Browser JS errors: {errors}')
                        ctx.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(10)


if __name__ == '__main__':
    unittest.main()
