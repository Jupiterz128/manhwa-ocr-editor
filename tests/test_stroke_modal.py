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
from PIL import Image, ImageDraw
import web_api as studio
from manhwa_ocr import Bubble, Page, read_mtp, write_mtp
from text_layout import layout_text
from text_style_sampler import effects, styled_tile


class StrokeStyleUnitTests(unittest.TestCase):
    def test_effects_stroke_defaults_and_legacy_backwards_compatibility(self):
        # Empty / default stroke
        default_s = effects()['stroke']
        self.assertEqual(default_s, {
            'enabled': False,
            'color': [255, 255, 255],
            'size': 3,
            'position': 'outside',
            'opacity': 100,
            'blend_mode': 'Normal',
        })

        # Legacy payload missing position, opacity, blend_mode -> defaults to outside / 100 / Normal
        legacy = effects({
            'stroke': {
                'enabled': True,
                'color': [10, 20, 30],
                'size': 5,
            }
        })['stroke']
        self.assertTrue(legacy['enabled'])
        self.assertEqual(legacy['color'], [10, 20, 30])
        self.assertEqual(legacy['size'], 5)
        self.assertEqual(legacy['position'], 'outside')
        self.assertEqual(legacy['opacity'], 100)
        self.assertEqual(legacy['blend_mode'], 'Normal')

        # Full Photoshop Classic payload (1..20px, positions, opacity, blend_mode, hex color)
        rich = effects({
            'stroke': {
                'enabled': True,
                'color': '#f59e0b',
                'size': 18,
                'position': 'inside',
                'opacity': 72,
                'blend_mode': 'multiply',
            }
        })['stroke']
        self.assertEqual(rich, {
            'enabled': True,
            'color': [245, 158, 11],
            'size': 18,
            'position': 'inside',
            'opacity': 72,
            'blend_mode': 'Multiply',
        })

        # Clamping & invalid fallbacks
        clamped = effects({
            'stroke': {
                'enabled': True,
                'size': 99,
                'position': 'bogus',
                'opacity': -20,
                'blend_mode': 'unknown',
            }
        })['stroke']
        self.assertEqual(clamped['size'], 20)
        self.assertEqual(clamped['position'], 'outside')
        self.assertEqual(clamped['opacity'], 0)
        self.assertEqual(clamped['blend_mode'], 'Normal')

    def test_styled_tile_outside_center_inside_and_persistence(self):
        # Solid 60x60 glyph tile with a 24x24 dark blue square centered in it
        tile = Image.new('RGBA', (60, 60), (0, 0, 0, 0))
        dr = ImageDraw.Draw(tile)
        dr.rectangle((18, 18, 41, 41), fill=(20, 30, 40, 255))

        # 1) Outside stroke (size=6, red): expands tile by 2*6, NEVER eats into interior fill!
        out_outside = styled_tile(tile, {
            'stroke': {
                'enabled': True,
                'color': [255, 0, 0],
                'size': 6,
                'position': 'outside',
                'opacity': 100,
                'blend_mode': 'Normal',
            }
        })
        arr_out = np.asarray(out_outside)
        self.assertEqual(out_outside.size, (72, 72))
        # Center of glyph (shifted by +6 -> 30+6=36) stays pure fill (20, 30, 40, 255)
        self.assertEqual(tuple(int(v) for v in arr_out[36, 36]), (20, 30, 40, 255))
        # Interior edge pixel (18+6=24) also stays pure fill — zero inner eating!
        self.assertEqual(tuple(int(v) for v in arr_out[24, 36]), (20, 30, 40, 255))
        # Outside ring pixel (18+6-3=21) is pure red outer stroke
        self.assertEqual(tuple(int(v) for v in arr_out[21, 36]), (255, 0, 0, 255))

        # Outside stroke with 50% opacity -> alpha outside is ~128, interior stays opaque fill
        out_half = styled_tile(tile, {
            'stroke': {
                'enabled': True,
                'color': [255, 0, 0],
                'size': 6,
                'position': 'outside',
                'opacity': 50,
                'blend_mode': 'Normal',
            }
        })
        arr_half = np.asarray(out_half)
        self.assertEqual(tuple(int(v) for v in arr_half[36, 36]), (20, 30, 40, 255))
        self.assertAlmostEqual(int(arr_half[21, 36, 3]), 128, delta=2)

        # 2) Inside stroke (size=4, green): keeps exact tile bounds (0 outside bleed), colors inner border
        out_inside = styled_tile(tile, {
            'stroke': {
                'enabled': True,
                'color': [0, 255, 0],
                'size': 4,
                'position': 'inside',
                'opacity': 100,
                'blend_mode': 'Normal',
            }
        })
        arr_in = np.asarray(out_inside)
        self.assertEqual(out_inside.size, (60, 60))
        self.assertEqual(int(arr_in[15, 30, 3]), 0)  # strictly transparent outside glyph
        self.assertEqual(tuple(int(v) for v in arr_in[19, 30]), (0, 255, 0, 255))  # inside border is green
        self.assertEqual(tuple(int(v) for v in arr_in[30, 30]), (20, 30, 40, 255))  # deep center stays fill

        # 3) Center stroke (size=6, yellow): extends 3px outside AND 3px inside
        out_center = styled_tile(tile, {
            'stroke': {
                'enabled': True,
                'color': [255, 255, 0],
                'size': 6,
                'position': 'center',
                'opacity': 100,
                'blend_mode': 'Normal',
            }
        })
        arr_ctr = np.asarray(out_center)
        self.assertEqual(out_center.size, (66, 66))
        self.assertEqual(tuple(int(v) for v in arr_ctr[19, 33]), (255, 255, 0, 255))  # outside half
        self.assertEqual(tuple(int(v) for v in arr_ctr[22, 33]), (255, 255, 0, 255))  # inside half
        self.assertEqual(tuple(int(v) for v in arr_ctr[33, 33]), (20, 30, 40, 255))   # center fill

        # 4) Persistence roundtrip across SQLite, JSON, and MTP
        im = Image.new('RGB', (480, 320), 'white')
        b = Bubble(box=[40, 40, 440, 260], text='STROKE', translation='ทดสอบขอบนอก', score=0.98)
        b.text_layer = layout_text(im, b)
        stroke_cfg = {
            'enabled': True,
            'color': [245, 158, 11],
            'size': 12,
            'position': 'center',
            'opacity': 80,
            'blend_mode': 'Screen',
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'store'):
            root = Path(tmp)
            src = root / '01.png'
            im.save(src)
            studio.save('sjob', '01.png', [Page(str(src), [b])], {})
            studio.action('sjob', 0, 'edit', {'id': b.id, 'style': {'stroke': stroke_cfg}})
            pages_sql, _ = studio.load('sjob')
            saved_s = pages_sql[0].bubbles[0].text_layer['stroke']
            self.assertEqual(saved_s, stroke_cfg)

            jpath = root / 'sproj.json'
            studio.save_json_project('sjob', {'path': str(jpath)})
            opened = studio.open_json_project({'path': str(jpath)})
            pages_json, _ = studio.load(opened['key'])
            self.assertEqual(pages_json[0].bubbles[0].text_layer['stroke'], saved_s)

            mtp = root / 'sproj.mtp'
            write_mtp(mtp, pages_sql, {}, threading.Event(), lambda _: None)
            pages_mtp, _, _ = read_mtp(mtp, threading.Event(), lambda _: None, root / 'unpacked')
            self.assertEqual(pages_mtp[0].bubbles[0].text_layer['stroke'], saved_s)

    def test_jsx_and_frontend_contracts(self):
        root = Path(__file__).resolve().parent.parent
        jsx = (root / 'text_layer_fx.jsx').read_text(encoding='utf-8')
        html = (root / 'web' / 'index.html').read_text(encoding='utf-8')
        css = (root / 'web' / 'studio.css').read_text(encoding='utf-8')
        js = (root / 'web' / 'studio.js').read_text(encoding='utf-8')

        for token in ('OutF', 'CtrF', 'InsF', 'FrFX', 'FStl', 'Styl', 'Opct', 'BlnM'):
            self.assertIn(token, jsx)
        for elem_id in ('strokeModal', 'strokeHeader', 'smSize', 'smSizeNum', 'smPosition', 'smBlend',
                        'smOpacity', 'smOpacityNum', 'smColor', 'smColorPick', 'smOk', 'smCancel', 'openStrokeBtn'):
            self.assertIn(f'id="{elem_id}"', html)
        self.assertIn('#strokeModal', css)
        self.assertIn('.txt-stroke', css)
        self.assertIn('.txt-fill', css)
        self.assertIn('normalizeStroke', js)
        self.assertIn('openStrokeModal', js)


