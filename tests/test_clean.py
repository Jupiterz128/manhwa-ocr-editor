import sys,tempfile,unittest,threading
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bubble_cleaner import *


class CleanTests(unittest.TestCase):
    def fixture(self,color='white',light_text=False):
        im=Image.new('RGB',(300,220),'#555555');d=ImageDraw.Draw(im)
        d.ellipse((20,20,280,200),fill=color,outline='black',width=4)
        d.text((95,95),'HELLO WORLD',fill='white' if light_text else 'black')
        bubble=SimpleNamespace(box=[20,20,281,201],lines=[{'box':[90,90,200,112]}])
        return im,bubble

    def test_flat_white_and_colored_fill_preserves_border(self):
        for color,light in [('white',False),('#ffadc2',True),('#203040',True)]:
            im,b=self.fixture(color,light);r=prepare_region(im,b)
            self.assertTrue(r.boundary_found)
            before=np.asarray(r.image).copy()
            out,mask,method=Cleaner().clean(r)
            self.assertEqual(method,'solid')
            expected=np.array(Image.new('RGB',(1,1),color).getpixel((0,0)))
            self.assertTrue(np.all(np.asarray(out)[mask>0]==expected))
            self.assertTrue(np.array_equal(np.asarray(out)[mask==0],before[mask==0]))
            self.assertFalse(np.any(mask[r.safe==0]))

    def test_gradient_keeps_variation_instead_of_flat_rectangle(self):
        y,x=np.mgrid[:150,:250]
        gray=(100+x*.35+y*.15).astype(np.uint8)
        expected=np.stack([gray,gray,gray],axis=2)
        arr=expected.copy();arr[65:80,70:180]=0
        mask=np.zeros((150,250),np.uint8);mask[63:82,68:182]=255
        safe=np.full(mask.shape,255,np.uint8)
        r=CleanRegion([0,0,250,150],Image.fromarray(arr),mask,safe,True)
        out,used,method=Cleaner().clean(r)
        self.assertEqual(method,'gradient')
        delta=np.abs(np.asarray(out).astype(float)-expected)
        self.assertLess(float(delta[used>0].mean()),6)
        self.assertGreater(float(np.asarray(out)[used>0].std()),8)
        self.assertTrue(np.array_equal(np.asarray(out)[used==0],arr[used==0]))

    def test_untrusted_model_output_cannot_change_unmasked_pixels(self):
        im,b=self.fixture();r=prepare_region(im,b)
        cleaner=Cleaner();cleaner.lama=lambda image,mask:Image.new('RGB',(image.width+(-image.width)%8,image.height+(-image.height)%8),'red')
        out,mask,method=cleaner.clean(r,'lama')
        self.assertEqual(out.size,r.image.size)
        self.assertTrue(np.array_equal(np.asarray(out)[mask==0],np.asarray(r.image)[mask==0]))
        self.assertTrue(np.all(np.asarray(out)[mask>0]==[255,0,0]))

    def test_no_ocr_coordinates_use_visual_fallback_and_empty_is_explicit(self):
        im,b=self.fixture();b.lines=[];r=prepare_region(im,b)
        self.assertTrue(np.any(r.mask));Cleaner().clean(r)
        r.mask[:]=0
        with self.assertRaisesRegex(ValueError,'มาสก์'):Cleaner().clean(r)

    def test_cancel_before_clean(self):
        im,b=self.fixture();r=prepare_region(im,b);cancel=threading.Event();cancel.set()
        with self.assertRaises(InterruptedError):Cleaner().clean(r,cancel=cancel)

    def test_patch_roundtrip_and_exclusive_export(self):
        im,b=self.fixture();original=np.asarray(im).copy();r=prepare_region(im,b)
        out,mask,method=Cleaner().clean(r)
        patch=encode_patch(r.box,out,mask,method,'bubble')
        composed=compose_page(im,[patch])
        self.assertTrue(np.array_equal(np.asarray(im),original))
        self.assertFalse(np.array_equal(np.asarray(composed),original))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'clean.png';export_cleaned(im,[patch],path);saved=path.read_bytes()
            with Image.open(path) as saved_image:
                self.assertEqual(saved_image.size,im.size)
                self.assertEqual(saved_image.format,'PNG')
            with self.assertRaises(FileExistsError):export_cleaned(im,[patch],path)
            self.assertEqual(path.read_bytes(),saved)
        broken=dict(patch,box=[0,0,1,1])
        with self.assertRaises(ValueError):decode_patch(broken)

    def test_user_color_and_guard_are_applied_only_inside_safe_mask(self):
        im,b=self.fixture();r=prepare_region(im,b)
        r.mask[:8,:30]=255  # Attempt to paint beyond protected contour.
        out,mask,_=Cleaner().clean(r,'solid',(10,20,30))
        self.assertFalse(np.any(mask[r.safe==0]))
        self.assertTrue(np.all(np.asarray(out)[mask>0]==[10,20,30]))

    def test_uncertain_boundary_is_marked_for_manual_review(self):
        im=Image.new('RGB',(300,200),'white')
        ImageDraw.Draw(im).text((100,80),'TEXT',fill='black')
        b=SimpleNamespace(box=[60,40,240,150],lines=[{'box':[95,75,140,95]}])
        self.assertFalse(prepare_region(im,b).boundary_found)

    def test_full_line_mask_covers_thick_letters_and_halo(self):
        im,b=self.fixture();d=ImageDraw.Draw(im)
        d.rectangle((100,92,125,115),fill='black')
        b.lines=[{'box':[95,90,160,117],'score':.01}]
        r=prepare_region(im,b)
        x,y=112-r.box[0],102-r.box[1]
        self.assertEqual(r.mask[y,x],255)
        out,mask,method=Cleaner().clean(r)
        self.assertEqual(out.getpixel((x,y)),(255,255,255))

    def test_cubic_gradient_reconstructs_large_missing_area(self):
        yy,xx=np.mgrid[:240,:320];y=yy/239;x=xx/319
        expected=np.clip(np.stack([70+110*y+20*y**3,50+130*y+10*x,100+90*y+20*x*y],axis=2),0,255).astype(np.uint8)
        mask=np.zeros((240,320),np.uint8);mask[80:155,70:260]=255
        arr=expected.copy();arr[mask>0]=20
        r=CleanRegion([0,0,320,240],Image.fromarray(arr),mask,np.full_like(mask,255),True)
        out,used,method=Cleaner().clean(r)
        self.assertEqual(method,'gradient')
        self.assertLess(float(np.abs(np.asarray(out).astype(float)-expected)[used>0].mean()),1.0)
        self.assertTrue(np.array_equal(np.asarray(out)[used==0],arr[used==0]))

    def test_texture_auto_uses_available_lama_without_flattening(self):
        rng=np.random.default_rng(2);arr=rng.integers(20,220,(120,160,3),dtype=np.uint8)
        mask=np.zeros((120,160),np.uint8);mask[50:70,60:100]=255
        r=CleanRegion([0,0,160,120],Image.fromarray(arr),mask,np.full_like(mask,255),True)
        c=Cleaner();c.lama=lambda im,ma:Image.new('RGB',im.size,'blue')
        out,used,method=c.clean(r)
        self.assertEqual(method,'lama')
        self.assertTrue(np.array_equal(np.asarray(out)[used==0],arr[used==0]))

    def test_manual_solid_fill_can_cover_entire_safe_interior(self):
        im,b=self.fixture();r=prepare_region(im,b);r.mask=r.safe.copy()
        out,mask,method=Cleaner().clean(r,'solid',(255,255,255))
        self.assertTrue(np.all(np.asarray(out)[mask>0]==255))


if __name__=='__main__':unittest.main()
