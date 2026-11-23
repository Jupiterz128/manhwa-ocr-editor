"""Headless Photopea PSD exporter using Playwright (no local Adobe Photoshop required).

Drop this file into the root of Jupiterz128/manhwa-ocr-editor alongside
photoshop_export.py, then wire `backend="photopea"` in photoshop_export.py
and `--psd-backend` in cli_pipeline.py.

Requirements:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from photoshop_export import prepare_job

# Optional folder containing .ttf / .otf Thai fonts to preload into Photopea
DEFAULT_FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

HOST_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;overflow:hidden;">
<iframe id="pp" src="https://www.photopea.com#%7B%22environment%22%3A%7B%7D%7D"
        style="width:1280px;height:800px;border:0;"></iframe>
<script>
  window._ppQueue = [];
  window._ppWaiter = null;
  window.addEventListener("message", function (e) {
    const data = e.data;
    if (window._ppWaiter) {
      const resolve = window._ppWaiter;
      window._ppWaiter = null;
      resolve(data);
    } else {
      window._ppQueue.push(data);
    }
  });

  window.ppNextMessage = function () {
    if (window._ppQueue.length > 0) {
      return Promise.resolve(window._ppQueue.shift());
    }
    return new Promise((resolve) => { window._ppWaiter = resolve; });
  };

  window.ppWaitDone = async function () {
    const buffers = [];
    while (true) {
      const msg = await window.ppNextMessage();
      if (msg === "done") return buffers;
      if (msg instanceof ArrayBuffer) {
        const bytes = new Uint8Array(msg);
        let bin = "";
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) {
          bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        buffers.push(btoa(bin));
      }
    }
  };

  window.ppSendBytes = async function (b64) {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const frame = document.getElementById("pp").contentWindow;
    frame.postMessage(arr.buffer, "*");
    return await window.ppWaitDone();
  };

  window.ppRunScript = async function (scriptText) {
    const frame = document.getElementById("pp").contentWindow;
    frame.postMessage(scriptText, "*");
    return await window.ppWaitDone();
  };
</script>
</body>
</html>
"""


def _build_layout_script(job: dict[str, Any]) -> str:
    """Build Photopea-compatible script mirroring photoshop_export.jsx."""
    payload = json.dumps(
        {
            "font": job.get("font", "THSarabunNew-Bold"),
            "patches": job.get("patches", []),
            "texts": job.get("texts", []),
        },
        ensure_ascii=False,
    )
    return f"""
(function() {{
  var job = {payload};
  var doc = app.documents[0];
  app.activeDocument = doc;
  doc.flatten();
  doc.activeLayer.name = "Background";

  function layerBounds(layer) {{
    var b = layer.bounds;
    return [b[0].as("px"), b[1].as("px"), b[2].as("px"), b[3].as("px")];
  }}

  // Merge any additional opened patch documents (CLEAN layers) onto doc[0]
  for (var p = 0; p < job.patches.length; p++) {{
    if (app.documents.length < 2) break;
    var patchDoc = app.documents[1];
    var patchMeta = job.patches[p];
    app.activeDocument = patchDoc;
    var added = patchDoc.activeLayer.duplicate(doc, ElementPlacement.PLACEATBEGINNING);
    patchDoc.close(SaveOptions.DONOTSAVECHANGES);
    app.activeDocument = doc;
    added.name = patchMeta.name || "CLEAN";
    var pb = layerBounds(added);
    added.translate((patchMeta.left || 0) - pb[0], (patchMeta.top || 0) - pb[1]);
  }}

  // Create editable Thai text layers inside "TEXT - Thai" group
  if (job.texts && job.texts.length > 0) {{
    var textGroup = doc.layerSets.add();
    textGroup.name = "TEXT - Thai";

    for (var i = 0; i < job.texts.length; i++) {{
      var data = job.texts[i];
      var rect = data.box || data.bubble_box || [0, 0, 200, 100];
      var layer = textGroup.artLayers.add();
      layer.name = data.name || ("TEXT " + (i + 1));
      layer.kind = LayerKind.TEXT;

      var item = layer.textItem;
      item.kind = TextType.POINTTEXT;
      item.contents = String(data.text || "").replace(/\\n/g, "\\r");
      item.font = data.font || job.font;

      var rgb = data.color || [0, 0, 0];
      var sc = new SolidColor();
      sc.rgb.red = rgb[0];
      sc.rgb.green = rgb[1];
      sc.rgb.blue = rgb[2];
      item.color = sc;

      item.justification = Justification.CENTER;
      item.useAutoLeading = true;
      item.autoLeadingAmount = 120;

      var size = Number(data.font_size || 24);
      item.size = size;
      item.position = [(rect[0] + rect[2]) / 2, rect[1] + size];

      var fitW = Math.max(16, rect[2] - rect[0]);
      var fitH = Math.max(16, rect[3] - rect[1]);
      for (var step = 0; step < 30; step++) {{
        var tb = layerBounds(layer);
        var w = tb[2] - tb[0];
        var h = tb[3] - tb[1];
        if ((w <= fitW && h <= fitH) || size <= 8) break;
        size = Math.max(8, size - 1);
        item.size = size;
      }}

      var cb = layerBounds(layer);
      var cx = (rect[0] + rect[2]) / 2;
      var cy = (rect[1] + rect[3]) / 2;
      layer.translate(cx - (cb[0] + cb[2]) / 2, cy - (cb[1] + cb[3]) / 2);

      var rot = Number(data.rotation || 0);
      if (Math.abs(rot) > 0.1) {{
        layer.rotate(rot, AnchorPosition.MIDDLECENTER);
        var rb = layerBounds(layer);
        layer.translate(cx - (rb[0] + rb[2]) / 2, cy - (rb[1] + rb[3]) / 2);
      }}
    }}
  }}
}})();
"""


