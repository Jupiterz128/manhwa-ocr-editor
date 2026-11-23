import sys,unittest,threading,tempfile,shutil
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image,ImageDraw
from bubble_cleaner import Cleaner,CleanRegion
from clean_quality import clean_checked
from check_installation import check

class QualityTests(unittest.TestCase):
    def fixture(self):
        im=Image.new('RGB',(300,200),'white');ImageDraw.Draw(im).rectangle((120,80,160,115),fill='black')
        safe=np.zeros((200,300),np.uint8);safe[10:-10,10:-10]=255
        mask=np.zeros_like(safe);mask[75:120,115:165]=255
        return CleanRegion([100,200,400,400],im,mask,safe,True)

    def test_remaining_text_expands_mask_and_rechecks(self):
        r=self.fixture();calls=[]
        def read(out):
            calls.append(out)
            return [{'text':'missed','box':[275,280,310,315]}] if len(calls)==1 else []
        out,mask,method,report=clean_checked(Cleaner(),r,reader=read)
        self.assertEqual(report['passes'],2);self.assertEqual(len(calls),2)
        self.assertGreater(np.count_nonzero(mask),np.count_nonzero(r.mask))
        self.assertFalse(report['needs_review'])
        self.assertTrue(np.array_equal(np.asarray(out)[mask==0],np.asarray(r.image)[mask==0]))

    def test_persistent_detection_never_claims_clean(self):
        r=self.fixture()
        out,mask,method,report=clean_checked(Cleaner(),r,reader=lambda out:[{'text':'still','box':[220,280,260,315]}])
        self.assertEqual(report['passes'],4)
        self.assertTrue(report['remaining']);self.assertTrue(report['needs_review'])

    def test_ocr_failure_retains_result_and_marks_unverified(self):
        def broken(out):raise RuntimeError('OCR offline')
        out,mask,method,report=clean_checked(Cleaner(),self.fixture(),reader=broken)
        self.assertIn('offline',report['verification_error']);self.assertTrue(report['needs_review'])
        self.assertTrue(np.any(mask))

    def test_visual_check_repairs_missed_shadow_without_ocr_text(self):
        r=self.fixture();arr=np.asarray(r.image).copy();arr[120:133,150:180]=210;r.image=Image.fromarray(arr)
        out,mask,method,report=clean_checked(Cleaner(),r,reader=lambda out:[])
        self.assertTrue(np.all(np.asarray(out)[122:130,155:175]==255))
        self.assertTrue(report['visual_checked']);self.assertEqual(report['visual_residual_pixels'],0)

    def test_empty_clean_bubble_is_processed_as_no_change(self):
        r=self.fixture();r.image=Image.new('RGB',r.image.size,'white');r.mask[:]=0
        out,mask,method,report=clean_checked(Cleaner(),r,reader=lambda out:[])
        self.assertEqual(method,'no-change');self.assertEqual(report['passes'],1)
        self.assertFalse(report['needs_review']);self.assertFalse(np.any(mask))

    def test_auto_does_not_silently_fall_back_to_telea(self):
        r=self.fixture();rng=np.random.default_rng(42)
        r.image=Image.fromarray(rng.integers(0,255,(200,300,3),dtype=np.uint8))
        with tempfile.TemporaryDirectory() as d,patch('bubble_cleaner.lama_model_path',return_value=Path(d)/'absent.pt'):
            with self.assertRaisesRegex(RuntimeError,'LaMa'):Cleaner().clean(r)

    def test_mixed_installation_is_detected_without_model_import(self):
        source=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            for name in ('manhwa_ocr.py','bubble_cleaner.py','clean_editor.py','clean_quality.py','clean_diagnostics.py','text_layout.py','text_editor.py','windows_text.py','photoshop_export.py','photoshop_export.jsx','app_ui.py','sound_effects.py','frame_geometry.py'):shutil.copyfile(source/name,folder/name)
            self.assertEqual(check(folder),[])
            (folder/'clean_editor.py').write_text("COMPONENT_VERSION='2.4'\n")
            self.assertIn('clean_editor.py',check(folder)[0])

    def test_cancel_before_any_repair(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(InterruptedError):clean_checked(Cleaner(),self.fixture(),cancel=cancel)
