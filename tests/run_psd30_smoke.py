"""Opt-in native Photoshop test: creates and closes only new test documents."""
import sys,json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from text_layout import layout_text,composite_text_crop
from bubble_cleaner import prepare_region,Cleaner,encode_patch,compose_page
from photoshop_export import export_psd


def main():
    folder=Path(__file__).resolve().parents[1]/'examples/v3_0';folder.mkdir(exist_ok=True)
    im=Image.new('RGB',(640,420),'#a1b6c0');draw=ImageDraw.Draw(im)
    draw.ellipse((40,30,600,390),fill='white',outline='black',width=4)
    draw.text((240,195),'HELLO WORLD',fill='black')
    bubble=SimpleNamespace(id='sample-thai-1',box=[40,30,600,390],lines=[{'box':[230,190,330,215]}],
        translation='สวัสดีครับ นี่คือข้อความภาษาไทย\nที่สามารถแก้ไขต่อใน Photoshop ได้',text_layer={})
    bubble.text_layer=layout_text(im,bubble)
    region=prepare_region(im,bubble);out,mask,method=Cleaner().clean(region,'solid',(255,255,255))
    page=SimpleNamespace(bubbles=[bubble],clean_patches=[encode_patch(region.box,out,mask,method,bubble.id)])
    composite_text_crop(compose_page(im,page.clean_patches),[bubble],(0,0)).save(folder/'local_preview.png')
    im.save(folder/'original.png')
    dest=folder/'native_smoke.psd'
    suffix=1
    while dest.exists():dest=folder/f'native_smoke_{suffix}.psd';suffix+=1
    before=set(folder.iterdir())
    report=export_psd(im,page,dest)
    assert set(folder.iterdir())-before=={dest},'Export must publish only the PSD'
    style=report['text_layers'][0]
    assert style['tracking']==0 and style['kerning']=='AutoKernType.METRICS' and style['antialias']=='AntiAlias.SHARP'
    assert style['horizontal_scale']==style['vertical_scale']==100 and style['baseline_shift']==0
    assert style['language']=='Language.ENGLISHUK' and style['auto_leading'] is True
    from psd_tools import PSDImage
    psd=PSDImage.open(dest)
    layers=[{'name':l.name,'kind':l.kind} for l in psd.descendants()]
    text=[l for l in psd.descendants() if l.kind=='type']
    assert len(text)==1
    assert text[0].text.replace('\r','\n').rstrip('\n')==bubble.text_layer['text']
    assert text[0].resource_dict['FontSet'][0]['Name'].value=='THSarabunNew-Bold'
    clean=[l for l in psd.descendants() if l.name=='CLEAN']
    assert len(clean)==1 and clean[0].kind=='pixel'
    assert clean[0].bbox==(0,0,im.width,im.height)
    assert clean[0].topil().convert('RGB').tobytes()==compose_page(im,page.clean_patches).tobytes()
    with tempfile.TemporaryDirectory() as temporary:
        clean_dest=Path(temporary)/'clean_only.psd'
        export_psd(im,page,clean_dest,include_text=False)
        clean_psd=PSDImage.open(clean_dest)
        assert len(clean_psd)==2 and {l.name for l in clean_psd}=={'Background','CLEAN'}
        assert list(Path(temporary).iterdir())==[clean_dest]
    report['only_psd_published']=True
    report['clean_only_has_two_pixel_layers']=True
    report['clean_layer_equals_exported_clean_image']=True
    report['parsed_layers']=layers
    (folder/'smoke_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
