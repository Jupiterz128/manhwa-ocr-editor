"""Download the publisher's LaMa weights once, then test the app's adapter."""
import argparse
import os
from bubble_cleaner import LAMA_URL,lama_model_path,Cleaner


def main():
    parser=argparse.ArgumentParser(description='Prepare or check local LaMa. OCR packages are not changed.')
    options=parser.add_mutually_exclusive_group()
    options.add_argument('--check',action='store_true',help='Check only; never download')
    options.add_argument('--redownload',action='store_true',help='Replace weights only after validating a fresh download')
    args=parser.parse_args()
    if args.check:
        status=Cleaner().check_lama();print(status['message'],flush=True)
        print('Model:',status['path'],flush=True)
        return 0 if status['ready'] else 1
    try:import torch
    except ImportError:
        print('PyTorch is unavailable in this environment. Run install_windows.bat first.',flush=True)
        return 1
    path=lama_model_path();path.parent.mkdir(parents=True,exist_ok=True)
    if not path.is_file() or args.redownload:
        temporary=path.with_suffix('.download')
        print('Downloading LaMa weights from the model publisher. This can take several minutes.',flush=True)
        try:
            torch.hub.download_url_to_file(LAMA_URL,str(temporary),progress=True)
            # Validate the checkpoint before it becomes the active model.
            model=torch.jit.load(str(temporary),map_location='cpu').eval()
            with torch.inference_mode():
                mask=torch.zeros((1,1,64,64));mask[:,:,24:40,24:40]=1
                output=model(torch.ones((1,3,64,64)),mask)
                if tuple(output.shape)!=(1,3,64,64) or not torch.isfinite(output).all():
                    raise RuntimeError('Downloaded LaMa failed the inference check; existing model was preserved.')
            del model,output
            os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)
    print('Testing LaMa on a small image...',flush=True)
    status=Cleaner().check_lama()
    if not status['ready']:
        print(status['message'],flush=True)
        print('For damaged weights: install_clean_lama_windows.bat --redownload',flush=True)
        return 1
    print('LaMa is ready. Return to CLEAN and select lama.',flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
