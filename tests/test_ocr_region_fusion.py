import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from PIL import Image
import manhwa_ocr as m
import text_layout as t
from test_speed import Reader, Detector


class RegionFusionTests(unittest.TestCase):
    def test_merge_filter_cache_and_font_reuse(self):
        im=Image.new('RGB',(300,500),'white')
        barrier=Barrier(2,timeout=5)
        def primary(tile):
            barrier.wait()
            return [(2,.9,[200,200,290,300])]
        def secondary(tile,gpu):
            barrier.wait()
            return [('SFX',.45,[200,200,290,300]),('Happy bubble',.38,[10,10,80,90]),
                    ('ST',.99,[90,90,120,120])]
        with TemporaryDirectory() as folder, patch.object(m,'EasyEngine',Reader), patch.object(m,'ComicDetector',Detector):
            pipeline=m.LocalPipeline(m.OCRCache(Path(folder)/'cache.db'), mode='easy',type_scan=True)
            with patch.object(pipeline.detector,'detect',side_effect=primary) as rt, patch.object(t._BUBBLE_TYPE_DETECTOR,'signature',return_value=('model',1,2)), patch.object(t._BUBBLE_TYPE_DETECTOR,'detect_tile',side_effect=secondary) as ai:
                for _ in range(2):
                    bubbles=pipeline.scan(im)
                    self.assertEqual([b.frame_type for b in bubbles],['happy'])
                self.assertEqual((rt.call_count,ai.call_count),(1,1))
                with patch.object(t._BUBBLE_TYPE_DETECTOR,'match',side_effect=AssertionError):
                    self.assertEqual(t.classify_bubble_type(im,bubbles[0]),'happy')
    def test_speech_protects_overlapping_text_free(self):
        rect=[20,20,80,80]
        rows,blocked=m._merge_tile_regions(Image.new('RGB',(100,100)),
            [(2,.9,rect),(0,.9,[10,10,90,90])],[('SFX',.95,rect)])
        self.assertFalse(blocked)
        self.assertEqual(len(rows),2)
