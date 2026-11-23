import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
import numpy as np
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bubble_cleaner import Cleaner,CleanRegion,encode_region,decode_region
from clean_quality import clean_checked
from clean_diagnostics import CleanRun,counts,summary_text


class Clean27Tests(unittest.TestCase):
    def region(self,gradient=False):
        y,x=np.mgrid[:140,:220]
        gray=(120+x*.4+y*.1).astype(np.uint8) if gradient else np.full((140,220),220,np.uint8)
        expected=np.repeat(gray[:,:,None],3,axis=2)
        mask=np.zeros(gray.shape,np.uint8);mask[45:90,65:150]=255
        arr=expected.copy();arr[mask>0]=0
        return CleanRegion([0,0,220,140],Image.fromarray(arr),mask,np.full_like(mask,255),True),expected

    def test_flat_background_with_border_and_halo_contamination(self):
        region,expected=self.region();arr=np.array(region.image)
        # Contamination is deliberately outside the selected mask; it must not be erased.
        arr[:4]=0;arr[-4:]=0;arr[:,:4]=0;arr[:,-4:]=0
        arr[39:45,60:155]=70;arr[90:97,60:155]=70
        region.image=Image.fromarray(arr)
        out,mask,chosen=Cleaner().clean(region)
        self.assertEqual(chosen,'solid')
        self.assertTrue(np.array_equal(np.array(out)[mask>0],expected[mask>0]))
        self.assertTrue(np.array_equal(np.array(out)[mask==0],arr[mask==0]))

    def test_gradient_with_border_contamination(self):
        region,expected=self.region(True);arr=np.array(region.image)
        arr[:4]=0;arr[-4:]=0;arr[:,:4]=0;arr[:,-4:]=0
        region.image=Image.fromarray(arr)
        out,mask,chosen=Cleaner().clean(region)
        self.assertEqual(chosen,'gradient')
        self.assertLess(np.abs(np.array(out).astype(float)-expected)[mask>0].mean(),1)

    def test_periodic_texture_is_not_flattened(self):
        region,_=self.region();y,x=np.mgrid[:140,:220]
        arr=np.repeat((100+((x//3+y//3)%2)*80).astype(np.uint8)[:,:,None],3,axis=2)
        region.image=Image.fromarray(arr)
        cleaner=Cleaner();cleaner.lama=MagicMock(return_value=region.image.copy())
        self.assertEqual(cleaner.clean(region)[2],'lama')
        cleaner.lama.assert_called_once()

    def test_manual_lama_does_not_require_color_samples(self):
        region,_=self.region();region.mask=region.safe.copy()
        cleaner=Cleaner();cleaner.lama=lambda image,mask:Image.new('RGB',image.size,'blue')
        with patch('bubble_cleaner.sample_background',side_effect=AssertionError('must not sample')):
            out,mask,chosen=cleaner.clean(region,'lama')
        self.assertEqual(chosen,'lama');self.assertEqual(out.getpixel((100,70)),(0,0,255))

    def test_auto_insufficient_samples_can_use_lama(self):
        region,_=self.region();region.mask=region.safe.copy()
        cleaner=Cleaner();cleaner.lama=lambda image,mask:Image.new('RGB',image.size,'blue')
        self.assertEqual(cleaner.clean(region)[2],'lama')
        self.assertIn('sample_error',cleaner.last_diagnostics)

    def test_manual_inpaint_does_not_require_flat_background(self):
        region,_=self.region()
        with patch('bubble_cleaner.sample_background',side_effect=AssertionError('must not sample')):
            self.assertEqual(Cleaner().clean(region,'inpaint')[2],'inpaint')

    def test_missing_lama_readiness_never_downloads(self):
        with tempfile.TemporaryDirectory() as folder,patch('bubble_cleaner.lama_model_path',return_value=Path(folder)/'missing.pt'),patch('bubble_cleaner.LamaModel') as model:
            status=Cleaner().check_lama()
            self.assertFalse(status['ready']);self.assertEqual(status['code'],'missing_weights')
            model.assert_not_called()

    def test_bad_checkpoint_is_not_ready_and_failure_is_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint=Path(folder)/'bad.pt';checkpoint.write_bytes(b'bad')
            with patch('bubble_cleaner.lama_model_path',return_value=checkpoint),patch('bubble_cleaner.LamaModel',side_effect=RuntimeError('bad checkpoint')) as model:
                cleaner=Cleaner()
                for _ in range(2):self.assertFalse(cleaner.check_lama()['ready'])
                self.assertEqual(model.call_count,1)
                checkpoint.write_bytes(b'changed checkpoint')
                cleaner.check_lama();self.assertEqual(model.call_count,2)

    def test_readiness_requires_inference_not_just_load(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint=Path(folder)/'bad.pt';checkpoint.touch()
            broken=MagicMock(side_effect=RuntimeError('inference failed'))
            with patch('bubble_cleaner.lama_model_path',return_value=checkpoint),patch('bubble_cleaner.LamaModel',return_value=broken):
                status=Cleaner().check_lama()
                self.assertFalse(status['ready']);self.assertIn('inference failed',status['message'])

    def test_successful_readiness_reuses_loaded_model(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint=Path(folder)/'model.pt';checkpoint.touch()
            model=MagicMock(side_effect=lambda image,mask:image.copy());model.device='cpu'
            with patch('bubble_cleaner.lama_model_path',return_value=checkpoint),patch('bubble_cleaner.LamaModel',return_value=model) as load:
                cleaner=Cleaner();self.assertTrue(cleaner.check_lama()['ready'])
                self.assertTrue(cleaner.check_lama()['ready']);self.assertIs(cleaner.lama,model)
                self.assertEqual(load.call_count,1);self.assertEqual(model.call_count,1)

    def test_draft_roundtrip_and_invalid_dimensions(self):
        region,_=self.region();encoded=encode_region(region)
        restored=decode_region(json.loads(json.dumps(encoded)))
        self.assertTrue(np.array_equal(region.mask,restored.mask))
        self.assertEqual(region.image.tobytes(),restored.image.tobytes())
        encoded['box']=[0,0,219,140]
        with self.assertRaises(ValueError):decode_region(encoded)

    def test_failure_preserves_expanded_mask(self):
        region,_=self.region();initial=region.mask.copy();calls=[]
        cleaner=Cleaner();original=cleaner.clean
        def failing(work,*args):
            calls.append(1)
            if len(calls)>1:raise RuntimeError('second repair failed')
            return original(work,*args)
        cleaner.clean=failing
        with self.assertRaisesRegex(RuntimeError,'second repair'):
            clean_checked(cleaner,region,reader=lambda out:[{'text':'left','box':[150,40,170,70]}])
        self.assertGreater(np.count_nonzero(region.mask),np.count_nonzero(initial))

    def test_uncertain_boundary_is_review_even_with_empty_ocr(self):
        region,_=self.region();region.boundary_found=False
        self.assertTrue(clean_checked(Cleaner(),region,reader=lambda out:[])[3]['needs_review'])

    def test_102_counts_are_mutually_exclusive(self):
        records=[{'status':s} for s,n in [('success',20),('review',42),('failed',40)] for _ in range(n)]
        result=counts(records,102)
        self.assertEqual(result['attempted'],102);self.assertEqual(result['outputs'],62)
        self.assertEqual(result['success'],20);self.assertEqual(result['review'],42)
        self.assertIn('ล้มเหลว 40',summary_text(result))

    def test_partial_cancel_has_pending_and_durable_reason(self):
        region,_=self.region()
        with tempfile.TemporaryDirectory() as folder:
            run=CleanRun(folder,4,{'ready':False})
            run.append({'index':1,'status':'failed','error':'specific reason'},region)
            run.append({'index':2,'status':'cancelled'})
            run.finish()
            result=json.loads((run.folder/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(result['attempted'],2);self.assertEqual(result['pending'],2)
            self.assertEqual(result['failed'],1);self.assertEqual(result['cancelled'],1)
            self.assertEqual(result['success'],0)
            saved=decode_region(json.loads((run.folder/'region-0001.json').read_text(encoding='utf-8')))
            self.assertTrue(np.array_equal(saved.mask,region.mask))
            self.assertIn('specific reason',(run.folder/'regions.jsonl').read_text(encoding='utf-8'))
