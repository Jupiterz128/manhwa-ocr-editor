"""Export native editable text through the installed Photoshop COM scripting API.

No hand-built TySh blocks or raster substitutes. The script owns only new documents.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from bubble_cleaner import compose_page
from text_layout import FONT_POSTSCRIPT,is_current,validate_layer

COMPONENT_VERSION='3.1'


def photoshop_progid():
    if os.name!='nt':raise RuntimeError('ส่งออก PSD รุ่นนี้ต้องใช้ Photoshop Desktop บน Windows')
    import winreg
    names=['Photoshop.Application']
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,'') as root:
        index=0
        while True:
            try:name=winreg.EnumKey(root,index);index+=1
            except OSError:break
            if name.startswith('Photoshop.Application.') and name.count('.')==2:names.append(name)
    def priority(name):
        try:return tuple(int(v) for v in name.split('.')[2:])
        except ValueError:return ()
    for name in sorted(names,key=priority,reverse=True):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,name+'\\CLSID') as key:
                value,_=winreg.QueryValueEx(key,'')
                if value:return name
        except OSError:continue
    raise RuntimeError('ไม่พบ Photoshop Desktop ที่รองรับการส่งออก PSD กรุณาติดตั้ง/ซ่อม Photoshop ก่อน (ไม่แปลงข้อความเป็นภาพแทน)')


def _ascii_temp_root(fallback_parent):
    """Return a pure-ASCII staging directory so Photoshop COM/ExtendScript never chokes on Thai or `[...]` paths."""
    candidates=[]
    if os.name=='nt':
        pub=os.environ.get('PUBLIC','C:\\Users\\Public')
        if pub:candidates.append(Path(pub)/'ManhwaOCREditorTemp')
        sys_drive=os.environ.get('SystemDrive','C:')
        candidates.append(Path(sys_drive+'\\')/'Temp'/'ManhwaOCREditorTemp')
    candidates.append(Path(tempfile.gettempdir())/'ManhwaOCREditorTemp')
    for root in candidates:
        try:
            if all(ord(c)<128 and c not in '[]' for c in str(root)):
                root.mkdir(parents=True,exist_ok=True)
                return root
        except OSError:
            continue
    fallback_parent.mkdir(parents=True,exist_ok=True)
    return fallback_parent


def run_script(script,cancel=None):
    progid=photoshop_progid()
    script=Path(script).resolve()
    # JScript uses IDispatch directly, avoiding .NET's dependency on the registered type library.
    bridge=script.with_name('photoshop_bridge.js')
    bridge.write_text("""function whenReady(call) {
        for (var attempt = 0; ; attempt++) {
            try { return call(); }
            catch (e) {
                var code = e.number >>> 0;
                if ((code !== 0x8001010a && code !== 0x80010001) || attempt >= 30) throw e;
                WScript.Sleep(1000);
            }
        }
    }
    try {
        var ps = whenReady(function() { return new ActiveXObject(WScript.Arguments.Item(0)); });
        whenReady(function() { return ps.DoJavaScriptFile(WScript.Arguments.Item(1)); });
    } catch (e) {
        WScript.StdErr.WriteLine('Photoshop COM 0x' + (e.number >>> 0).toString(16) + ': ' + e.message);
        WScript.Quit(1);
    }""",encoding='ascii')
    host=Path(os.environ['SystemRoot'])/'System32'/'cscript.exe'
    with tempfile.TemporaryFile() as log:
        process=subprocess.Popen([str(host),'//Nologo','//B','//U',str(bridge),progid,str(script)],
            stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        while True:
            try:code=process.wait(timeout=.2);break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():Path(script).with_name('cancel.flag').touch()
        if code:
            log.seek(0);message=log.read().decode('utf-16-le',errors='replace')
            raise RuntimeError('Photoshop ส่งออกไม่สำเร็จ: '+message[-2000:])


def _apply_custom_fonts(folder,job,cancel=None):
    """Apply per-bubble custom fonts in a clean second pass while preserving rotation & exact center."""
    custom=[t for t in job.get('texts',[]) if t.get('font') and t['font']!=job['font']]
    if not custom:
        return []
    font_job={
        'psd':job['output'],
        'report':str(Path(folder)/'font_report.json'),
        'items':[{
            'name':t['name'],
            'ps':t['font'],
            'choice':t.get('font_choice',''),
            'size':t['font_size'],
            'rot':float(t.get('rotation',0) or 0.0),
            'cx':(float(t['box'][0])+float(t['box'][2]))*0.5,
            'cy':(float(t['box'][1])+float(t['box'][3]))*0.5,
        } for t in custom],
    }
    jsx_code=r'''
function _slug(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ''); }
function _writeJson(path, obj) {
  var f = new File(path); f.encoding = 'UTF-8';
  f.open('w'); f.write(obj); f.close();
}
var _prev = null, _doc = null, _dialogs = app.displayDialogs;
var _before = app.documents.length;
var _warns = [], _ok = true, _err = '';
try {
  if (_before) _prev = app.activeDocument;
  app.displayDialogs = DialogModes.NO;
  _doc = app.open(new File(fontJob.psd));
  var _grp = _doc.layerSets.getByName('TEXT - Thai');
  var _psMap = {}, _slugMap = {};
  for (var fi = 0; fi < app.fonts.length; fi++) {
    var fn = app.fonts[fi];
    _psMap[fn.postScriptName] = fn.postScriptName;
    _slugMap[_slug(fn.postScriptName)] = fn.postScriptName;
    _slugMap[_slug(fn.name)] = fn.postScriptName;
  }
  for (var i = 0; i < fontJob.items.length; i++) {
    var it = fontJob.items[i];
    var target = _psMap[it.ps] || _slugMap[_slug(it.ps)] || _slugMap[_slug(it.choice)];
    if (!target && _slug(it.choice) === 'mnkrasip') target = _slugMap.mnkrasipregular || _slugMap.krasip || _slugMap.mnkrasipbold;
    if (!target && _slug(it.choice) === 'tfmaniwika') target = _slugMap['tfmanawika'];
    if (!target && _slug(it.choice) === 'tfphetai') target = _slugMap['tfphethai'];
    if (!target) {
      _warns.push('ไม่พบฟอนต์ ' + (it.choice || it.ps) + ' ใน Photoshop (ใช้ฟอนต์เริ่มต้นแทน — กรุณาติดตั้งฟอนต์ลงใน Windows แล้วรีสตาร์ท Photoshop)');
      continue;
    }
    var lyr = _grp.artLayers.getByName(it.name);
    var hasRot = Math.abs(it.rot || 0) >= 0.05;
    if (hasRot) lyr.rotate(-it.rot, AnchorPosition.MIDDLECENTER);
    lyr.textItem.font = target;
    lyr.textItem.size = new UnitValue(it.size, 'px');
    if (hasRot) lyr.rotate(it.rot, AnchorPosition.MIDDLECENTER);
    var bnd = lyr.bounds;
    var curCx = (bnd[0].as('px') + bnd[2].as('px')) / 2.0;
    var curCy = (bnd[1].as('px') + bnd[3].as('px')) / 2.0;
    if (Math.abs(it.cx - curCx) >= 0.25 || Math.abs(it.cy - curCy) >= 0.25) {
      lyr.translate(new UnitValue(it.cx - curCx, 'px'), new UnitValue(it.cy - curCy, 'px'));
    }
  }
  var opts = new PhotoshopSaveOptions(); opts.layers = true;
  _doc.saveAs(new File(fontJob.psd), opts, false, Extension.LOWERCASE);
} catch (e) {
  _ok = false; _err = String(e);
} finally {
  if (_doc) _doc.close(SaveOptions.DONOTSAVECHANGES);
  if (_prev) app.activeDocument = _prev;
  app.displayDialogs = _dialogs;
  var esc = function(s) { return String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\r|\n/g, ' '); };
  var wjson = [];
  for (var wi = 0; wi < _warns.length; wi++) wjson.push('"' + esc(_warns[wi]) + '"');
  _writeJson(fontJob.report, '{"ok":' + (_ok ? 'true' : 'false') + ',"error":"' + esc(_err) + '","warnings":[' + wjson.join(',') + ']}');
}
'''
    script=Path(folder)/'apply_fonts.jsx'
    script.write_text('\ufeffvar fontJob = '+json.dumps(font_job,ensure_ascii=True)+';\n'+jsx_code,encoding='utf-8')
    run_script(script,cancel)
    rep_path=Path(font_job['report'])
    if rep_path.is_file():
        rep=json.loads(rep_path.read_text(encoding='utf-8-sig'))
        if not rep.get('ok'):
            raise RuntimeError('ตั้งค่าฟอนต์ใน Photoshop ไม่สำเร็จ: '+rep.get('error',''))
        return rep.get('warnings',[])
    return []


def prepare_job(folder,image,page,include_text=True):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    image.convert('RGB').save(folder/'background.png')
    job={'background':str(folder/'background.png'),'font':FONT_POSTSCRIPT,'patches':[],'texts':[],
         'output':str(folder/'result.psd'),
         'report':str(folder/'report.json'),'cancel_file':str(folder/'cancel.flag')}
    if page.clean_patches:
        path=folder/'clean.png'
        compose_page(image,page.clean_patches).save(path)
        job['patches'].append({'path':str(path),'left':0,'top':0,'name':'CLEAN'})
    if include_text:
        for index,b in enumerate(page.bubbles):
            if not b.translation.strip() and not b.text_layer:continue
            if not is_current(b):raise ValueError(f'กรอบ {index+1}: คำแปล/ฟอนต์/กรอบเปลี่ยนแล้วหรือยังไม่ได้เพิ่ม Text — กดเพิ่ม Text เพื่อจัดใหม่ก่อน')
            layer=b.text_layer;validate_layer(layer)
            if layer['box'][2]>image.width or layer['box'][3]>image.height:raise ValueError('ชั้นข้อความอยู่นอกภาพ')
            job['texts'].append(dict(layer,id=b.id,name=f'TEXT {index+1:03d} - {b.id}'))
    return job


def _publish_exclusive(source,destination):
    """Publish PSD across drives and non-NTFS (exFAT) volumes without ever overwriting."""
    try:
        os.link(source,destination)
    except OSError:
        try:
            with destination.open('xb') as dst,source.open('rb') as src:
                shutil.copyfileobj(src,dst,length=1024*1024)
        except BaseException:
            if not destination.exists():raise
            raise


def export_psd(image,page,destination,include_text=True,cancel=None):
    destination=Path(destination)
    if destination.suffix.lower()!='.psd':raise ValueError('กรุณาใช้ชื่อไฟล์ .psd')
    if destination.exists():raise FileExistsError(destination)
    if max(image.size)>30000:raise ValueError('PSD รองรับด้านละไม่เกิน 30,000 px กรุณาแบ่งหน้ายาวก่อนส่งออก')
    if cancel is not None and cancel.is_set():raise InterruptedError('หยุดส่งออกแล้ว')
    photoshop_progid()
    destination.parent.mkdir(parents=True,exist_ok=True)
    temp_root=_ascii_temp_root(destination.parent)
    with tempfile.TemporaryDirectory(prefix='manhwa-psd-',dir=temp_root) as temporary:
        folder=Path(temporary).resolve();job=prepare_job(folder,image,page,include_text)
        base_job=copy.deepcopy(job)
        for item in base_job['texts']:
            item['font']=FONT_POSTSCRIPT
            if item.get('rotation') and not item.get('bubble_quad'):
                b_ref=item['bubble_box']
                item['bubble_quad']=[[b_ref[0],b_ref[1]],[b_ref[2],b_ref[1]],[b_ref[2],b_ref[3]],[b_ref[0],b_ref[3]]]
        script=folder/'export.jsx'
        code=Path(__file__).with_suffix('.jsx').read_text(encoding='utf-8')
        script.write_text('\ufeffvar job = '+json.dumps(base_job,ensure_ascii=True)+';\n'+code,encoding='utf-8')
        run_script(script,cancel)
        if not Path(job['report']).is_file():raise RuntimeError('Photoshop ไม่ส่งรายงานผลกลับมา')
        report=json.loads(Path(job['report']).read_text(encoding='utf-8-sig'))
        if cancel is not None and cancel.is_set():raise InterruptedError('หยุดส่งออกแล้ว')
        if not report.get('ok'):raise RuntimeError(report.get('error','Photoshop ส่งออกไม่สำเร็จ'))
        font_warnings=_apply_custom_fonts(folder,job,cancel)
        if font_warnings:
            report.setdefault('warnings',[]).extend(font_warnings)
        source=Path(job['output'])
        with source.open('rb') as stream:
            if stream.read(4)!=b'8BPS':raise RuntimeError('ผลลัพธ์ไม่ใช่ PSD')
        if destination.exists():raise FileExistsError(destination)
        try:
            with destination.open('xb') as dst,source.open('rb') as src:
                shutil.copyfileobj(src,dst,length=1024*1024)
        except FileExistsError:
            raise
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        report['destination']=str(destination)
        return report
