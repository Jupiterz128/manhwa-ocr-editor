"""Opt-in Photoshop round trip on generated non-user artwork only."""
import sys,tempfile,json,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image,ImageDraw
from psd_tools import PSDImage
from manhwa_ocr import Bubble,Page
from frame_geometry import box_quad,quad_bounds
from text_layout import layout_text,composite_text_crop
from photoshop_export import export_psd,run_script
from bubble_cleaner import encode_patch


def main():
    out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
    im=Image.new('RGB',(800,700),'#535b66');d=ImageDraw.Draw(im);bs=[]
    for center,angle,color in [((380,180),-18,'white'),((420,500),21,'#111111')]:
        r=np.radians(angle);basis=np.array([[np.cos(r),-np.sin(r)],[np.sin(r),np.cos(r)]])
        q=(np.asarray(box_quad([-210,-100,210,100]))@basis.T+center).tolist()
        d.polygon([tuple(p) for p in q],fill=color)
        b=Bubble(quad_bounds(q),quad=q,translation='สวัสดีครับ\nยินดีที่ได้รู้จัก')
        bs.append(b)
    for b in bs:b.text_layer=layout_text(im,b)
    patch=encode_patch([0,0,8,8],im.crop((0,0,8,8)),np.full((8,8),255,np.uint8),'solid','fixture')
    page=Page('generated.png',bs,clean_patches=[patch])
    target=out/'auto_tilt.psd'
    report=export_psd(im,page,target)
    composite_text_crop(im,bs,(0,0)).save(out/'auto_tilt_preview.png')
    psd=PSDImage.open(target);layers=[l for l in psd.descendants() if l.kind=='type']
    assert len(layers)==2
    measured=[]
    for layer in layers:
        i=int(layer.name.split()[1])-1;wanted=bs[i].text_layer
        xx,xy,yx,yy,tx,ty=layer.transform
        angle=math.degrees(math.atan2(xy,xx));measured.append(angle)
        assert abs(angle-wanted['rotation'])<.05,(angle,wanted)
        x0,y0,x1,y1=layer.bbox;box=wanted['box']
        assert abs((x0+x1-box[0]-box[2])/2)<2
        assert abs((y0+y1-box[1]-box[3])/2)<2
    assert np.array_equal(np.array(psd[0].topil().convert('RGB')),np.array(im))
    assert len([l for l in psd if l.kind=='pixel'])==2
    with tempfile.TemporaryDirectory() as folder:
        script=Path(folder)/'edit.jsx';result=Path(folder)/'result.txt'
        job={'source':str(target.resolve()),'destination':str((out/'edited_auto_tilt.psd').resolve()),'result':str(result),'text':'แก้ข้อความเอียงแล้ว'}
        script.write_text('var job='+json.dumps(job,ensure_ascii=True)+';\n'+r'''
var previous=app.documents.length ? app.activeDocument : null, doc=null, result='';
try {
 doc=app.open(new File(job.source));
 var layer=doc.layerSets.getByName('TEXT - Thai').artLayers[0];
 if(layer.kind!==LayerKind.TEXT) throw new Error('Not editable type');
 if(layer.textItem.tracking!==0 || layer.textItem.autoKerning!==AutoKernType.METRICS || layer.textItem.antiAliasMethod!==AntiAlias.SHARP || !layer.textItem.useAutoLeading) throw new Error('Character settings changed');
 layer.textItem.contents=job.text;
 var options=new PhotoshopSaveOptions();options.layers=true;
 doc.saveAs(new File(job.destination),options,true,Extension.LOWERCASE);
 result='OK';
} catch(e) {result=String(e);}
finally {
 if(doc) doc.close(SaveOptions.DONOTSAVECHANGES);
 if(previous) app.activeDocument=previous;
 var f=new File(job.result);f.open('w');f.write(result);f.close();
}
''',encoding='utf-8')
        run_script(script)
        assert result.read_text()=='OK',result.read_text()
    edited=PSDImage.open(out/'edited_auto_tilt.psd')
    assert any(job['text'] in l.text for l in edited.descendants() if l.kind=='type')
    report.update(rotation_read_from_saved_psd=measured,reopened_edited_and_saved=True,background_identical=True)
    (out/'auto_tilt_native_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    psd.composite().save(out/'auto_tilt_photoshop.png')
    print('Native PSD verified: rotated editable type, Character settings, original + one CLEAN layer, edit/save round trip.')


if __name__=='__main__':main()
