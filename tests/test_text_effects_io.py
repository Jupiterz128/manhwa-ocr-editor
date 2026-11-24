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


    @unittest.skipUnless(os.environ.get('TEST_PHOTOPEA_FX')=='1','opt-in live Photopea batch')
    def test_reused_session_recovers_and_keeps_lossless_editable_layers(self):
        import numpy as np
        from PIL import Image
        from bubble_cleaner import encode_patch
        from photopea_export import PhotopeaSession, export_psd_photopea
        from psd_tools import PSDImage

        image,bubble=styled_fixture()
        page=Page('',[bubble])
        with tempfile.TemporaryDirectory() as tmp, PhotopeaSession() as session:
            tab=session._page
            for idx in range(3):
                if idx==2:
                    tab.close()
                color=(40*idx,140,50)
                patch=Image.new('RGB',(12,12),color)
                page.clean_patches=[encode_patch([0,0,12,12],patch,np.full((12,12),255,np.uint8),'solid',bubble.id)]
                dest=Path(tmp)/f'{idx}.psd'
                report=export_psd_photopea(image,page,dest,session=session)
                self.assertEqual(report['text_layers'],1)
                self.assertEqual(len(session.loaded_fonts),1)
                if idx<2:
                    self.assertIs(session._page,tab)
                else:
                    self.assertIsNot(session._page,tab)
                docs=session._run_script('app.echoToOE("documents:"+app.documents.length);')
                self.assertIn('documents:0',docs['echoes'])
                with dest.open('rb') as stream:
                    self.assertEqual(stream.read(4),b'8BPS')
                layers={layer.name:layer for layer in PSDImage.open(dest)}
                self.assertEqual(set(layers),{'TEXT - Thai','CLEAN','Background'})
                self.assertEqual(layers['Background'].topil().convert('RGB').tobytes(),image.convert('RGB').tobytes())
                expected=image.convert('RGB').copy()
                expected.paste(patch,(0,0))
                self.assertEqual(layers['CLEAN'].topil().convert('RGB').tobytes(),expected.tobytes())
                layer=layers['TEXT - Thai'][0]
                self.assertEqual(layer.kind,'type')
                self.assertEqual({e.name for e in layer.effects if e.enabled},{'Stroke','GradientOverlay'})
                style=layer.engine_dict['StyleRun']['RunArray'][0]['StyleSheet']['StyleSheetData']
                para=layer.engine_dict['ParagraphRun']['RunArray'][0]['ParagraphSheet']['Properties']
                self.assertTrue(style['AutoLeading'].value)
                self.assertAlmostEqual(float(para['AutoLeading']),1.2)
