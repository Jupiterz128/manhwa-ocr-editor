"""Opt-in re-open/edit/save check in the installed Photoshop, on generated files only."""
import json
from pathlib import Path
import sys
import tempfile
import uuid
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from photoshop_export import run_script
from psd_tools import PSDImage


def main():
    folder=Path(__file__).resolve().parents[1]/'examples/v3_0'
    # Reports record their original absolute location; resolve the bundled
    # generated fixture beside the script's current project after relocation.
    source=folder/Path(json.loads((folder/'smoke_report.json').read_text(encoding='utf-8'))['destination']).name
    destination=folder/('edited_native_'+uuid.uuid4().hex[:6]+'.psd')
    suffix=' — แก้ไขใน Photoshop แล้ว'
    with tempfile.TemporaryDirectory() as temporary:
        script=Path(temporary)/'verify.jsx';report_path=Path(temporary)/'result.txt'
        job={'source':str(source),'destination':str(destination),'suffix':suffix,'report':str(report_path)}
        code=r'''
var previous=null, doc=null, dialogs=app.displayDialogs;
var before=app.documents.length, result='';
try {
 if(before) previous=app.activeDocument;
 app.displayDialogs=DialogModes.NO;
 doc=app.open(new File(job.source));
 var layer=doc.layerSets.getByName('TEXT - Thai').artLayers[0];
 if(layer.kind!==LayerKind.TEXT) throw new Error('Not a type layer');
 if(layer.textItem.tracking!==0 || layer.textItem.autoKerning!==AutoKernType.METRICS || layer.textItem.antiAliasMethod!==AntiAlias.SHARP ||
    layer.textItem.horizontalScale!==100 || layer.textItem.verticalScale!==100 || layer.textItem.baselineShift.as('pt')!==0 ||
    layer.textItem.language!==Language.ENGLISHUK || !layer.textItem.useAutoLeading)
    throw new Error('Saved Character settings did not survive reopen');
 layer.textItem.contents+=job.suffix;
 var options=new PhotoshopSaveOptions();options.layers=true;
 doc.saveAs(new File(job.destination),options,true,Extension.LOWERCASE);
 result='OK';
} catch(e) {result=String(e);}
finally {
 if(doc) doc.close(SaveOptions.DONOTSAVECHANGES);
 if(previous) app.activeDocument=previous;
 app.displayDialogs=dialogs;
 var f=new File(job.report);f.open('w');f.write(result+'\n'+before+'\n'+app.documents.length);f.close();
}
'''
        script.write_text('var job='+json.dumps(job,ensure_ascii=True)+';\n'+code,encoding='utf-8')
        run_script(script)
        result=report_path.read_text().splitlines()
        assert result[0]=='OK',result
        assert result[1]==result[2],result
    psd=PSDImage.open(destination);original=PSDImage.open(source)
    layer=next(layer for layer in psd.descendants() if layer.kind=='type')
    assert suffix in layer.text
    with Image.open(folder/'original.png') as image:
        assert np.array_equal(np.array(psd[0].topil().convert('RGB')),np.array(image.convert('RGB')))
    original_clean=next(layer for layer in original.descendants() if layer.kind=='pixel' and layer.name.startswith('CLEAN'))
    edited_clean=next(layer for layer in psd.descendants() if layer.kind=='pixel' and layer.name.startswith('CLEAN'))
    assert original_clean.bbox==edited_clean.bbox
    assert original_clean.topil().tobytes()==edited_clean.topil().tobytes()
    report={'reopened_in_photoshop':True,'edited_type_text':layer.text,'saved_and_read_back':True,'character_settings_verified_after_reopen':True,
        'background_equals_source':True,'clean_patch_unchanged':True,'documents_before':int(result[1]),
        'documents_after':int(result[2]),'artifact':str(destination)}
    (folder/'native_edit_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
