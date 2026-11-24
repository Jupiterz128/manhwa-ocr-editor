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


class CanvasUXStaticTests(unittest.TestCase):
    def test_markup_and_script_contracts(self):
        root = Path(__file__).resolve().parent.parent / 'web'
        html = (root / 'index.html').read_text(encoding='utf-8')
        js = (root / 'studio.js').read_text(encoding='utf-8')
        css = (root / 'studio.css').read_text(encoding='utf-8')

        # 1. Non-passive wheel listener with preventDefault + stopPropagation
        self.assertIn("addEventListener('wheel'", js)
        self.assertIn('{passive:false}', js)
        self.assertIn('e.preventDefault()', js)
        self.assertIn('e.stopPropagation()', js)

        # 2. Contextual Task Bar mounted once in exact order: grip, font, color, align, size
        bar_idx = html.index('id="textBar"')
        grip_idx = html.index('id="textBarGrip"', bar_idx)
        font_idx = html.index('id="tbFont"', bar_idx)
        color_idx = html.index('id="tbColor"', bar_idx)
        align_idx = html.index('id="tbAlign"', bar_idx)
        size_idx = html.index('id="tbSize"', bar_idx)
        self.assertTrue(grip_idx < font_idx < color_idx < align_idx < size_idx)
        self.assertIn('#textBar.dragging-hide', css)

        # 3. Fit button in bottom toolbar replaced by eye compare toggle; Fit kept in commands & '0' shortcut
        tools_slice = html[html.index('class="float tools"'):html.index('</article>')]
        self.assertNotIn('data-cmd="fit"', tools_slice)
        self.assertIn('id="compareBtn"', tools_slice)
        self.assertIn('data-cmd="compareOriginal"', tools_slice)
        self.assertIn("'0':'fit'", js)
        self.assertIn("fit:['ซูมพอดีหน้า (Fit)'", js)


