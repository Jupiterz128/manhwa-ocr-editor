"""Reusable Chromium exporter for editable Photopea PSD documents."""
import json
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from text_layout import FONT_NAME, FONT_POSTSCRIPT, resolve_font, _read_font_entry, _match_entry

PHOTOPEA_URL='https://www.photopea.com'


HOST_HTML="""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;overflow:hidden">
<iframe id="pp" style="width:1280px;height:800px;border:0"></iframe>
<script>
const iframe = document.getElementById('pp');
let pending = null;
window.addEventListener('message', (e) => {
  if (!pending || e.source !== iframe.contentWindow) return;
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
    iframe.contentWindow.postMessage(buf, '*', [buf]);
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
                if (size > 8 && (b[2]-b[0] > fitW+2 || b[3]-b[1] > fitH+2)) {
                    var low = 8, high = size;
                    var bestBounds = null;
                    for (var step = 0; step < 5 && high-low >= 0.5; step++) {
                        var trial = step === 0 ? Math.max(8, Math.floor(size*Math.min(
                            fitW/(b[2]-b[0]), fitH/(b[3]-b[1]))*2)/2) : Math.floor(low+high)/2;
                        if (step && trial <= low) break;
                        item.size = trial;
                        size = trial;
                        b = bpx(lyr);
                        if (b[2]-b[0] <= fitW+2 && b[3]-b[1] <= fitH+2) {
                            low = trial;
                            bestBounds = b;
                        } else {
                            high = trial;
                        }
                    }
                    if (size !== low) {
                        item.size = low;
                        size = low;
                        b = bestBounds || bpx(lyr);
                    }
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


class _TransferHandler(BaseHTTPRequestHandler):
    """Stream only the current job's named assets over a session-private loopback URL."""
    def log_message(self, *args):
        pass

    def do_GET(self):
        transfer = self.server
        if self.path == transfer.prefix:
            body = HOST_HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)
            return
        entry = transfer.assets.get(self.path)
        if entry is None:
            self.send_error(404)
            return
        path, mime = entry
        with path.open('rb') as src:
            self.send_response(200)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(path.stat().st_size))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            shutil.copyfileobj(src,self.wfile,1024*1024)

    def do_POST(self):
        transfer = self.server
        output = transfer.output
        if self.path != transfer.prefix+'result' or output is None:
            self.send_error(404)
            return
        length = int(self.headers.get('Content-Length','0'))
        if not 4 <= length <= 2**31:
            self.send_error(400)
            return
        with output.open('wb') as dst:
            while length:
                part = self.rfile.read(min(length,1024*1024))
                if not part:
                    self.send_error(400)
                    return
                dst.write(part)
                length -= len(part)
        self.send_response(200)
        self.end_headers()


