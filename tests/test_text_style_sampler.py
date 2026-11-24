import unittest
from types import SimpleNamespace
import cv2
import numpy as np
from PIL import Image
from text_style_sampler import effects, sample_style, styled_tile


class SourceEffectsTests(unittest.TestCase):
    def setUp(self):
        self.mask = np.zeros((90, 240), np.uint8)
        cv2.putText(self.mask, 'HEY', (12, 68), cv2.FONT_HERSHEY_SIMPLEX,
                    2, 255, 9, cv2.LINE_8)
        self.bg = np.full((*self.mask.shape, 3), 255, np.uint8)
        self.bubble = SimpleNamespace(box=[5,8,150,79], lines=[], frame_type='normal')

    def test_plain_on_white(self):
        rgb = self.bg.copy()
        rgb[self.mask > 0] = 0
        for cleaned in (None, Image.fromarray(self.bg)):
            self.assertEqual(sample_style(Image.fromarray(rgb), cleaned, self.bubble), effects())

    def test_outlined_horizontal_gradient(self):
        self.bubble.frame_type = 'system'
        rgb = self.bg.copy()
        outline = cv2.dilate(self.mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(7,7)))
        rgb[outline > 0] = (10,10,10)
        ys,xs = np.nonzero(self.mask)
        t=(xs-xs.min())/np.ptp(xs)
        rgb[ys,xs]=np.column_stack((200-150*t,40+170*t,120+0*t))
        for clean in (None, Image.fromarray(self.bg)):
            result=sample_style(Image.fromarray(rgb),clean,self.bubble)
            self.assertTrue(result['stroke']['enabled'],result)
            self.assertEqual(result['stroke']['color'],[10,10,10])
            self.assertTrue(result['gradient']['enabled'],result)
            self.assertEqual(result['gradient']['angle'],0)
            tile=Image.fromarray(self.mask).convert('RGBA')
            tile.putalpha(Image.fromarray(self.mask))
            preview=styled_tile(tile,result)
            self.assertGreater(preview.width,tile.width)
            self.assertGreater(np.sum(np.asarray(preview)[:,:,3]>0),len(xs))
