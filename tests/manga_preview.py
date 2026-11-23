"""Synthetic Thai typesetting preview; no user artwork is included."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image,ImageDraw
from manhwa_ocr import Bubble
from text_layout import layout_text,composite_text_crop

out=Path(__file__).resolve().parents[1]/'examples/v3_1'
im=Image.new('RGB',(1200,700),'#ede8e3');d=ImageDraw.Draw(im)
specs=[([40,40,570,325],'ตอนเจอกันครั้งแรก เธอก็มาทำผมให้ฉันนี่','white'),
       ([630,40,1160,420],'ฉันจะตอบแทน ผมเปียสองข้างที่น่ารัก แก้ตัวละกัน','white'),
       ([70,390,570,665],'ฉันต้องเอาชีวิตรอดไปให้ได้','#18181c')]
bubbles=[]
for box,text,fill in specs:
    d.ellipse(box,fill=fill,outline='#be9aac',width=3)
    b=Bubble(box,translation=text);bubbles.append(b)
    b.text_layer=layout_text(im,b)
    print(json.dumps(b.text_layer,ensure_ascii=False))
composite_text_crop(im,bubbles,(0,0)).save(out/'manga_typesetting.png')
