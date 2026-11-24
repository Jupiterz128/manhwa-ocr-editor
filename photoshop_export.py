"""Export native editable PSDs via Photoshop COM (default) or Photopea (Playwright).

No hand-built TySh blocks or raster substitutes. When `backend='photopea'` is selected,
headless Chromium drives Photopea without launching Adobe Photoshop Desktop.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from bubble_cleaner import compose_page
from text_layout import FONT_NAME, FONT_POSTSCRIPT, is_current, resolve_font, validate_layer

COMPONENT_VERSION='4.0'
DEFAULT_PSD_BACKEND='photoshop'
PHOTOPEA_URL='https://www.photopea.com'
_PHOTOPEA_HOST='https://photopea-host.local'

_PHOTOPEA_HTML="""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;overflow:hidden">
<iframe id="pp" style="width:1280px;height:800px;border:0"></iframe>
<script>
const iframe = document.getElementById('pp');
let pending = null;
window.addEventListener('message', (e) => {
  if (!pending) return;
  if (e.data === 'done') {
    const p = pending;
    pending = null;
    p.resolve(p.result);
  } else if (e.data instanceof ArrayBuffer) {
    pending.result.buffers.push(e.data);
  } else {
    pending.result.echoes.push(String(e.data));
  }
});

window.__ppAbort = (reason) => {
  if (!pending) return;
  const p = pending;
  pending = null;
  p.reject(new Error(reason || 'Photopea aborted'));
};

window.__ppInit = (url, timeoutMs) => new Promise((resolve, reject) => {
  const timer = setTimeout(() => {
    pending = null;
    reject(new Error('Photopea did not finish loading within ' + timeoutMs + ' ms'));
  }, timeoutMs || 90000);
  pending = {
    result: { echoes: [], buffers: [] },
    resolve: (res) => { clearTimeout(timer); resolve(res); },
    reject: (err) => { clearTimeout(timer); reject(err); }
  };
  iframe.src = url;
});

window.__ppSendAsset = async (assetUrl, timeoutMs) => {
  const resp = await fetch(assetUrl);
  if (!resp.ok) throw new Error('Asset fetch failed: ' + resp.status);
  const buf = await resp.arrayBuffer();
  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending = null;
      reject(new Error('Photopea asset load timed out'));
    }, timeoutMs || 90000);
    pending = {
      result: { echoes: [], buffers: [] },
      resolve: (res) => { clearTimeout(timer); resolve({ echoes: res.echoes, bufferCount: res.buffers.length }); },
      reject: (err) => { clearTimeout(timer); reject(err); }
    };
    iframe.contentWindow.postMessage(buf, '*');
  });
};