@unittest.skipUnless(os.getenv('TEST_STUDIO_BROWSER') == '1', 'requires Chromium and Photopea')
class StrokeModalBrowserTests(unittest.TestCase):
    def test_photoshop_stroke_modal_outer_render_eyedropper_60fps_and_photopea_psd(self):
        import uvicorn
        from playwright.sync_api import sync_playwright
        from psd_tools import PSDImage

        SWATCH_TEAL = '#14B8A6'
        SWATCH_GOLD = '#F59E0B'
        SWATCH_RED = '#FF0000'

        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'studio'):
            im = Image.new('RGB', (800, 640), '#f8fafc')
            dr = ImageDraw.Draw(im)
            # Crisp color swatches on the left for eyedropper sampling
            dr.rectangle((20, 20, 140, 140), fill=SWATCH_TEAL)
            dr.rectangle((20, 160, 140, 280), fill=SWATCH_GOLD)
            dr.rectangle((20, 300, 140, 420), fill=SWATCH_RED)
            # Large white balloon on the right
            dr.rounded_rectangle((220, 80, 740, 320), radius=24, fill='white', outline='#111111', width=4)
            path = Path(tmp) / 'stroke_page.png'
            im.save(path)

            b1 = Bubble(box=[220, 80, 740, 320], text='OUTER', translation='ทดสอบขอบนอก', score=0.99)
            b1.text_layer = layout_text(im, b1)
            b1.text_layer['font_size'] = 64
            b1.text_layer['color'] = [17, 17, 17]

            key = 'strkfx'
            studio.save(key, 'stroke_page.png', [Page(str(path), [b1])], {})

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

                        def img_to_client(ix, iy):
                            return tab.evaluate(
                                """([ix, iy]) => {
                                  const r = document.getElementById('art').getBoundingClientRect();
                                  return [r.left + (ix / 800) * r.width, r.top + (iy / 640) * r.height];
                                }""",
                                [ix, iy],
                            )

                        # 1) Open Layer Style — Stroke modal and verify defaults
                        tab.locator('#openStrokeBtn').click()
                        modal = tab.locator('#strokeModal')
                        modal.wait_for(state='visible')
                        self.assertEqual(tab.locator('#smSize').input_value(), '3')
                        self.assertEqual(tab.locator('#smSizeNum').input_value(), '3')
                        self.assertEqual(tab.locator('#smPosition').input_value(), 'outside')
                        self.assertEqual(tab.locator('#smBlend').input_value(), 'Normal')
                        self.assertEqual(tab.locator('#smOpacity').input_value(), '100')
                        self.assertEqual(tab.locator('#smColor').input_value().lower(), '#ffffff')

                        # Verify Outer Stroke DOM layering: stroke behind fill (z-index 0 vs 1), width = 2*size*zoom
                        layer_info = tab.evaluate("""() => {
                          const b = document.querySelector('.bubble.selected');
                          const fill = b.querySelector('.txt-fill');
                          const stroke = b.querySelector('.txt-stroke');
                          return {
                            fillZ: getComputedStyle(fill).zIndex,
                            strokeZ: getComputedStyle(stroke).zIndex,
                            fillStrokeCss: fill.style.webkitTextStroke,
                            strokeCss: stroke.style.webkitTextStroke,
                            pos: stroke.dataset.strokePosition,
                            sameBox: Math.abs(fill.getBoundingClientRect().left - stroke.getBoundingClientRect().left) < 0.5
                              && Math.abs(fill.getBoundingClientRect().top - stroke.getBoundingClientRect().top) < 0.5,
                          };
                        }""")
                        self.assertEqual(layer_info['fillZ'], '1')
                        self.assertEqual(layer_info['strokeZ'], '0')
                        self.assertEqual(layer_info['fillStrokeCss'], '')
                        self.assertIn('px', layer_info['strokeCss'])
                        self.assertEqual(layer_info['pos'], 'outside')
                        self.assertTrue(layer_info['sameBox'])

                        # Visually verify Outer Stroke does NOT eat Thai character body!
                        # Set thick red outer stroke (8px) on dark (#111111) Thai letters and check rendered pixels
                        tab.locator('#smColor').fill(SWATCH_RED)
                        tab.locator('#smColor').dispatch_event('input')
                        tab.locator('#smSize').fill('8')
                        tab.locator('#smSize').dispatch_event('input')
                        tab.wait_for_timeout(60)
                        bubble_png = tab.locator('.bubble.selected').screenshot()
                        bubble_arr = np.asarray(Image.open(io.BytesIO(bubble_png)).convert('RGB'))
                        dark_body_px = int(np.sum((bubble_arr[:, :, 0] < 50) & (bubble_arr[:, :, 1] < 50) & (bubble_arr[:, :, 2] < 50)))
                        red_halo_px = int(np.sum((bubble_arr[:, :, 0] > 200) & (bubble_arr[:, :, 1] < 60) & (bubble_arr[:, :, 2] < 60)))
                        self.assertGreater(dark_body_px, 200, 'Thai text fill body must remain intact under Outer Stroke')
                        self.assertGreater(red_halo_px, 200, 'Outer red stroke halo must be clearly visible around Thai text')

                        # Verify Center and Inside live preview modes
                        tab.locator('#smPosition').select_option('center')
                        self.assertEqual(tab.evaluate("() => getComputedStyle(document.querySelector('.bubble.selected .txt-stroke')).zIndex"), '2')
                        tab.locator('#smPosition').select_option('inside')
                        inside_filter = tab.evaluate("() => document.querySelector('.bubble.selected .txt-stroke').style.filter")
                        self.assertIn('#sin_', inside_filter)
                        tab.locator('#smPosition').select_option('outside')

                        # 2) Drag Stroke window header at 60fps with translate3d
                        head_bb = tab.locator('#strokeHeader').bounding_box()
                        before_modal = modal.bounding_box()
                        tab.mouse.move(head_bb['x'] + 40, head_bb['y'] + 12)
                        tab.mouse.down()
                        win_fps = tab.evaluate("""() => new Promise(resolve => {
                          let first = 0, count = 0;
                          function step(t) {
                            first ||= t;
                            strokeHeader.dispatchEvent(new PointerEvent('pointermove', {
                              pointerId: strokeWinDrag.id, clientX: strokeWinDrag.dx + 880 + count, clientY: strokeWinDrag.dy + 110, buttons: 1
                            }));
                            if (++count === 61) resolve(60000 / (t - first)); else requestAnimationFrame(step);
                          }
                          requestAnimationFrame(step);
                        })""")
                        tab.mouse.up()
                        after_modal = modal.bounding_box()
                        self.assertGreater(abs(after_modal['x'] - before_modal['x']), 20)
                        self.assertIn('translate3d(', modal.evaluate('e => e.style.transform'))

                        # 3) Drag Size & Opacity sliders at 60fps with ZERO DOM recreation
                        slider_metrics = tab.evaluate("""() => new Promise(resolve => {
                          const szEl = document.getElementById('smSize');
                          const opEl = document.getElementById('smOpacity');
                          const stEl = document.querySelector('.bubble.selected .txt-stroke');
                          let first = 0, count = 0;
                          function step(t) {
                            first ||= t;
                            szEl.value = String(1 + (count % 20)); szEl.dispatchEvent(new Event('input'));
                            opEl.value = String(20 + (count % 81)); opEl.dispatchEvent(new Event('input'));
                            if (++count === 61) {
                              resolve({
                                fps: 60000 / (t - first),
                                sameSizeNode: szEl === document.getElementById('smSize'),
                                sameOpacityNode: opEl === document.getElementById('smOpacity'),
                                sameStrokeNode: stEl === document.querySelector('.bubble.selected .txt-stroke'),
                              });
                            } else requestAnimationFrame(step);
                          }
                          requestAnimationFrame(step);
                        })""")
                        self.assertTrue(slider_metrics['sameSizeNode'])
                        self.assertTrue(slider_metrics['sameOpacityNode'])
                        self.assertTrue(slider_metrics['sameStrokeNode'])

                        # Cancel restores original disabled state
                        tab.locator('#smCancel').click()
                        modal.wait_for(state='hidden')
                        self.assertFalse(tab.locator('#stroke').is_checked())

                        # 4) Re-open Stroke modal -> test Button Eyedropper (#smColorPick) AND Alt+Click!
                        tab.locator('#openStrokeBtn').click()
                        modal.wait_for(state='visible')

                        # Button Eyedropper -> SWATCH_TEAL (#14B8A6)
                        tab.locator('#smColorPick').click()
                        tx, ty = img_to_client(80, 80)
                        tab.mouse.move(tx, ty)
                        tab.wait_for_timeout(40)
                        self.assertEqual(tab.locator('#edHex').inner_text(), SWATCH_TEAL)
                        self.assertIn('Stroke', tab.locator('#edTarget').inner_text())
                        tab.mouse.click(tx, ty)
                        self.assertTrue(modal.is_visible(), 'Stroke modal must stay open after button eyedropper')
                        self.assertEqual(tab.locator('#smColor').input_value().upper(), SWATCH_TEAL)
                        self.assertEqual(tab.locator('#smColorHex').inner_text(), SWATCH_TEAL)

                        # Alt-Hold + Left-Click -> SWATCH_GOLD (#F59E0B) while Stroke modal is open!
                        gx, gy = img_to_client(80, 220)
                        tab.mouse.move(gx, gy)
                        tab.keyboard.down('Alt')
                        tab.wait_for_timeout(40)
                        self.assertTrue(tab.locator('#eyedropLoupe').is_visible())
                        self.assertEqual(tab.locator('#edHex').inner_text(), SWATCH_GOLD)
                        self.assertIn('Stroke', tab.locator('#edTarget').inner_text())
                        tab.mouse.click(gx, gy)
                        tab.keyboard.up('Alt')
                        self.assertTrue(modal.is_visible(), 'Stroke modal must stay open after Alt+Click eyedropper')
                        self.assertEqual(tab.locator('#smColor').input_value().upper(), SWATCH_GOLD)
                        self.assertEqual(tab.locator('#smColorHex').inner_text(), SWATCH_GOLD)

                        # Configure Size=6, Position=outside, Blend=Multiply, Opacity=85, Color=#F59E0B -> OK
                        tab.locator('#smSize').fill('6')
                        tab.locator('#smSize').dispatch_event('input')
                        tab.locator('#smPosition').select_option('outside')
                        tab.locator('#smBlend').select_option('Multiply')
                        tab.locator('#smOpacity').fill('85')
                        tab.locator('#smOpacity').dispatch_event('input')

                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#smOk').click()
                        modal.wait_for(state='hidden')
                        self.assertTrue(tab.locator('#stroke').is_checked())

                        # Verify persisted in SQLite
                        saved_pages, _ = studio.load(key)
                        s_saved = saved_pages[0].bubbles[0].text_layer['stroke']
                        self.assertEqual(s_saved, {
                            'enabled': True,
                            'color': [245, 158, 11],
                            'size': 6,
                            'position': 'outside',
                            'opacity': 85,
                            'blend_mode': 'Multiply',
                        })

                        # 5) Export PSD via Photopea and inspect binary FrFX descriptor
                        tab.locator('[data-cmd=export]').click()
                        tab.locator('#format').select_option('psd')
                        tab.locator('#modal button.primary').click()
                        tab.locator('#exportStatus.export-ok').wait_for(timeout=120000)

                        psd_path = next((studio.ROOT / key / 'exports').glob('*/*.psd'))
                        doc = PSDImage.open(psd_path)
                        type_layer = next(l for l in doc.descendants() if l.kind == 'type' and b1.id in l.name)
                        self.assertIn('Stroke', {e.name for e in type_layer.effects if e.enabled})
                        fx = type_layer.tagged_blocks.get_data(b'lfx2')
                        frfx = fx[b'FrFX']
                        self.assertEqual(frfx[b'Styl'].enum, b'OutF')
                        self.assertAlmostEqual(float(frfx[b'Sz  ']), 6.0, delta=0.5)
                        self.assertAlmostEqual(float(frfx[b'Opct']), 85.0, delta=0.5)
                        self.assertEqual(frfx[b'Md  '].enum, b'Mltp')
                        clr = frfx[b'Clr ']
                        self.assertAlmostEqual(float(clr[b'Rd  ']), 245.0, delta=1.5)
                        self.assertAlmostEqual(float(clr[b'Grn ']), 158.0, delta=1.5)
                        self.assertAlmostEqual(float(clr[b'Bl  ']), 11.0, delta=1.5)

                        self.assertEqual(errors, [], f'Browser JS errors: {errors}')
                        ctx.close()
                        browser.close()

                    # Also verify Center (CtrF) and Inside (InsF) PSD FrFX enums via Photopea
                    from photoshop_export import PhotopeaSession, export_psd
                    with PhotopeaSession() as pp_session:
                        for pos_val, expected_enum in (('center', b'CtrF'), ('inside', b'InsF')):
                            b_test = Bubble(box=[220, 80, 740, 320], text='POS', translation='ทดสอบตำแหน่งขอบ', score=0.99)
                            b_test.text_layer = layout_text(im, b_test)
                            b_test.text_layer['stroke'] = {
                                'enabled': True,
                                'color': [20, 184, 166],
                                'size': 4,
                                'position': pos_val,
                                'opacity': 90,
                                'blend_mode': 'Screen',
                            }
                            out_pos = Path(tmp) / f'stroke_{pos_val}.psd'
                            export_psd(im, Page(str(path), [b_test]), out_pos, backend='photopea', session=pp_session)
                            pos_doc = PSDImage.open(out_pos)
                            pos_layer = next(l for l in pos_doc.descendants() if l.kind == 'type')
                            pos_frfx = pos_layer.tagged_blocks.get_data(b'lfx2')[b'FrFX']
                            self.assertEqual(pos_frfx[b'Styl'].enum, expected_enum)
                            self.assertAlmostEqual(float(pos_frfx[b'Sz  ']), 4.0, delta=0.5)
                            self.assertAlmostEqual(float(pos_frfx[b'Opct']), 90.0, delta=0.5)
                            self.assertEqual(pos_frfx[b'Md  '].enum, b'Scrn')
                            pclr = pos_frfx[b'Clr ']
                            self.assertAlmostEqual(float(pclr[b'Rd  ']), 20.0, delta=1.5)
                            self.assertAlmostEqual(float(pclr[b'Grn ']), 184.0, delta=1.5)
                            self.assertAlmostEqual(float(pclr[b'Bl  ']), 166.0, delta=1.5)

                    print(
                        f'Stroke modal verified: win_drag={win_fps:.1f}fps, sliders={slider_metrics["fps"]:.1f}fps, '
                        f'body_px={dark_body_px}, halo_px={red_halo_px}, PSD FrFX=OutF/CtrF/InsF verified',
                        flush=True,
                    )
                finally:
                    server.should_exit = True
                    thread.join(10)


if __name__ == '__main__':
    unittest.main()
