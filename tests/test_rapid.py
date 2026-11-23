"""RapidOCR integration contracts without downloading model weights."""
import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import manhwa_ocr as m


class RapidTests(unittest.TestCase):
    def test_explicit_v5_language_and_cpu_configuration(self):
        calls=[]
        fake=SimpleNamespace(RapidOCR=lambda **kw:calls.append(kw),
            EngineType=SimpleNamespace(ONNXRUNTIME='onnxruntime'),LangDet=SimpleNamespace(CH='ch'),
            LangRec=SimpleNamespace(EN='en',KOREAN='korean'),ModelType=SimpleNamespace(MOBILE='mobile'),
            OCRVersion=SimpleNamespace(PPOCRV5='PP-OCRv5',PPOCRV4='PP-OCRv4'))
        with tempfile.TemporaryDirectory() as d,patch.object(m,'DATA_DIR',Path(d)),patch.dict(sys.modules,{'rapidocr':fake,'onnxruntime':SimpleNamespace()}):
            en=m.RapidEngine('en',True)
            ko=m.RapidEngine('ko+en')
        self.assertEqual(calls[0]['params']['Rec.lang_type'],'en')
        self.assertEqual(calls[1]['params']['Rec.lang_type'],'korean')
        for call in calls:
            p=call['params']
            self.assertEqual(p['Rec.ocr_version'],'PP-OCRv5')
            self.assertFalse(p['Global.use_cls'])
            self.assertFalse(p['EngineConfig.onnxruntime.use_cuda'])
            self.assertEqual(p['Det.limit_type'],'max')
        self.assertNotEqual(en.signature,ko.signature)

    def test_adapter_rgb_coordinates_and_empty_results(self):
        e=m.RapidEngine.__new__(m.RapidEngine)
        seen=[]
        def read(im,**kw):
            seen.append((im.getpixel((0,0)),kw))
            return SimpleNamespace(boxes=np.array([[[1,2],[60,2],[60,25],[1,25]]]),txts=('UGH!',),scores=(.98,))
        e.reader=read
        ls=e.read(Image.new('RGB',(80,40),(255,0,0)))
        self.assertEqual(seen[0][0],(255,0,0))
        self.assertFalse(seen[0][1]['use_cls'])
        self.assertEqual(ls[0].text,'UGH!')
        self.assertEqual(ls[0].box,[1,2,60,25])
        e.reader=lambda *a,**k:SimpleNamespace(boxes=None,txts=None,scores=None)
        self.assertEqual(e.read(Image.new('RGB',(80,40))),[])

    def test_auto_does_not_load_or_call_rapid(self):
        class Basic:
            name='Basic'
            signature='basic'
            def __init__(self,*args): pass
            def read(self,image): return [m.Line('LGH',[10,10,50,30],.95,self.name)]
        with tempfile.TemporaryDirectory() as d,patch.object(m,'RapidEngine',side_effect=AssertionError('No new auto cost')),patch.object(m,'EasyEngine',Basic),patch.object(m,'PaddleEngine',Basic):
            p=m.LocalPipeline(m.OCRCache(Path(d)/'cache.db'),neural=False,bubble_only=False)
            self.assertEqual(p.scan(Image.new('RGB',(100,100)))[0].text,'LGH')

    def test_rapid_crop_suggests_without_replacing_source_and_caches(self):
        class Fake:
            name='Rapid test';signature='rapid-test';calls=0
            def __init__(self,*args): pass
            def read(self,image):
                self.calls+=1
                return [m.Line('UGH',[5,5,40,25],.8,self.name)]
        with tempfile.TemporaryDirectory() as d,patch.object(m,'RapidEngine',Fake),patch.object(m.GoogleVisionOCR,'read',side_effect=AssertionError('No cloud')):
            p=m.LocalPipeline(m.OCRCache(Path(d)/'cache.db'),mode='rapid',neural=False)
            image=Image.new('RGB',(300,300));b=m.Bubble([30,30,160,120],text='LGH',translation='คำแปลเดิม')
            cs=p.candidates(image,b)
            self.assertEqual(cs[0]['text'],'UGH')
            self.assertEqual(b.text,'LGH')
            self.assertEqual(b.translation,'คำแปลเดิม')
            count=p.engines[0].calls
            p.candidates(image,b)
            self.assertEqual(p.engines[0].calls,count)

    def test_missing_optional_dependency_is_actionable(self):
        with patch.dict(sys.modules,{'rapidocr':None}):
            with self.assertRaisesRegex(RuntimeError,'install_rapid_windows.bat'):
                m.RapidEngine('en')


if __name__=='__main__':unittest.main()
