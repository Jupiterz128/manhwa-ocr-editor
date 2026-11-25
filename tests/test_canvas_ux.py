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

        # 1. Non-passive wheel listener with Alt+Wheel zoom & Ctrl+Wheel mask brush + Spacebar Hand Tool
        self.assertIn("addEventListener('wheel'", js)
        self.assertIn('{passive:false}', js)
        self.assertIn('e.altKey', js)
        self.assertIn('e.preventDefault()', js)
        self.assertIn('e.stopPropagation()', js)
        self.assertIn('isColorPickContext', js)
        self.assertIn('Alt+Wheel', js)
        self.assertIn('Space+Drag', js)
        self.assertIn('Alt + ล้อเมาส์', html)
        self.assertIn('.handle{position:absolute;width:14px;height:14px;', css)
        self.assertIn('.rot-zone[data-rot=nw]{left:-38px;top:-38px}', css)
        self.assertIn('#viewport.hand,#viewport.hand *{cursor:grab!important}', css)
        # Text preview layer uses white-space:pre (POINTTEXT parity — never auto-wraps on zoom)
        self.assertIn('.bubble{position:absolute;pointer-events:auto;white-space:pre;', css)
        self.assertNotIn('white-space:pre-wrap', css)
        # 1:1 PSD parity: Auto Leading 120% (line-height:1.2), zero inner padding in Text view, ink metric compensation
        self.assertIn('line-height:1.2;', css)
        self.assertIn('.bubble.view-thai{padding:0;', css)
        self.assertIn('.bubble>.txt-fill,.bubble>.txt-stroke{grid-area:1/1;position:relative;width:max-content}', css)
        for token in ('measureTextInkOffset', 'applyTextPlacement', 'actualBoundingBoxAscent', 'actualBoundingBoxDescent'):
            self.assertIn(token, js)

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

                        # === TEST 1: Alt + Wheel Zoom-to-Cursor on tall 720x10000 image (and Alt in normal mode NEVER shows eyedropper!) ===
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
                        tab.keyboard.down('Alt')
                        tab.wait_for_timeout(40)
                        self.assertFalse(tab.locator('#eyedropLoupe').is_visible(), 'Alt in normal mode must NOT show eyedropper loupe')
                        self.assertNotIn('eyedropper', tab.locator('#viewport').get_attribute('class') or '')
                        for _ in range(4):
                            tab.mouse.wheel(0, -120)
                        tab.keyboard.up('Alt')
                        after_in = img_point_under_cursor()
                        self.assertGreater(after_in['z'], before_pt['z'] * 1.25)
                        self.assertLessEqual(abs(after_in['y'] - before_pt['y']), 2.0, f'Zoom-in drifted Y: {before_pt} -> {after_in}')

                        # Zoom further until wider than viewport (horizontal scrolling active) -> both X and Y stay pinned
                        tab.keyboard.down('Alt')
                        for _ in range(5):
                            tab.mouse.wheel(0, -120)
                        tab.keyboard.up('Alt')
                        wide_before = img_point_under_cursor()
                        tab.keyboard.down('Alt')
                        for _ in range(3):
                            tab.mouse.wheel(0, -120)
                        tab.keyboard.up('Alt')
                        wide_after = img_point_under_cursor()
                        self.assertGreater(wide_after['z'], wide_before['z'] * 1.2)
                        self.assertLessEqual(abs(wide_after['x'] - wide_before['x']), 2.0, f'Wide zoom-in drifted X: {wide_before} -> {wide_after}')
                        self.assertLessEqual(abs(wide_after['y'] - wide_before['y']), 2.0, f'Wide zoom-in drifted Y: {wide_before} -> {wide_after}')

                        # Zoom back out with Alt+Wheel and verify cursor Y stays pinned (no jump to top)
                        tab.keyboard.down('Alt')
                        for _ in range(14):
                            tab.mouse.wheel(0, 120)
                        tab.keyboard.up('Alt')
                        after_out = img_point_under_cursor()
                        self.assertLess(after_out['z'], before_pt['z'])
                        self.assertLessEqual(abs(after_out['y'] - before_pt['y']), 3.0, f'Zoom-out drifted Y: {before_pt} -> {after_out}')
                        self.assertGreater(after_out['scrollTop'], 2000)

                        # Test Fit shortcut '0' and Command Palette Ctrl+K (with updated Alt+Wheel & Space+Drag shortcuts)
                        tab.keyboard.press('0')
                        fit_pct = tab.locator('#percent').inner_text()
                        self.assertNotEqual(fit_pct, '100%')
                        tab.keyboard.press('Control+k')
                        tab.locator('#modal .pal-item').first.wait_for(state='visible')
                        pal_txt = tab.locator('#modal').inner_text()
                        self.assertIn('Fit', pal_txt)
                        self.assertIn('Alt+Wheel', pal_txt)
                        self.assertIn('Space+Drag', pal_txt)
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

                        # Verify positioned ~44px below selected bubble (clearing lower rotate zones) and inside viewport
                        bar_bb = text_bar.bounding_box()
                        sel_bb = tab.locator('.bubble.selected').bounding_box()
                        self.assertAlmostEqual(bar_bb['y'], sel_bb['y'] + sel_bb['height'] + 44, delta=3)
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
                        self.assertAlmostEqual(new_bar_bb['y'], new_sel_bb['y'] + new_sel_bb['height'] + 44, delta=3)

                        # === TEST 2B: Corner Handle vs Rotate Zone Safety Spacing in Move (V) Mode ===
                        geom_check = tab.evaluate(
                            """() => {
                              const b = document.querySelector('.bubble.selected');
                              const results = {};
                              for (const corner of ['nw', 'ne', 'se', 'sw']) {
                                const h = b.querySelector(`.handle[data-handle="${corner}"]`);
                                const r = b.querySelector(`.rot-zone[data-rot="${corner}"]`);
                                const hr = h.getBoundingClientRect();
                                const rr = r.getBoundingClientRect();
                                const hz = parseInt(getComputedStyle(h).zIndex, 10);
                                const rz = parseInt(getComputedStyle(r).zIndex, 10);
                                const overlapX = Math.min(hr.right, rr.right) - Math.max(hr.left, rr.left);
                                const overlapY = Math.min(hr.bottom, rr.bottom) - Math.max(hr.top, rr.top);
                                const gapX = corner.includes('w') ? hr.left - rr.right : rr.left - hr.right;
                                const gapY = corner.includes('n') ? hr.top - rr.bottom : rr.top - hr.bottom;
                                const hitHandle = document.elementFromPoint(hr.left + hr.width / 2, hr.top + hr.height / 2);
                                const moatX = corner.includes('w') ? hr.left - 6 : hr.right + 6;
                                const moatY = corner.includes('n') ? hr.top - 6 : hr.bottom + 6;
                                const hitMoat = document.elementFromPoint(moatX, moatY);
                                const hitRot = document.elementFromPoint(rr.left + rr.width / 2, rr.top + rr.height / 2);
                                results[corner] = {
                                  hw: hr.width, hh: hr.height,
                                  hz, rz,
                                  overlaps: overlapX > 0 && overlapY > 0,
                                  gapX, gapY,
                                  handleTarget: hitHandle === h,
                                  handleCursor: getComputedStyle(h).cursor,
                                  moatIsRot: !!hitMoat?.closest?.('.rot-zone'),
                                  rotTarget: hitRot === r,
                                  rotCursor: getComputedStyle(r).cursor,
                                };
                              }
                              return results;
                            }"""
                        )
                        for corner, info in geom_check.items():
                            self.assertGreaterEqual(info['hw'], 13.5, f'{corner} handle too small: {info}')
                            self.assertGreaterEqual(info['hh'], 13.5, f'{corner} handle too small: {info}')
                            self.assertGreater(info['hz'], info['rz'], f'{corner} handle z-index must exceed rot-zone: {info}')
                            self.assertFalse(info['overlaps'], f'{corner} rot-zone overlaps handle: {info}')
                            self.assertGreaterEqual(info['gapX'], 12.0, f'{corner} rot-zone X gap < 12px: {info}')
                            self.assertGreaterEqual(info['gapY'], 12.0, f'{corner} rot-zone Y gap < 12px: {info}')
                            self.assertTrue(info['handleTarget'], f'{corner} handle center not hit: {info}')
                            self.assertIn('resize', info['handleCursor'])
                            self.assertFalse(info['moatIsRot'], f'{corner} 6px safety moat must NOT trigger rotate: {info}')
                            self.assertTrue(info['rotTarget'], f'{corner} rot-zone center not hit: {info}')
                            self.assertIn('data:image/svg+xml', info['rotCursor'])

                        # === TEST 2C: Spacebar + Left-Click Drag (Hand Tool) in Move (V) mode at 60fps ===
                        tab.evaluate('document.getElementById("viewport").scrollTop = 80')
                        sel_before_pan = tab.locator('.bubble.selected').bounding_box()
                        sel_left_before = tab.locator('.bubble.selected').evaluate('e => e.style.left')
                        sel_top_before = tab.locator('.bubble.selected').evaluate('e => e.style.top')
                        px_pan = sel_before_pan['x'] + sel_before_pan['width'] / 2
                        py_pan = sel_before_pan['y'] + sel_before_pan['height'] / 2
                        tab.mouse.move(px_pan, py_pan)
                        tab.keyboard.down('Space')
                        tab.wait_for_timeout(30)
                        self.assertIn('hand', tab.locator('#viewport').get_attribute('class') or '')
                        self.assertEqual(tab.locator('.bubble.selected').evaluate('e => getComputedStyle(e).cursor'), 'grab')
                        self.assertEqual(tab.locator('.handle').first.evaluate('e => getComputedStyle(e).cursor'), 'grab')
                        tab.mouse.down()
                        self.assertIn('panning', tab.locator('#viewport').get_attribute('class') or '')
                        self.assertEqual(tab.locator('.bubble.selected').evaluate('e => getComputedStyle(e).cursor'), 'grabbing')
                        pan_fps = tab.evaluate(
                            """([sx, sy]) => new Promise(resolve => {
                              const vp = document.getElementById('viewport');
                              let first = 0, count = 0;
                              function step(t) {
                                first ||= t;
                                vp.dispatchEvent(new PointerEvent('pointermove', {
                                  clientX: sx, clientY: sy - count * 4, buttons: 1, bubbles: true
                                }));
                                if (++count === 61) resolve(60000 / (t - first));
                                else requestAnimationFrame(step);
                              }
                              requestAnimationFrame(step);
                            })""",
                            [px_pan, py_pan],
                        )
                        tab.mouse.up()
                        tab.keyboard.up('Space')
                        self.assertNotIn('hand', tab.locator('#viewport').get_attribute('class') or '')
                        self.assertGreater(tab.evaluate('document.getElementById("viewport").scrollTop'), 260)
                        # Bubble itself was NOT dragged while panning with Spacebar!
                        self.assertEqual(tab.locator('.bubble.selected').evaluate('e => e.style.left'), sel_left_before)
                        self.assertEqual(tab.locator('.bubble.selected').evaluate('e => e.style.top'), sel_top_before)

                        # Verify Spacebar inside <textarea> still types space normally without triggering Hand Tool
                        tab.locator('#thai').click()
                        thai_before = tab.locator('#thai').input_value()
                        tab.keyboard.press('Space')
                        self.assertNotIn('hand', tab.locator('#viewport').get_attribute('class') or '')
                        self.assertEqual(len(tab.locator('#thai').input_value()), len(thai_before) + 1)
                        tab.locator('#thai').fill(thai_before)
                        tab.locator('#thai').blur()
                        tab.evaluate('document.getElementById("viewport").scrollTop = 0')
                        tab.wait_for_timeout(40)

                        # Verify Mask Edit (M) hides Text Bar, Ctrl+Wheel adjusts brush radius, Alt+Wheel zooms, Space+Drag pans, and Esc restores Text Bar!
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
                        rad1 = int(tab.locator('#radius').input_value())
                        self.assertGreater(rad1, rad0)
                        self.assertEqual(tab.locator('#percent').inner_text(), z_mask_before)
                        # Alt+Wheel in Mask Edit (M) zooms without altering brush radius!
                        tab.keyboard.down('Alt')
                        tab.mouse.wheel(0, -120)
                        tab.mouse.wheel(0, -120)
                        tab.keyboard.up('Alt')
                        self.assertEqual(int(tab.locator('#radius').input_value()), rad1)
                        self.assertNotEqual(tab.locator('#percent').inner_text(), z_mask_before)
                        # Space+Drag in Mask Edit (M) pans smoothly without painting!
                        scroll_m0 = tab.evaluate('document.getElementById("viewport").scrollTop')
                        tab.mouse.move(vp_box['x'] + 320, vp_box['y'] + 340)
                        tab.keyboard.down('Space')
                        self.assertIn('hand', tab.locator('#viewport').get_attribute('class') or '')
                        tab.mouse.down()
                        tab.mouse.move(vp_box['x'] + 320, vp_box['y'] + 200, steps=5)
                        tab.mouse.up()
                        tab.keyboard.up('Space')
                        self.assertGreater(tab.evaluate('document.getElementById("viewport").scrollTop'), scroll_m0 + 60)
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
                        print(f'Canvas UX verified: switch avg={switch_stats["avgMs"]:.2f}ms, pan={pan_fps:.1f}fps, eye toggle={toggle_ms:.1f}ms', flush=True)

                        self.assertEqual(errors, [], f'Browser JS errors: {errors}')
                        ctx.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(10)

    def test_text_line_wrapping_invariant_across_zoom_levels_30_to_180(self):
        import uvicorn
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'studio'):
            im = Image.new('RGB', (900, 1400), '#f8fafc')
            dr = ImageDraw.Draw(im)
            specs = [
                ([60, 80, 340, 260], 'NARROW', 'การกลับมาของบรรพชนรุ่นแรกในรอบหนึ่งพันปี'),
                ([420, 80, 840, 280], 'WIDE', 'เจ้าคิดจริงๆ หรือว่าจะหนีรอดเงื้อมมือของข้าไปได้ง่ายๆ แบบนี้!'),
                ([120, 380, 400, 620], 'TALL', 'อย่าประมาทศัตรูที่อยู่ตรงหน้าเป็นอันขาด เพราะพลังของมันยังไม่ตื่นขึ้นเต็มที่'),
                ([460, 420, 740, 560], 'SHORT', 'ไม่มีทาง!'),
            ]
            bubbles = []
            for idx, (box, src, th) in enumerate(specs):
                dr.rounded_rectangle(box, radius=20, fill='white', outline='#111111', width=4)
                b = Bubble(box=box, text=src, translation=th, score=0.98)
                b.text_layer = layout_text(im, b)
                if idx == 0:
                    b.text_layer['stroke'] = {
                        'enabled': True, 'size': 4, 'position': 'outside',
                        'blend_mode': 'Normal', 'opacity': 100, 'color': [255, 255, 255],
                    }
                bubbles.append(b)
            path = Path(tmp) / 'zoom_wrap.png'
            im.save(path)
            key = 'zoomwrap'
            studio.save(key, 'zoom_wrap.png', [Page(str(path), bubbles)], {})
            expected_lines = {b.id: len((b.text_layer.get('text') or b.translation).split('\n')) for b in bubbles}

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
                        # Ensure Text view is active
                        tab.locator('#views button', has_text='Text').click()
                        tab.wait_for_timeout(80)

                        def set_zoom_to(target_pct):
                            # Use Alt+Wheel steps or exact zoom buttons, or directly invoke fit/zoom steps
                            tab.evaluate(
                                """(pct) => {
                                  // Trigger wheel zoom or exact zoom via UI zoom buttons until close, then inspect
                                  const vp = document.getElementById('viewport');
                                  const st = document.getElementById('stage');
                                  const getPct = () => parseInt(document.getElementById('percent').textContent, 10);
                                  let guard = 0;
                                  while (Math.abs(getPct() - pct) > 4 && guard++ < 60) {
                                    const dir = getPct() < pct ? -120 : 120;
                                    const rect = vp.getBoundingClientRect();
                                    vp.dispatchEvent(new WheelEvent('wheel', {
                                      deltaY: dir, altKey: true, cancelable: true, bubbles: true,
                                      clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2,
                                    }));
                                  }
                                }""",
                                target_pct,
                            )

                        def measure_bubbles():
                            return tab.evaluate(
                                """() => {
                                  const out = {};
                                  for (const b of document.querySelectorAll('#overlay .bubble')) {
                                    const fill = b.querySelector('.txt-fill');
                                    const stroke = b.querySelector('.txt-stroke');
                                    const range = document.createRange();
                                    const lines = [];
                                    for (const node of fill.childNodes) {
                                      if (node.nodeType === Node.TEXT_NODE) {
                                        range.selectNodeContents(node);
                                        for (const r of range.getClientRects()) {
                                          if (r.width <= 0.1 || r.height <= 0.1) continue;
                                          const cy = r.top + r.height / 2;
                                          if (!lines.some(y => Math.abs(y - cy) < Math.max(2, r.height * 0.45))) {
                                            lines.push(cy);
                                          }
                                        }
                                      }
                                    }
                                    const fr = fill.getBoundingClientRect();
                                    const sr = stroke.getBoundingClientRect();
                                    out[b.dataset.id] = {
                                      whiteSpace: getComputedStyle(b).whiteSpace,
                                      visualLines: lines.length,
                                      explicitLines: fill.textContent.split('\\n').length,
                                      strokeHidden: stroke.hidden,
                                      boxDelta: stroke.hidden ? 0 : Math.max(
                                        Math.abs(fr.left - sr.left),
                                        Math.abs(fr.top - sr.top),
                                        Math.abs(fr.width - sr.width),
                                        Math.abs(fr.height - sr.height)
                                      ),
                                    };
                                  }
                                  return {
                                    pct: parseInt(document.getElementById('percent').textContent, 10),
                                    bubbles: out,
                                  };
                                }"""
                            )

                        for target_pct in (30, 45, 65, 85, 100, 140, 180):
                            set_zoom_to(target_pct)
                            tab.wait_for_timeout(30)
                            snap = measure_bubbles()
                            self.assertLessEqual(abs(snap['pct'] - target_pct), 6, f'Failed to reach {target_pct}%: got {snap["pct"]}%')
                            for bid, exp_n in expected_lines.items():
                                info_b = snap['bubbles'][bid]
                                self.assertEqual(info_b['whiteSpace'], 'pre', f'At {snap["pct"]}%: {info_b}')
                                self.assertEqual(info_b['explicitLines'], exp_n)
                                self.assertEqual(
                                    info_b['visualLines'], exp_n,
                                    f'Line wrap changed at {snap["pct"]}% for {bid}: visual={info_b["visualLines"]} != expected={exp_n}',
                                )
                                self.assertLess(info_b['boxDelta'], 0.5, f'Fill/stroke mismatch at {snap["pct"]}%: {info_b}')

                        self.assertEqual(errors, [], f'Browser JS errors: {errors}')
                        ctx.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(10)

    def test_web_preview_matches_composite_and_photopea_psd_within_3px(self):
        import io
        import numpy as np
        import uvicorn
        from playwright.sync_api import sync_playwright
        from psd_tools import PSDImage
        from photopea_export import PhotopeaSession
        from photoshop_export import prepare_job
        from text_layout import composite_text_crop

        def dark_ink_center(img: Image.Image, roi):
            arr = np.asarray(img.convert('RGB'))
            x0 = max(0, int(round(roi[0])))
            y0 = max(0, int(round(roi[1])))
            x1 = min(arr.shape[1], int(round(roi[2])))
            y1 = min(arr.shape[0], int(round(roi[3])))
            crop = arr[y0:y1, x0:x1]
            mask = (crop[:, :, 0] < 80) & (crop[:, :, 1] < 80) & (crop[:, :, 2] < 80)
            ys, xs = np.nonzero(mask)
            self.assertGreater(len(xs), 10, f'No dark text ink found in ROI {roi}')
            bx0, bx1 = int(xs.min()) + x0, int(xs.max()) + x0 + 1
            by0, by1 = int(ys.min()) + y0, int(ys.max()) + y0 + 1
            return ((bx0 + bx1) / 2.0, (by0 + by1) / 2.0)

        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, 'ROOT', Path(tmp) / 'studio'):
            root = Path(tmp)
            im = Image.new('RGB', (960, 1280), '#ffffff')
            specs = [
                # (bubble_box, src, th, extra)
                ([60, 60, 360, 260], 'SINGLE', 'สวัสดีครับ', {}),
                ([460, 60, 860, 280], 'MULTI', 'การกลับมาของบรรพชนรุ่นแรกในรอบพันปี', {}),
                ([80, 380, 420, 620], 'DESC', 'จุดสูงสุดของภูเขาฤดูหนาว', {}),
                # Offset editor_box vs text_layer.box (simulates big transform box not sharing center with text_layer.box)
                ([480, 380, 860, 640], 'OFFSET', 'พี่น้องเอ๋ย! นี่คือพื้นที่ต้องห้ามที่สุด', {'offset_editor': (24, -18)}),
                # Right-aligned multi-line
                ([80, 740, 440, 980], 'RIGHT', 'เจ้าคิดจริงๆ หรือว่าจะหนีรอดไปได้', {'alignment': 'right'}),
                # Rotated Thai text
                ([500, 740, 860, 980], 'ROT', 'หยุดอยู่ตรงนั้นเดี๋ยวนี้!', {'rotation': -12}),
            ]
            bubbles = []
            for box, src, th, extra in specs:
                b = Bubble(box=box, text=src, translation=th, score=0.99)
                if 'rotation' in extra:
                    b.custom_rotation = extra['rotation']
                b.text_layer = layout_text(im, b)
                b.text_layer['color'] = [0, 0, 0]
                b.text_layer['stroke'] = {
                    'enabled': False, 'size': 3, 'position': 'outside',
                    'blend_mode': 'Normal', 'opacity': 100, 'color': [255, 255, 255],
                }
                b.text_layer['gradient'] = {'enabled': False}
                if 'alignment' in extra:
                    b.text_layer['alignment'] = extra['alignment']
                if 'offset_editor' in extra:
                    odx, ody = extra['offset_editor']
                    lb = b.text_layer['box']
                    b.text_layer['editor_box'] = [
                        lb[0] - 40 + odx, lb[1] - 35 + ody,
                        lb[2] + 40 + odx, lb[3] + 35 + ody,
                    ]
                bubbles.append(b)

            img_path = root / 'align_page.png'
            im.save(img_path)
            page_obj = Page(str(img_path), bubbles)

            # 1) Windows Uniscribe composite_text_crop
            comp_img = composite_text_crop(im.copy(), bubbles, (0, 0))

            # 2) Photopea headless PSD export
            job_dir = root / 'pp_job'
            job = prepare_job(job_dir, im, page_obj, include_text=True)
            psd_path = root / 'aligned.psd'
            job['output'] = str(psd_path)
            sess = PhotopeaSession()
            try:
                sess.export_job(job)
            finally:
                sess.close()
            psd_img = PSDImage.open(psd_path).composite()

            # 3) Live Web Preview screenshot in Chromium at 100% and 65% zoom
            key = 'align1to1'
            studio.save(key, 'align_page.png', [page_obj], {})
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
                        ctx = browser.new_context(viewport={'width': 1600, 'height': 1400}, device_scale_factor=1)
                        tab = ctx.new_page()
                        errors = []
                        tab.on('pageerror', lambda err: errors.append(str(err)))
                        tab.add_init_script(f"localStorage.setItem('studio-project','{key}')")
                        tab.goto(f'http://127.0.0.1:{sock.getsockname()[1]}')
                        tab.locator('#bid').wait_for()
                        tab.locator('#views button', has_text='Text').click()
                        tab.locator('#percent').click()  # 100% zoom
                        tab.evaluate('document.fonts.ready')
                        tab.wait_for_timeout(250)
                        # Compare mode hides overlay frames/handles so only pure Thai ink on white is captured
                        tab.locator('#compareBtn').click()
                        tab.wait_for_timeout(120)

                        web_100 = Image.open(io.BytesIO(tab.locator('#stage').screenshot())).convert('RGB')

                        # Also check at ~65% zoom to verify metric offset scales 1:1 with zoom
                        tab.evaluate(
                            """() => {
                              const vp = document.getElementById('viewport');
                              const rect = vp.getBoundingClientRect();
                              let guard = 0;
                              while (parseInt(document.getElementById('percent').textContent, 10) > 68 && guard++ < 40) {
                                vp.dispatchEvent(new WheelEvent('wheel', {
                                  deltaY: 120, altKey: true, cancelable: true, bubbles: true,
                                  clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2,
                                }));
                              }
                            }"""
                        )
                        tab.wait_for_timeout(120)
                        web_zoomed = Image.open(io.BytesIO(tab.locator('#stage').screenshot())).convert('RGB')
                        zoom_scale = web_zoomed.width / float(im.width)

                        self.assertEqual(errors, [], f'Browser JS errors: {errors}')
                        ctx.close()
                        browser.close()
                finally:
                    server.should_exit = True
                    thread.join(10)

            max_dx_psd = max_dy_psd = max_dx_comp = max_dy_comp = max_dy_zoom = 0.0
            for idx, b in enumerate(bubbles):
                lbox = b.text_layer['box']
                lcx = (lbox[0] + lbox[2]) / 2.0
                lcy = (lbox[1] + lbox[3]) / 2.0
                roi = [lbox[0] - 50, lbox[1] - 50, lbox[2] + 50, lbox[3] + 50]
                w_cx, w_cy = dark_ink_center(web_100, roi)
                p_cx, p_cy = dark_ink_center(psd_img, roi)
                c_cx, c_cy = dark_ink_center(comp_img, roi)
                z_roi = [v * zoom_scale for v in roi]
                zw_cx, zw_cy = dark_ink_center(web_zoomed, z_roi)
                zw_cx /= zoom_scale
                zw_cy /= zoom_scale

                dx_psd, dy_psd = abs(w_cx - p_cx), abs(w_cy - p_cy)
                dx_comp, dy_comp = abs(w_cx - c_cx), abs(w_cy - c_cy)
                dx_zoom, dy_zoom = abs(zw_cx - p_cx), abs(zw_cy - p_cy)
                max_dx_psd = max(max_dx_psd, dx_psd)
                max_dy_psd = max(max_dy_psd, dy_psd)
                max_dx_comp = max(max_dx_comp, dx_comp)
                max_dy_comp = max(max_dy_comp, dy_comp)
                max_dy_zoom = max(max_dy_zoom, dy_zoom)

                self.assertLessEqual(dx_psd, 3.0, f'Bubble {idx} ({b.text}) Web vs PSD dx={w_cx-p_cx:+.2f}px')
                self.assertLessEqual(dy_psd, 3.0, f'Bubble {idx} ({b.text}) Web vs PSD dy={w_cy-p_cy:+.2f}px')
                self.assertLessEqual(dx_comp, 3.0, f'Bubble {idx} ({b.text}) Web vs composite dx={w_cx-c_cx:+.2f}px')
                self.assertLessEqual(dy_comp, 3.0, f'Bubble {idx} ({b.text}) Web vs composite dy={w_cy-c_cy:+.2f}px')
                self.assertLessEqual(abs(w_cx - lcx), 3.0, f'Bubble {idx} ({b.text}) Web vs layer.box dx={w_cx-lcx:+.2f}px')
                self.assertLessEqual(abs(w_cy - lcy), 3.0, f'Bubble {idx} ({b.text}) Web vs layer.box dy={w_cy-lcy:+.2f}px')
                self.assertLessEqual(dx_zoom, 3.0, f'Bubble {idx} ({b.text}) zoomed Web vs PSD dx={zw_cx-p_cx:+.2f}px')
                self.assertLessEqual(dy_zoom, 3.0, f'Bubble {idx} ({b.text}) zoomed Web vs PSD dy={zw_cy-p_cy:+.2f}px')

            print(
                f'1:1 alignment verified across 6 bubbles: max|Web-PSD|=({max_dx_psd:.2f},{max_dy_psd:.2f})px, '
                f'max|Web-Comp|=({max_dx_comp:.2f},{max_dy_comp:.2f})px, zoomed max|dy|={max_dy_zoom:.2f}px',
                flush=True,
            )


if __name__ == '__main__':
    unittest.main()
