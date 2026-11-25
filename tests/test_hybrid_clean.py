import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import torch
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from bubble_cleaner import Cleaner, CleanRegion, LamaModel
import diffusion_cleaner
import web_api as studio
from manhwa_ocr import Bubble, Page


class ModelSelectionTests(unittest.TestCase):
    def test_native_checkpoint_and_offline_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / 'big-lama.pt'
            large = legacy.with_name('lama_large_512px.ckpt')
            legacy.touch()
            large.touch()
            with patch('bubble_cleaner.lama_model_path', return_value=legacy), \
                 patch('lama_large.load_generator') as native, \
                 patch('torch.jit.load') as jit:
                native.return_value = Mock()
                m1 = LamaModel()
                self.assertTrue(m1.large)
                native.assert_called_once_with(large)
                jit.assert_not_called()

                # Corrupt/unreadable large checkpoint automatically falls back to big-lama.pt
                native.side_effect = ValueError('bad checkpoint')
                m2 = LamaModel()
                self.assertFalse(m2.large)
                self.assertIn('Big-LaMa', m2.fallback_reason)
                jit.assert_called_once_with(str(legacy), map_location='cpu')

                # Missing large checkpoint falls back cleanly to big-lama.pt
                large.unlink()
                m3 = LamaModel()
                self.assertFalse(m3.large)

                # Explicit legacy=True skips large checkpoint even if present
                large.touch()
                native.reset_mock()
                native.side_effect = None
                m4 = LamaModel(legacy=True)
                self.assertFalse(m4.large)
                native.assert_not_called()

                # Both checkpoints missing -> clear Thai RuntimeError
                large.unlink()
                legacy.unlink()
                with self.assertRaisesRegex(RuntimeError, 'ยังไม่มีโมเดล LaMa'):
                    LamaModel()

    def test_lama_large_preserves_pixels_outside_mask_even_above_512px(self):
        rng = np.random.default_rng(7)
        arr = rng.integers(20, 235, size=(700, 720, 3), dtype=np.uint8)
        im = Image.fromarray(arr)
        mask = np.zeros((700, 720), dtype=np.uint8)
        mask[100:600, 120:620] = 255

        obj = object.__new__(LamaModel)
        obj.torch = torch
        obj.device = torch.device('cpu')
        obj.large = True
        obj.fallback_reason = ''
        # Fake generator outputs constant bright magenta (1.0, 0.0, 1.0)
        obj.model = lambda inp, m: torch.tensor([1.0, 0.0, 1.0]).view(1, 3, 1, 1).expand_as(inp)

        out = obj(im, Image.fromarray(mask))
        out_arr = np.asarray(out)
        self.assertEqual(out_arr.shape, arr.shape)
        # Pixels outside mask must match original 100% bit-for-bit
        self.assertTrue(np.array_equal(out_arr[mask == 0], arr[mask == 0]))
        # Pixels inside mask must be updated by the generator
        self.assertGreater(int(out_arr[mask > 0, 0].mean()), 240)
        self.assertLess(int(out_arr[mask > 0, 1].mean()), 15)

    def test_lama_large_forces_float32_and_logs_diagnostics_on_invalid_output(self):
        im = Image.new('RGB', (123, 97), (140, 160, 180))
        mask = np.zeros((97, 123), dtype=np.uint8)
        mask[30:65, 40:85] = 255

        obj = object.__new__(LamaModel)
        obj.torch = torch
        obj.device = torch.device('cpu')
        obj.large = True
        obj.fallback_reason = ''

        seen = {}
        def inspect_model(inp, m):
            seen['inp_dtype'] = inp.dtype
            seen['mask_dtype'] = m.dtype
            seen['inp_min'] = float(inp.min())
            seen['inp_max'] = float(inp.max())
            seen['autocast_cpu'] = torch.is_autocast_cpu_enabled()
            return inp.clone()

        obj.model = inspect_model
        with torch.autocast('cpu', dtype=torch.bfloat16, enabled=True):
            out = obj(im, Image.fromarray(mask))
        self.assertEqual(seen['inp_dtype'], torch.float32)
        self.assertEqual(seen['mask_dtype'], torch.float32)
        self.assertGreaterEqual(seen['inp_min'], 0.0)
        self.assertLessEqual(seen['inp_max'], 1.0)
        self.assertFalse(seen['autocast_cpu'])
        self.assertEqual(out.size, im.size)

        # Invalid NaN output must log min/max/dtype/has_nan to stderr and raise Thai RuntimeError
        def nan_model(inp, m):
            bad = inp.clone()
            bad[0, 0, 0, 0] = float('nan')
            return bad

        obj.model = nan_model
        buf = io.StringIO()
        with patch('sys.stderr', buf):
            with self.assertRaisesRegex(RuntimeError, 'LaMa คืนค่าสีที่ไม่ถูกต้อง'):
                obj(im, Image.fromarray(mask))
        logged = buf.getvalue()
        self.assertIn('min=', logged)
        self.assertIn('max=', logged)
        self.assertIn('dtype=', logged)
        self.assertIn('has_nan=True', logged)


