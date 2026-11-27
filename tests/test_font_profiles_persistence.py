import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import text_layout as tl
import web_api as studio
from manhwa_ocr import Bubble, Page, write_mtp


ALL_FRAME_KEYS = (
    'normal',
    'shout',
    'thought',
    'system',
    'narration',
    'floating',
    'happy',
    'phone',
    'terrible',
)


class FontProfilePersistenceUnitTests(unittest.TestCase):
    def test_frontend_font_profile_contracts(self):
        js = (Path(__file__).resolve().parent.parent / 'web' / 'studio.js').read_text(encoding='utf-8')
        for token in (
            'function cloneProfileFonts(',
            'function cloneProfileRecord(',
            'function clonePresetsMap(',
            'function resolveFontOption(',
            'function fillFontSelect(',
            'function setFontSelectValue(',
            'tempOpt.dataset.preservedFont=',
            'await loadPresets(true)',
            'availableFonts',
        ):
            self.assertIn(token, js, f'Missing expected token in web/studio.js: {token}')

    def test_atomic_write_and_exact_9_types_roundtrip_and_isolation(self):
        profile_1 = {
            'normal': 'Itim',
            'shout': 'EDMuzazhi',
            'thought': 'TF MANIWIKA',
            'system': 'TH Baijam Bold',
            'narration': 'TF LANNA',
            'floating': 'JS Toomtam',
            'happy': 'Layiji JaRaKeFadHang',
            'phone': 'IrisUPC Bold',
            'terrible': 'iannnnn-TIGER',
        }
        profile_2 = {
            'normal': 'JS Toomtam',
            'shout': 'Layiji TaMaiTine1',
            'thought': 'MN KRASIP',
            'system': 'TF PHETAI',
            'narration': 'TF Phethai Italic',
            'floating': 'TH Baijam Bold',
            'happy': 'Itim',
            'phone': 'EDMuzazhi',
            'terrible': 'TF MANIWIKA',
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'font_presets.json'
            with patch.object(tl, 'presets_config_path', return_value=cfg):
                default_snapshot = {k: dict(v) for k, v in tl.DEFAULT_FONT_PRESETS.items()}
                tl.save_font_presets({
                    '1. มังฮวาเกาหลี': profile_1,
                    '2. การ์ตูนต่อสู้': profile_2,
                })
                self.assertTrue(cfg.is_file())
                self.assertEqual(list(Path(tmp).glob('*.tmp')), [])

                loaded = tl.load_font_presets()
                self.assertEqual(loaded['1. มังฮวาเกาหลี'], profile_1)
                self.assertEqual(loaded['2. การ์ตูนต่อสู้'], profile_2)

                # Mutating returned dict must never affect subsequent loads or DEFAULT_FONT_PRESETS
                loaded['1. มังฮวาเกาหลี']['normal'] = 'FC Muffin'
                loaded['2. การ์ตูนต่อสู้']['shout'] = 'FC Muffin'
                reloaded = tl.load_font_presets()
                self.assertEqual(reloaded['1. มังฮวาเกาหลี'], profile_1)
                self.assertEqual(reloaded['2. การ์ตูนต่อสู้'], profile_2)
                self.assertEqual(tl.DEFAULT_FONT_PRESETS, default_snapshot)

    def test_atomic_write_preserves_existing_file_on_replace_failure(self):
        original_map = {k: 'Itim' for k in ALL_FRAME_KEYS}
        new_map = {k: 'EDMuzazhi' for k in ALL_FRAME_KEYS}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'font_presets.json'
            with patch.object(tl, 'presets_config_path', return_value=cfg):
                tl.save_font_presets({'1. มังฮวาเกาหลี': original_map})
                with patch('text_layout.os.replace', side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):
                        tl.save_font_presets({'1. มังฮวาเกาหลี': new_map})
                # Temp file cleaned up and original file completely intact
                self.assertEqual(list(Path(tmp).glob('*.tmp')), [])
                self.assertEqual(tl.load_font_presets()['1. มังฮวาเกาหลี'], original_map)

    def test_case_insensitive_trim_alias_bom_and_unlisted_fonts_preserved(self):
        raw = {
            '1. มังฮวาเกาหลี': {
                'normal': '  itim  ',
                'shout': 'fc muffin',
                'thought': 'TF Phethai',
                'system': 'TH Sarabun New',
                'narration': '  js toomtam ',
                'floating': 'MN Krasip Regular',
                'happy': 'CustomUnlistedHappy',
                'phone': '  FC Lamoon Bold  ',
                'terrible': 'tf phethai italic',
            },
            '2. การ์ตูนต่อสู้': {
                'normal': 'TF LANNA',
                'shout': 'EDMuzazhi',
                'thought': 'TF MANIWIKA',
                'system': 'IrisUPC Bold',
                'narration': 'TH Baijam Bold',
                'floating': 'iannnnn-TIGER',
                'happy': 'Layiji JaRaKeFadHang',
                'phone': 'Layiji TaMaiTine1',
                'terrible': 'MN KRASIP',
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'font_presets.json'
            # Write with UTF-8 BOM to verify BOM tolerance
            cfg.write_text('\ufeff' + json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
            with patch.object(tl, 'presets_config_path', return_value=cfg):
                loaded = tl.load_font_presets()
                p1 = loaded['1. มังฮวาเกาหลี']
                self.assertEqual(p1['normal'], 'Itim')
                self.assertEqual(p1['shout'], 'FC Muffin')
                self.assertEqual(p1['thought'], 'TF PHETAI')
                self.assertEqual(p1['system'], 'TH Sarabun New Bold')
                self.assertEqual(p1['narration'], 'JS Toomtam')
                self.assertEqual(p1['floating'], 'MN KRASIP')
                self.assertEqual(p1['happy'], 'CustomUnlistedHappy')
                self.assertEqual(p1['phone'], 'FC Lamoon Bold')
                self.assertEqual(p1['terrible'], 'TF Phethai Italic')

                # Saving profile 1 via API with unlisted font & missing key must preserve unlisted font and not touch profile 2
                res = studio.update_font_presets({
                    'name': '1. มังฮวาเกาหลี',
                    'mapping': {
                        **p1,
                        'happy': 'CustomUnlistedHappy',
                        'phone': 'FC Lamoon Bold',
                        'terrible': '',  # empty falls back to existing profile value, not FC Muffin
                    },
                })
                p1_after = res['presets']['1. มังฮวาเกาหลี']
                self.assertEqual(p1_after['happy'], 'CustomUnlistedHappy')
                self.assertEqual(p1_after['phone'], 'FC Lamoon Bold')
                self.assertEqual(p1_after['terrible'], 'TF Phethai Italic')
                self.assertEqual(res['presets']['2. การ์ตูนต่อสู้'], raw['2. การ์ตูนต่อสู้'])

    def test_opening_mtp_or_new_project_never_overwrites_saved_profiles(self):
        p1_custom = {
            'normal': 'Itim',
            'shout': 'EDMuzazhi',
            'thought': 'TF MANIWIKA',
            'system': 'TH Baijam Bold',
            'narration': 'TF LANNA',
            'floating': 'JS Toomtam',
            'happy': 'Layiji JaRaKeFadHang',
            'phone': 'IrisUPC Bold',
            'terrible': 'iannnnn-TIGER',
        }
        p2_custom = {
            'normal': 'JS Toomtam',
            'shout': 'Layiji TaMaiTine1',
            'thought': 'MN KRASIP',
            'system': 'TF PHETAI',
            'narration': 'TF Phethai Italic',
            'floating': 'TH Baijam Bold',
            'happy': 'Itim',
            'phone': 'EDMuzazhi',
            'terrible': 'TF MANIWIKA',
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / 'font_presets.json'
            with patch.object(studio, 'ROOT', root / 'store'), \
                 patch.object(tl, 'presets_config_path', return_value=cfg):
                tl.save_font_presets({
                    '1. มังฮวาเกาหลี': p1_custom,
                    '2. การ์ตูนต่อสู้': p2_custom,
                })
                img_path = root / 'page1.png'
                Image.new('RGB', (400, 300), 'white').save(img_path)
                mtp_path = root / 'chapter.mtp'
                b = Bubble(box=[40, 40, 200, 120], text='HI', translation='สวัสดี', frame_type='normal')
                write_mtp(mtp_path, [Page(str(img_path), [b])], {'normal': 'FC Muffin'}, threading.Event(), lambda _: None)

                client = TestClient(studio.app)
                hdr = {'x-studio': 'editor'}
                with mtp_path.open('rb') as fh:
                    up_mtp = client.post('/api/projects', files=[('files', ('chapter.mtp', fh, 'application/octet-stream'))], headers=hdr)
                self.assertEqual(up_mtp.status_code, 200, up_mtp.text)
                with img_path.open('rb') as fh:
                    up_png = client.post('/api/projects', files=[('files', ('page1.png', fh, 'image/png'))], headers=hdr)
                self.assertEqual(up_png.status_code, 200, up_png.text)

                # Switch project preset via API
                studio.set_project_font_preset(up_png.json(), {'preset': '2. การ์ตูนต่อสู้'})
                studio.set_project_font_preset(up_png.json(), {'preset': '1. มังฮวาเกาหลี'})

                after = tl.load_font_presets()
                self.assertEqual(after['1. มังฮวาเกาหลี'], p1_custom)
                self.assertEqual(after['2. การ์ตูนต่อสู้'], p2_custom)


@unittest.skipUnless(os.environ.get('TEST_STUDIO_BROWSER') == '1', 'opt-in Chromium browser test')
class FontProfileBrowserTests(unittest.TestCase):
    def _read_modal_selects(self, tab):
        return tab.evaluate(
            """() => {
              const out = {};
              for (const s of document.querySelectorAll('#modal .preset-grid select[data-ftype]')) {
                out[s.dataset.ftype] = s.value;
              }
              return out;
            }"""
        )

    def _set_modal_selects(self, tab, mapping):
        for ftype, font_name in mapping.items():
            tab.locator(f'#modal .preset-grid select[data-ftype="{ftype}"]').select_option(font_name)

    def test_browser_two_profiles_9_fonts_switch_modal_reload_and_mtp(self):
        import uvicorn
        from playwright.sync_api import sync_playwright

        profile_1 = {
            'normal': 'Itim',
            'shout': 'EDMuzazhi',
            'thought': 'TF MANIWIKA',
            'system': 'TH Baijam Bold',
            'narration': 'TF LANNA',
            'floating': 'JS Toomtam',
            'happy': 'Layiji JaRaKeFadHang',
            'phone': 'IrisUPC Bold',
            'terrible': 'iannnnn-TIGER',
        }
        profile_2 = {
            'normal': 'JS Toomtam',
            'shout': 'Layiji TaMaiTine1',
            'thought': 'MN KRASIP',
            'system': 'TF PHETAI',
            'narration': 'TF Phethai Italic',
            'floating': 'TH Baijam Bold',
            'happy': 'Itim',
            'phone': 'EDMuzazhi',
            'terrible': 'TF MANIWIKA',
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / 'font_presets.json'
            img_path = root / '01.png'
            Image.new('RGB', (480, 360), 'white').save(img_path)
            mtp_path = root / 'sample.mtp'
            b = Bubble(box=[50, 50, 240, 140], text='HELLO', translation='สวัสดี', frame_type='normal')
            write_mtp(mtp_path, [Page(str(img_path), [b])], {'normal': 'FC Muffin'}, threading.Event(), lambda _: None)

            with patch.object(studio, 'ROOT', root / 'store'), \
                 patch.object(tl, 'presets_config_path', return_value=cfg):
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
                        url = f'http://127.0.0.1:{sock.getsockname()[1]}'
                        with sync_playwright() as pw:
                            browser = pw.chromium.launch()
                            context = browser.new_context(viewport={'width': 1440, 'height': 900})
                            tab = context.new_page()
                            errors = []
                            tab.on('pageerror', lambda err: errors.append(str(err)))
                            tab.goto(url)
                            tab.wait_for_function("() => document.querySelectorAll('#presetSelect option').length >= 2")

                            # 1) Edit all 9 slots in '1. มังฮวาเกาหลี' and save
                            tab.locator('#presetSelect').select_option('1. มังฮวาเกาหลี')
                            tab.locator('[data-cmd=editPreset]').click()
                            tab.locator('#modal .preset-grid select[data-ftype="terrible"]').wait_for(state='visible')
                            self._set_modal_selects(tab, profile_1)
                            tab.locator('#savePresetBtn').click()
                            tab.locator('#dialog').wait_for(state='hidden')

                            # 2) Switch to '2. การ์ตูนต่อสู้', edit all 9 slots, and save
                            tab.locator('#presetSelect').select_option('2. การ์ตูนต่อสู้')
                            tab.locator('[data-cmd=editPreset]').click()
                            tab.locator('#modal .preset-grid select[data-ftype="terrible"]').wait_for(state='visible')
                            self._set_modal_selects(tab, profile_2)
                            tab.locator('#savePresetBtn').click()
                            tab.locator('#dialog').wait_for(state='hidden')

                            # 3) Switch back and forth, open/close modal, verify all 9 slots on both profiles
                            for _ in range(2):
                                tab.locator('#presetSelect').select_option('1. มังฮวาเกาหลี')
                                tab.locator('[data-cmd=editPreset]').click()
                                tab.locator('#modal .preset-grid select[data-ftype="terrible"]').wait_for(state='visible')
                                self.assertEqual(self._read_modal_selects(tab), profile_1)
                                tab.locator('#close').click()
                                tab.locator('#dialog').wait_for(state='hidden')

                                tab.locator('#presetSelect').select_option('2. การ์ตูนต่อสู้')
                                tab.locator('[data-cmd=editPreset]').click()
                                tab.locator('#modal .preset-grid select[data-ftype="terrible"]').wait_for(state='visible')
                                self.assertEqual(self._read_modal_selects(tab), profile_2)
                                tab.locator('#close').click()
                                tab.locator('#dialog').wait_for(state='hidden')

                            # 4) Import a .mtp file -> must NOT overwrite either profile
                            tab.locator('#files').set_input_files(mtp_path)
                            tab.locator('#bid').wait_for(state='visible')

                            # 5) Full page reload -> both profiles still keep exact 9 fonts
                            tab.reload()
                            tab.wait_for_function("() => document.querySelectorAll('#presetSelect option').length >= 2")

                            tab.locator('#presetSelect').select_option('1. มังฮวาเกาหลี')
                            tab.locator('[data-cmd=editPreset]').click()
                            tab.locator('#modal .preset-grid select[data-ftype="terrible"]').wait_for(state='visible')
                            self.assertEqual(self._read_modal_selects(tab), profile_1)
                            tab.locator('#close').click()
                            tab.locator('#dialog').wait_for(state='hidden')

                            tab.locator('#presetSelect').select_option('2. การ์ตูนต่อสู้')
                            tab.locator('[data-cmd=editPreset]').click()
                            tab.locator('#modal .preset-grid select[data-ftype="terrible"]').wait_for(state='visible')
                            self.assertEqual(self._read_modal_selects(tab), profile_2)
                            tab.locator('#close').click()
                            tab.locator('#dialog').wait_for(state='hidden')

                            self.assertEqual(errors, [])
                            context.close()
                            browser.close()
                    finally:
                        server.should_exit = True
                        thread.join(10)

    def test_browser_case_mismatch_and_unlisted_font_never_snaps_to_first_option(self):
        import uvicorn
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / 'font_presets.json'
            with patch.object(studio, 'ROOT', root / 'store'), \
                 patch.object(tl, 'presets_config_path', return_value=cfg):
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
                        url = f'http://127.0.0.1:{sock.getsockname()[1]}'
                        with sync_playwright() as pw:
                            browser = pw.chromium.launch()
                            context = browser.new_context(viewport={'width': 1440, 'height': 900})
                            tab = context.new_page()
                            errors = []
                            tab.on('pageerror', lambda err: errors.append(str(err)))

                            # Put 'FC Muffin' FIRST in the dropdown list and return case-mismatched,
                            # alias, and unlisted font names to prove ZERO slots snap to FC Muffin.
                            fc_first_fonts = ['FC Muffin'] + [f for f in tl.FONT_CHOICES if f != 'FC Muffin']
                            mocked_presets = {
                                '1. มังฮวาเกาหลี': {
                                    'normal': '  itim  ',
                                    'shout': 'edmuzazhi',
                                    'thought': 'TF Phethai',
                                    'system': 'TH Sarabun New',
                                    'narration': '  js toomtam  ',
                                    'floating': 'MN Krasip Regular',
                                    'happy': 'UnlistedMangaFont',
                                    'phone': '  CustomChatFont Pro  ',
                                    'terrible': 'tf phethai italic',
                                },
                                '2. การ์ตูนต่อสู้': dict(tl.DEFAULT_FONT_PRESETS['2. การ์ตูนต่อสู้']),
                            }
                            intercept_get = {'enabled': True}

                            def handle_presets_route(route):
                                if route.request.method == 'GET' and intercept_get['enabled']:
                                    route.fulfill(
                                        status=200,
                                        content_type='application/json',
                                        body=json.dumps({
                                            'presets': mocked_presets,
                                            'fonts': fc_first_fonts,
                                            'frame_types': [{'key': k, 'label': v} for k, v in tl.FRAME_TYPES],
                                        }, ensure_ascii=False),
                                    )
                                else:
                                    route.continue_()

                            tab.route('**/api/font-presets', handle_presets_route)
                            tab.goto(url)
                            tab.wait_for_function("() => document.querySelectorAll('#presetSelect option').length >= 2")

                            tab.locator('#presetSelect').select_option('1. มังฮวาเกาหลี')
                            tab.locator('[data-cmd=editPreset]').click()
                            tab.locator('#modal .preset-grid select[data-ftype="terrible"]').wait_for(state='visible')

                            # Confirm first option in dropdown really is 'FC Muffin'
                            first_opt = tab.evaluate("() => document.querySelector('#modal .preset-grid select[data-ftype=\"normal\"]').options[0].value")
                            self.assertEqual(first_opt, 'FC Muffin')

                            expected = {
                                'normal': 'Itim',
                                'shout': 'EDMuzazhi',
                                'thought': 'TF PHETAI',
                                'system': 'TH Sarabun New Bold',
                                'narration': 'JS Toomtam',
                                'floating': 'MN KRASIP',
                                'happy': 'UnlistedMangaFont',
                                'phone': 'CustomChatFont Pro',
                                'terrible': 'TF Phethai Italic',
                            }
                            actual = self._read_modal_selects(tab)
                            self.assertEqual(actual, expected)
                            self.assertNotIn('FC Muffin', actual.values())

                            # Unlisted fonts were injected as temporary preserved options
                            preserved_count = tab.locator('#modal .preset-grid option[data-preserved-font="1"]').count()
                            self.assertEqual(preserved_count, 2)

                            # Save modal -> backend must persist exact values including unlisted fonts
                            tab.locator('#savePresetBtn').click()
                            tab.locator('#dialog').wait_for(state='hidden')

                            saved_on_disk = tl.load_font_presets()['1. มังฮวาเกาหลี']
                            self.assertEqual(saved_on_disk, expected)

                            # Disable mock, reload page from real backend, open modal -> still exact!
                            intercept_get['enabled'] = False
                            tab.reload()
                            tab.wait_for_function("() => document.querySelectorAll('#presetSelect option').length >= 2")
                            tab.locator('#presetSelect').select_option('1. มังฮวาเกาหลี')
                            tab.locator('[data-cmd=editPreset]').click()
                            tab.locator('#modal .preset-grid select[data-ftype="terrible"]').wait_for(state='visible')
                            self.assertEqual(self._read_modal_selects(tab), expected)

                            self.assertEqual(errors, [])
                            context.close()
                            browser.close()
                    finally:
                        server.should_exit = True
                        thread.join(10)


if __name__ == '__main__':
    unittest.main()
