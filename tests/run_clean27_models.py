"""Optional real, installed model smoke tests; never part of unit discovery.

Synthetic images only. Does not benchmark the user's missing 102-region job.
Run from the application folder: .venv/Scripts/python.exe tests/run_clean27_models.py
"""
import json
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw,ImageFont
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import manhwa_ocr as m
from bubble_cleaner import Cleaner,CleanRegion,prepare_region
from clean_quality import clean_checked


def main():
    root=Path(__file__).resolve().parents[1]
    output=root/'examples/v2_7';output.mkdir(parents=True,exist_ok=True)
    result={'scope':'Synthetic smoke tests on Windows; not the original 102 bubbles','cases':[]}
    cleaner=Cleaner();result['lama']=cleaner.check_lama()
    if not result['lama']['ready']:raise RuntimeError(result['lama']['message'])
    with tempfile.TemporaryDirectory() as temporary:
        app=m.ManhwaOCRApp.__new__(m.ManhwaOCRApp)
        app.clean_reader=None;app.clean_reader_settings=None
        app.cache=m.OCRCache(Path(temporary)/'cache.sqlite3');app.events=queue.Queue()
        cancel=threading.Event()
        for language,text,font_path in [('en','Where are you going? UGH','C:/Windows/Fonts/arial.ttf'),
                                       ('ko+en','안녕하세요 어디로 가세요?','C:/Windows/Fonts/malgun.ttf')]:
            for kind in (['solid','gradient'] if language=='en' else ['solid']):
                y,x=np.mgrid[:220,:680]
                gray=(230-y*.17+x*.015).astype(np.uint8) if kind=='gradient' else np.full((220,680),245,np.uint8)
                image=Image.fromarray(np.repeat(gray[:,:,None],3,axis=2))
                draw=ImageDraw.Draw(image);draw.rounded_rectangle((10,10,669,209),radius=40,outline='black',width=3)
                font=ImageFont.truetype(font_path,32)
                draw.text((90,90),text,font=font,fill='black')
                bubble=m.Bubble([10,10,670,210]);start=time.monotonic()
                settings=('rapid',language,False)
                lines=app.cleaning_lines(image,bubble,settings,cancel);bubble.lines=lines
                if not lines:raise AssertionError('Real OCR returned no lines')
                region=prepare_region(image,bubble)
                def read(crop):
                    page=image.copy();page.paste(crop,(region.box[0],region.box[1]))
                    return app.cleaning_lines(page,bubble,settings,cancel)
                out,mask,method,report=clean_checked(cleaner,region,reader=read)
                assert np.array_equal(np.array(out)[mask==0],np.array(region.image)[mask==0])
                stem=language.replace('+','_')+'_'+kind
                image.save(output/(stem+'_source.png'))
                cleaned=image.copy();cleaned.paste(out,region.box[:2]);cleaned.save(output/(stem+'_clean.png'))
                pair=Image.new('RGB',(image.width*2,image.height));pair.paste(image);pair.paste(cleaned,(image.width,0))
                pair.save(output/(stem+'_comparison.png'))
                result['cases'].append({'language':language,'expected':text,'recognized':[line['text'] for line in lines],
                    'background':kind,'chosen':method,'seconds':round(time.monotonic()-start,3),
                    'outside_mask_equal':True,'quality':report})
        # LaMa actual repair, including a crop that is not divisible by eight.
        y,x=np.mgrid[:131,:197]
        texture=np.stack([120+30*np.sin(x/9),140+25*np.cos(y/12),150+20*np.sin((x+y)/13)],axis=2).astype(np.uint8)
        source=texture.copy();source[49:80,65:132]=20
        mask=np.zeros((131,197),np.uint8);mask[47:82,63:134]=255
        region=CleanRegion([0,0,197,131],Image.fromarray(source),mask,np.full_like(mask,255),True)
        start=time.monotonic();out,used,method=cleaner.clean(region,'lama')
        assert out.size==region.image.size
        assert np.array_equal(np.array(out)[used==0],source[used==0])
        out.save(output/'lama_texture_clean.png');region.image.save(output/'lama_texture_source.png')
        result['cases'].append({'background':'synthetic texture','chosen':method,'seconds':round(time.monotonic()-start,3),
            'outside_mask_equal':True,'mean_absolute_error_in_mask':float(np.abs(np.array(out).astype(float)-texture)[used>0].mean()),
            'quality':'Actual inference only; visually inspect, not certified clean'})
    (root/'tests/clean27_models.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
