import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
import web_api as studio
from manhwa_ocr import Bubble, Page
from text_layout import layout_text
from text_style_sampler import effects


class LastFxMemoryUnitTests(unittest.TestCase):
    def test_frontend_last_fx_memory_contracts(self):
        root = Path(__file__).resolve().parent.parent / 'web'
        js = (root / 'studio.js').read_text(encoding='utf-8')

        for token in (
            "LAST_FX_STROKE_KEY='manhwa_last_fx_stroke'",
            "LAST_FX_GRADIENT_KEY='manhwa_last_fx_gradient'",
            'function cloneStroke(',
            'function cloneGradient(',
            'function hasOwnStroke(',
            'function hasOwnGradient(',
            'function loadLastFxStroke(',
            'function saveLastFxStroke(',
            'function loadLastFxGradient(',
            'function saveLastFxGradient(',
            'function resolveStrokeForEnable(',
            'function resolveGradientForEnable(',
            'function captureStyle(',
            'function pushStyleHistory(',
            'if(snap.style){',
            'g.stops.map(st=>({pos:st.pos,color:st.color}))',
        ):
            self.assertIn(token, js, f'Missing expected token in web/studio.js: {token}')

    def test_backend_multi_bubble_fx_isolation_and_deep_copy(self):
        im = Image.new('RGB', (640, 480), 'white')
        b1 = Bubble(box=[40, 40, 280, 180], text='ONE', translation='บับเบิลหนึ่ง', score=0.99)
        b2 = Bubble(box=[320, 40, 600, 180], text='TWO', translation='บับเบิลสอง', score=0.99)
        b1.text_layer = layout_text(im, b1)
        b2.text_layer = layout_text(im, b2)

        grad_3stop = {
            'enabled': True,
            'stops': [
                {'pos': 0, 'color': '#EF4444'},
                {'pos': 50, 'color': '#8B5CF6'},
                {'pos': 100, 'color': '#22C55E'},
            ],
            'angle': -45,
            'scale': 125,
            'opacity': 85,
            'reverse': True,
            'style': 'reflected',
            'blend_mode': 'overlay',
            'align_with_layer': False,
        }
        stroke_black = {
            'enabled': True,
            'color': [0, 0, 0],
            'size': 6,
            'position': 'outside',
            'opacity': 88,
            'blend_mode': 'Multiply',
        }

        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'store'):
            src = Path(tmp) / '01.png'
            im.save(src)
            studio.save('lastfx_u', '01.png', [Page(str(src), [b1, b2])], {})
            studio.action('lastfx_u', 0, 'edit', {'id': b1.id, 'style': {'stroke': stroke_black, 'gradient': grad_3stop}})

            # Mutating input dict after saving to b1 must NOT affect b1, and saving modified copy to b2 keeps b1 intact
            grad_b2 = json.loads(json.dumps(grad_3stop))
            grad_b2['stops'][1]['color'] = '#EAB308'
            studio.action('lastfx_u', 0, 'edit', {'id': b2.id, 'style': {'stroke': stroke_black, 'gradient': grad_b2}})

            loaded_pages, _ = studio.load('lastfx_u')
            l1 = loaded_pages[0].bubbles[0].text_layer
            l2 = loaded_pages[0].bubbles[1].text_layer
            self.assertEqual(l1['stroke'], stroke_black)
            self.assertEqual(l2['stroke'], stroke_black)
            self.assertEqual(l1['gradient']['stops'][1]['color'], '#8B5CF6')
            self.assertEqual(l2['gradient']['stops'][1]['color'], '#EAB308')
            self.assertEqual(effects(l1)['gradient']['stops'], grad_3stop['stops'])
            self.assertEqual(effects(l2)['gradient']['stops'][1], {'pos': 50, 'color': '#EAB308'})


