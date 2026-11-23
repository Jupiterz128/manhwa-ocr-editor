import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

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
