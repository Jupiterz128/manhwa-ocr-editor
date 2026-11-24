"""Local Windows completion sounds, per-player volume fixed at 15%."""
import ctypes
import os
from pathlib import Path
import queue
import threading
import time
import uuid

COMPONENT_VERSION='4.0'
VOLUME=150  # MCI volume range: 0..1000. Set and verify before every play.
SOUNDS={'ocr_all':'levelup.mp3','mask_all':'erro.mp3',
        'clean_all':'za-warudo-stop-time-sound.mp3','psd':'tuturu_1.mp3'}
ASSET_DIR=Path(__file__).resolve().parent/'assets'/'sounds'


class WindowsAudio:
    def __init__(self):
        if os.name!='nt':raise RuntimeError('เสียงแจ้งเตือนใช้ได้บน Windows')
        self.dll=ctypes.WinDLL('winmm')
        self.dll.mciSendStringW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint,ctypes.c_void_p]
        self.dll.mciSendStringW.restype=ctypes.c_uint
        self.dll.mciGetErrorStringW.argtypes=[ctypes.c_uint,ctypes.c_wchar_p,ctypes.c_uint]
        self.alias='manhwa_'+uuid.uuid4().hex
        self.opened=False

    def command(self,command):
        result=ctypes.create_unicode_buffer(1024)
        code=self.dll.mciSendStringW(command,result,len(result),None)
        if code:
            self.dll.mciGetErrorStringW(code,result,len(result))
            raise RuntimeError(result.value or f'Audio error {code}')
        return result.value

    def prepare(self,path):
        self.close()
        path=Path(path).resolve()
        if not path.is_file():raise FileNotFoundError(path.name)
        self.command(f'open "{path}" type mpegvideo alias {self.alias}')
        self.opened=True
        try:
            requested=VOLUME
            for _ in range(4):
                self.command(f'setaudio {self.alias} volume to {requested}')
                actual=int(self.command(f'status {self.alias} volume'))
                if 0<=actual<=VOLUME:
                    self.actual_volume=actual;break
                requested=max(0,requested-max(1,actual-VOLUME))
            else:raise RuntimeError('จำกัดเสียงไม่เกิน 15% ไม่สำเร็จ จึงไม่เล่นเสียง')
        except Exception:
            self.close();raise

    def play(self):
        if not self.opened:raise RuntimeError('ยังไม่ได้เตรียมเสียง')
        self.command(f'play {self.alias} from 0')

    def playing(self):
        return self.opened and self.command(f'status {self.alias} mode')=='playing'

    def close(self):
        if self.opened:
            self.command(f'close {self.alias}')
            self.opened=False


class SoundEffects:
    """One background player: replace the previous cue instead of layering sounds."""
    def __init__(self,log=lambda message:None,backend_factory=WindowsAudio):
        self.log=log;self.backend_factory=backend_factory
        self.requests=queue.Queue();self.closed=threading.Event();self.thread=None

    def play(self,event):
        if self.closed.is_set() or event not in SOUNDS:return
        self.requests.put(ASSET_DIR/SOUNDS[event])
        if self.thread is None:
            self.thread=threading.Thread(target=self._run,name='manhwa-sounds',daemon=True)
            self.thread.start()

    def _run(self):
        backend=None;started=0
        try:
            while not self.closed.is_set():
                try:path=self.requests.get(timeout=.1)
                except queue.Empty:path=None
                if self.closed.is_set():break
                if path is not None:
                    # If work finished in quick succession, use the latest cue only.
                    while True:
                        try:path=self.requests.get_nowait()
                        except queue.Empty:break
                    try:
                        if backend is None:backend=self.backend_factory()
                        backend.prepare(path)
                        if self.closed.is_set():break
                        backend.play();started=time.monotonic()
                    except Exception as exc:
                        self.log('เล่นเสียงแจ้งเตือนไม่ได้ (ไม่กระทบผลงาน): '+str(exc))
                        if backend is not None:
                            try:backend.close()
                            except Exception:pass
                            backend=None
                elif backend is not None:
                    try:
                        if not backend.playing() or time.monotonic()-started>120:backend.close()
                    except Exception as exc:
                        self.log('ปิดเสียงแจ้งเตือน: '+str(exc))
                        try:backend.close()
                        except Exception:pass
                        backend=None
        finally:
            if backend is not None:
                try:backend.close()
                except Exception:pass

    def close(self):
        self.closed.set()
