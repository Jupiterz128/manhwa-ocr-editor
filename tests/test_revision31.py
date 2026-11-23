"""Regression checks for centred type, border protection and stable gradients."""
import sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import cv2
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bubble_cleaner import prepare_region,CleanRegion,Cleaner,smooth_background
from clean_quality import residual_mask,clean_checked
from text_layout import layout_text,rendered_tile,composite_text_crop,validate_layer,centered_rectangle
from manhwa_ocr import Bubble


class Revision31Tests(unittest.TestCase):
    def test_offset_ocr_selection_centres_on_closed_balloon(self):
        im=Image.new('RGB',(560,420),'gray');d=ImageDraw.Draw(im)
        d.ellipse((70,60,490,350),fill='white',outline='black',width=4)
        d.text((160,155),'HELLO',fill='black')
        b=Bubble([150,115,380,280],translation='ยินดีที่ได้รู้จัก',lines=[{'box':[158,150,200,169]}])
        layer=layout_text(im,b);r=layer['box']
        self.assertTrue(layer['boundary_found'])
        self.assertAlmostEqual((r[0]+r[2])/2,280,delta=4)
        self.assertAlmostEqual((r[1]+r[3])/2,205,delta=4)
        tile=rendered_tile(layer['text'],layer['font_size'])
        self.assertLessEqual(tile.width,r[2]-r[0]);self.assertLessEqual(tile.height,r[3]-r[1])

    def test_tail_does_not_pull_text_to_bottom_of_balloon(self):
        mask=np.zeros((300,320),np.uint8)
        cv2.ellipse(mask,(160,110),(130,85),0,0,360,255,-1)
        cv2.fillPoly(mask,[np.array([[155,185],[175,185],[200,295]],np.int32)],255)
        box=centered_rectangle(mask)
        self.assertAlmostEqual((box[0]+box[2])/2,160,delta=4)
        self.assertAlmostEqual((box[1]+box[3])/2,110,delta=5)
        self.assertTrue(np.all(mask[box[1]:box[3],box[0]:box[2]]))

    def test_wide_narration_is_readable_and_auto_white_on_dark(self):
        im=Image.new('RGB',(700,400),'#14151a')
        b=Bubble([120,145,575,260],translation='ฉันก็ต้องเอาชีวิตรอดไปให้ได้')
        b.text_layer=layout_text(im,b);layer=b.text_layer;validate_layer(layer)
        self.assertEqual(layer['color'],[255,255,255])
        self.assertGreater(layer['font_size'],35)
        preview=np.array(composite_text_crop(im,[b],(0,0)))
        self.assertGreater(int(preview.max()),200)
        # Rendering a white layer must not recolour the shared cached black tile.
        tile=rendered_tile(layer['text'],layer['font_size'])
        self.assertEqual(tile.getchannel('R').getextrema(),(0,0))

    def test_auto_black_on_light_and_long_thai_remains_inside(self):
        im=Image.new('RGB',(600,450),'gray');ImageDraw.Draw(im).ellipse((30,20,570,425),fill='white',outline='black',width=4)
        b=Bubble([30,20,570,425],translation='ข้อความภาษาไทยที่มีวรรณยุกต์และสระไม่ควรหลุดออกจากบับเบิ้ล '*6)
        layer=layout_text(im,b);validate_layer(layer)
        self.assertEqual(layer['color'],[0,0,0]);box=layer['box']
        tile=rendered_tile(layer['text'],layer['font_size'])
        self.assertLessEqual(tile.width,box[2]-box[0]);self.assertLessEqual(tile.height,box[3]-box[1])
        self.assertEqual(layer['text'].replace('\n','').replace(' ',''),b.translation.replace(' ',''))

    def test_closed_glyph_does_not_become_clean_boundary(self):
        im=Image.new('RGB',(300,240),'white');d=ImageDraw.Draw(im)
        d.ellipse((125,95,170,145),outline='black',width=7)
        b=Bubble([45,30,255,210],lines=[{'box':[120,90,175,150]}])
        region=prepare_region(im,b)
        self.assertFalse(region.boundary_found)
        self.assertGreater(np.count_nonzero(region.safe),10000)

    def test_open_outline_is_preserved_when_line_mask_overlaps_it(self):
        im=Image.new('RGB',(420,320),'white');d=ImageDraw.Draw(im)
        d.arc((40,30,380,280),-70,220,fill='#703d63',width=4)
        d.rectangle((90,130,310,153),fill='black')
        b=Bubble([35,25,385,285],lines=[{'box':[38,127,380,156]}])
        region=prepare_region(im,b)
        out,mask,method=Cleaner().clean(region,'solid',(255,255,255))
        source=np.array(region.image);border=(source[:,:,0]==112)&(source[:,:,1]==61)&(source[:,:,2]==99)
        self.assertTrue(np.any(border));self.assertFalse(np.any(mask[border]))
        self.assertTrue(np.array_equal(np.array(out)[border],source[border]))

    def test_residual_components_do_not_bridge_clean_space(self):
        arr=np.full((200,300,3),255,np.uint8);arr[65:74,70:80]=180;arr[130:140,225:236]=180
        mask=np.zeros((200,300),np.uint8);mask[55:85,60:90]=255;mask[120:150,215:246]=255
        region=CleanRegion([0,0,300,200],Image.fromarray(arr),mask,np.full_like(mask,255),True)
        result,checked=residual_mask(region.image,region,mask)
        self.assertTrue(checked);self.assertTrue(result[69,74]);self.assertTrue(result[134,230])
        self.assertFalse(result[101,150])

    def test_narration_glow_outside_ocr_box_is_in_mask(self):
        im=Image.new('RGB',(620,260),'#17171a');d=ImageDraw.Draw(im)
        d.rectangle((155,106,460,141),fill='#888899')
        d.text((165,110),'THERE ARE THINGS',fill='white')
        b=Bubble([150,100,465,138],lines=[{'box':[153,105,461,137]}])
        region=prepare_region(im,b)
        self.assertFalse(region.boundary_found)
        self.assertEqual(region.mask[141-region.box[1],300-region.box[0]],255)

    def test_uncertain_outline_keeps_exterior_islands_out_of_safe_area(self):
        im=Image.new('RGB',(420,320),'#8090a0');d=ImageDraw.Draw(im)
        d.ellipse((40,30,380,280),fill='white',outline='black',width=4)
        b=Bubble([35,25,385,205],lines=[{'box':[110,110,300,145]}])
        # Exercise fallback even when the closed-contour stage cannot certify it.
        with patch('bubble_cleaner.cv2.findContours',return_value=([],None)):
            region=prepare_region(im,b)
        original=np.array(region.image)
        exterior=np.all(original==[128,144,160],axis=2)
        self.assertTrue(np.any(exterior))
        self.assertFalse(np.any(region.safe[exterior]))

    def test_gradient_without_surrounding_support_uses_lama(self):
        yy,xx=np.mgrid[:180,:260]
        arr=np.repeat((80+xx*.2+yy*.1).astype(np.uint8)[:,:,None],3,axis=2)
        mask=np.zeros((180,260),np.uint8);mask[5:170,:180]=255
        arr[mask>0]=0
        region=CleanRegion([0,0,260,180],Image.fromarray(arr),mask,np.full_like(mask,255),True)
        surface,error=smooth_background(region.image,mask,region.safe)
        self.assertIsNone(surface)
        cleaner=Cleaner();cleaner.lama=lambda image,mask:Image.new('RGB',image.size,(100,100,100))
        result,used,chosen=cleaner.clean(region)
        self.assertEqual(chosen,'lama');self.assertTrue(np.array_equal(np.array(result)[used==0],arr[used==0]))

    def test_plane_prediction_has_no_colour_overshoot(self):
        yy,xx=np.mgrid[:180,:260]
        truth=np.stack([60+xx*.25,80+yy*.3,100+xx*.1+yy*.1],axis=2).astype(np.uint8)
        mask=np.zeros((180,260),np.uint8);mask[50:130,75:195]=255
        arr=truth.copy();arr[mask>0]=0
        region=CleanRegion([0,0,260,180],Image.fromarray(arr),mask,np.full_like(mask,255),True)
        out,used,chosen=Cleaner().clean(region)
        self.assertEqual(chosen,'gradient')
        self.assertLess(float(np.abs(np.array(out).astype(float)-truth)[used>0].mean()),1)
        self.assertTrue(np.array_equal(np.array(out)[used==0],arr[used==0]))


if __name__=='__main__':unittest.main()