window.__ppRunScript = async (script, uploadUrl, timeoutMs) => {
  const res = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending = null;
      reject(new Error('Photopea script timed out'));
    }, timeoutMs || 120000);
    pending = {
      result: { echoes: [], buffers: [] },
      resolve: (r) => { clearTimeout(timer); resolve(r); },
      reject: (err) => { clearTimeout(timer); reject(err); }
    };
    iframe.contentWindow.postMessage(script, '*');
  });
  let uploaded = 0;
  for (const buf of res.buffers) {
    const up = await fetch(uploadUrl, { method: 'POST', body: buf });
    if (!up.ok) throw new Error('PSD buffer upload failed: ' + up.status);
    uploaded++;
  }
  return { echoes: res.echoes, uploaded };
};
</script>
</body>
</html>
"""


def resolve_psd_backend(backend=None):
    value=(backend or os.environ.get('MANHWA_PSD_BACKEND') or DEFAULT_PSD_BACKEND).strip().lower()
    if value not in ('photopea','photoshop'):
        raise ValueError(f'ไม่รองรับตัวส่งออก PSD "{value}" (รองรับ photopea หรือ photoshop)')
    return value


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
    started=script.with_suffix('.started')
    marker=json.dumps(str(started),ensure_ascii=True)
    original=script.read_text(encoding='utf-8-sig')
    script.write_text(f'var _started=new File({marker}); _started.open("w"); _started.close();\n'+original,
                      encoding='utf-8-sig')
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
        # Photoshop may display a startup billing dialog while COM is waiting.
        # Run UI Automation separately so an unresponsive window cannot stall the exporter.
        helper=None
        start=time.monotonic()
        next_check=start+2
        try:
            while True:
                try:code=process.wait(timeout=.2);break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        script.with_name('cancel.flag').touch()
                    now=time.monotonic()
                    if now-start>=60 and not started.exists():
                        process.kill()
                        process.wait()
                        raise RuntimeError('Photoshop ไม่เริ่มสคริปต์ภายใน 60 วินาที กรุณาตรวจหน้าต่างแจ้งเตือนใน Photoshop แล้วลองอีกครั้ง')
                    if helper and helper.poll()==0:
                        next_check=float('inf')
                    if now>=next_check and now-start<45 and (helper is None or helper.poll() is not None):
                        tool=Path(__file__).with_name('dismiss_photoshop_payment.ps1')
                        helper=subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy',
                            'Bypass','-File',str(tool)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
                        next_check=now+5
        finally:
            if helper and helper.poll() is None:
                helper.kill()
                helper.wait()
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


def _photopea_build_pass1_script(payload):
    return 'var job = ' + json.dumps(payload, ensure_ascii=True) + ';\n' + r'''
(function () {
    var out = { ok: false, clean_layers: 0, text_count: 0 };
    try {
        if (app.documents.length < 1) throw new Error('Background document not opened in Photopea');
        var mainDoc = app.documents[0];
        app.activeDocument = mainDoc;
        mainDoc.activeLayer.name = 'Background';

        for (var i = 0; i < job.patches.length; i++) {
            var patch = job.patches[i];
            if (app.documents.length < 2) throw new Error('Patch document missing in Photopea');
            var patchDoc = app.documents[1];
            app.activeDocument = patchDoc;
            patchDoc.activeLayer.name = patch.name;
            patchDoc.activeLayer.duplicate(mainDoc, ElementPlacement.PLACEATBEGINNING);
            patchDoc.close(SaveOptions.DONOTSAVECHANGES);
            app.activeDocument = mainDoc;
            mainDoc.layers[0].name = patch.name;
            if (patch.left || patch.top) {
                var pb = mainDoc.layers[0].bounds;
                mainDoc.layers[0].translate(patch.left - pb[0].value, patch.top - pb[1].value);
            }
            out.clean_layers++;
        }

        if (job.texts.length) {
            var grp = mainDoc.layerSets.add();
            grp.name = 'TEXT - Thai';
            for (var t = 0; t < job.texts.length; t++) {
                var data = job.texts[t];
                var rect = data.box;
                var lyr = grp.artLayers.add();
                lyr.name = data.name;
                lyr.kind = LayerKind.TEXT;
                var item = lyr.textItem;
                item.contents = (data.text || '').split('\n').join('\r');
                item.font = data.font || job.font;
                item.size = data.font_size;
                var ink = new SolidColor();
                ink.rgb.red = data.color[0];
                ink.rgb.green = data.color[1];
                ink.rgb.blue = data.color[2];
                item.color = ink;
                item.useAutoLeading = true; item.autoLeadingAmount = 120;
                item.justification = data.alignment === 'left' ? Justification.LEFT : data.alignment === 'right' ? Justification.RIGHT : Justification.CENTER;
                item.position = [(rect[0] + rect[2]) / 2, rect[1] + data.font_size];
                out.text_count++;
            }
        }
        out.ok = true;
    } catch (err) {
        out.error = String(err);
    }
    app.echoToOE('PASS1:' + JSON.stringify(out));
})();
'''


def _photopea_build_pass2_script(payload):
    return Path(__file__).with_name('text_layer_fx.jsx').read_text('utf-8')+'\nvar job = ' + json.dumps(payload, ensure_ascii=True) + ';\n' + r'''
(function () {
    function bpx(layer) {
        var b = layer.bounds;
        return [b[0].value, b[1].value, b[2].value, b[3].value];
    }
    function hex2(n) {
        var s = Math.max(0, Math.min(255, Math.round(n))).toString(16).toUpperCase();
        return s.length < 2 ? '0' + s : s;
    }
    var report = { ok: false, backend: 'photopea', photoshop: 'Photopea', clean_layers: job.patches.length, text_layers: [] };
    try {
        var doc = app.activeDocument;
        if (job.texts.length) {
            var grp = doc.layerSets.getByName('TEXT - Thai');
            for (var t = 0; t < job.texts.length; t++) {
                var data = job.texts[t];
                var rect = data.box;
                var fitW = data.text_size ? data.text_size[0] : (rect[2] - rect[0]);
                var fitH = data.text_size ? data.text_size[1] : (rect[3] - rect[1]);
                var lyr = grp.artLayers.getByName(data.name);
                var item = lyr.textItem;
                var size = data.font_size;
                var b = bpx(lyr);
                for (var step = 0; step < 24; step++) {
                    var bw = b[2] - b[0];
                    var bh = b[3] - b[1];
                    if (bw <= 0 || bh <= 0) break;
                    if (bw <= fitW + 2 && bh <= fitH + 2) break;
                    if (size <= 8) break;
                    var ratio = Math.min(fitW / Math.max(1, bw), fitH / Math.max(1, bh));
                    var nextSize = Math.max(8, Math.floor(Math.min(size - 0.5, size * ratio * 0.97) * 2) / 2);
                    if (nextSize >= size) break;
                    size = nextSize;
                    item.size = size;
                    b = bpx(lyr);
                }
                var targetCx = (rect[0] + rect[2]) / 2;
                var targetCy = (rect[1] + rect[3]) / 2;
                if (b[2] > b[0] && b[3] > b[1]) {
                    lyr.translate(targetCx - (b[0] + b[2]) / 2, targetCy - (b[1] + b[3]) / 2);
                }
                if (data.rotation && Math.abs(data.rotation) >= 0.05) {
                    lyr.rotate(data.rotation, AnchorPosition.MIDDLECENTER);
                    var br = bpx(lyr);
                    if (br[2] > br[0] && br[3] > br[1]) {
                        lyr.translate(targetCx - (br[0] + br[2]) / 2, targetCy - (br[1] + br[3]) / 2);
                    }
                }
                b = bpx(lyr);
                var colHex = hex2(data.color[0]) + hex2(data.color[1]) + hex2(data.color[2]);
                applyTextFX(doc,lyr,data,true);
                report.text_layers.push({
                    id: data.id,
                    name: lyr.name,
                    text: item.contents,
                    font: item.font,
                    font_size: size,
                    auto_leading: true,
                    color: colHex,
                    bounds: b,
                    box: rect,
                    rotation: data.rotation || 0,
                    tracking: 0,
                    kerning: 'AutoKernType.METRICS',
                    antialias: 'AntiAlias.SHARP',
                    horizontal_scale: 100,
                    vertical_scale: 100,
                    baseline_shift: 0,
                    language: 'Language.ENGLISHUK',
                    auto_leading_amount: 120
                });
            }
        }
        doc.saveToOE('psd');
        report.ok = true;
    } catch (err) {
        report.error = String(err);
    }
    app.echoToOE('REPORT:' + JSON.stringify(report));
})();
'''


class PhotopeaSession:
    """Reusable headless Playwright Chromium session driving Photopea's Live Messaging API."""

    def __init__(self, url=PHOTOPEA_URL, headless=True, timeout_ms=120000):
        self.url=url
        self.headless=headless
        self.timeout_ms=int(timeout_ms)
        self._playwright=None
        self._browser=None
        self._context=None
        self._page=None
        self._assets={}
        self._captured=[]
        self._loaded_fonts={}
        self._font_alerts=0
        self._hb_ready=False
        self._page_errors=[]

    def start(self):
        if self._page is not None:
            return self
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                'ยังไม่ได้ติดตั้ง playwright ใน .venv กรุณารัน: '
                '.venv\\Scripts\\python.exe -m pip install playwright && '
                '.venv\\Scripts\\python.exe -m playwright install chromium'
            ) from exc
        self._playwright=sync_playwright().start()
        try:
            self._browser=self._playwright.chromium.launch(headless=self.headless)
            self._context=self._browser.new_context(viewport={'width':1280,'height':800})
            self._page=self._context.new_page()
            self._page.on('console',self._on_console)
            self._page.on('pageerror',self._on_pageerror)
            self._page.route(_PHOTOPEA_HOST+'/**',self._handle_route)
            self._page.goto(_PHOTOPEA_HOST+'/')
            self._page.evaluate('([u, t]) => window.__ppInit(u, t)',
                                [self.url+'#%7B%7D', self.timeout_ms])
        except Exception:
            self.close()
            raise
        return self

    def __enter__(self):
        self.start()
        return self

    def __exit__(self,exc_type,exc,tb):
        self.close()
        return False

    def close(self):
        for obj,method in ((self._context,'close'),(self._browser,'close'),(self._playwright,'stop')):
            if obj is not None:
                try:getattr(obj,method)()
                except Exception:pass
        self._page=self._context=self._browser=self._playwright=None
        self._assets.clear()
        self._captured.clear()
        self._loaded_fonts.clear()

    def _on_console(self,msg):
        txt=msg.text or ''
        if 'HB ready' in txt:
            self._hb_ready=True
        if 'Alert: Font ' in txt and ' loaded.' in txt:
            self._font_alerts+=1

    def _on_pageerror(self,err):
        self._page_errors.append(str(err))
        if self._page is not None:
            try:
                self._page.evaluate('r => window.__ppAbort(r)', f'Photopea page error: {err}')
            except Exception:
                pass

    def _handle_route(self,route):
        url=route.request.url
        if url==_PHOTOPEA_HOST+'/':
            route.fulfill(status=200,content_type='text/html; charset=utf-8',body=_PHOTOPEA_HTML)
        elif url.startswith(_PHOTOPEA_HOST+'/asset/'):
            key=url.split('/asset/',1)[1]
            entry=self._assets.get(key)
            if entry is None:
                route.fulfill(status=404,content_type='text/plain',body='not found')
            else:
                data,ctype=entry
                route.fulfill(status=200,content_type=ctype,body=data)
        elif url==_PHOTOPEA_HOST+'/result':
            self._captured.append(route.request.post_data_buffer or b'')
            route.fulfill(status=200,content_type='text/plain',body='ok')
        else:
            route.continue_()

    def _send_asset(self,key,data,content_type):
        self._assets[key]=(data,content_type)
        try:
            return self._page.evaluate(
                '([u, t]) => window.__ppSendAsset(u, t)',
                [f'{_PHOTOPEA_HOST}/asset/{key}', self.timeout_ms],
            )
        finally:
            self._assets.pop(key,None)

    def _run_script(self,script):
        self._page_errors.clear()
        res=self._page.evaluate(
            '([s, u, t]) => window.__ppRunScript(s, u, t)',
            [script, f'{_PHOTOPEA_HOST}/result', self.timeout_ms],
        )
        if self._page_errors:
            raise RuntimeError('Photopea error: '+'; '.join(self._page_errors))
        return res

    def ensure_font(self,choice):
        """Load a local TTF/OTF font into Photopea once per session. Returns (postscript, warning)."""
        try:
            info=resolve_font(choice or FONT_NAME)
        except Exception as exc:
            if choice and choice!=FONT_NAME:
                fallback=resolve_font(FONT_NAME)
                ps,_=self.ensure_font(FONT_NAME)
                return ps,f'ไม่พบฟอนต์ {choice} ในเครื่อง (ใช้ {fallback.choice} แทน): {exc}'
            raise
        if info.path in self._loaded_fonts:
            return self._loaded_fonts[info.path],None
        before_alerts=self._font_alerts
        font_bytes=Path(info.path).read_bytes()
        self._send_asset(f'font_{len(self._loaded_fonts)}.ttf',font_bytes,'application/octet-stream')
        # Photopea parses loaded fonts asynchronously right after sending 'done'.
        for _ in range(25):
            if self._font_alerts>before_alerts:
                break
            self._page.wait_for_timeout(120)
        self._loaded_fonts[info.path]=info.postscript
        return info.postscript,None

    def _close_all_documents(self):
        if self._page is None:
            return
        try:
            self._run_script('while (app.documents.length) { app.activeDocument.close(SaveOptions.DONOTSAVECHANGES); }')
        except Exception:
            pass

    def export_job(self,job,cancel=None):
        """Execute a prepared job inside Photopea, writing the binary PSD to `job['output']`."""
        self.start()
        if cancel is not None and cancel.is_set():
            raise InterruptedError('หยุดส่งออกแล้ว')
        warnings=[]
        payload={
            'font':FONT_POSTSCRIPT,
            'patches':[{'name':p['name'],'left':p.get('left',0),'top':p.get('top',0)} for p in job.get('patches',[])],
            'texts':[],
        }
        if job.get('texts'):
            default_ps,warn=self.ensure_font(FONT_NAME)
            payload['font']=default_ps
            if warn:warnings.append(warn)
            for item in job['texts']:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('หยุดส่งออกแล้ว')
                choice=item.get('font_choice') or FONT_NAME
                ps_name,warn=self.ensure_font(choice)
                if warn and warn not in warnings:
                    warnings.append(warn)
                entry=dict(item)
                entry['font']=ps_name
                payload['texts'].append(entry)

        self._captured.clear()
        self._close_all_documents()
        try:
            bg_bytes=Path(job['background']).read_bytes()
            self._send_asset('background.png',bg_bytes,'image/png')
            for idx,patch in enumerate(job.get('patches',[])):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('หยุดส่งออกแล้ว')
                p_bytes=Path(patch['path']).read_bytes()
                self._send_asset(f'patch_{idx}.png',p_bytes,'image/png')

            res1=self._run_script(_photopea_build_pass1_script(payload))
            pass1_echo=next((e[6:] for e in res1.get('echoes',[]) if e.startswith('PASS1:')),None)
            if not pass1_echo:
                raise RuntimeError('Photopea ไม่ตอบกลับหลังสร้างเลเยอร์')
            pass1=json.loads(pass1_echo)
            if not pass1.get('ok'):
                raise RuntimeError('Photopea สร้างเลเยอร์ไม่สำเร็จ: '+str(pass1.get('error','')))

            if payload['texts']:
                # Wait until Photopea's Thai HarfBuzz shaper finishes rasterizing bounds.
                check_script=(
                    '(function(){'
                    'var g=app.activeDocument.layerSets.getByName("TEXT - Thai");'
                    'var b=g.artLayers[0].bounds;'
                    'app.echoToOE("B:"+b[0].value+","+b[1].value+","+b[2].value+","+b[3].value);'
                    '})();'
                )
                for _ in range(40):
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError('หยุดส่งออกแล้ว')
                    rc=self._run_script(check_script)
                    echo=next((e[2:] for e in rc.get('echoes',[]) if e.startswith('B:')),'')
                    parts=[float(x) for x in echo.split(',')] if echo else []
                    if len(parts)==4 and (parts[2]-parts[0])>0 and (parts[3]-parts[1])>0:
                        break
                    self._page.wait_for_timeout(250)

            res2=self._run_script(_photopea_build_pass2_script(payload))
            rep_echo=next((e[7:] for e in res2.get('echoes',[]) if e.startswith('REPORT:')),None)
            if not rep_echo:
                raise RuntimeError('Photopea ไม่ส่งรายงานผลกลับมา')
            report=json.loads(rep_echo)
            if not report.get('ok'):
                raise RuntimeError(report.get('error','Photopea ส่งออก PSD ไม่สำเร็จ'))
            if warnings:
                report.setdefault('warnings',[]).extend(warnings)
            if not self._captured:
                raise RuntimeError('Photopea ไม่ส่งข้อมูลไฟล์ PSD กลับมา')
            psd_bytes=self._captured[-1]
            if psd_bytes[:4]!=b'8BPS':
                raise RuntimeError('ผลลัพธ์จาก Photopea ไม่ใช่ไฟล์ PSD')
            Path(job['output']).write_bytes(psd_bytes)
            return report
        finally:
            self._captured.clear()
            self._close_all_documents()


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