@unittest.skipUnless(os.getenv('TEST_STUDIO_BROWSER') == '1', 'requires Chromium and Photopea')
class LastFxMemoryBrowserTests(unittest.TestCase):
    def test_remember_last_stroke_and_gradient_with_reload_isolation_undo_redo_and_psd(self):
        import uvicorn
        from playwright.sync_api import sync_playwright
        from psd_tools import PSDImage

        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'studio'):
            im = Image.new('RGB', (800, 760), '#f8fafc')
            dr = ImageDraw.Draw(im)
            boxes = [
                [40, 40, 360, 200],
                [420, 40, 760, 200],
                [40, 260, 360, 420],
                [420, 260, 760, 420],
            ]
            for bx in boxes:
                dr.rounded_rectangle(bx, radius=20, fill='white', outline='#111111', width=3)
            img_path = Path(tmp) / 'last_fx_page.png'
            im.save(img_path)

            b1 = Bubble(box=boxes[0], text='EXISTING', translation='บับเบิลเก่ามีเอฟเฟกต์', score=0.99)
            b2 = Bubble(box=boxes[1], text='NEW TWO', translation='บับเบิลสอง', score=0.99)
            b3 = Bubble(box=boxes[2], text='NEW THREE', translation='บับเบิลสาม', score=0.99)
            b4 = Bubble(box=boxes[3], text='NEW FOUR', translation='บับเบิลสี่', score=0.99)
            for b in (b1, b2, b3, b4):
                b.text_layer = layout_text(im, b)
                b.text_layer['font_size'] = 38

            # B01 already has its own distinct Stroke and Gradient Overlay
            b1.text_layer['stroke'] = {
                'enabled': True,
                'color': [220, 38, 38],  # #DC2626
                'size': 4,
                'position': 'inside',
                'opacity': 90,
                'blend_mode': 'Screen',
            }
            b1.text_layer['gradient'] = {
                'enabled': True,
                'start_color': [6, 182, 212],
                'end_color': [29, 78, 216],
                'stops': [
                    {'pos': 0, 'color': '#06B6D4'},
                    {'pos': 100, 'color': '#1D4ED8'},
                ],
                'angle': 45,
                'scale': 110,
                'opacity': 95,
                'reverse': False,
                'style': 'linear',
                'blend_mode': 'normal',
                'align_with_layer': True,
            }
            b1.text_layer['manual_effects'] = True

            key = 'lastfx1'
            studio.save(key, 'last_fx_page.png', [Page(str(img_path), [b1, b2, b3, b4])], {})

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
                        tab.wait_for_function("document.getElementById('art').complete && document.getElementById('art').naturalWidth === 800")

                        def select_layer(idx):
                            tab.locator('#layers .layer-main').nth(idx).click()
                            expected = f'B{idx + 1:02d}'
                            tab.wait_for_function(f"document.getElementById('bid').textContent === '{expected}'")

                        # =====================================================================
                        # CASE 1: Set Stroke to Black on B02 -> B03 gets Black via checkbox + Undo/Redo
                        #         -> Reload page -> B04 gets Black via Stroke Modal
                        # =====================================================================
                        select_layer(1)  # B02 (fresh, no fx yet)
                        self.assertFalse(tab.locator('#stroke').is_checked())
                        tab.locator('#openStrokeBtn').click()
                        stroke_modal = tab.locator('#strokeModal')
                        stroke_modal.wait_for(state='visible')
                        # No saved last_fx_stroke yet -> defaults apply
                        self.assertEqual(tab.locator('#smColor').input_value().lower(), '#ffffff')
                        self.assertEqual(tab.locator('#smSize').input_value(), '3')
                        self.assertEqual(tab.locator('#smPosition').input_value(), 'outside')

                        # Configure Black stroke with custom params & click OK
                        tab.locator('#smColor').fill('#000000')
                        tab.locator('#smColor').dispatch_event('input')
                        tab.locator('#smSize').fill('6')
                        tab.locator('#smSize').dispatch_event('input')
                        tab.locator('#smPosition').select_option('outside')
                        tab.locator('#smOpacity').fill('88')
                        tab.locator('#smOpacity').dispatch_event('input')
                        tab.locator('#smBlend').select_option('Multiply')
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#smOk').click()
                        stroke_modal.wait_for(state='hidden')

                        saved_last_stroke = tab.evaluate("() => JSON.parse(localStorage.getItem('manhwa_last_fx_stroke'))")
                        self.assertEqual(saved_last_stroke, {
                            'color': [0, 0, 0],
                            'size': 6,
                            'position': 'outside',
                            'opacity': 88,
                            'blend_mode': 'Multiply',
                        })

                        # Switch to fresh bubble B03 and check #stroke -> must immediately inherit Black 6px stroke!
                        select_layer(2)  # B03
                        self.assertFalse(tab.locator('#stroke').is_checked())
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#stroke').check()
                        self.assertEqual(tab.locator('#edge').input_value().lower(), '#000000')
                        self.assertEqual(tab.locator('#inspStrokeMeta').inner_text(), '6px · Outside')
                        b3_stroke_css = tab.evaluate("""() => {
                          const s = document.querySelector('.bubble.selected .txt-stroke');
                          return { hidden: s.hidden, color: s.style.color, pos: s.dataset.strokePosition, opct: s.style.opacity };
                        }""")
                        self.assertFalse(b3_stroke_css['hidden'])
                        self.assertEqual(b3_stroke_css['color'], 'rgb(0, 0, 0)')
                        self.assertEqual(b3_stroke_css['pos'], 'outside')
                        self.assertEqual(b3_stroke_css['opct'], '0.88')

                        # Ctrl+Z Undo & Ctrl+Y Redo immediately after enabling last-used Stroke (while #stroke has focus!)
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.keyboard.press('Control+z')
                        tab.wait_for_function("() => !document.getElementById('stroke').checked")
                        self.assertTrue(tab.evaluate("() => document.querySelector('.bubble.selected .txt-stroke').hidden"))
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.keyboard.press('Control+y')
                        tab.wait_for_function("() => document.getElementById('stroke').checked")
                        self.assertEqual(tab.locator('#edge').input_value().lower(), '#000000')
                        self.assertEqual(tab.locator('#inspStrokeMeta').inner_text(), '6px · Outside')

                        # Reload page -> localStorage still remembers Black Stroke for fresh bubble B04!
                        tab.reload()
                        tab.locator('#bid').wait_for()
                        tab.wait_for_function("document.getElementById('art').complete && document.getElementById('art').naturalWidth === 800")

                        select_layer(3)  # B04
                        self.assertFalse(tab.locator('#stroke').is_checked())
                        tab.locator('#openStrokeBtn').click()
                        stroke_modal.wait_for(state='visible')
                        self.assertEqual(tab.locator('#smColor').input_value().lower(), '#000000')
                        self.assertEqual(tab.locator('#smSize').input_value(), '6')
                        self.assertEqual(tab.locator('#smPosition').input_value(), 'outside')
                        self.assertEqual(tab.locator('#smOpacity').input_value(), '88')
                        self.assertEqual(tab.locator('#smBlend').input_value(), 'Multiply')
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#smOk').click()
                        stroke_modal.wait_for(state='hidden')

                        # =====================================================================
                        # CASE 2: Set 3-stop Gradient Overlay (Red -> Purple -> Green) on B02
                        #         -> B03 inherits exact 3 stops via checkbox + Undo/Redo
                        #         -> Reload -> B04 inherits exact 3 stops via modal & edits B04 WITHOUT mutating B02/B03
                        # =====================================================================
                        select_layer(1)  # B02
                        tab.locator('#openGradBtn').click()
                        grad_modal = tab.locator('#gradModal')
                        grad_modal.wait_for(state='visible')
                        bar_bb = tab.locator('#gmBar').bounding_box()

                        # Stop 0: Red #EF4444 at 0%
                        tab.locator('#gmStops .gm-stop').nth(0).click()
                        tab.locator('#gmStopColor').fill('#ef4444')
                        tab.locator('#gmStopColor').dispatch_event('input')
                        # Stop 1 (last): Green #22C55E at 100%
                        tab.locator('#gmStops .gm-stop').nth(1).click()
                        tab.locator('#gmStopColor').fill('#22c55e')
                        tab.locator('#gmStopColor').dispatch_event('input')
                        # Add middle stop: Purple #8B5CF6 at 50%
                        tab.mouse.click(bar_bb['x'] + bar_bb['width'] * 0.5, bar_bb['y'] + bar_bb['height'] * 0.5)
                        self.assertEqual(tab.locator('#gmStops .gm-stop').count(), 3)
                        tab.locator('#gmStopColor').fill('#8b5cf6')
                        tab.locator('#gmStopColor').dispatch_event('input')
                        tab.locator('#gmStopPos').fill('50')
                        tab.locator('#gmStopPos').dispatch_event('change')

                        tab.locator('#gmBlend').select_option('overlay')
                        tab.locator('#gmOpacity').fill('85')
                        tab.locator('#gmOpacity').dispatch_event('input')
                        tab.locator('#gmReverse').check()
                        tab.locator('#gmStyle').select_option('reflected')
                        tab.locator('#gmAngleNum').fill('-45')
                        tab.locator('#gmAngleNum').dispatch_event('input')
                        tab.locator('#gmScaleNum').fill('125')
                        tab.locator('#gmScaleNum').dispatch_event('input')
                        tab.locator('#gmAlign').uncheck()

                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#gmOk').click()
                        grad_modal.wait_for(state='hidden')

                        saved_last_grad = tab.evaluate("() => JSON.parse(localStorage.getItem('manhwa_last_fx_gradient'))")
                        self.assertEqual(saved_last_grad['stops'], [
                            {'pos': 0, 'color': '#EF4444'},
                            {'pos': 50, 'color': '#8B5CF6'},
                            {'pos': 100, 'color': '#22C55E'},
                        ])
                        self.assertEqual(saved_last_grad['angle'], -45)
                        self.assertEqual(saved_last_grad['scale'], 125)
                        self.assertEqual(saved_last_grad['opacity'], 85)
                        self.assertTrue(saved_last_grad['reverse'])
                        self.assertEqual(saved_last_grad['style'], 'reflected')
                        self.assertEqual(saved_last_grad['blend_mode'], 'overlay')
                        self.assertFalse(saved_last_grad['align_with_layer'])

                        # Enable Gradient on fresh bubble B03 via checkbox -> must inherit exact 3 stops!
                        select_layer(2)  # B03
                        self.assertFalse(tab.locator('#gradient').is_checked())
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#gradient').check()
                        b3_grad_bg = tab.evaluate("() => document.querySelector('.bubble.selected .txt-fill').style.backgroundImage")
                        self.assertIn('linear-gradient(', b3_grad_bg)

                        # Undo & Redo Gradient enable on B03
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.keyboard.press('Control+z')
                        tab.wait_for_function("() => !document.getElementById('gradient').checked")
                        self.assertEqual(tab.evaluate("() => document.querySelector('.bubble.selected .txt-fill').style.backgroundImage"), '')
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.keyboard.press('Control+y')
                        tab.wait_for_function("() => document.getElementById('gradient').checked")
                        self.assertIn('linear-gradient(', tab.evaluate("() => document.querySelector('.bubble.selected .txt-fill').style.backgroundImage"))

                        # Reload page -> open Gradient modal on fresh bubble B04 -> verify 3 stops & edit middle stop
                        tab.reload()
                        tab.locator('#bid').wait_for()
                        tab.wait_for_function("document.getElementById('art').complete && document.getElementById('art').naturalWidth === 800")

                        select_layer(3)  # B04
                        self.assertFalse(tab.locator('#gradient').is_checked())
                        tab.locator('#openGradBtn').click()
                        grad_modal.wait_for(state='visible')
                        self.assertEqual(tab.locator('#gmStops .gm-stop').count(), 3)
                        self.assertEqual(tab.locator('#gmAngleNum').input_value(), '-45')
                        self.assertEqual(tab.locator('#gmScaleNum').input_value(), '125')
                        self.assertEqual(tab.locator('#gmOpacityNum').input_value(), '85')
                        self.assertTrue(tab.locator('#gmReverse').is_checked())
                        self.assertEqual(tab.locator('#gmStyle').input_value(), 'reflected')
                        self.assertEqual(tab.locator('#gmBlend').input_value(), 'overlay')
                        self.assertFalse(tab.locator('#gmAlign').is_checked())

                        # Edit B04 middle stop to Yellow (#EAB308) and confirm -> MUST NOT mutate B02 or B03!
                        tab.locator('#gmStops .gm-stop').nth(1).click()
                        self.assertEqual(tab.locator('#gmStopColor').input_value().lower(), '#8b5cf6')
                        tab.locator('#gmStopColor').fill('#eab308')
                        tab.locator('#gmStopColor').dispatch_event('input')
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#gmOk').click()
                        grad_modal.wait_for(state='hidden')

                        saved_pages, _ = studio.load(key)
                        g_b2 = saved_pages[0].bubbles[1].text_layer['gradient']
                        g_b3 = saved_pages[0].bubbles[2].text_layer['gradient']
                        g_b4 = saved_pages[0].bubbles[3].text_layer['gradient']
                        self.assertEqual(g_b2['stops'], [
                            {'pos': 0, 'color': '#EF4444'},
                            {'pos': 50, 'color': '#8B5CF6'},
                            {'pos': 100, 'color': '#22C55E'},
                        ])
                        self.assertEqual(g_b3['stops'], g_b2['stops'])
                        self.assertEqual(g_b4['stops'], [
                            {'pos': 0, 'color': '#EF4444'},
                            {'pos': 50, 'color': '#EAB308'},
                            {'pos': 100, 'color': '#22C55E'},
                        ])

                        # =====================================================================
                        # CASE 3: Click existing bubble B01 (already has Red 4px Inside stroke & Cyan->Blue gradient)
                        #         -> MUST NOT be overwritten by last fx!
                        #         -> Viewing/cancelling keeps last fx unchanged; editing B01 updates last fx
                        # =====================================================================
                        select_layer(0)  # B01
                        self.assertTrue(tab.locator('#stroke').is_checked())
                        self.assertEqual(tab.locator('#edge').input_value().lower(), '#dc2626')
                        self.assertEqual(tab.locator('#inspStrokeMeta').inner_text(), '4px · Inside')
                        self.assertTrue(tab.locator('#gradient').is_checked())

                        # Open Gradient Modal on B01 -> shows B01's own 2-stop Cyan->Blue gradient -> Cancel
                        tab.locator('#openGradBtn').click()
                        grad_modal.wait_for(state='visible')
                        self.assertEqual(tab.locator('#gmStops .gm-stop').count(), 2)
                        self.assertEqual(tab.locator('#gmAngleNum').input_value(), '45')
                        tab.locator('#gmStops .gm-stop').nth(0).click()
                        self.assertEqual(tab.locator('#gmStopColor').input_value().lower(), '#06b6d4')
                        tab.locator('#gmStops .gm-stop').nth(1).click()
                        self.assertEqual(tab.locator('#gmStopColor').input_value().lower(), '#1d4ed8')
                        tab.locator('#gmCancel').click()
                        grad_modal.wait_for(state='hidden')

                        # Last gradient in localStorage must STILL be B04's 3-stop gradient (unchanged by viewing B01)
                        still_last_grad = tab.evaluate("() => JSON.parse(localStorage.getItem('manhwa_last_fx_gradient'))")
                        self.assertEqual(len(still_last_grad['stops']), 3)
                        self.assertEqual(still_last_grad['stops'][1]['color'], '#EAB308')

                        # Open Stroke Modal on B01 -> shows B01's own Red 4px Inside -> edit Size to 7 -> OK updates last fx
                        tab.locator('#openStrokeBtn').click()
                        stroke_modal.wait_for(state='visible')
                        self.assertEqual(tab.locator('#smColor').input_value().lower(), '#dc2626')
                        self.assertEqual(tab.locator('#smSize').input_value(), '4')
                        self.assertEqual(tab.locator('#smPosition').input_value(), 'inside')
                        self.assertEqual(tab.locator('#smOpacity').input_value(), '90')
                        self.assertEqual(tab.locator('#smBlend').input_value(), 'Screen')

                        tab.locator('#smSize').fill('7')
                        tab.locator('#smSize').dispatch_event('input')
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#smOk').click()
                        stroke_modal.wait_for(state='hidden')

                        updated_last_stroke = tab.evaluate("() => JSON.parse(localStorage.getItem('manhwa_last_fx_stroke'))")
                        self.assertEqual(updated_last_stroke, {
                            'color': [220, 38, 38],
                            'size': 7,
                            'position': 'inside',
                            'opacity': 90,
                            'blend_mode': 'Screen',
                        })

                        # =====================================================================
                        # CASE 4: Export PSD via Photopea & verify 100% parity for B01 (existing) & B03 (remembered)
                        # =====================================================================
                        tab.locator('[data-cmd=export]').click()
                        tab.locator('#format').select_option('psd')
                        tab.locator('#modal button.primary').click()
                        tab.locator('#exportStatus.export-ok').wait_for(timeout=120000)

                        psd_path = next((studio.ROOT / key / 'exports').glob('*/*.psd'))
                        doc = PSDImage.open(psd_path)
                        type_layers = {l.name: l for l in doc.descendants() if l.kind == 'type'}
                        l_b1 = next(v for k, v in type_layers.items() if b1.id in k)
                        l_b3 = next(v for k, v in type_layers.items() if b3.id in k)

                        # B01 PSD: Red 7px Inside stroke + 2-stop Cyan->Blue gradient
                        fx1 = l_b1.tagged_blocks.get_data(b'lfx2')
                        self.assertEqual(fx1[b'FrFX'][b'Styl'].enum, b'InsF')
                        self.assertAlmostEqual(float(fx1[b'FrFX'][b'Sz  ']), 7.0, delta=0.5)
                        self.assertAlmostEqual(float(fx1[b'FrFX'][b'Clr '][b'Rd  ']), 220.0, delta=1.5)
                        self.assertEqual(len(fx1[b'GrFl'][b'Grad'][b'Clrs']), 2)
                        self.assertAlmostEqual(float(fx1[b'GrFl'][b'Angl']), 45.0, delta=0.5)

                        # B03 PSD: Remembered Black 6px Outside stroke + Remembered 3-stop Red->Purple->Green gradient
                        fx3 = l_b3.tagged_blocks.get_data(b'lfx2')
                        fr3 = fx3[b'FrFX']
                        self.assertEqual(fr3[b'Styl'].enum, b'OutF')
                        self.assertAlmostEqual(float(fr3[b'Sz  ']), 6.0, delta=0.5)
                        self.assertAlmostEqual(float(fr3[b'Opct']), 88.0, delta=0.5)
                        self.assertEqual(fr3[b'Md  '].enum, b'Mltp')
                        self.assertAlmostEqual(float(fr3[b'Clr '][b'Rd  ']), 0.0, delta=1.5)
                        self.assertAlmostEqual(float(fr3[b'Clr '][b'Grn ']), 0.0, delta=1.5)
                        self.assertAlmostEqual(float(fr3[b'Clr '][b'Bl  ']), 0.0, delta=1.5)

                        gr3 = fx3[b'GrFl']
                        clrs3 = gr3[b'Grad'][b'Clrs']
                        self.assertEqual(len(clrs3), 3)
                        self.assertEqual(int(clrs3[0][b'Lctn']), 0)
                        self.assertAlmostEqual(int(clrs3[1][b'Lctn']), 2048, delta=60)
                        self.assertEqual(int(clrs3[2][b'Lctn']), 4096)
                        self.assertAlmostEqual(float(gr3[b'Angl']), -45.0, delta=0.5)
                        self.assertAlmostEqual(float(gr3[b'Scl ']), 125.0, delta=0.5)
                        self.assertAlmostEqual(float(gr3[b'Opct']), 85.0, delta=0.5)
                        self.assertTrue(bool(gr3[b'Rvrs']))
                        self.assertFalse(bool(gr3[b'Algn']))

                        self.assertEqual(errors, [], f'Browser JS errors: {errors}')
                        ctx.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(10)


if __name__ == '__main__':
    unittest.main()