@unittest.skipUnless(os.getenv('TEST_STUDIO_BROWSER') == '1', 'requires Chromium')
class CanvasUXBrowserTests(unittest.TestCase):
    def test_zoom_to_cursor_contextual_bar_and_eye_compare_on_tall_strip(self):
        import uvicorn
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'studio'):
            # Build a tall 720x10000 SmartStitch-style strip with visual landmarks & bubbles
            im = Image.new('RGB', (720, 10000), '#1f2937')
            dr = ImageDraw.Draw(im)
            for band_y in range(0, 10000, 500):
                dr.rectangle((0, band_y, 720, band_y + 120), fill='#334155')
            # Draw 4 bubbles across the strip (top + middle around y=4800)
            specs = [
                ([80, 120, 340, 260], 'FIRST', 'บอลลูนแรกทดสอบ'),
                ([380, 200, 640, 340], 'SECOND', 'บอลลูนสองทดสอบ'),
                ([100, 460, 360, 600], 'THIRD', 'บอลลูนสามทดสอบ'),
                ([160, 4800, 560, 5000], 'DEEP', 'บอลลูนกลางภาพยาว'),
            ]
            bubbles = []
            for box, src, th in specs:
                dr.rounded_rectangle(box, radius=24, fill='white', outline='#111111', width=4)
                # Paint distinctive original red ink inside each balloon so Original vs Clean differs visibly
                cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
                dr.rectangle((cx - 40, cy - 18, cx + 40, cy + 18), fill='#dc2626')
                b = Bubble(box=box, text=src, translation=th, score=0.97, lines=[{'box': [cx - 45, cy - 22, cx + 45, cy + 22]}])
                b.text_layer = layout_text(im, b)
                bubbles.append(b)
            path = Path(tmp) / 'tall_strip.png'
            im.save(path)

            # Create clean patches so Clean view is pure white inside bubbles while Original has red ink
            page_obj = Page(str(path), bubbles)
            import numpy as np
            from bubble_cleaner import encode_patch
            patches = []
            for b in bubbles:
                x0, y0, x1, y1 = b.box
                clean_patch = Image.new('RGB', (x1 - x0, y1 - y0), 'white')
                mask = np.full((y1 - y0, x1 - x0), 255, dtype=np.uint8)
                patches.append(encode_patch([x0, y0, x1, y1], clean_patch, mask, method='solid', bubble_id=b.id))
            page_obj.clean_patches = patches

            key = 'tallux'
            studio.save(key, 'tall_strip.png', [page_obj], {})

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

                        # === TEST 1: Ctrl + Wheel Zoom-to-Cursor on tall 720x10000 image ===
                        tab.locator('#percent').click()  # 100% zoom -> 720x10000 scrollable
                        tab.evaluate('document.getElementById("viewport").scrollTop = 4500')
                        vp_box = tab.locator('#viewport').bounding_box()
                        cursor_x = vp_box['x'] + vp_box['width'] * 0.52
                        cursor_y = vp_box['y'] + vp_box['height'] * 0.45
                        tab.mouse.move(cursor_x, cursor_y)

                        def img_point_under_cursor():
                            return tab.evaluate(
                                """([cx, cy]) => {
                                  const st = document.getElementById('stage').getBoundingClientRect();
                                  const z = st.width / 720;
                                  return {
                                    x: (cx - st.left) / z,
                                    y: (cy - st.top) / z,
                                    z,
                                    scrollTop: document.getElementById('viewport').scrollTop,
                                  };
                                }""",
                                [cursor_x, cursor_y],
                            )

                        before_pt = img_point_under_cursor()
                        tab.keyboard.down('Control')
                        for _ in range(4):
                            tab.mouse.wheel(0, -120)
                        tab.keyboard.up('Control')
                        after_in = img_point_under_cursor()
                        self.assertGreater(after_in['z'], before_pt['z'] * 1.25)
                        self.assertLessEqual(abs(after_in['y'] - before_pt['y']), 2.0, f'Zoom-in drifted Y: {before_pt} -> {after_in}')

                        # Zoom further until wider than viewport (horizontal scrolling active) -> both X and Y stay pinned
                        tab.keyboard.down('Control')
                        for _ in range(5):
                            tab.mouse.wheel(0, -120)
                        tab.keyboard.up('Control')
                        wide_before = img_point_under_cursor()
                        tab.keyboard.down('Control')
                        for _ in range(3):
                            tab.mouse.wheel(0, -120)
                        tab.keyboard.up('Control')
                        wide_after = img_point_under_cursor()
                        self.assertGreater(wide_after['z'], wide_before['z'] * 1.2)
                        self.assertLessEqual(abs(wide_after['x'] - wide_before['x']), 2.0, f'Wide zoom-in drifted X: {wide_before} -> {wide_after}')
                        self.assertLessEqual(abs(wide_after['y'] - wide_before['y']), 2.0, f'Wide zoom-in drifted Y: {wide_before} -> {wide_after}')

                        # Zoom back out with Ctrl+Wheel and verify cursor Y stays pinned (no jump to top)
                        tab.keyboard.down('Control')
                        for _ in range(14):
                            tab.mouse.wheel(0, 120)
                        tab.keyboard.up('Control')
                        after_out = img_point_under_cursor()
                        self.assertLess(after_out['z'], before_pt['z'])
                        self.assertLessEqual(abs(after_out['y'] - before_pt['y']), 3.0, f'Zoom-out drifted Y: {before_pt} -> {after_out}')
                        self.assertGreater(after_out['scrollTop'], 2000)

                        # Test Fit shortcut '0' and Command Palette Ctrl+K
                        tab.keyboard.press('0')
                        fit_pct = tab.locator('#percent').inner_text()
                        self.assertNotEqual(fit_pct, '100%')
                        tab.keyboard.press('Control+k')
                        tab.locator('#modal .pal-item').first.wait_for(state='visible')
                        self.assertIn('Fit', tab.locator('#modal').inner_text())
                        tab.locator('#close').click()

                        # === TEST 2: Contextual Task Bar in Text + Move (V) mode ===
                        tab.locator('#percent').click()
                        tab.evaluate('document.getElementById("viewport").scrollTop = 0')
                        # Enter Move (V) -> switches to Text view and shows Contextual Task Bar
                        tab.keyboard.press('v')
                        text_bar = tab.locator('#textBar')
                        text_bar.wait_for(state='visible')
                        tab.wait_for_timeout(60)

                        # Verify left-to-right visual order: grip < font < color < align < size
                        order = tab.evaluate(
                            """() => {
                              const ids = ['textBarGrip', 'tbFont', 'tbColor', 'tbAlign', 'tbSizeDec', 'tbSize', 'tbSizeInc'];
                              return ids.map(id => {
                                const r = document.getElementById(id).getBoundingClientRect();
                                return { id, x: r.left, w: r.width };
                              });
                            }"""
                        )
                        xs = [item['x'] for item in order]
                        self.assertEqual(xs, sorted(xs), f'Task bar controls out of order: {order}')
                        self.assertTrue(all(item['w'] > 0 for item in order), f'Zero-width control: {order}')

                        # Verify positioned ~14px below selected bubble and inside viewport
                        bar_bb = text_bar.bounding_box()
                        sel_bb = tab.locator('.bubble.selected').bounding_box()
                        self.assertAlmostEqual(bar_bb['y'], sel_bb['y'] + sel_bb['height'] + 14, delta=3)
                        self.assertGreaterEqual(bar_bb['x'], vp_box['x'])
                        self.assertLessEqual(bar_bb['x'] + bar_bb['width'], vp_box['x'] + vp_box['width'])

                        # Verify Mount-Once & Zero-Fetch rapid switching across Text Layers (< 2ms JS, 0 network requests)
                        reqs = []
                        tab.on('request', lambda r: reqs.append(r.url))
                        switch_stats = tab.evaluate(
                            """() => {
                              const optBefore = document.getElementById('tbFont').options[0];
                              const layers = Array.from(document.querySelectorAll('#layers .layer-main'));
                              const times = [];
                              for (let round = 0; round < 3; round++) {
                                for (let i = 0; i < 3; i++) {
                                  const t0 = performance.now();
                                  layers[i].click();
                                  times.push(performance.now() - t0);
                                }
                              }
                              const optAfter = document.getElementById('tbFont').options[0];
                              return {
                                sameOptionNode: optBefore === optAfter,
                                maxMs: Math.max(...times),
                                avgMs: times.reduce((a, b) => a + b, 0) / times.length,
                                times,
                              };
                            }"""
                        )
                        self.assertTrue(switch_stats['sameOptionNode'], 'Font <option> DOM nodes were recreated on switch!')
                        self.assertLess(switch_stats['avgMs'], 5.0, f'Layer switch too slow: {switch_stats}')
                        self.assertEqual([u for u in reqs if '/edit' in u], [], f'Unexpected /edit fetch on layer switch: {reqs}')

                        # Test Drag Grip (⋮⋮) moves Contextual Task Bar smoothly at 60fps
                        tab.wait_for_timeout(40)
                        before_drag_bb = text_bar.bounding_box()
                        grip_bb = tab.locator('#textBarGrip').bounding_box()
                        tab.mouse.move(grip_bb['x'] + 4, grip_bb['y'] + 4)
                        tab.mouse.down()
                        tab.mouse.move(grip_bb['x'] + 90, grip_bb['y'] + 60, steps=6)
                        tab.mouse.up()
                        tab.wait_for_timeout(40)
                        dragged_bb = text_bar.bounding_box()
                        self.assertGreater(abs(dragged_bb['x'] - before_drag_bb['x']), 40)

                        # Switch to B01 -> re-anchors under B01
                        tab.locator('#layers .layer-main').nth(0).click()
                        tab.wait_for_timeout(40)

                        # Test Typography controls sync: Font, Color, Align, Size (+ / - / input)
                        all_fonts = tab.locator('#tbFont option').all_text_contents()
                        alt_font = all_fonts[1] if len(all_fonts) > 1 else all_fonts[0]
                        tab.locator('#tbFont').select_option(alt_font)
                        self.assertEqual(tab.locator('#font').input_value(), alt_font)

                        tab.locator('#tbColor').fill('#12ab34')
                        self.assertEqual(tab.locator('#ink').input_value(), '#12ab34')
                        self.assertEqual(
                            tab.locator('.bubble.selected').evaluate('e => getComputedStyle(e).color'),
                            'rgb(18, 171, 52)',
                        )

                        tab.locator('#tbAlign [data-tb-align=right]').click()
                        self.assertEqual(tab.locator('#align').input_value(), 'right')
                        self.assertIn('active', tab.locator('#tbAlign [data-tb-align=right]').get_attribute('class'))
                        self.assertEqual(
                            tab.locator('.bubble.selected').evaluate('e => getComputedStyle(e).textAlign'),
                            'right',
                        )

                        sz0 = int(tab.locator('#tbSize').input_value())
                        tab.locator('#tbSizeInc').click()
                        tab.locator('#tbSizeInc').click()
                        self.assertEqual(int(tab.locator('#tbSize').input_value()), sz0 + 2)
                        self.assertEqual(int(tab.locator('#size').input_value()), sz0 + 2)
                        tab.locator('#tbSizeDec').click()
                        self.assertEqual(int(tab.locator('#tbSize').input_value()), sz0 + 1)
                        self.assertEqual(int(tab.locator('#size').input_value()), sz0 + 1)

                        # Wait for debounced save and verify auto_leading=True on backend
                        tab.wait_for_timeout(450)
                        saved_pages, _ = studio.load(key)
                        b0_layer = saved_pages[0].bubbles[0].text_layer
                        self.assertEqual(b0_layer['color'], [18, 171, 52])
                        self.assertEqual(b0_layer['alignment'], 'right')
                        self.assertEqual(b0_layer['font_size'], float(sz0 + 1))
                        self.assertTrue(b0_layer['auto_leading'])

                        # Verify Contextual Task Bar hides during drag (pointermove) and reappears below new position on pointerup
                        sel_bb = tab.locator('.bubble.selected').bounding_box()
                        cx, cy = sel_bb['x'] + sel_bb['width'] / 2, sel_bb['y'] + sel_bb['height'] / 2
                        tab.mouse.move(cx, cy)
                        tab.mouse.down()
                        tab.mouse.move(cx + 30, cy + 40, steps=4)
                        during_drag_opacity = text_bar.evaluate('e => getComputedStyle(e).opacity')
                        self.assertEqual(during_drag_opacity, '0', 'Task bar did not hide during pointermove drag!')
                        tab.mouse.up()
                        tab.wait_for_timeout(60)
                        after_drag_opacity = text_bar.evaluate('e => getComputedStyle(e).opacity')
                        self.assertEqual(after_drag_opacity, '1')
                        new_sel_bb = tab.locator('.bubble.selected').bounding_box()
                        new_bar_bb = text_bar.bounding_box()
                        self.assertAlmostEqual(new_bar_bb['y'], new_sel_bb['y'] + new_sel_bb['height'] + 14, delta=3)

                        # Verify Mask Edit (M) hides Text Bar, Ctrl+Wheel adjusts brush radius, and cancelling M restores Text Bar!
                        tab.keyboard.press('m')
                        tab.locator('#cleanPanel').wait_for(state='visible')
                        tab.wait_for_function("document.querySelector('#paint').dataset.ready==='yes'")
                        self.assertFalse(text_bar.is_visible(), 'Text bar must not overlap Mask Edit bar')
                        rad0 = int(tab.locator('#radius').input_value())
                        z_mask_before = tab.locator('#percent').inner_text()
                        tab.mouse.move(vp_box['x'] + 300, vp_box['y'] + 260)
                        tab.keyboard.down('Control')
                        tab.mouse.wheel(0, -100)
                        tab.keyboard.up('Control')
                        self.assertGreater(int(tab.locator('#radius').input_value()), rad0)
                        self.assertEqual(tab.locator('#percent').inner_text(), z_mask_before)
                        tab.keyboard.press('Escape')
                        tab.locator('#cleanPanel').wait_for(state='hidden')
                        text_bar.wait_for(state='visible')

                        # === TEST 3: Eye Toggle (replaces Fit) switches instantly to Original + Thai Text without frames ===
                        self.assertEqual(tab.locator('.float.tools [data-cmd=fit]').count(), 0)
                        eye_btn = tab.locator('#compareBtn')
                        self.assertTrue(eye_btn.is_visible())
                        self.assertEqual(eye_btn.get_attribute('aria-pressed'), 'false')
                        self.assertFalse(tab.locator('#artOrig').is_visible())

                        # Toggle ON -> zero network requests, instant visibility of #artOrig + Thai text, frames/handles hidden
                        compare_reqs = []
                        tab.on('request', lambda r: compare_reqs.append(r.url))
                        t0 = time.perf_counter()
                        eye_btn.click()
                        toggle_ms = (time.perf_counter() - t0) * 1000
                        self.assertEqual(eye_btn.get_attribute('aria-pressed'), 'true')
                        self.assertIn('active', eye_btn.get_attribute('class'))
                        self.assertTrue(tab.locator('#artOrig').is_visible())
                        self.assertIn('compare-mode', tab.locator('#overlay').get_attribute('class'))
                        # Frames & handles hidden, Thai text still rendered!
                        self.assertEqual(tab.locator('.bubble.selected .handle').first.is_visible(), False)
                        self.assertEqual(
                            tab.locator('.bubble.selected').inner_text().replace('\n', ''),
                            'บอลลูนแรกทดสอบ',
                        )
                        self.assertEqual(compare_reqs, [], f'Eye toggle triggered network fetch: {compare_reqs}')

                        # Toggle OFF -> instantly returns to Clean + Text + frames + Contextual Task Bar
                        eye_btn.click()
                        self.assertEqual(eye_btn.get_attribute('aria-pressed'), 'false')
                        self.assertFalse(tab.locator('#artOrig').is_visible())
                        self.assertNotIn('compare-mode', tab.locator('#overlay').get_attribute('class'))
                        self.assertTrue(tab.locator('.bubble.selected .handle').first.is_visible())
                        text_bar.wait_for(state='visible')
                        print(f'Canvas UX verified: switch avg={switch_stats["avgMs"]:.2f}ms, eye toggle={toggle_ms:.1f}ms', flush=True)

                        self.assertEqual(errors, [], f'Browser JS errors: {errors}')
                        ctx.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(10)


if __name__ == '__main__':
    unittest.main()
