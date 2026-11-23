"""Automatic orientation, safe rotated Thai type, and backward compatibility."""
import sys,unittest,copy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import cv2
from PIL import Image
import manhwa_ocr as m
from frame_geometry import auto_tilt,quad_angle,quad_bounds,box_quad
from text_layout import layout_text,validate_layer,is_current,composite_text_crop


def rotated(box,angle,offset=(300,250)):
    r=np.radians(angle);basis=np.array([[np.cos(r),-np.sin(r)],[np.sin(r),np.cos(r)]])
    return (np.asarray(box_quad(box))@basis.T+offset).tolist()


class AutoTiltTests(unittest.TestCase):
    def lines(self,angle=-18):
        return [m.Line(text,quad_bounds(q),.1,'test',q) for text,q in
                [('FIRST',rotated([-120,-45,120,-15],angle)),
                 ('SECOND',rotated([-120,0,120,30],angle))]]

    def test_group_detects_angle_including_low_confidence_and_orders_lines(self):
        lines=self.lines()
        result=m.group_lines(list(reversed(lines)),[[100,100,500,400]])
        self.assertEqual(len(result),1);b=result[0]
        self.assertAlmostEqual(quad_angle(b.quad),-18,places=5)
        self.assertEqual(b.text,'FIRST SECOND')
        for line in lines:
            for p in line.quad:self.assertGreaterEqual(cv2.pointPolygonTest(np.float32(b.quad),tuple(p),True),-.01)

    def test_geometry_group_uses_line_axes_instead_of_tall_axis_bounds(self):
        result=m.group_lines(list(reversed(self.lines(-25))))
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].text,'FIRST SECOND')

    def test_straight_missing_and_conflicting_angles_not_guessed(self):
        for lines in [self.lines(0),[m.Line('OLD',[10,10,100,30])],self.lines(-20)[:1]+self.lines(20)[1:]]:
            b=m.Bubble([0,0,600,500],lines=[m.asdict(l) for l in lines]);auto_tilt(b)
            self.assertEqual(b.quad,[])

    def test_manual_corners_and_id_are_preserved(self):
        q=rotated([-140,-60,140,60],12)
        b=m.Bubble(quad_bounds(q),quad=q,lines=[m.asdict(l) for l in self.lines()]);before=m.asdict(b)
        auto_tilt(b);self.assertEqual(m.asdict(b),before)

    def test_auto_frame_never_extends_past_page_edge(self):
        q=rotated([-120,-10,120,10],-12,(120,100))
        # Shift until one OCR vertex sits on the right page boundary.
        q=np.asarray(q);q[:,0]+=300-q[:,0].max();q=q.tolist()
        line=m.Line('EDGE',quad_bounds(q),.9,'test',q)
        b=m.Bubble(line.box,lines=[m.asdict(line)])
        auto_tilt(b,(300,200));self.assertTrue(b.quad)
        self.assertTrue(np.all(np.asarray(b.quad)<=np.array([300,200])+1e-6))

    def test_auto_frame_mask_is_clipped_and_does_not_change_source(self):
        from bubble_cleaner import prepare_region
        im=Image.new('RGB',(600,500),'white');lines=self.lines()
        b=m.Bubble([100,100,500,400],lines=[m.asdict(l) for l in lines]);auto_tilt(b)
        before=im.tobytes();region=prepare_region(im,b)
        mask=np.zeros_like(region.safe)
        cv2.fillPoly(mask,[np.rint(np.asarray(b.quad)-region.box[:2]).astype(np.int32)],255)
        self.assertFalse(np.any(region.safe[mask==0]));self.assertFalse(np.any(region.mask[mask==0]))
        self.assertEqual(before,im.tobytes())

    def test_rotated_preview_stays_in_frame_and_uses_black_or_white(self):
        for angle in [-22,19]:
            for background in ['white','black']:
                im=Image.new('RGB',(600,500),background)
                q=rotated([-145,-75,145,75],angle)
                b=m.Bubble(quad_bounds(q),quad=q,translation='สวัสดีครับ\nยินดีที่ได้รู้จัก')
                b.text_layer=layout_text(im,b);validate_layer(b.text_layer)
                self.assertAlmostEqual(b.text_layer['rotation'],angle,places=5)
                self.assertEqual(b.text_layer['color'],[0,0,0] if background=='white' else [255,255,255])
                out=composite_text_crop(im,[b],(0,0));changed=np.any(np.array(im)!=np.array(out),axis=2)
                self.assertGreater(changed.sum(),200)
                inside=np.zeros((500,600),np.uint8);cv2.fillPoly(inside,[np.rint(q).astype(np.int32)],255)
                self.assertFalse(np.any(changed & (inside==0)))
                yy,xx=np.where(changed)
                self.assertAlmostEqual((xx.min()+xx.max())/2,300,delta=3)
                self.assertAlmostEqual((yy.min()+yy.max())/2,250,delta=3)
                # Covariance of each row's pixels follows the chosen tilt.
                r=np.radians(angle);normal=-xx*np.sin(r)+yy*np.cos(r)
                self.assertLess(np.ptp(normal),b.text_layer['text_size'][1]+3)

    def test_changed_corners_invalidate_layer_even_when_box_unchanged(self):
        im=Image.new('RGB',(600,500),'white');q=rotated([-145,-75,145,75],-20)
        b=m.Bubble(quad_bounds(q),quad=copy.deepcopy(q),translation='ทดสอบ')
        b.text_layer=layout_text(im,b);self.assertTrue(is_current(b))
        b.quad=rotated([-145,-75,145,75],20)
        self.assertFalse(is_current(b))
        bad=copy.deepcopy(b.text_layer);bad['rotation']=float('nan')
        with self.assertRaises(ValueError):validate_layer(bad)

    def test_old_straight_type_is_still_current(self):
        b=m.Bubble([20,20,400,180],translation='สวัสดีครับ')
        b.text_layer=layout_text(Image.new('RGB',(500,250),'white'),b)
        b.text_layer.pop('bubble_quad',None)
        self.assertTrue(is_current(b));validate_layer(b.text_layer)

    def test_bubble_only_pipeline_applies_angle_without_extra_ocr(self):
        from types import SimpleNamespace
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(m,'EasyEngine',return_value=SimpleNamespace(name='test')):
                pipeline=m.LocalPipeline(m.OCRCache(Path(folder)/'cache.db'),mode='easy',lang='en',neural=False)
            # Configure a pre-existing detector; avoid loading OCR/network models.
            pipeline.detector=SimpleNamespace(detect=lambda im:[(0,.9,[100,100,500,400])])
            engine=SimpleNamespace(name='test')
            local=self.lines();
            for line in local:m.move_line(line,m.translation(-100,-100))
            with patch.object(pipeline,'iter_engines',return_value=iter([engine])),patch.object(pipeline,'read',return_value=local) as read:
                result=pipeline.scan_bubbles(Image.new('RGB',(600,500),'white'))
            self.assertEqual(read.call_count,1)
            self.assertAlmostEqual(quad_angle(result[0].quad),-18,places=4)


if __name__=='__main__':unittest.main()
