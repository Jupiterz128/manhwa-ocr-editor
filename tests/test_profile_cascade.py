import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
import text_layout as tl
import web_api as studio
from manhwa_ocr import Bubble, Page


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.enterContext(patch.object(studio,'ROOT',folder/'store'))
        self.enterContext(patch.object(tl,'presets_config_path',return_value=folder/'profiles.json'))
        self.path = folder/'01.png'
        self.image = Image.new('RGB',(600,380),'gray')
        draw = ImageDraw.Draw(self.image)
        draw.ellipse((20,20,580,360),fill='white',outline='black',width=4)
        draw.text((230,145),'HELLO!',fill='black',font_size=35)
        self.image.save(self.path)
        self.mapping = {t:('Itim' if i%2==0 else tl.FONT_NAME) for i,(t,_) in enumerate(tl.FRAME_TYPES)}
        tl.save_font_presets({'test':self.mapping})

    def test_all_nine_types_use_saved_category_and_real_postscript(self):
        pages=[]
        for category in self.mapping:
            b=Bubble([205,135,325,190],translation='ไปกัน',frame_type=category,detected_frame_type='shout')
            pages.append(Page(str(self.path),[b]))
        studio.save('book','Book',pages,{},font_preset='test')
        result=studio.apply_font_preset('book',{'preset':'test'})
        self.assertEqual(result['laid_out'],9)
        for category,p in zip(self.mapping,studio.load('book')[0]):
            b=p.bubbles[0]
            self.assertEqual(b.frame_type,category)
            self.assertEqual(b.font_choice,self.mapping[category])
            self.assertEqual(b.text_layer['font'],tl.resolve_font(b.font_choice).postscript)

    def test_forward_only_and_manual_override(self):
        def page(types):
            return Page(str(self.path),[Bubble([10,10,60,60],frame_type=t) for t in types])
        self.mapping['normal']=tl.FONT_NAME
        tl.save_font_presets({'test':self.mapping})
        pages=[page(['normal']),page(['normal','normal','thought','normal','thought']),
               page(['normal','normal','thought'])]
        pages[2].bubbles[1].manual_font=True
        studio.save('book','Book',pages,{},font_preset='test')
        b=pages[1].bubbles[2]
        studio.action('book',1,'edit',{'id':b.id,'frame_type':'normal'})
        studio.action('book',1,'edit',{'id':b.id,'font_choice':'Itim'})
        pages,_=studio.load('book')
        self.assertEqual([[i.font_choice for i in p.bubbles] for p in pages],
                         [[tl.FONT_NAME],[tl.FONT_NAME,tl.FONT_NAME,'Itim','Itim',tl.FONT_NAME],
                          ['Itim',tl.FONT_NAME,tl.FONT_NAME]])
        self.assertEqual(tl.load_font_presets()['test']['normal'],'Itim')

    def test_model_center_and_manual_drag(self):
        model=tl.bubble_seg_model_path().with_name('manga109-640.ts')
        if not model.exists():
            self.skipTest('Manga109 weights are not installed')
        b=Bubble([205,135,325,190],translation='ไปกัน',frame_type='normal')
        region=tl.BUBBLE_SEGMENTER.segment_bubble(self.image,b.box)
        self.assertIsNotNone(region)
        roi,mask=region
        x,y,_=tl.pole_of_inaccessibility(mask)
        expected=(x+roi[0],y+roi[1])
        self.assertGreater(expected[0]-265,20)
        studio.save('book','Book',[Page(str(self.path),[b])],{},font_preset='test')
        def apply():
            result=studio.apply_font_preset('book',{'preset':'test','all_pages':False,'page':0})
            self.assertEqual(result['laid_out'],1)
            return studio.load('book')[0][0].bubbles[0].text_layer['editor_box']
        r=apply()
        self.assertAlmostEqual((r[0]+r[2])/2,expected[0],delta=4)
        self.assertAlmostEqual((r[1]+r[3])/2,expected[1],delta=4)
        moved=[r[0]+15,r[1]+10,r[2]+15,r[3]+10]
        studio.action('book',0,'edit',{'id':b.id,'box':moved})
        self.assertEqual(apply(),moved)