def _collect_font_files(fonts_dir: Path | None) -> list[Path]:
    folder = fonts_dir or DEFAULT_FONTS_DIR
    if not folder.exists():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in {".ttf", ".otf"}
    )


def export_psd_photopea(
    image: Any,
    page: Any,
    destination: str | os.PathLike[str],
    include_text: bool = True,
    cancel: Any = None,
    fonts_dir: Path | None = None,
    timeout_ms: int = 120_000,
) -> dict[str, Any]:
    """Export an editable .psd via headless Chromium + Photopea Live API."""
    from playwright.sync_api import sync_playwright

    dest = Path(destination)
    if dest.suffix.lower() != ".psd":
        raise ValueError(f"Destination must end with .psd: {dest}")
    if dest.exists():
        raise FileExistsError(f"Destination already exists: {dest}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="manhwa_photopea_"))
    try:
        job = prepare_job(tmp_dir, image, page, include_text=include_text)
        font_files = _collect_font_files(fonts_dir)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            browser_page = context.new_page()
            browser_page.set_default_timeout(timeout_ms)

            browser_page.set_content(HOST_HTML, wait_until="domcontentloaded")
            # Wait for Photopea initial ready signal ("done")
            browser_page.evaluate("() => window.ppWaitDone()")

            # 1. Preload Thai fonts (.ttf / .otf) into Photopea session
            for font_path in font_files:
                b64_font = base64.b64encode(font_path.read_bytes()).decode("ascii")
                browser_page.evaluate("(b) => window.ppSendBytes(b)", b64_font)

            # 2. Load background.png
            bg_b64 = base64.b64encode(Path(job["background"]).read_bytes()).decode("ascii")
            browser_page.evaluate("(b) => window.ppSendBytes(b)", bg_b64)

            # 3. Load clean patch PNGs (if any)
            for patch in job.get("patches", []):
                patch_b64 = base64.b64encode(Path(patch["path"]).read_bytes()).decode("ascii")
                browser_page.evaluate("(b) => window.ppSendBytes(b)", patch_b64)

            # 4. Run layer assembly & editable Thai text creation script
            layout_js = _build_layout_script(job)
            browser_page.evaluate("(s) => window.ppRunScript(s)", layout_js)

            # 5. Export PSD bytes from Photopea
            buffers: list[str] = browser_page.evaluate(
                "() => window.ppRunScript(\"app.activeDocument.saveToOE('psd');\")"
            )
            browser.close()

        if not buffers:
            raise RuntimeError("Photopea did not return PSD binary data.")

        psd_bytes = base64.b64decode(buffers[-1])
        if not psd_bytes.startswith(b"8BPS"):
            raise RuntimeError("Exported file is not a valid PSD (missing 8BPS header).")

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(psd_bytes)

        return {
            "backend": "photopea",
            "output": str(dest),
            "clean_layers": len(job.get("patches", [])),
            "text_layers": len(job.get("texts", [])),
            "loaded_fonts": [f.name for f in font_files],
            "warnings": [],
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)