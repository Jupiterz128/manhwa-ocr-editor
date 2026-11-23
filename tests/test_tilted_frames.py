"""Geometry, small punctuation and numeric PSD naming regressions."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from PIL import Image,ImageDraw
from manhwa_ocr import Bubble,Line
from frame_geometry import *
from bubble_cleaner import prepare_region,Cleaner
from clean_quality import clean_checked


class TiltedTests(unittest.TestCase):
    def test_perspective_crop_and_ocr_roundtrip(self):
        q=[[50,70],[230,25],[250,115],[70,150]]
        b=Bubble(quad_bounds(q),quad=q)
        crop,inverse=ocr_crop(Image.new('RGB',(300,200),'white'),b)
        corners=box_quad([0,0,*crop.size])
        self.assertTrue(np.allclose(map_points(corners,inverse),q,atol=.001))
        line=Line('WORDS',[10,12,100,30],.4,'test',[[10,12],[100,12],[100,30],[10,30]])
        original=np.array(line.quad)
        move_line(line,inverse)
        self.assertTrue(np.allclose(map_points(line.quad,np.linalg.inv(inverse)),original,atol=.001))
        self.assertEqual(line.box,quad_bounds(line.quad))

    def test_tile_offset_moves_polygon_and_box_together(self):
        l=Line('tiny',[10,20,100,40],.01,'test',[[10,25],[95,20],[100,35],[15,40]])
        move_line(l,translation(12,1800))
        self.assertEqual(l.box,[22,1820,112,1840])
        self.assertEqual(l.quad[0],[22,1825])

    def test_invalid_corners_are_rejected(self):
        self.assertFalse(valid_quad([[0,0],[50,50],[50,0],[0,50]]))
        self.assertFalse(valid_quad([[0,0],[1,0],[1,1],[0,1]]))
        self.assertFalse(valid_quad([[0,0],[50,0],[float('nan'),50],[0,50]]))

    def test_tilted_mask_is_confined_and_has_background_context(self):
        im=Image.new('RGB',(320,240),'#94b6c8');d=ImageDraw.Draw(im)
        q=[[45,95],[250,40],[270,130],[65,185]]
        d.polygon(q,fill='white')
        # Several dark strokes, including punctuation in the last position.
        for x in range(95,211,18):
            y=round(105-(x-95)*.27);d.rectangle((x,y,x+4,y+13),fill='black')
        d.ellipse((220,90,223,93),fill='black')
        b=Bubble(quad_bounds(q),quad=q)
        region=prepare_region(im,b)
        allowed=np.zeros_like(region.mask);paint_shape(allowed,{'quad':q},region.box[:2])
        self.assertFalse(np.any(region.mask[allowed==0]));self.assertGreater(np.count_nonzero(region.mask),100)
        out,used,method=Cleaner().clean(region,'solid')
        original=np.array(region.image);result=np.array(out)
        self.assertTrue(np.array_equal(result[used==0],original[used==0]))
        self.assertTrue(np.all(result[used>0]>240))

    def test_short_y_with_false_outline_read_keeps_white_balloon(self):
        im=Image.new('RGB',(360,330),'black');d=ImageDraw.Draw(im)
        d.ellipse((80,75,280,270),fill='white')
        d.line((158,150,170,165,182,145),fill='black',width=4)
        d.line((170,165,167,179),fill='black',width=4)
        for x in [184,194,204]:d.ellipse((x,176,x+3,179),fill='black')
        b=Bubble([78,73,283,273],text='Y...',lines=[
            {'text':'Y...','box':[154,140,212,183]},
            {'text':'(")','box':[78,73,283,270],'score':.99}])
        region=prepare_region(im,b)
        self.assertTrue(region.boundary_found)
        self.assertLess(np.count_nonzero(region.mask),np.count_nonzero(region.safe)*.65)
        result,used,method,report=clean_checked(Cleaner(),region,reader=lambda image:[])
        self.assertEqual(method,'solid')
        self.assertFalse(report['needs_review'])
        arr=np.array(result);original=np.array(region.image)
        self.assertTrue(np.array_equal(arr[used==0],original[used==0]))
        self.assertTrue(np.all(arr[used>0]==255))
        self.assertFalse(outline_read({'text':'...','box':[180,172,212,184]},b.box))

    def test_numeric_psd_names_and_fallback(self):
        for path,number,expected in [('67_1.PNG',9,'01'),('01_4.png',1,'04'),('67_123.jpg',1,'123'),('004.png',1,'04'),('cover.png',3,'03')]:
            self.assertEqual(psd_stem(path,number),expected)

    def test_punctuation_on_uniform_pixels_does_not_expand_mask(self):
        from bubble_cleaner import CleanRegion
        im=Image.new('RGB',(200,150),'white');d=ImageDraw.Draw(im)
        d.ellipse((97,73,100,76),fill='black')
        mask=np.zeros((150,200),np.uint8);mask[65:85,90:110]=255
        safe=np.zeros_like(mask);safe[10:-10,10:-10]=255
        r=CleanRegion([0,0,200,150],im,mask,safe,True)
        out,used,method,report=clean_checked(Cleaner(),r,reader=lambda image:[{'text':"'",'box':[85,60,125,95]}])
        self.assertEqual(report['passes'],1);self.assertFalse(report['remaining'])
        self.assertTrue(np.array_equal(used,mask))

    def test_flat_tilted_context_and_draft_roundtrip(self):
        import json
        from bubble_cleaner import CleanRegion,encode_region,decode_region
        im=Image.new('RGB',(200,100),(198,218,234))
        mask=np.zeros((100,200),np.uint8);mask[12:25,18:120]=255
        safe=np.zeros_like(mask);safe[10:90,10:190]=255
        r=CleanRegion([0,0,200,100],im,mask,safe,False,manual_frame=True)
        restored=decode_region(json.loads(json.dumps(encode_region(r))))
        self.assertTrue(restored.manual_frame)
        cleaner=Cleaner();out,used,method=cleaner.clean(restored)
        self.assertEqual(method,'solid');self.assertTrue(cleaner.last_diagnostics['flat_tilted_context'])
        self.assertTrue(np.array_equal(np.array(out),np.array(im)))
        old=encode_region(r);old.pop('manual_frame')
        self.assertFalse(decode_region(old).manual_frame)


if __name__=='__main__':unittest.main()
