"""Prepare and smoke-test optional PP-OCRv5 ONNX language models (EN, KO, ZH)."""
from PIL import Image
from manhwa_ocr import RapidEngine


def main():
    for language in ('en', 'ko+en', 'zh+en'):
        print(f'Preparing RapidOCR {language}: first run downloads models. Please wait...', flush=True)
        engine = RapidEngine(language)
        engine.read(Image.new('RGB', (160, 80), 'white'))
        print(f'RapidOCR {language} ready.', flush=True)
        del engine


if __name__ == '__main__':
    main()
