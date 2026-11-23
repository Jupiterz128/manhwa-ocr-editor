import unittest,sys
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bubble_cleaner import LamaModel

class GPUFallbackTests(unittest.TestCase):
    def model(self):
        obj=object.__new__(LamaModel)
        class OOM(RuntimeError):pass
        obj.torch=SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=OOM,empty_cache=Mock()),
            device=lambda name:SimpleNamespace(type=name),set_num_threads=Mock(),get_num_threads=lambda:8)
        obj.device=SimpleNamespace(type='cuda');obj.model=Mock();obj.fallback_reason=''
        return obj,OOM

    def test_oom_retries_same_inputs_on_cpu(self):
        obj,oom=self.model();image=object();mask=object();result=object();gpu_model=obj.model
        obj._predict=Mock(side_effect=[oom('full'),result])
        self.assertIs(obj(image,mask),result)
        self.assertEqual(obj.device.type,'cpu');self.assertTrue(obj.fallback_reason)
        self.assertEqual(obj._predict.call_count,2)
        for args in obj._predict.call_args_list:self.assertEqual(args.args,(image,mask))
        gpu_model.to.assert_called_once_with(obj.device)
        self.assertIs(obj.model,gpu_model.to.return_value)
        obj.torch.cuda.empty_cache.assert_called_once()

    def test_other_failures_are_not_hidden(self):
        obj,_=self.model();obj._predict=Mock(side_effect=RuntimeError('invalid model'))
        with self.assertRaisesRegex(RuntimeError,'invalid model'):obj(object(),object())
        self.assertEqual(obj._predict.call_count,1)

if __name__=='__main__':unittest.main()
