import sys,unittest,re
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from text_layout import manga_wrap,find_font,layout_text
from windows_text import TextRenderer
from manhwa_ocr import Bubble
from PIL import Image,ImageDraw


@unittest.skipUnless(sys.platform=='win32','Windows Thai shaping required')
class MangaTypesettingTests(unittest.TestCase):
    def test_two_anchors_in_one_interior_get_separate_text(self):
        from unittest.mock import patch
        import numpy as np
        im=Image.new('RGB',(280,360),'white')
        mask=np.zeros((360,280),np.uint8)
        mask[30:330,40:240]=255
        first=Bubble([100,130,180,165],translation='คำแรก')
        second=Bubble([100,156,180,191],translation='คำต่อมา')
        pages=[first,second]
        with patch('text_layout.BUBBLE_SEGMENTER.segment_bubble',return_value=([0,0,280,360],mask)):
            for same_center in (False,True):
                if same_center:second.box=list(first.box)
                top,bottom=[layout_text(im,b,page_bubbles=pages)['box'] for b in pages]
                self.assertLess(top[3],bottom[1])

    def test_two_lines_lower_is_slightly_longer(self):
        text='ตอนเจอกันครั้งแรก เธอก็มาทำผมให้ฉันนี่'
        with TextRenderer(42,find_font()[0]) as r:
            lines=manga_wrap(text,420,r,2)
            self.assertEqual(len(lines),2)
            widths=[r.measure(v)[0] for v in lines]
            self.assertGreater(widths[1],widths[0])
            self.assertLess(widths[1]/widths[0],1.35)
            self.assertEqual(re.sub(r'\s','',text),re.sub(r'\s','',''.join(lines)))

    def test_three_lines_middle_is_widest(self):
        text='ฉันจะตอบแทน ผมเปียสองข้างที่น่ารัก แก้ตัวละกัน'
        with TextRenderer(42,find_font()[0]) as r:
            lines=manga_wrap(text,420,r,3)
            self.assertEqual(len(lines),3)
            widths=[r.measure(v)[0] for v in lines]
            self.assertGreater(widths[1],max(widths[0],widths[2]))
            self.assertEqual(re.sub(r'\s','',text),re.sub(r'\s','',''.join(lines)))

    def test_explicit_line_breaks_remain_under_user_control(self):
        text='ฉันจะตอบแทน\nแก้ตัวละกัน'
        with TextRenderer(42,find_font()[0]) as r:
            self.assertEqual(manga_wrap(text,420,r,2),text.splitlines())

    def test_layout_keeps_type_style_and_fits_ink_inside_box(self):
        im=Image.new('RGB',(600,450),'gray');ImageDraw.Draw(im).ellipse((30,20,570,425),fill='white',outline='black',width=4)
        b=Bubble([35,25,565,420],translation='ฉันจะตอบแทน ผมเปียสองข้างที่น่ารัก แก้ตัวละกัน')
        layer=layout_text(im,b);box=layer['box']
        with TextRenderer(layer['font_size'],find_font()[0]) as r:tile=r.render(layer['text'].splitlines())
        self.assertLessEqual(tile.width,box[2]-box[0]);self.assertLessEqual(tile.height,box[3]-box[1])
        self.assertEqual(layer['paragraph_style'],'manga-centred');self.assertEqual(layer['alignment'],'center')
        self.assertEqual(layer['tracking'],0);self.assertEqual(layer['kerning'],'metrics');self.assertEqual(layer['antialias'],'sharp')

    def test_final_layout_middle_line_is_widest_and_centered(self):
        im=Image.new('RGB',(600,440),'gray');ImageDraw.Draw(im).ellipse((35,30,565,410),fill='white',outline='black',width=3)
        b=Bubble([35,30,565,410],translation='ฉันจะตอบแทน ผมเปียสองข้างที่น่ารัก แก้ตัวละกัน')
        layer=layout_text(im,b)
        with TextRenderer(layer['font_size'],find_font()[0]) as r:widths=[r.measure(v)[0] for v in layer['text'].splitlines()]
        self.assertEqual(len(widths),3);self.assertGreater(widths[1],max(widths[0],widths[2]))
        box=layer['box'];self.assertAlmostEqual((box[0]+box[2])/2,300,delta=3)
        self.assertAlmostEqual((box[1]+box[3])/2,220,delta=3)
        self.assertGreaterEqual(layer['font_size'],50)

    def test_long_indivisible_word_does_not_force_orphan_first_line(self):
        from text_layout import manga_line_score
        self.assertLess(manga_line_score([290,230]),manga_line_score([110,320]))


if __name__=='__main__':unittest.main()
