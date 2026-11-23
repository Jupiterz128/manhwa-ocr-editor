"""Download PP-OCRv5 weights from Paddle's official model archives."""
import argparse
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

MODELS = [
    'PP-OCRv5_mobile_det',
    'korean_PP-OCRv5_mobile_rec',
    'en_PP-OCRv5_mobile_rec',
    'PP-OCRv5_mobile_rec',
]
BASE = 'https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/'
FILES = {'inference.json', 'inference.pdiparams', 'inference.yml', 'inference.pdmodel'}

def complete(path):
    return (path/'inference.pdiparams').is_file() and (path/'inference.yml').is_file() and (
        (path/'inference.json').is_file() or (path/'inference.pdmodel').is_file())

def download(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name in MODELS:
        target = destination/name
        if complete(target):
            print('Already available:', name, flush=True)
            continue
        url = BASE + name + '_infer.tar'
        print('Downloading:', name, flush=True)
        with tempfile.TemporaryDirectory(dir=destination) as temp:
            temp = Path(temp)
            archive = temp/'weights.tar'
            with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as f:
                shutil.copyfileobj(response, f, length=1024*1024)
            stage = temp/'model'
            stage.mkdir()
            with tarfile.open(archive) as tar:
                for member in tar:
                    basename = Path(member.name).name
                    if member.isfile() and basename in FILES:
                        source = tar.extractfile(member)
                        if source is not None:
                            with source, (stage/basename).open('wb') as f:
                                shutil.copyfileobj(source, f)
            if not complete(stage):
                raise RuntimeError('Incomplete model archive: '+name)
            (stage/'MODEL_SOURCE.txt').write_text(url+'\n', encoding='utf-8')
            target.mkdir(exist_ok=True)
            for p in stage.iterdir():
                os.replace(p, target/p.name)
            print('Ready:', target, flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, default=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'ManhwaOCREditor'/'paddle_models')
    args = parser.parse_args()
    download(args.destination.resolve())
