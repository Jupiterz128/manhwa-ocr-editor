"""Build the fixed 640px local typesetting model with ultralytics==8.3.227."""
import hashlib
import os
from pathlib import Path
import requests
import torch
from safetensors.torch import load
from ultralytics.nn.tasks import SegmentationModel


def main():
    folder = Path(os.environ['LOCALAPPDATA']) / 'ManhwaOCREditor/seg_models'
    folder.mkdir(parents=True, exist_ok=True)
    response = requests.get('https://huggingface.co/mayocream/manga109-segmentation-bubble/resolve/main/model.safetensors', timeout=120)
    response.raise_for_status()
    weights = response.content
    if hashlib.sha256(weights).hexdigest() != 'a58cd1b5a12c37a3178302097ed10ae57674bf420c52304e210c7ba19aded5dd':
        raise ValueError('Unexpected Manga109 weights')
    model = SegmentationModel('yolo11n-seg.yaml', nc=1, verbose=False).eval()
    model.load_state_dict(load(weights), strict=True)
    model.model[-1].export = True
    model.model[-1].format = 'torchscript'
    torch.set_num_threads(2)
    with torch.inference_mode():
        sample = torch.zeros(1, 3, 640, 640)
        model(sample)
        compiled = torch.jit.trace(model, sample, check_trace=False)
    temporary = folder / 'manga109-640.partial'
    compiled.save(str(temporary))
    temporary.replace(folder / 'manga109-640.ts')


if __name__ == '__main__':
    main()
