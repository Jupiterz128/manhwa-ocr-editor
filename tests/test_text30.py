import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manhwa_ocr import Bubble,Page
from text_layout import translation_template,parse_translations,largest_rectangle,layout_text,validate_layer,is_current,rendered_tile,composite_text_crop
from photoshop_export import prepare_job,export_psd
from bubble_cleaner import encode_patch,compose_page


class TranslationTests(unittest.TestCase):
    def fixture(self):
        return [Page('one.png',[Bubble([0,0,100,100],text='Hello',id='one'),Bubble([0,0,100,100],text='Hello',id='two')])]

    def test_ids_map_duplicate_sources_even_when_reordered(self):
        pages=self.fixture()
        text='รหัส: two\nคำต้นฉบับ: Hello\nคำแปลไทย: สอง\n\nรหัส: one\nคำต้นฉบับ: Hello\nคำแปลไทย: หนึ่ง'
        self.assertEqual(parse_translations(text,pages),{'one':'หนึ่ง','two':'สอง'})
        self.assertEqual(pages[0].bubbles[0].translation,'')

    def test_legacy_pairs_accept_only_unique_matching_source(self):
        pages=self.fixture()
        with self.assertRaisesRegex(ValueError,'ซ้ำ'):parse_translations('คำต้นฉบับ: Hello\nคำแปลไทย: สวัสดี',pages)
        pages[0].bubbles.pop()
        self.assertEqual(parse_translations('คำต้นฉบับ: Hello\nคำแปลไทย: สวัสดี',pages),{'one':'สวัสดี'})

    def test_import_rejects_unknown_and_duplicate_ids(self):
        pages=self.fixture()
        for text in ['รหัส: bad\nคำต้นฉบับ: Hello\nคำแปลไทย: ไทย',
                     ('รหัส: one\nคำต้นฉบับ: Hello\nคำแปลไทย: \n')*2]:
            with self.assertRaises(ValueError):parse_translations(text,pages)

    def test_exact_id_accepts_changed_source_and_typo_without_overwriting_source(self):
        pages=self.fixture();before=copy.deepcopy(pages);notes=[]
        text='รหัส: one\nคำต้นฉbับ: Changed elsewhere\nคำแปลไทย: สวัสดี'
        self.assertEqual(parse_translations(text,pages,notes),{'one':'สวัสดี'})
        self.assertEqual(len(notes),2)
        self.assertEqual(pages,before)

    def test_exact_id_accepts_missing_source_and_alternative_labels(self):
        pages=self.fixture();notes=[]
        self.assertEqual(parse_translations('ID: two\nคำแปล: สอง',pages,notes),{'two':'สอง'})
        self.assertTrue(notes)
        self.assertEqual(parse_translations('รหัสกรอบ: one\nคำต้นฉบับ: Hello\nคำแปลไท: หนึ่ง',pages),{'one':'หนึ่ง'})

    def test_no_id_still_requires_unique_unchanged_source(self):
        pages=self.fixture();pages[0].bubbles.pop()
        with self.assertRaises(ValueError):parse_translations('คำต้นฉบับ: Hallo\nคำแปลไทย: สวัสดี',pages)
        self.assertEqual(parse_translations('คำต้นฉbับ: Hello\nคำแปลไทย: สวัสดี',pages),{'one':'สวัสดี'})

    def test_id_is_not_fuzzy_matched_and_failure_returns_no_partial_notices(self):
        pages=self.fixture();notes=[]
        text='รหัส: one\nคำต้นฉbับ: Changed\nคำแปลไทย: ไทย\nรหัส: tw0\nคำต้นฉบับ: Hello\nคำแปลไทย: สอง'
        with self.assertRaisesRegex(ValueError,'tw0'):parse_translations(text,pages,notes)
        self.assertEqual(notes,[])
        self.assertTrue(all(not b.translation for b in pages[0].bubbles))

    def test_multiline_and_blank_translation_roundtrip(self):
        pages=self.fixture();pages[0].bubbles[0].translation='บรรทัดหนึ่ง\nบรรทัดสอง'
        self.assertEqual(parse_translations(translation_template(pages),pages),{'one':'บรรทัดหนึ่ง\nบรรทัดสอง'})

    def test_formatted_labels_from_translation_service_preserve_content(self):
        pages=self.fixture()
        for text in [
            '```txt\n**รหัส:** one\n**คำต้นฉบับ:** Hello\n**คำแปลไทย:** สวัสดี\n```',
            '- **รหัส**： one\n- **คำต้นฉบับ**： Hello\n- **คำแปลไทย**： สวัสดี',
            '1. __รหัส:__ one\n2. __คำต้นฉบับ:__ Hello\n3. __คำแปลไทย:__ สวัสดี']:
            with self.subTest(text=text):self.assertEqual(parse_translations(text,pages),{'one':'สวัสดี'})
        pages[0].bubbles[0].text='**Hello**'
        self.assertEqual(parse_translations('รหัส: one\nคำต้นฉบับ: **Hello**\nคำแปลไทย: **สวัสดี**',pages),{'one':'**สวัสดี**'})

    def test_invalid_import_names_line_and_does_not_change_project(self):
        pages=self.fixture()
        before=copy.deepcopy(pages)
        with self.assertRaises(ValueError) as error:
            parse_translations('\nนี่คือคำแปลของคุณ\nรหัส: one',pages)
        self.assertEqual(error.exception.line,2)
        self.assertIn('นี่คือคำแปล',str(error.exception))
        self.assertEqual(pages,before)

    def test_missing_field_and_empty_template_explain_what_to_fix(self):
        pages=self.fixture()
        with self.assertRaisesRegex(ValueError,'ขาดหัวข้อ คำแปลไทย:'):
            parse_translations('รหัส: one\nคำต้นฉบับ: Hello',pages)
        with self.assertRaisesRegex(ValueError,'ไฟล์ที่แปลเสร็จแล้ว'):
            parse_translations(translation_template(pages),pages)

    def test_rectangle_never_crosses_bubble_interior(self):
        mask=np.zeros((100,140),np.uint8)
        import cv2
        cv2.ellipse(mask,(70,50),(60,40),0,0,360,255,-1)
        x0,y0,x1,y1=largest_rectangle(mask)
        self.assertGreater((x1-x0)*(y1-y0),3000)
        self.assertTrue(np.all(mask[y0:y1,x0:x1]>0))