class DiffusionCleanerTests(unittest.TestCase):
    def _sample_region(self):
        rng = np.random.default_rng(42)
        arr = rng.integers(40, 200, size=(240, 320, 3), dtype=np.uint8)
        im = Image.fromarray(arr)
        mask = np.zeros((240, 320), dtype=np.uint8)
        mask[80:160, 100:220] = 255
        safe = np.full((240, 320), 255, dtype=np.uint8)
        return im, CleanRegion([0, 0, 320, 240], im.copy(), mask, safe, False)

    def test_repair_clears_lama_feathers_inside_mask_and_preserves_outside(self):
        im, reg = self._sample_region()
        orig_arr = np.asarray(im)
        cleaner = Cleaner()
        cleaner.lama = object()
        cleaner._lama_status = {'ready': True}

        calls = []

        def fake_run(cmd, **kwargs):
            # Confirm LaMa was unloaded BEFORE worker subprocess starts
            self.assertIsNone(cleaner.lama)
            self.assertIsNone(cleaner._lama_status)
            calls.append((cmd, kwargs))
            work = Path(cmd[2])
            self.assertTrue((work / 'source.png').is_file())
            self.assertTrue((work / 'mask.png').is_file())
            Image.new('RGB', (512, 512), (250, 10, 20)).save(work / 'result.png')

        with tempfile.TemporaryDirectory() as fake_prefix:
            vendor_diffusers = Path(fake_prefix) / 'brushnet' / 'diffusers'
            vendor_diffusers.mkdir(parents=True)
            with patch.object(sys, 'prefix', fake_prefix), \
                 patch('torch.cuda.is_available', return_value=True), \
                 patch('torch.cuda.empty_cache') as empty_cache, \
                 patch('subprocess.run', side_effect=fake_run):
                repaired, out_mask = diffusion_cleaner.repair(reg, cleaner, im)

        empty_cache.assert_called_once()
        self.assertEqual(len(calls), 1)
        out_arr = np.asarray(repaired)
        # Pixels outside mask must be 100% untouched
        self.assertTrue(np.array_equal(out_arr[out_mask == 0], orig_arr[out_mask == 0]))
        # Deep interior of mask gets full BrushNet fill
        center = out_arr[110:130, 140:180]
        self.assertTrue(np.all(center[:, :, 0] >= 245))
        self.assertTrue(np.all(center[:, :, 1] <= 15))
        # Boundary pixel (1px inside mask) is soft-blended, not hard-clipped to 250
        edge_px = out_arr[80, 160]
        self.assertLess(int(edge_px[0]), 248)

    def test_repair_validation_and_thai_errors(self):
        im, reg = self._sample_region()
        empty_reg = CleanRegion(reg.box, reg.image.copy(), np.zeros_like(reg.mask), reg.safe.copy(), False)
        with self.assertRaisesRegex(ValueError, 'ระบาย Mask'):
            diffusion_cleaner.repair(empty_reg, Cleaner(), im)

        with patch('torch.cuda.is_available', return_value=False):
            with self.assertRaisesRegex(ValueError, 'CUDA'):
                diffusion_cleaner.repair(reg, Cleaner(), im)

        with tempfile.TemporaryDirectory() as empty_prefix, \
             patch.object(sys, 'prefix', empty_prefix), \
             patch('torch.cuda.is_available', return_value=True):
            with self.assertRaisesRegex(ValueError, 'requirements-brushnet.txt'):
                diffusion_cleaner.repair(reg, Cleaner(), im)


