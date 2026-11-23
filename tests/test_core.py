import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import manhwa_ocr as m
from PIL import Image, ImageDraw

class CoreTests(unittest.TestCase):
    def test_exact_output_and_korean_spacing(self):
        p=m.Page('page.png',[m.Bubble([0,0,10,10],text='안녕\n하세요!  Hello?',translation='สวัสดี\nครับ')])
        self.assertEqual(m.format_output([p]),'คำต้นฉบับ: 안녕 하세요! Hello?\nคำแปลไทย: สวัสดี ครับ')
        self.assertEqual(m.join_lines(['오늘은 날씨가','좋아요']),'오늘은 날씨가 좋아요')

    def test_bubbles_do_not_merge_across_known_boundaries(self):
        ls=[m.Line('A',[20,20,65,35],.9),m.Line('B',[20,42,70,57],.9),
            m.Line('C',[110,20,160,35],.9),m.Line('D',[110,42,160,57],.9)]
        result=m.group_lines(ls,[[5,5,85,80],[95,5,180,80]])
        self.assertEqual([b.text for b in result],['A B','C D'])

    def test_geometry_line_groups(self):
        ls=[m.Line('HELLO',[10,10,60,30],.9),m.Line('WORLD',[10,37,65,57],.9),
            m.Line('OTHER',[200,10,260,30],.9)]
        self.assertEqual([b.text for b in m.group_lines(ls)],['HELLO WORLD','OTHER'])

    def test_repeated_dialogue_distinct_locations(self):
        ls=[m.Line('Yes',[10,10,70,30],.7),m.Line('Yes',[11,11,71,31],.9),m.Line('Yes',[10,300,70,320],.9)]
        self.assertEqual(len(m.dedupe_lines(ls)),2)

    def test_tall_tile_coverage(self):
        im=Image.new('RGB',(40,6200))
        spans=[(y,y+t.height) for y,t in m.tiles(im)]
        self.assertEqual(spans[0][0],0)
        self.assertEqual(spans[-1][1],6200)
        self.assertTrue(all(a[1]>b[0] for a,b in zip(spans,spans[1:])))

    def test_contour_excludes_page_background(self):
        im=Image.new('RGB',(400,600),'#555555')
        ImageDraw.Draw(im).ellipse((60,70,210,190),fill='white',outline='black',width=4)
        regions=m.contour_regions(im)
        self.assertEqual(len(regions),1)
        self.assertGreater(m.intersection(regions[0],[60,70,210,190]),10000)

    def test_cache_image_and_engine_identity(self):
        with tempfile.TemporaryDirectory() as d:
            cache=m.OCRCache(Path(d)/'cache.db')
            im=Image.new('RGB',(10,10),'white')
            key=cache.key(im,'e:ko')
            cache.put(key,[{'text':'안녕'}])
            self.assertEqual(cache.get(key),[{'text':'안녕'}])
            self.assertIsNone(cache.get(cache.key(im,'e:en')))
            self.assertIsNone(cache.get(cache.key(Image.new('RGB',(10,10),'black'),'e:ko')))

    def test_google_preserves_detected_spaces(self):
        symbols=[{'text':'안'},{'text':'녕','property':{'detectedBreak':{'type':'SPACE'}}},
                 {'text':'하'},{'text':'세'},{'text':'요','property':{'detectedBreak':{'type':'LINE_BREAK'}}}]
        data={'fullTextAnnotation':{'pages':[{'blocks':[{'paragraphs':[{'confidence':.9,
            'boundingBox':{'vertices':[{'x':0,'y':0},{'x':100,'y':30}]},'words':[{'symbols':symbols}]}]}]}]}}
        self.assertEqual(m.GoogleVisionOCR.parse(data)[0].text,'안녕 하세요')

    def test_google_cache_and_cap_before_request(self):
        with tempfile.TemporaryDirectory() as d:
            g=m.GoogleVisionOCR(m.OCRCache(Path(d)/'cache.db'))
            im=Image.new('RGB',(10,10),'white')
            fake=SimpleNamespace(status_code=200,json=lambda:{'responses':[{}]})
            with patch('requests.post',return_value=fake) as post:
                self.assertEqual(g.read(im,'fake-test-key'),[])
                self.assertEqual(g.calls,1)
                g.read(im,'fake-test-key')
                self.assertEqual(post.call_count,1)
                self.assertEqual(g.hits,1)
                g.calls=10
                with self.assertRaises(RuntimeError):
                    g.read(Image.new('RGB',(10,10),'black'),'fake-test-key')
                self.assertEqual(post.call_count,1)

    def test_google_failed_request_counts_without_leaking_key(self):
        import requests
        with tempfile.TemporaryDirectory() as d:
            g=m.GoogleVisionOCR(m.OCRCache(Path(d)/'cache.db'))
            with patch('requests.post',side_effect=requests.ConnectionError('secret-key-in-url')):
                with self.assertRaises(RuntimeError) as context:
                    g.read(Image.new('RGB',(10,10)),'fake-test-key')
            self.assertNotIn('secret-key',str(context.exception))
            self.assertEqual(g.calls,1)

    def test_paddle_v3_explicit_recognition_language(self):
        calls=[]
        fake=SimpleNamespace(PaddleOCR=lambda **kwargs:calls.append(kwargs))
        with patch.dict(sys.modules,{'paddleocr':fake}), patch.object(m,'package_version',return_value='3.7.0'):
            m.PaddleEngine('ko+en',False)
            m.PaddleEngine('en',False)
        self.assertEqual(calls[0]['text_recognition_model_name'],'korean_PP-OCRv5_mobile_rec')
        self.assertEqual(calls[1]['text_recognition_model_name'],'en_PP-OCRv5_mobile_rec')
        self.assertNotIn('lang',calls[0])
        self.assertFalse(calls[0]['use_textline_orientation'])
        self.assertFalse(calls[0]['enable_mkldnn'])

    def test_failed_primary_uses_other_local_engine(self):
        class Broken:
            name='Broken local'
            signature='broken:v1'
            def __init__(self,*args):pass
            def read(self,image):raise RuntimeError('backend failed')
        class Good(Broken):
            name='Working local'
            signature='good:v1'
            def read(self,image):return [m.Line('Recovered text',[20,20,200,50],.95,self.name)]
        with tempfile.TemporaryDirectory() as d, patch.object(m,'EasyEngine',Broken), patch.object(m,'PaddleEngine',Good), patch.object(m.GoogleVisionOCR,'read',side_effect=AssertionError('No cloud')):
            p=m.LocalPipeline(m.OCRCache(Path(d)/'cache.db'),mode='auto',neural=False,retry=False,bubble_only=False)
            self.assertEqual(p.engines[0].name,'Broken local')
            result=p.scan(Image.new('RGB',(300,400),'white'))
            self.assertEqual(result[0].text,'Recovered text')

    def test_failed_crop_retry_keeps_primary_text(self):
        class FirstPassOnly:
            name='Local test'
            signature='first-pass:v1'
            def __init__(self,*args):pass
            def read(self,image):
                if image.size != (300,400):raise RuntimeError('crop backend failed')
                return [m.Line('Keep this text',[20,20,200,50],.5,self.name)]
        with tempfile.TemporaryDirectory() as d, patch.object(m,'EasyEngine',FirstPassOnly):
            p=m.LocalPipeline(m.OCRCache(Path(d)/'cache.db'),mode='easy',neural=False,retry=True,bubble_only=False)
            result=p.scan(Image.new('RGB',(300,400),'white'))
            self.assertEqual(result[0].text,'Keep this text')
            self.assertEqual(result[0].candidates,[])

    def test_paddle_v3_adapter_bgr_and_coordinates(self):
        engine=m.PaddleEngine.__new__(m.PaddleEngine)
        engine.v3=True
        captured=[]
        def predict(arr):
            captured.append(arr[0,0].tolist())
            return [SimpleNamespace(json={'res':{'rec_texts':['안녕하세요'],
                'rec_scores':[.9],'rec_polys':[[[1,2],[30,2],[30,15],[1,15]]]}})]
        engine.reader=SimpleNamespace(predict=predict)
        lines=engine.read(Image.new('RGB',(40,30),(255,0,0)))
        self.assertEqual(captured,[[0,0,255]])
        self.assertEqual(lines[0].box,[1,2,30,15])

    def test_paddle_v2_adapter(self):
        engine=m.PaddleEngine.__new__(m.PaddleEngine)
        engine.v3=False
        engine.reader=SimpleNamespace(ocr=lambda arr,cls:[[[[[0,0],[30,0],[30,20],[0,20]],('Hello',.8)]]])
        self.assertEqual(engine.read(Image.new('RGB',(40,40)))[0].text,'Hello')

    def test_local_scan_never_calls_google_and_is_cacheable(self):
        class FakeEngine:
            name='Fake local'
            signature='fake:v1'
            calls=0
            def __init__(self,*args):pass
            def read(self,image):
                self.calls+=1
                return [m.Line('Hello',[20,20,100,45],.95,self.name)]
        with tempfile.TemporaryDirectory() as d, patch.object(m,'PaddleEngine',FakeEngine), patch.object(m,'EasyEngine',FakeEngine), patch.object(m.GoogleVisionOCR,'read',side_effect=AssertionError('Cloud forbidden')):
            pipeline=m.LocalPipeline(m.OCRCache(Path(d)/'cache.db'),mode='easy',neural=False,retry=False,bubble_only=False)
            im=Image.new('RGB',(300,600),'white')
            result=pipeline.scan(im)
            self.assertEqual(result[0].text,'Hello')
            pipeline.scan(im)
            self.assertEqual(pipeline.engines[0].calls,1)
            self.assertGreater(pipeline.hits,0)

    def test_cancel(self):
        p=m.LocalPipeline.__new__(m.LocalPipeline)
        p.cancel=threading.Event()
        p.cancel.set()
        with self.assertRaises(m.Cancelled):p.check()

    def test_project_json_retains_manual_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p=m.Page('page.png',[m.Bubble([1,2,3,4],text='안녕',translation='สวัสดี',reviewed=True,
                candidates=[{'text':'안녕!','engine':'test','score':.9}])])
            target=Path(d)/'project.json'
            m.atomic_json(target,m.asdict(p))
            data=m.json.loads(target.read_text(encoding='utf-8'))
            b=m.Bubble(**data['bubbles'][0])
            self.assertEqual(b.translation,'สวัสดี')
            self.assertTrue(b.reviewed)
            self.assertEqual(b.id,p.bubbles[0].id)

if __name__=='__main__':unittest.main()
