"""Mask-only BrushNet repair. A short-lived worker isolates CUDA and forked diffusers."""
import gc
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import cv2
import numpy as np
from PIL import Image


def repair(region, cleaner, page=None):
    mask = ((region.mask > 0) & (region.safe > 0)).astype(np.uint8) * 255
    bounds = Image.fromarray(mask).getbbox()
    if not bounds:
        raise ValueError('ระบาย Mask ก่อนใช้ AI วาดซ่อม')
    import torch
    if not torch.cuda.is_available():
        raise ValueError('AI วาดซ่อมต้องใช้การ์ดจอ CUDA')
    vendor = Path(sys.prefix) / 'brushnet'
    if not (vendor / 'diffusers').is_dir():
        raise ValueError('ยังไม่ได้ติดตั้ง BrushNet ใน venv ตาม requirements-brushnet.txt')
    # Preview sessions share this cleaner under the API lock. Reload LaMa on next use.
    if cleaner is not None:
        cleaner.lama = None
        cleaner._lama_status = None
    gc.collect()
    torch.cuda.empty_cache()
    x,y,r,b = bounds
    source = region.image if page is None else page
    ox, oy = (0, 0) if page is None else region.box[:2]
    w,h = source.size
    roi = (max(0,ox+x-160), max(0,oy+y-160), min(w,ox+r+160), min(h,oy+b+160))
    src = source.crop(roi).convert('RGB')
    m = Image.new('L', src.size)
    m.paste(Image.fromarray(mask), (ox-roi[0],oy-roi[1]))
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        src.resize((512,512), Image.Resampling.LANCZOS).save(work/'source.png')
        m.resize((512,512), Image.Resampling.NEAREST).save(work/'mask.png')
        try:
            subprocess.run([sys.executable, __file__, folder], check=True, timeout=1800,
                           env={**os.environ, 'PYTHONPATH': str(vendor), 'HF_HUB_DOWNLOAD_TIMEOUT':'90'},
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired as exc:
            raise ValueError('AI วาดซ่อมหมดเวลารอ ตรวจสอบการดาวน์โหลดโมเดลแล้วลองอีกครั้ง') from exc
        except subprocess.CalledProcessError as exc:
            detail = work/'error.txt'
            if detail.exists():
                raise ValueError(detail.read_text(encoding='utf8')) from exc
            raise ValueError('AI วาดซ่อมหยุดทำงาน กรุณาลองอีกครั้ง') from exc
        except OSError as exc:
            raise ValueError('AI วาดซ่อมไม่สำเร็จ ตรวจสอบอินเทอร์เน็ต พื้นที่จัดเก็บ และหน่วยความจำ GPU') from exc
        with Image.open(work/'result.png') as result:
            fill = result.convert('RGB').resize(src.size, Image.Resampling.LANCZOS)
    distance = cv2.distanceTransform(np.asarray(m), cv2.DIST_L2, 5)
    alpha = Image.fromarray(np.uint8(np.clip(distance/4,0,1)*255))
    output = region.image.convert('RGB')
    output.paste(fill, (roi[0]-ox,roi[1]-oy), alpha)
    return output, mask


def worker(work):
    import torch
    from diffusers import BrushNetModel, StableDiffusionBrushNetPipeline
    total = torch.cuda.get_device_properties(0).total_memory
    torch.cuda.set_per_process_memory_fraction(min(1.0, (6 * 1024**3) / total))
    config = dict(torch_dtype=torch.float16, variant='fp16', use_safetensors=True)
    brush = BrushNetModel.from_pretrained('Sanster/brushnet_segmentation_mask', **config)
    pipe = StableDiffusionBrushNetPipeline.from_pretrained(
        'stable-diffusion-v1-5/stable-diffusion-v1-5', brushnet=brush,
        safety_checker=None, requires_safety_checker=False, **config)
    pipe.enable_attention_slicing()
    pipe.enable_model_cpu_offload()
    mask = Image.open(work/'mask.png').convert('RGB')
    image = Image.open(work/'source.png').convert('RGB')
    image.paste((0,0,0), mask=mask.convert('L'))
    with torch.inference_mode():
        pipe('', image, mask, num_inference_steps=22).images[0].save(work/'result.png')
    print(f"BrushNet peak CUDA reserved: {torch.cuda.max_memory_reserved()/1024**3:.2f} GiB", flush=True)
    del pipe, brush
    gc.collect()
    torch.cuda.empty_cache()


if __name__ == '__main__':
    work = Path(sys.argv[1])
    try:
        worker(work)
    except Exception as exc:
        import torch
        if isinstance(exc, torch.cuda.OutOfMemoryError):
            reason = 'หน่วยความจำ GPU ไม่พอ กรุณาปิดงานอื่นที่ใช้ GPU แล้วลองอีกครั้ง'
        elif isinstance(exc, OSError):
            reason = 'โหลดโมเดลวาดซ่อมไม่สำเร็จ ตรวจสอบอินเทอร์เน็ตและพื้นที่จัดเก็บ'
        else:
            reason = 'AI วาดซ่อมไม่สำเร็จ'
        (work/'error.txt').write_text(f'{reason} ({type(exc).__name__})', encoding='utf8')
        raise
