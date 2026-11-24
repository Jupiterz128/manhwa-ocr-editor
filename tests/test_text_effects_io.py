import os
import tempfile
import threading
import unittest
from pathlib import Path
import test_text30
from manhwa_ocr import Page, write_mtp, read_mtp
from text_layout import layout_text
from text_style_sampler import effects


def styled_fixture():
    im,b=test_text30.LayoutTests().fixture()
    b.text_layer=layout_text(im,b)
    b.text_layer.update(stroke=dict(enabled=True,color=[30,50,100],size=3),
                        gradient=dict(enabled=True,start_color=[255,20,0],end_color=[0,200,255],angle=45),
                        manual_effects=True)
    return im,b


class RoundTripTests(unittest.TestCase):
    def test_mtp_effects(self):
        im,b=styled_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'01.png'
            im.save(path)
            project=path.with_suffix('.mtp')
            cancel=threading.Event()
            write_mtp(project,[Page(str(path),[b])],{},cancel,lambda msg:None)
            pages,_,_=read_mtp(project,cancel,lambda msg:None,Path(tmp)/'unpacked')
            loaded=pages[0].bubbles[0].text_layer
            self.assertEqual(effects(loaded),effects(b.text_layer))
            self.assertTrue(loaded['manual_effects'])

    @unittest.skipUnless(os.environ.get('TEST_PHOTOPEA_FX')=='1','opt-in live Photopea export')
    def test_photopea_live_effects(self):
        from photoshop_export import export_psd
        from psd_tools import PSDImage
        im,b=styled_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'style.psd'
            export_psd(im,Page('',[b]),out,backend='photopea')
            with out.open('rb') as stream:
                self.assertEqual(stream.read(4),b'8BPS')
            doc=PSDImage.open(out)
            layer=next(l for l in doc.descendants() if l.kind=='type')
            self.assertEqual({e.name for e in layer.effects if e.enabled},{'Stroke','GradientOverlay'})
            fx=layer.tagged_blocks.get_data(b'lfx2')
            self.assertEqual(float(fx[b'FrFX'][b'Sz  ']),3)
            grad=fx[b'GrFl']
            self.assertEqual(float(grad[b'Angl']),45)
            self.assertEqual([int(s[b'Lctn']) for s in grad[b'Grad'][b'Clrs']],[0,4096])

