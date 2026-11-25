import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageDraw
from bubble_cleaner import auto_region
from manhwa_ocr import Bubble, Page
from text_style_sampler import effects, sample_style
import web_api as studio


class SafetyTests(unittest.TestCase):
    def test_black_outline_survives(self):
        image = Image.new('RGB',(400,300),'white')
        draw = ImageDraw.Draw(image)
        draw.ellipse((100,80,300,220),outline='black',width=5)
        draw.text((180,140),'TEXT',fill='black')
        b = Bubble([100,80,301,221],text='TEXT',score=.95,
                   lines=[dict(text='TEXT',box=[178,138,210,154])])
        region = auto_region(image,b)
        self.assertTrue(region.mask.any())
        walls = Image.new('L',image.size)
        ImageDraw.Draw(walls).ellipse((100,80,300,220),outline=255,width=5)
        self.assertFalse(np.any(np.asarray(walls.crop(region.box)) & region.safe))

    def test_invalid_or_oversized_detections(self):
        image = Image.new('RGB',(400,300),'white')
        for text,score in [('hello',.9),('♥',1),('',1),('hello',0)]:
            box = [0,0,400,300] if text=='hello' and score else [50,50,100,100]
            self.assertIsNone(auto_region(image,Bubble(box,text=text,score=score)))

        draw=ImageDraw.Draw(image)
        draw.rectangle((10,10,45,280),fill='black')
        draw.text((185,125),'YES',fill='black')
        bubble=Bubble([0,0,400,300],text='YES',score=.9,
                      lines=[dict(text='YES',box=[180,120,215,145])])
        reg=auto_region(image,bubble)
        self.assertTrue(reg.mask.any())
        self.assertGreater(reg.box[0],45)
        y,x=np.nonzero(reg.auto_limit)
        self.assertGreaterEqual((x+reg.box[0]).min(),172)
        self.assertLessEqual((y+reg.box[1]).max(),153)

    def test_diagonal_core_normal(self):
        mask=np.zeros((130,160),np.uint8)
        cv2.putText(mask,'ABC',(12,85),cv2.FONT_HERSHEY_SIMPLEX,2,255,8)
        y,x=np.nonzero(mask)
        for angle,axis in [(45,x-y),(135,x+y)]:
            image=np.full((130,160,3),255,np.uint8)
            t=(axis-axis.min())/np.ptp(axis)
            image[y,x]=t[:,None]*np.array([165,174,83])
            b=Bubble([5,20,155,100],frame_type='normal')
            g=sample_style(Image.fromarray(image),None,b,include_color=True)['gradient']
            self.assertTrue(g['enabled'])
            self.assertEqual(g['angle'],angle)
            self.assertLess(max(g['start_color']),30)
            self.assertGreater(g['end_color'][1],140)

    def test_page_font_and_restore(self):
        self.assertEqual(effects()['stroke'],dict(enabled=False,color=[255]*3,size=3,position='outside',opacity=100,blend_mode='Normal'))
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio,'ROOT',Path(tmp)):
            source=Path(tmp)/'page.png'
            Image.new('RGB',(180,120),'white').save(source)
            bubbles=[Bubble([10,10,40,40]),Bubble([60,60,80,80])]
            pages=[Page(str(source),bubbles),Page(str(source),[Bubble([10,10,40,40])])]
            studio.save('chapter','Chapter',pages,{})
            result=studio.action('chapter',0,'page_font',{'font':'Itim'})
            loaded,_=studio.load('chapter')
            self.assertEqual([b.font_choice for b in loaded[0].bubbles],['Itim']*2)
            self.assertEqual(loaded[1].bubbles[0].font_choice,pages[1].bubbles[0].font_choice)
            studio.action('chapter',0,'page_font',{'fonts':result['previous']})
            loaded,_=studio.load('chapter')
            self.assertEqual([b.font_choice for b in loaded[0].bubbles],[b.font_choice for b in bubbles])