def _publish_psd_file(source,destination):
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


def _export_psd_photoshop(image,page,destination,include_text=True,cancel=None):
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
        code=Path(__file__).with_name('text_layer_fx.jsx').read_text('utf-8')+'\n'+Path(__file__).with_suffix('.jsx').read_text(encoding='utf-8')
        script.write_text('\ufeffvar job = '+json.dumps(base_job,ensure_ascii=True)+';\n'+code,encoding='utf-8')
        run_script(script,cancel)
        if not Path(job['report']).is_file():raise RuntimeError('Photoshop ไม่ส่งรายงานผลกลับมา')
        report=json.loads(Path(job['report']).read_text(encoding='utf-8-sig'))
        if cancel is not None and cancel.is_set():raise InterruptedError('หยุดส่งออกแล้ว')
        if not report.get('ok'):raise RuntimeError(report.get('error','Photoshop ส่งออกไม่สำเร็จ'))
        font_warnings=_apply_custom_fonts(folder,job,cancel)
        if font_warnings:
            report.setdefault('warnings',[]).extend(font_warnings)
        _publish_psd_file(Path(job['output']),destination)
        report['backend']='photoshop'
        report['destination']=str(destination)
        return report


def _export_psd_photopea(image,page,destination,include_text=True,cancel=None,session=None):
    destination.parent.mkdir(parents=True,exist_ok=True)
    temp_root=_ascii_temp_root(destination.parent)
    with tempfile.TemporaryDirectory(prefix='manhwa-psd-',dir=temp_root) as temporary:
        folder=Path(temporary).resolve()
        job=prepare_job(folder,image,page,include_text)
        if session is not None:
            report=session.export_job(job,cancel=cancel)
        else:
            with PhotopeaSession() as own_session:
                report=own_session.export_job(job,cancel=cancel)
        if cancel is not None and cancel.is_set():
            raise InterruptedError('หยุดส่งออกแล้ว')
        _publish_psd_file(Path(job['output']),destination)
        report['backend']='photopea'
        report['destination']=str(destination)
        return report


def export_psd(image,page,destination,include_text=True,cancel=None,backend=None,session=None):
    destination=Path(destination)
    if destination.suffix.lower()!='.psd':raise ValueError('กรุณาใช้ชื่อไฟล์ .psd')
    if destination.exists():raise FileExistsError(destination)
    if max(image.size)>30000:raise ValueError('PSD รองรับด้านละไม่เกิน 30,000 px กรุณาแบ่งหน้ายาวก่อนส่งออก')
    if cancel is not None and cancel.is_set():raise InterruptedError('หยุดส่งออกแล้ว')
    engine=resolve_psd_backend(backend)
    if engine=='photoshop':
        return _export_psd_photoshop(image,page,destination,include_text=include_text,cancel=cancel)
    return _export_psd_photopea(image,page,destination,include_text=include_text,cancel=cancel,session=session)
