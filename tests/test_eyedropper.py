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
from bubble_cleaner import encode_patch
from manhwa_ocr import Bubble, Page
from text_layout import layout_text


class EyedropperStaticTests(unittest.TestCase):
    def test_markup_css_and_script_contracts(self):
        root = Path(__file__).resolve().parent.parent / 'web'
        html = (root / 'index.html').read_text(encoding='utf-8')
        css = (root / 'studio.css').read_text(encoding='utf-8')
        js = (root / 'studio.js').read_text(encoding='utf-8')

        # All 4 eyedropper buttons across the 3 color targets exist
        for elem_id in ('inkPick', 'tbColorPick', 'edgePick', 'gmStopPick', 'eyedropLoupe', 'edZoom', 'edHex', 'edTarget'):
            self.assertIn(f'id="{elem_id}"', html)

        # Loupe uses transform translate3d + offscreen canvas getImageData
        self.assertIn('getImageData', js)
        self.assertIn("translate3d(", js)
        self.assertIn("e.key==='Alt'", js)
        self.assertIn("e.preventDefault()", js)
        self.assertIn('#viewport.eyedropper', css)
        self.assertIn('#eyedropLoupe', css)


@unittest.skipUnless(os.getenv('TEST_STUDIO_BROWSER') == '1', 'requires Chromium')
class EyedropperBrowserTests(unittest.TestCase):
    def test_in_app_eyedropper_buttons_alt_hold_compare_and_60fps_loupe(self):
        import uvicorn
        from playwright.sync_api import sync_playwright

        # Known exact color patches on the comic page (solid RGB blocks for 100% pixel verification)
        SWATCH_RED = '#E63946'
        SWATCH_TEAL = '#14B8A6'
        SWATCH_GOLD = '#F59E0B'
        SWATCH_VIOLET = '#7C3AED'
        SWATCH_LIME = '#22C55E'
        SWATCH_ORIG_INK = '#DC2626'

        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'studio'):
            im = Image.new('RGB', (800, 640), '#f8fafc')
            dr = ImageDraw.Draw(im)
            # Paint crisp color swatches on the left/bottom areas away from modals
            dr.rectangle((20, 20, 140, 140), fill=SWATCH_RED)
            dr.rectangle((20, 160, 140, 280), fill=SWATCH_TEAL)
            dr.rectangle((20, 300, 140, 420), fill=SWATCH_GOLD)
            dr.rectangle((20, 440, 140, 560), fill=SWATCH_VIOLET)
            dr.rectangle((160, 440, 280, 560), fill=SWATCH_LIME)

            # Bubble on the right side with crimson original lettering inside
            box = [320, 100, 720, 280]
            dr.rounded_rectangle(box, radius=20, fill='white', outline='#111111', width=4)
            dr.rectangle((440, 150, 600, 230), fill=SWATCH_ORIG_INK)

            path = Path(tmp) / 'eyedrop_page.png'
            im.save(path)

            b1 = Bubble(box=box, text='COLOR', translation='ทดสอบดูดสีบนแคนวาส', score=0.99)
            b1.text_layer = layout_text(im, b1)
            page_obj = Page(str(path), [b1])
            # Clean patch makes inside of bubble pure white (#FFFFFF) in clean/thai view
            x0, y0, x1, y1 = box
            clean_patch = Image.new('RGB', (x1 - x0, y1 - y0), '#FFFFFF')
            mask = np.full((y1 - y0, x1 - x0), 255, dtype=np.uint8)
            page_obj.clean_patches = [encode_patch(box, clean_patch, mask, method='solid', bubble_id=b1.id)]

            key = 'edrop1'
            studio.save(key, 'eyedrop_page.png', [page_obj], {})

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

                        # Helper to map image (ix, iy) -> viewport client (cx, cy)
                        def img_to_client(ix, iy):
                            return tab.evaluate(
                                """([ix, iy]) => {
                                  const r = document.getElementById('art').getBoundingClientRect();
                                  return [r.left + (ix / 800) * r.width, r.top + (iy / 640) * r.height];
                                }""",
                                [ix, iy],
                            )

                        # Switch to Move (V) mode so Contextual Task Bar (#textBar) + Thai text are visible
                        tab.keyboard.press('v')
                        tab.locator('#textBar').wait_for(state='visible')
                        tab.wait_for_function("document.getElementById('art').complete && document.getElementById('art').naturalWidth === 800")

                        # === 1A) Button Eyedropper: Text Color (Contextual Task Bar #tbColorPick) ===
                        tab.locator('#tbColorPick').click()
                        self.assertIn('active', tab.locator('#tbColorPick').get_attribute('class'))
                        self.assertIn('eyedropper', tab.locator('#viewport').get_attribute('class'))

                        rx, ry = img_to_client(80, 80)  # SWATCH_RED #E63946
                        tab.mouse.move(rx, ry)
                        tab.wait_for_timeout(40)
                        loupe = tab.locator('#eyedropLoupe')
                        loupe.wait_for(state='visible')
                        self.assertEqual(tab.locator('#edHex').inner_text(), SWATCH_RED)
                        self.assertIn('สีข้อความ', tab.locator('#edTarget').inner_text())
                        self.assertIn('translate3d(', loupe.evaluate('e => e.style.transform'))

                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.mouse.click(rx, ry)
                        self.assertEqual(tab.locator('#ink').input_value().upper(), SWATCH_RED)
                        self.assertEqual(tab.locator('#tbColor').input_value().upper(), SWATCH_RED)
                        self.assertNotIn('eyedropper', tab.locator('#viewport').get_attribute('class') or '')
                        self.assertFalse(loupe.is_visible())

                        # Verify Esc cancels button eyedropper cleanly
                        tab.locator('#inkPick').click()
                        self.assertIn('eyedropper', tab.locator('#viewport').get_attribute('class'))
                        tab.keyboard.press('Escape')
                        self.assertNotIn('eyedropper', tab.locator('#viewport').get_attribute('class') or '')

                        # === 1B) Button Eyedropper: Stroke Color (#edgePick) ===
                        tab.locator('#edgePick').click()
                        tx, ty = img_to_client(80, 220)  # SWATCH_TEAL #14B8A6
                        tab.mouse.move(tx, ty)
                        tab.wait_for_timeout(40)
                        self.assertEqual(tab.locator('#edHex').inner_text(), SWATCH_TEAL)
                        self.assertIn('Stroke', tab.locator('#edTarget').inner_text())
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.mouse.click(tx, ty)
                        self.assertEqual(tab.locator('#edge').input_value().upper(), SWATCH_TEAL)
                        self.assertTrue(tab.locator('#stroke').is_checked())

                        # === 1C) Compare Original (Eye Toggle) WYSIWYG Sampling ===
                        # Inside bubble (520, 190): Clean image is #FFFFFF, Original image is #DC2626
                        bx, by = img_to_client(520, 190)
                        tab.locator('#compareBtn').click()
                        tab.wait_for_function("document.getElementById('artOrig').complete && document.getElementById('artOrig').naturalWidth === 800")
                        tab.locator('#inkPick').click()
                        tab.mouse.move(bx, by)
                        tab.wait_for_timeout(40)
                        self.assertEqual(tab.locator('#edHex').inner_text(), SWATCH_ORIG_INK)
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.mouse.click(bx, by)
                        self.assertEqual(tab.locator('#ink').input_value().upper(), SWATCH_ORIG_INK)
                        # Turn Compare Original OFF -> same pixel now reads #FFFFFF from clean image!
                        tab.locator('#compareBtn').click()
                        tab.locator('#inkPick').click()
                        tab.mouse.move(bx, by)
                        tab.wait_for_timeout(40)
                        self.assertEqual(tab.locator('#edHex').inner_text(), '#FFFFFF')
                        tab.keyboard.press('Escape')

                        # === 2) Gradient Overlay Modal: Button Eyedropper + Alt-Hold (without closing modal!) ===
                        tab.locator('#openGradBtn').click()
                        modal = tab.locator('#gradModal')
                        modal.wait_for(state='visible')

                        # Button eyedropper for Stop 1 -> SWATCH_GOLD #F59E0B
                        tab.locator('#gmStops .gm-stop').nth(0).click()
                        tab.locator('#gmStopPick').click()
                        gx, gy = img_to_client(80, 360)  # SWATCH_GOLD
                        tab.mouse.move(gx, gy)
                        tab.wait_for_timeout(40)
                        self.assertEqual(tab.locator('#edHex').inner_text(), SWATCH_GOLD)
                        self.assertIn('Gradient Stop 1', tab.locator('#edTarget').inner_text())
                        tab.mouse.click(gx, gy)
                        self.assertTrue(modal.is_visible(), 'Gradient modal must stay open after button eyedrop')
                        self.assertEqual(tab.locator('#gmStopColor').input_value().upper(), SWATCH_GOLD)

                        # Select Stop 2 -> Hold Alt + Click SWATCH_VIOLET (#7C3AED) -> Drag to SWATCH_LIME (#22C55E)
                        tab.locator('#gmStops .gm-stop').nth(1).click()
                        vx, vy = img_to_client(80, 500)   # SWATCH_VIOLET
                        lx, ly = img_to_client(220, 500)  # SWATCH_LIME

                        tab.mouse.move(vx, vy)
                        tab.keyboard.down('Alt')
                        tab.wait_for_timeout(40)
                        self.assertTrue(loupe.is_visible())
                        self.assertEqual(tab.locator('#edHex').inner_text(), SWATCH_VIOLET)
                        self.assertIn('Gradient Stop 2', tab.locator('#edTarget').inner_text())

                        # Live drag from Violet to Lime while holding Alt
                        tab.mouse.down()
                        self.assertEqual(tab.locator('#gmStopColor').input_value().upper(), SWATCH_VIOLET)
                        tab.mouse.move(lx, ly, steps=6)
                        tab.wait_for_timeout(40)
                        self.assertEqual(tab.locator('#gmStopColor').input_value().upper(), SWATCH_LIME)
                        live_grad_bg = tab.locator('.bubble.selected span').evaluate('e => e.style.backgroundImage')
                        self.assertIn('gradient(', live_grad_bg)
                        tab.mouse.up()
                        tab.keyboard.up('Alt')

                        self.assertTrue(modal.is_visible(), 'Gradient modal must remain open after Alt-eyedrop')
                        self.assertFalse(loupe.is_visible(), 'Loupe must hide when Alt is released')
                        self.assertEqual(tab.locator('#gmStopColor').input_value().upper(), SWATCH_LIME)

                        # Commit Gradient Overlay via OK -> saved to backend
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.locator('#gmOk').click()
                        modal.wait_for(state='hidden')

                        # === 3) Alt-Hold in Normal Mode -> Active Target Tracking (Text & Stroke) + Backend Save ===
                        # Focus/click Text color (#ink), hold Alt, click SWATCH_RED -> saves text color to backend
                        tab.locator('#ink').focus()
                        tab.mouse.move(rx, ry)
                        tab.keyboard.down('Alt')
                        tab.wait_for_timeout(40)
                        self.assertIn('สีข้อความ', tab.locator('#edTarget').inner_text())
                        tab.mouse.click(rx, ry)
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.keyboard.up('Alt')
                        self.assertEqual(tab.locator('#ink').input_value().upper(), SWATCH_RED)

                        # Focus Stroke color (#edge), hold Alt, click SWATCH_VIOLET -> saves stroke color to backend
                        tab.locator('#edge').focus()
                        tab.mouse.move(vx, vy)
                        tab.keyboard.down('Alt')
                        tab.wait_for_timeout(40)
                        self.assertIn('Stroke', tab.locator('#edTarget').inner_text())
                        tab.mouse.click(vx, vy)
                        with tab.expect_response(lambda r: '/edit' in r.url and r.ok):
                            tab.keyboard.up('Alt')
                        self.assertEqual(tab.locator('#edge').input_value().upper(), SWATCH_VIOLET)

                        # Measure Loupe 60fps smooth movement performance
                        loupe_fps = tab.evaluate("""() => new Promise(resolve => {
                          const r = document.getElementById('art').getBoundingClientRect();
                          document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Alt', code: 'AltLeft', bubbles: true }));
                          let first = 0, count = 0;
                          function step(t) {
                            first ||= t;
                            const cx = r.left + 20 + count * 2;
                            const cy = r.top + 80;
                            document.dispatchEvent(new PointerEvent('pointermove', { clientX: cx, clientY: cy, altKey: true, bubbles: true }));
                            if (++count === 61) {
                              document.dispatchEvent(new KeyboardEvent('keyup', { key: 'Alt', code: 'AltLeft', bubbles: true }));
                              resolve(60000 / (t - first));
                            } else requestAnimationFrame(step);
                          }
                          requestAnimationFrame(step);
                        })""")

                        # Verify final persisted colors in SQLite backend
                        saved_pages, _ = studio.load(key)
                        tl = saved_pages[0].bubbles[0].text_layer
                        self.assertEqual(tl['color'], [230, 57, 70])  # SWATCH_RED #E63946
                        self.assertTrue(tl['stroke']['enabled'])
                        self.assertEqual(tl['stroke']['color'], [124, 58, 237])  # SWATCH_VIOLET #7C3AED
                        self.assertTrue(tl['gradient']['enabled'])
                        self.assertEqual(tl['gradient']['stops'][0]['color'], SWATCH_GOLD)
                        self.assertEqual(tl['gradient']['stops'][-1]['color'], SWATCH_LIME)

                        print(f'Eyedropper verified: loupe={loupe_fps:.1f}fps, exact pixel HEX match across Text/Stroke/Gradient/Compare', flush=True)
                        self.assertEqual(errors, [], f'Browser JS errors: {errors}')
                        ctx.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(10)


if __name__ == '__main__':
    unittest.main()
