# Lumina reference for Manhwa OCR Editor 3.1

Reviewed on 2026-10-09 at commit `bfbf042aff4c18290198f65c18f31430cd3f5a06`:

- https://github.com/lumina-tl/lumina/blob/bfbf042aff4c18290198f65c18f31430cd3f5a06/src/renderer/lib/canvas/tools/text/font-fit.ts
- https://github.com/lumina-tl/lumina/blob/bfbf042aff4c18290198f65c18f31430cd3f5a06/python/services/inpaint/lama/preprocess.py
- https://github.com/lumina-tl/lumina/blob/bfbf042aff4c18290198f65c18f31430cd3f5a06/python/services/inpaint/lama/postprocess.py
- https://github.com/lumina-tl/lumina/blob/bfbf042aff4c18290198f65c18f31430cd3f5a06/src/renderer/lib/pipeline/inpaint.ts

Source license: MIT, copyright (c) 2026 Lumina contributors. The accompanying LICENSE retains the complete notice.

The Python implementation in this application adapts the ideas of aspect-dependent text fitting, measured wrapped text, bounded text masks and local reconstruction. It is not a port of the Electron application and does not import its runtime or model weights. Thai uses the existing Thai tokenization and Windows Uniscribe; native PSD still uses the existing Photoshop exporter.

Differences: no overflow allowance or forced minimum that clips text; line leading remains Auto 120%, Tracking 0, Metrics and Sharp. The background around holes is validated before gradient reconstruction, and no changed pixels are allowed outside the approved mask. Gaussian feathering outside masks is not adopted. Disconnected residual components are not joined into one convex hull.

Lumina's source license does not cover downloaded models. In particular, its optional RF-DETR segmentation model has non-commercial research restrictions. No Lumina model is installed or redistributed by this update. Existing RapidOCR and LaMa installations remain in use.