class PhotopeaSession:
    """Reusable headless Playwright Chromium session driving Photopea's Live Messaging API."""

    def __init__(self, url=PHOTOPEA_URL, headless=True, timeout_ms=120000, fonts_dir=None):
        self.url=url
        self.headless=headless
        self.timeout_ms=int(timeout_ms)
        self._playwright=None
        self._browser=None
        self._context=None
        self._page=None
        self._server=None
        self._server_thread=None
        self.loaded_fonts: set[str] = set()
        self.fonts_dir = Path(fonts_dir) if fonts_dir else None
        self._custom_fonts = None
        self._font_alerts=0
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
            self._server = ThreadingHTTPServer(('127.0.0.1',0),_TransferHandler)
            self._server.assets = {}
            self._server.output = None
            self._server.prefix = '/'+secrets.token_urlsafe(24)+'/'
            self._host = f'http://127.0.0.1:{self._server.server_port}'+self._server.prefix
            self._server_thread = threading.Thread(target=self._server.serve_forever,args=(.05,),daemon=True)
            self._server_thread.start()
            self._page.goto(self._host,wait_until='domcontentloaded')
            self._page.evaluate('([u, t]) => window.__ppInit(u, t)',
                                [self.url+'#%7B%7D', self.timeout_ms])
        except Exception:
            self.close()
            raise
        return self

    def __enter__(self):
        try:
            self.start()
        except Exception as exc:
            if not self._lost_session(exc):
                raise
            self.close()
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
        if self._server is not None:
            if self._server_thread and self._server_thread.is_alive():
                self._server.shutdown()
                self._server_thread.join()
            self._server.server_close()
            self._server=self._server_thread=None
        self.loaded_fonts.clear()
        self._font_alerts = 0
        self._page_errors.clear()

    def _on_console(self,msg):
        txt=msg.text or ''
        if 'Alert: Font ' in txt and ' loaded.' in txt:
            self._font_alerts+=1

    def _on_pageerror(self,err):
        self._page_errors.append(str(err))
        if self._page is not None:
            try:
                self._page.evaluate('r => window.__ppAbort(r)', f'Photopea page error: {err}')
            except Exception:
                pass

    def _send_asset(self,key,path,content_type):
        route=self._server.prefix+'asset/'+key
        self._server.assets[route]=(Path(path),content_type)
        try:
            return self._page.evaluate(
                '([u, t]) => window.__ppSendAsset(u, t)',
                [self._host+'asset/'+key, self.timeout_ms],
            )
        finally:
            self._server.assets.pop(route,None)

    def _run_script(self,script):
        self._page_errors.clear()
        res=self._page.evaluate(
            '([s, u, t]) => window.__ppRunScript(s, u, t)',
            [script, self._host+'result', self.timeout_ms],
        )
        if self._page_errors:
            raise RuntimeError('Photopea error: '+'; '.join(self._page_errors))
        return res

    def ensure_font(self,choice):
        """Load a local TTF/OTF font into Photopea once per session. Returns (postscript, warning)."""
        try:
            if self.fonts_dir and self._custom_fonts is None:
                self._custom_fonts = [_read_font_entry(p) for p in self.fonts_dir.iterdir()
                                      if p.suffix.lower() in {'.ttf','.otf'}] if self.fonts_dir.is_dir() else []
            entry = _match_entry(choice or FONT_NAME, self._custom_fonts) if self._custom_fonts else None
            if entry:
                path, ps = str(entry['path']), entry['ps']
            else:
                info = resolve_font(choice or FONT_NAME)
                path, ps = str(info.path), info.postscript
        except Exception as exc:
            if choice and choice!=FONT_NAME:
                fallback=resolve_font(FONT_NAME)
                ps,_=self.ensure_font(FONT_NAME)
                return ps,f'ไม่พบฟอนต์ {choice} ในเครื่อง (ใช้ {fallback.choice} แทน): {exc}'
            raise
        if path in self.loaded_fonts:
            return ps,None
        before_alerts=self._font_alerts
        self._send_asset(f'font_{len(self.loaded_fonts)}.ttf',path,'application/octet-stream')
        # Photopea parses loaded fonts asynchronously right after sending 'done'.
        for _ in range(25):
            if self._font_alerts>before_alerts:
                break
            self._page.wait_for_timeout(120)
        self.loaded_fonts.add(path)
        return ps,None

    def _close_all_documents(self):
        if self._page is None:
            return
        try:
            self._run_script('while (app.documents.length) { app.documents[0].close(SaveOptions.DONOTSAVECHANGES); }')
        except Exception:
            self.close()

    @staticmethod
    def _lost_session(exc):
        message = str(exc).lower()
        return any(s in message for s in (
            'timed out', 'timeout', 'did not finish loading', 'crash',
            'has been closed', 'target closed', 'execution context was destroyed',
        ))

    def export_job(self,job,cancel=None):
        """Execute a prepared job inside Photopea, writing the binary PSD to `job['output']`."""
        for attempt in range(2):
            try:
                return self._export_once(job,cancel)
            except Exception as exc:
                if isinstance(exc, InterruptedError) or (cancel and cancel.is_set()):
                    raise
                if attempt or not self._lost_session(exc):
                    raise
                self.close()

    def _export_once(self,job,cancel):
        if cancel is not None and cancel.is_set():
            raise InterruptedError('หยุดส่งออกแล้ว')
        self.start()
        warnings=[]
        payload={
            'font':FONT_POSTSCRIPT,
            'patches':[{'name':p['name'],'left':p.get('left',0),'top':p.get('top',0)} for p in job.get('patches',[])],
            'texts':[],
        }
        if job.get('texts'):
            for item in job['texts']:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('หยุดส่งออกแล้ว')
                choice=item.get('font_choice') or item.get('font') or FONT_NAME
                ps_name,warn=self.ensure_font(choice)
                if warn and warn not in warnings:
                    warnings.append(warn)
                entry=dict(item)
                entry['font']=ps_name
                payload['texts'].append(entry)

        self._server.output=Path(job['output'])
        try:
            self._send_asset('background.png',job['background'],'image/png')
            for idx,patch in enumerate(job.get('patches',[])):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('หยุดส่งออกแล้ว')
                self._send_asset(f'patch_{idx}.png',patch['path'],'image/png')

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
            if res2.get('uploaded') != 1:
                raise RuntimeError('Photopea ไม่ส่งข้อมูลไฟล์ PSD กลับมา')
        except BaseException:
            self.close()
            raise
        self._server.output=None
        self._close_all_documents()
        return report


def export_psd_photopea(image, page, destination, include_text=True, cancel=None,
                        fonts_dir=None, timeout_ms=120_000, session=None):
    """Standalone entry point, optionally sharing a session with other pages."""
    from contextlib import nullcontext
    from photoshop_export import export_psd
    manager = nullcontext(session) if session is not None else PhotopeaSession(
        fonts_dir=fonts_dir, timeout_ms=timeout_ms)
    with manager as active:
        report = export_psd(image,page,destination,include_text,cancel,backend='photopea',session=active)
        return dict(report, output=str(destination), text_layers=len(report['text_layers']),
                    loaded_fonts=sorted(Path(p).name for p in active.loaded_fonts))