class MaskPreviewSafetyRouteTests(unittest.TestCase):
    def _fixture(self):
        im = Image.new('RGB', (400, 300), '#9fb3c8')
        d = ImageDraw.Draw(im)
        d.ellipse((40, 40, 360, 260), fill='white', outline='#111111', width=4)
        d.rectangle((140, 125, 260, 175), fill='black')
        b = Bubble(
            box=[40, 40, 360, 260],
            text='TEST',
            lines=[{'box': [140, 125, 260, 175], 'text': 'TEST'}],
            score=0.95,
        )
        return im, b

    def test_diffusion_and_lama_preview_never_overwrite_before_apply_and_undo_reverts(self):
        im, b = self._fixture()
        client = TestClient(studio.app)
        headers = {'x-studio': 'editor'}

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(studio, 'ROOT', Path(tmp)), \
             patch.object(studio, '_make_verifier', return_value=lambda _: []):
            source = Path(tmp) / '01.png'
            im.save(source)
            src_hash_before = hashlib.sha256(source.read_bytes()).digest()
            studio.save('safejob', 'Safe', [Page(str(source), [b])], {})

            # 1. Start mask session
            r = client.post('/api/safejob/page/0/mask_start', json={'id': b.id}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()['has_preview'])

            # 2. Preview with standard engine (LaMa/auto) -> must NOT save clean_patches or masks
            r = client.post('/api/safejob/page/0/mask_preview', json={'id': b.id, 'method': 'auto'}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()['has_preview'])
            pages_now, masks_now = studio.load('safejob')
            self.assertEqual(pages_now[0].clean_patches, [])
            self.assertNotIn(b.id, masks_now)

            # 3. Preview with engine='diffusion' (AI วาดซ่อม) -> distinct green fill in preview only
            def fake_repair(reg, cleaner, page):
                out = reg.image.copy()
                arr = np.asarray(out).copy()
                arr[reg.mask > 0] = (40, 210, 90)
                return Image.fromarray(arr), reg.mask.copy()

            with patch('diffusion_cleaner.repair', side_effect=fake_repair):
                r = client.post(
                    '/api/safejob/page/0/mask_preview',
                    json={'id': b.id, 'engine': 'diffusion'},
                    headers=headers,
                )
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body['has_preview'])
            self.assertEqual(body['method'], 'diffusion')
            self.assertIn('กดตกลง', body['note'])

            # Preview endpoint reflects diffusion result, while 'clean' view and DB are untouched!
            prev_img = Image.open(io.BytesIO(client.get(f'/api/safejob/image/0?view=mask_preview&bid={b.id}').content)).convert('RGB')
            clean_img = Image.open(io.BytesIO(client.get('/api/safejob/image/0?view=clean').content)).convert('RGB')
            prev_px = prev_img.getpixel((200, 150))
            clean_px = clean_img.getpixel((200, 150))
            self.assertEqual(prev_px, (40, 210, 90))
            self.assertEqual(clean_px, (0, 0, 0))
            pages_now, masks_now = studio.load('safejob')
            self.assertEqual(pages_now[0].clean_patches, [])
            self.assertNotIn(b.id, masks_now)

            # 4. Ctrl+Z (mask_undo) even with NO painted strokes clears preview_patch on server!
            r = client.post('/api/safejob/page/0/mask_undo', json={'id': b.id}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()['has_preview'])
            self.assertIsNone(studio.mask_sessions[('safejob', 0, b.id)]['preview_patch'])
            prev_after_undo = Image.open(io.BytesIO(client.get(f'/api/safejob/image/0?view=mask_preview&bid={b.id}').content)).convert('RGB')
            self.assertEqual(prev_after_undo.getpixel((200, 150)), (0, 0, 0))

            # 5. Diffusion preview again -> Cancel ("ยกเลิก") reverts 100% cleanly without saving
            with patch('diffusion_cleaner.repair', side_effect=fake_repair):
                r = client.post(
                    '/api/safejob/page/0/mask_preview',
                    json={'id': b.id, 'engine': 'diffusion'},
                    headers=headers,
                )
            self.assertEqual(r.status_code, 200)
            r = client.post('/api/safejob/page/0/mask_cancel', json={'id': b.id}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(('safejob', 0, b.id), studio.mask_sessions)
            pages_now, masks_now = studio.load('safejob')
            self.assertEqual(pages_now[0].clean_patches, [])
            self.assertNotIn(b.id, masks_now)

            # 6. Diffusion preview -> Apply ("ตกลง") NOW commits patch to project
            client.post('/api/safejob/page/0/mask_start', json={'id': b.id}, headers=headers)
            with patch('diffusion_cleaner.repair', side_effect=fake_repair):
                client.post('/api/safejob/page/0/mask_preview', json={'id': b.id, 'engine': 'diffusion'}, headers=headers)
            r = client.post('/api/safejob/page/0/mask_apply', json={'id': b.id}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()['page']['clean'])
            pages_done, masks_done = studio.load('safejob')
            self.assertEqual(len(pages_done[0].clean_patches), 1)
            self.assertEqual(pages_done[0].clean_patches[0]['method'], 'diffusion')
            self.assertIn(b.id, masks_done)
            clean_after_apply = Image.open(io.BytesIO(client.get('/api/safejob/image/0?view=clean').content)).convert('RGB')
            self.assertEqual(clean_after_apply.getpixel((200, 150)), (40, 210, 90))
            # Original image on disk is NEVER modified
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), src_hash_before)


if __name__ == '__main__':
    unittest.main()
