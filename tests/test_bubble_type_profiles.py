import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import text_layout as layout


class BubbleTypeProfiles(unittest.TestCase):
    def test_old_custom_profile_adds_three_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'font_presets.json'
            path.write_text(json.dumps({'Custom': {'normal': 'Itim'}}))
            with patch.object(layout, 'presets_config_path', return_value=path):
                saved = layout.load_font_presets()['Custom']
        self.assertEqual(len(saved), 9)
        self.assertEqual(saved['normal'], 'Itim')
        self.assertEqual([saved[k] for k in ('terrible','happy','phone')],
                         ['MN KRASIP','JS Toomtam','TH Baijam Bold'])

    def test_tag_overrides_existing_lock_before_inference(self):
        b = SimpleNamespace(translation='[ข่มขู่] ระวัง',frame_type_locked=True,frame_type='normal')
        self.assertEqual(layout.classify_bubble_type(None,b),'terrible')
        self.assertEqual(b.translation,'ระวัง')
        self.assertEqual(layout.classify_bubble_type(None,b),'terrible')

    def test_tile_coordinates_duplicate_suppression_and_page_cache(self):
        image = Image.new('RGB',(400,2880))
        calls = []
        class Session:
            def run(self, names, inputs):
                top = 1280 * len(calls)
                calls.append(inputs['input'].shape)
                boxes = np.array([[[.5,(1440-top)/1600,.5,.1]]],np.float32)
                return boxes,np.array([[[-6,6]]],np.float32)
        detector = layout.BubbleTypeDetector()
        model = Session(),SimpleNamespace(name='input',shape=[1,3,480,480]),[None,'phone']
        with tempfile.TemporaryDirectory() as root:
            config = Path(root)/'font_presets.json'
            folder = config.parent/'seg_models'/'bubble_type_v11'
            folder.mkdir(parents=True)
            for name in ('weights.onnx','class_names.txt'):
                (folder/name).touch()
            with patch.object(layout,'presets_config_path',return_value=config), patch.object(layout,'_load_type_model',return_value=model):
                self.assertEqual(detector.match(image,(175,1430,225,1450)),'phone')
                self.assertEqual(len(detector.scan(image)),1)
                self.assertIsNone(detector.match(image,(0,0,10,10)))
        self.assertEqual(calls,[(1,3,480,480)]*2)

    def test_model_prediction_never_overridden_by_heuristics(self):
        # Oval speech balloon (with tail) surrounded by sparkles/dots and detached
        # background rays that would otherwise make the heuristic guess 'thought'.
        image = Image.new('RGB', (480, 480), '#d0d0d0')
        draw = ImageDraw.Draw(image)
        cx, cy, rx, ry = 240, 240, 120, 90
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill='white', outline='black', width=3)
        draw.polygon([(cx - 18, cy + ry - 4), (cx + 12, cy + ry - 4), (cx - 4, cy + ry + 34)], fill='white', outline='black')
        for i in range(48):
            a = 2 * math.pi * i / 48
            # Detached radial strokes (background speedlines) + sparkles around the balloon
            p0 = (round(cx + rx * 1.12 * math.cos(a)), round(cy + ry * 1.12 * math.sin(a)))
            p1 = (round(cx + rx * 1.32 * math.cos(a)), round(cy + ry * 1.32 * math.sin(a)))
            draw.line((p0, p1), fill='black', width=2)
            dx = round(cx + rx * 1.42 * math.cos(a))
            dy = round(cy + ry * 1.42 * math.sin(a))
            draw.rectangle((dx - 1, dy - 1, dx + 1, dy + 1), fill='black')
        box = [cx - rx, cy - ry, cx + rx, cy + ry]

        def make_bubble(**extra):
            attrs = dict(
                box=box, text='HELLO', translation='สวัสดี', lines=[],
                frame_type='normal', frame_type_locked=False,
                detected_frame_type='', detector_class=0,
            )
            attrs.update(extra)
            return SimpleNamespace(**attrs)

        # Confirm the noisy image genuinely triggers the 'thought' fallback when model returns None.
        with patch.object(layout._BUBBLE_TYPE_DETECTOR, 'match', return_value=None):
            self.assertEqual(layout.classify_bubble_type(image, make_bubble(), refresh=True), 'thought')
            self.assertEqual(layout.classify_bubble_type(image, make_bubble(), refresh=False), 'thought')

        # Also test a sparkle-only oval balloon (triggers dot_count >= 8 -> 'thought' in both modes).
        sparkle_im = Image.new('RGB', (480, 480), '#d0d0d0')
        sdraw = ImageDraw.Draw(sparkle_im)
        sdraw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill='white', outline='black', width=3)
        for i in range(24):
            a = 2 * math.pi * i / 24
            dx = round(cx + rx * 1.20 * math.cos(a))
            dy = round(cy + ry * 1.20 * math.sin(a))
            sdraw.rectangle((dx - 1, dy - 1, dx + 1, dy + 1), fill='black')
        with patch.object(layout._BUBBLE_TYPE_DETECTOR, 'match', return_value=None):
            self.assertEqual(layout.classify_bubble_type(sparkle_im, make_bubble(), refresh=True), 'thought')
            self.assertEqual(layout.classify_bubble_type(sparkle_im, make_bubble(), refresh=False), 'thought')

        # When the 12-class model predicts 'normal' (or any other class), return it directly
        # for both refresh=False and refresh=True without heuristics overriding it.
        for sample_im in (image, sparkle_im):
            for predicted in ('normal', 'floating', 'shout', 'thought', 'narration', 'system', 'happy', 'terrible', 'phone'):
                with patch.object(layout._BUBBLE_TYPE_DETECTOR, 'match', return_value=predicted):
                    for refresh in (False, True):
                        b = make_bubble(detected_frame_type='thought' if refresh else '')
                        self.assertEqual(layout.classify_bubble_type(sample_im, b, refresh=refresh), predicted)