@unittest.skipUnless(os.name=='nt','Windows text shaping')
class LayoutTests(unittest.TestCase):
    def fixture(self):
        image=Image.new('RGB',(600,380),'gray');ImageDraw.Draw(image).ellipse((20,20,580,360),fill='white',outline='black',width=4)
        return image,Bubble([20,20,580,360],text='Hello',translation='คำแปลภาษาไทยที่แก้ไขได้')

    def test_thai_layout_fits_uses_required_style_and_preserves_pixels(self):
        image,b=self.fixture();before=image.tobytes();b.text_layer=layout_text(image,b)
        layer=b.text_layer;validate_layer(layer)
        self.assertEqual(layer['font'],'THSarabunNew-Bold');self.assertTrue(layer['auto_leading'])
        self.assertEqual(layer['color'],[0,0,0]);self.assertTrue(is_current(b))
        self.assertEqual(layer['tracking'],0);self.assertEqual(layer['kerning'],'metrics')
        self.assertEqual(layer['antialias'],'sharp');self.assertEqual(layer['horizontal_scale'],100)
        self.assertEqual(layer['vertical_scale'],100);self.assertEqual(layer['baseline_shift'],0)
        self.assertEqual(layer['language'],'English: UK')
        tile=rendered_tile(layer['text'],layer['font_size']);box=layer['box']
        self.assertLessEqual(tile.width,box[2]-box[0]);self.assertLessEqual(tile.height,box[3]-box[1])
        preview=composite_text_crop(image,[b],(0,0));self.assertNotEqual(preview.tobytes(),before)
        self.assertEqual(image.tobytes(),before)
        a=np.array(image);out=np.array(preview);allowed=np.zeros(a.shape[:2],bool)
        allowed[box[1]:box[3],box[0]:box[2]]=True
        self.assertTrue(np.array_equal(a[~allowed],out[~allowed]))

    def test_more_text_reduces_font_and_keeps_combining_marks(self):
        image,b=self.fixture();short=layout_text(image,b)
        b.translation='ข้อความภาษาไทยที่มีสระและวรรณยุกต์ เช่น นี้ น้ำ ผู้รู้ '*8
        long=layout_text(image,b)
        self.assertLess(long['font_size'],short['font_size'])
        self.assertEqual(long['text'].replace('\n','').replace(' ',''),b.translation.replace(' ',''))
        self.assertFalse(any(line and '\u0e31'<=line[0]<='\u0e4e' and line[0] not in 'เแโใไๆๅ' for line in long['text'].splitlines()))

    def test_short_text_leaves_room_and_connected_balloon_stays_in_selected_lobe(self):
        image,b=self.fixture();b.translation='ได้'
        layer=layout_text(image,b)
        # A short translation remains modest, without the old 16%-of-selection
        # cap which made even large speech balloons hard to read.
        tile=rendered_tile(layer['text'],layer['font_size'])
        self.assertLess(tile.height,(b.box[3]-b.box[1])*.30)
        # A contour may extend beyond the selected lobe; text must stay in its own box.
        region=SimpleNamespace(box=[0,0,600,380],safe=np.full((380,600),255,np.uint8),boundary_found=True)
        b.box=[80,40,300,190]
        with patch('text_layout.prepare_region',return_value=region):layer=layout_text(image,b)
        x0,y0,x1,y1=layer['box']
        self.assertGreater(x0,b.box[0]+5);self.assertGreater(y0,b.box[1]+5)
        self.assertLess(x1,b.box[2]-5);self.assertLess(y1,b.box[3]-5)

    def test_letter_outline_is_not_accepted_as_a_balloon_boundary(self):
        image,b=self.fixture()
        safe=np.zeros((380,600),np.uint8);safe[185:205,295:315]=255
        region=SimpleNamespace(box=[0,0,600,380],safe=safe,boundary_found=True)
        with patch('text_layout.prepare_region',return_value=region):layer=layout_text(image,b)
        self.assertFalse(layer['boundary_found']);self.assertTrue(layer['needs_review'])
        self.assertGreater(layer['box'][2]-layer['box'][0],100)

    def test_translation_edit_invalidates_old_layout(self):
        image,b=self.fixture();b.text_layer=layout_text(image,b)
        b.translation+='ใหม่';self.assertFalse(is_current(b))
        self.assertEqual(composite_text_crop(image,[b],(0,0)).tobytes(),image.tobytes())

    def test_modified_character_style_is_rejected_before_export(self):
        image,b=self.fixture();layer=layout_text(image,b)
        for key,value in [('tracking',10),('kerning','optical'),('antialias','smooth'),('horizontal_scale',95),('vertical_scale',95),('baseline_shift',2),('language','Thai')]:
            with self.subTest(key=key),self.assertRaises(ValueError):validate_layer(dict(layer,**{key:value}))

    def test_tiny_area_or_unrenderable_text_fails_explicitly(self):
        image,b=self.fixture();b.box=[0,0,12,12]
        with self.assertRaises(ValueError):layout_text(image,b)
        b.box=[20,20,580,360];b.translation='😀'
        with self.assertRaisesRegex(ValueError,'ไม่รองรับ'):layout_text(image,b)


