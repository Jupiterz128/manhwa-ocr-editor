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
            b=Bubble([205,135,325,190],translation='ไปกัน',frame_type=category,frame_type_locked=True,detected_frame_type='shout')
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

    def test_mtp_stitch_reclassifies_and_centers_on_tall_page(self):
        import math
        import threading
        from manhwa_ocr import write_mtp,read_mtp

        folder=self.path.parent
        pages=[]
        for kind in ('shout','thought'):
            image=Image.new('RGB',(480,2000),'#c0c0c0')
            draw=ImageDraw.Draw(image)
            cx,cy=240,1470
            def point(i,n,f):
                a=2*math.pi*i/n
                return round(cx+140*f*math.cos(a)),round(cy+108*f*math.sin(a))
            if kind=='shout':
                star=[point(i,48,1 if i%2==0 else .77) for i in range(48)]
                draw.polygon(star,fill='white',outline='black',width=3)
            else:
                draw.ellipse((cx-140,cy-108,cx+140,cy+108),fill='white')
                for i in range(54):
                    draw.line((point(i,54,.87),point(i,54,1.12)),fill='black',width=2)
            draw.text((cx-50,cy-46),'HELLO',fill='black',font_size=19)
            path=folder/(kind+'.png')
            image.save(path)
            pages.append(Page(str(path),[Bubble([cx-53,cy-48,cx+13,cy-19],
                                                text='HELLO',translation='ไปกัน',frame_type='normal')]))
        stop=threading.Event()
        mtp=folder/'test.mtp'
        write_mtp(mtp,pages,{},stop,lambda e:None)
        imported=read_mtp(mtp,stop,lambda e:None,folder/'unpacked')[0]
        self.assertTrue(all(b.frame_type=='normal' and not b.frame_type_locked for p in imported for b in p.bubbles))
        self.mapping.update(normal=tl.FONT_NAME,shout='Itim',thought='FC Muffin')
        tl.save_font_presets({'test':self.mapping})
        studio.save('imported','Book',imported,{},font_preset='test')
        key=studio.stitch_project('imported',{'height':6300,'width':720})['key']
        stitched,_=studio.load(key)
        self.assertEqual(len(stitched),1)
        with Image.open(stitched[0].path) as stitched_image:
            self.assertEqual(stitched_image.size,(720,6000))
        preset=studio.project_font_preset(key)
        self.assertEqual(preset,'test')
        result=studio.apply_font_preset(key,{'preset':preset})
        self.assertEqual(result['laid_out'],2,result)
        for i,(b,kind) in enumerate(zip(studio.load(key)[0][0].bubbles,('shout','thought'))):
            self.assertEqual(b.frame_type,kind)
            self.assertEqual(b.text_layer['font'],tl.resolve_font(self.mapping[kind]).postscript)
            self.assertEqual(b.text_layer['bubble_box'],b.box)
            x,y,r,bt=b.text_layer['box']
            self.assertAlmostEqual((x+r)/2,360,delta=12)
            self.assertAlmostEqual((y+bt)/2,2205+i*3000,delta=12)

    def test_mtp_preserves_only_explicit_category_locks(self):
        import threading
        from manhwa_ocr import read_mtp,write_mtp
        stop=threading.Event()
        pages=[Page(str(self.path),[
            Bubble([60,50,130,100],frame_type='phone',frame_type_locked=True,translation='โทร'),
            Bubble([60,130,130,180],frame_type='normal',translation='[คิด] อืม'),
            Bubble([60,210,130,260],frame_type='shout',translation='ไป')])]
        path=self.path.with_suffix('.mtp')
        write_mtp(path,pages,{},stop,lambda e:None)
        bubbles=read_mtp(path,stop,lambda e:None,self.path.parent/'unpacked')[0][0].bubbles
        self.assertEqual([(b.frame_type,b.frame_type_locked,b.translation) for b in bubbles],
                         [('phone',True,'โทร'),('thought',True,'อืม'),('shout',False,'ไป')])

    def test_smartstitch_15000px_seam_edge_and_far_offset_mtp(self):
        import math
        import threading
        from manhwa_ocr import read_mtp,write_mtp

        folder=self.path.parent
        pages=[]
        def pt(cx,cy,rx,ry,i,n,f):
            a=2*math.pi*i/n
            return round(cx+rx*f*math.cos(a)),round(cy+ry*f*math.sin(a))

        # 5 source pages (480x2000) -> SmartStitch at width=720 produces one 720x15000 strip.
        # Page 1: normal oval; Page 2: spiky shout across y=5120 tile seam;
        # Page 3: radial thought; Page 4: page-edge touching balloon; Page 5: large far-offset balloon.
        specs=[
            ('normal', (240,1467), (140,108), [163,1410,230,1437], 'normal'),
            ('shout',  (240,1467), (140,108), [187,1419,253,1447], 'shout'),
            ('thought',(240,1467), (140,108), [187,1419,253,1447], 'thought'),
            ('edge',   (133,1467), (147,147), [50,1397,110,1423],  'normal'),
            ('far',    (240,1000), (193,147), [97,930,150,957],    'normal'),
        ]
        for idx,(kind,(cx,cy),(rx,ry),box,_) in enumerate(specs):
            im=Image.new('RGB',(480,2000),'#c0c0c0')
            draw=ImageDraw.Draw(im)
            if kind=='shout':
                star=[pt(cx,cy,rx,ry,i,48,1 if i%2==0 else .77) for i in range(48)]
                draw.polygon(star,fill='white',outline='black',width=3)
            elif kind=='thought':
                draw.ellipse((cx-rx,cy-ry,cx+rx,cy+ry),fill='white')
                for i in range(54):
                    draw.line((pt(cx,cy,rx,ry,i,54,.87),pt(cx,cy,rx,ry,i,54,1.12)),fill='black',width=2)
            else:
                draw.ellipse((cx-rx,cy-ry,cx+rx,cy+ry),fill='white',outline='black',width=3)
            draw.text((box[0]+4,box[1]+4),'TXT',fill='black',font_size=18)
            p_path=folder/f'long_{idx}.png'
            im.save(p_path)
            pages.append(Page(str(p_path),[Bubble(box,text='TXT',translation='ไปกันเลย',frame_type='normal')]))

        stop=threading.Event()
        mtp=folder/'long_15k.mtp'
        write_mtp(mtp,pages,{},stop,lambda e:None)
        imported=read_mtp(mtp,stop,lambda e:None,folder/'unpacked_15k')[0]
        self.assertTrue(all(not b.frame_type_locked for p in imported for b in p.bubbles))

        self.mapping.update(normal=tl.FONT_NAME,shout='Itim',thought='FC Muffin')
        tl.save_font_presets({'test':self.mapping})
        studio.save('imp15k','Long15k',imported,{},font_preset='test')
        key=studio.stitch_project('imp15k',{'height':15500,'width':720})['key']
        stitched,_=studio.load(key)
        self.assertEqual(len(stitched),1)
        with Image.open(stitched[0].path) as stitched_im:
            self.assertEqual(stitched_im.size,(720,15000))

        result=studio.apply_font_preset(key,{'preset':'test'})
        self.assertEqual(result['laid_out'],5,result)
        bubbles=studio.load(key)[0][0].bubbles
        # Expected scaled centers on the 720x15000 stitched strip (scale = 720/480 = 1.5, each page = 3000px).
        expected=[
            ('normal',  (360, 2200), 14),
            ('shout',   (360, 5200), 14),
            ('thought', (360, 8200), 14),
            ('normal',  (200, 11200), 22),
            ('normal',  (360, 13500), 16),
        ]
        for b,(want_type,(tx,ty),tol) in zip(bubbles,expected):
            self.assertEqual(b.frame_type,want_type)
            self.assertEqual(b.text_layer['font'],tl.resolve_font(self.mapping[want_type]).postscript)
            x,y,r,bt=b.text_layer['box']
            self.assertAlmostEqual((x+r)/2,tx,delta=tol)
            self.assertAlmostEqual((y+bt)/2,ty,delta=tol)
