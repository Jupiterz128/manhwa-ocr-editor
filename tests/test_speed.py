"""Behavioral tests for bubble-first OCR. Fakes measure work, not inference time."""
import sys, tempfile, unittest, copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manhwa_ocr as m
from PIL import Image


class Reader:
    name = 'Test local'
    signature = 'test-local'
    def __init__(self, *args):
        self.calls = []
    def read(self, image):
        self.calls.append(image.size)
        return [m.Line('Hello 안녕', [5,6,25,20], .8, self.name)]


class Detector:
    calls = 0
    def __init__(self, *args): pass
    def detect(self, image):
        type(self).calls += 1
        return [(0,.95,[20,30,180,150]),(1,.9,[30,45,140,100]),
                (2,.95,[200,200,290,300])]


class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = m.OCRCache(Path(self.tmp.name)/'cache.db')
        self.addCleanup(self.tmp.cleanup)
        Detector.calls = 0

    def pipeline(self, **kw):
        with patch.object(m, 'EasyEngine', Reader), patch.object(m, 'ComicDetector', Detector):
            return m.LocalPipeline(self.cache, mode='easy', **kw)

    def test_only_balloon_crop_and_original_coordinates(self):
        p = self.pipeline()
        partial, progress = [], []
        p.on_partial = partial.append
        p.on_progress = progress.append
        with patch.object(m.GoogleVisionOCR,'read',side_effect=AssertionError('No cloud')):
            bs = p.scan(Image.new('RGB',(300,500),'white'))
        self.assertEqual(p.engines[0].calls, [(160,120)])
        self.assertEqual(len(bs), 1)  # text_free and text_bubble did not add OCR crops
        self.assertEqual(bs[0].box, [20,30,180,150])
        self.assertEqual(bs[0].lines[0]['box'], [25,36,45,50])
        self.assertEqual(bs[0].text, 'Hello 안녕')
        self.assertEqual(partial[0][0].text, bs[0].text)
        self.assertEqual(progress, sorted(progress))
        self.assertGreater(progress[-1], .98)

    def test_repeat_uses_both_detector_and_ocr_cache(self):
        p = self.pipeline()
        im = Image.new('RGB',(300,500),'white')
        p.scan(im); p.scan(im)
        self.assertEqual(Detector.calls, 1)
        self.assertEqual(len(p.engines[0].calls), 1)

    def test_no_bubbles_never_falls_back_to_full_page(self):
        p = self.pipeline()
        p.detector.detect = lambda image:[(2,.95,[10,10,80,50])]
        self.assertEqual(p.scan(Image.new('RGB',(300,500))), [])
        self.assertEqual(p.engines[0].calls, [])

    def test_cancel_retains_snapshot_from_first_bubble(self):
        p = self.pipeline()
        p.detector.detect = lambda image:[(0,.9,[10,10,100,60]),(0,.9,[10,100,100,160])]
        partial = []
        def accept(bs):
            partial.append(bs)
            p.cancel.set()
        p.on_partial = accept
        with self.assertRaises(m.Cancelled):
            p.scan(Image.new('RGB',(300,500)))
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0][0].text, 'Hello 안녕')

    def test_seam_duplicates_read_once(self):
        p = self.pipeline()
        calls = []
        def detect(image):
            calls.append(1)
            return [(0,.9,[20,1600,180,1720])] if len(calls)==1 else [(0,.9,[20,60,180,180])]
        p.detector.detect = detect
        bs = p.scan(Image.new('RGB',(300,2300)))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(bs), 1)
        self.assertEqual(bs[0].box, [20,1600,180,1720])

    def test_secondary_not_constructed_until_needed(self):
        secondary = []
        class Backup(Reader):
            def __init__(self,*args):
                secondary.append(1)
                super().__init__(*args)
        with patch.object(m,'EasyEngine',Reader),patch.object(m,'PaddleEngine',Backup),patch.object(m,'ComicDetector',Detector):
            p=m.LocalPipeline(self.cache)
        self.assertEqual(secondary, [])
        p.scan(Image.new('RGB',(300,500)))
        self.assertEqual(secondary, [])
        p.candidates(Image.new('RGB',(300,500)),m.Bubble([20,30,180,150]))
        self.assertEqual(secondary, [1])

    def test_easy_fast_and_detailed_parameters(self):
        args = []
        e = m.EasyEngine.__new__(m.EasyEngine)
        e.fast = True
        e.reader = SimpleNamespace(readtext=lambda image,**kw:args.append(kw) or [])
        e.read(Image.new('RGB',(100,100)))
        e.read_detailed(Image.new('RGB',(100,100)))
        self.assertIsNone(args[0]['rotation_info'])
        self.assertEqual(args[0]['decoder'],'greedy')
        self.assertEqual(args[1]['rotation_info'],[90,180,270])
        self.assertEqual(args[1]['decoder'],'beamsearch')

    def test_fast_cache_not_reused_for_detailed_scan(self):
        p = self.pipeline()
        image=Image.new('RGB',(300,500))
        p.scan(image)
        p.fast=False
        p.scan(image)
        self.assertEqual(len(p.engines[0].calls),2)

    def test_tall_page_partial_arrives_before_all_tiles_complete(self):
        p = self.pipeline(neural=False,bubble_only=False)
        partial=[]
        p.on_partial=lambda bs:partial.append((len(p.engines[0].calls),copy.deepcopy(bs)))
        # Different pixels per tile prevent the cache from collapsing test work.
        image=Image.new('RGB',(300,4000),'white')
        image.paste('black',(0,2000,300,4000))
        bs=p.scan(image)
        self.assertEqual(len(partial),3)
        self.assertEqual(partial[0][0],1)
        self.assertEqual(len(bs),3)

if __name__ == '__main__': unittest.main()