class PSDJobTests(unittest.TestCase):
    def test_background_is_original_and_clean_is_one_full_page_with_order_preserved(self):
        image=Image.new('RGB',(300,200),'red');page=Page('source.png')
        crop=Image.new('RGB',(100,80),'white');mask=np.zeros((80,100),np.uint8);mask[10:30,20:50]=255
        page.clean_patches=[encode_patch([40,50,140,130],crop,mask,'solid','id')]
        page.clean_patches.append(encode_patch([40,50,140,130],Image.new('RGB',(100,80),'blue'),mask,'solid','second'))
        with tempfile.TemporaryDirectory() as folder:
            job=prepare_job(folder,image,page,False)
            with Image.open(job['background']) as background:self.assertEqual(background.tobytes(),image.tobytes())
            self.assertEqual(len(job['patches']),1)
            self.assertEqual((job['patches'][0]['left'],job['patches'][0]['top']),(0,0))
            with Image.open(job['patches'][0]['path']) as patch_image:
                self.assertEqual(patch_image.size,image.size)
                self.assertEqual(patch_image.convert('RGB').tobytes(),compose_page(image,page.clean_patches).tobytes())
                self.assertEqual(patch_image.getpixel((65,65)),(0,0,255))

    def test_success_publishes_only_psd_and_removes_internal_files(self):
        def fake_photoshop(script,cancel):
            folder=Path(script).parent
            (folder/'result.psd').write_bytes(b'8BPSfixture')
            (folder/'report.json').write_text('{"ok":true,"clean_layers":0,"text_layers":[]}',encoding='utf-8')
        with tempfile.TemporaryDirectory() as folder,patch('photoshop_export.photoshop_progid',return_value='test'),patch('photoshop_export.run_script',side_effect=fake_photoshop):
            dest=Path(folder)/'out.psd'
            export_psd(Image.new('RGB',(10,10)),Page(''),dest)
            self.assertEqual(list(Path(folder).iterdir()),[dest])

    def test_unprepared_translation_cannot_silently_disappear(self):
        page=Page('source.png',[Bubble([0,0,100,100],translation='สวัสดี')])
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'เพิ่ม Text'):prepare_job(folder,Image.new('RGB',(100,100)),page)

    def test_existing_psd_is_never_overwritten_and_cancel_has_no_launch(self):
        with tempfile.TemporaryDirectory() as folder,patch('photoshop_export.run_script') as run:
            dest=Path(folder)/'out.psd';dest.write_bytes(b'original')
            with self.assertRaises(FileExistsError):export_psd(Image.new('RGB',(10,10)),Page(''),dest)
            self.assertEqual(dest.read_bytes(),b'original')
            cancel=threading.Event();cancel.set()
            with self.assertRaises(InterruptedError):export_psd(Image.new('RGB',(10,10)),Page(''),Path(folder)/'next.psd',cancel=cancel)
            run.assert_not_called()

    def test_failed_photoshop_does_not_publish_psd(self):
        with tempfile.TemporaryDirectory() as folder,patch('photoshop_export.photoshop_progid',return_value='test'),patch('photoshop_export.run_script',side_effect=RuntimeError('native export failed')):
            dest=Path(folder)/'out.psd'
            with self.assertRaisesRegex(RuntimeError,'native export failed'):export_psd(Image.new('RGB',(10,10)),Page(''),dest)
            self.assertFalse(dest.exists());self.assertEqual(list(Path(folder).iterdir()),[])

    def test_psd_dimension_limit_is_explicit(self):
        with self.assertRaisesRegex(ValueError,'30,000'):
            export_psd(Image.new('RGB',(1,30001)),Page(''),'unused.psd')
