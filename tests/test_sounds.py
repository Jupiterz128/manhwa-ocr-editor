import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sound_effects import WindowsAudio,SoundEffects,SOUNDS,ASSET_DIR,VOLUME


class SoundTests(unittest.TestCase):
    def player(self,volumes):
        player=WindowsAudio.__new__(WindowsAudio)
        player.alias='fixture';player.opened=False
        answers=iter(volumes);calls=[]
        def command(value):
            calls.append(value)
            return str(next(answers)) if value=='status fixture volume' else ''
        player.command=command
        return player,calls

    def test_volume_verified_before_play_and_driver_rounding_never_exceeds_cap(self):
        player,calls=self.player([151,149])
        player.prepare(ASSET_DIR/SOUNDS['ocr_all']);player.play()
        self.assertEqual(player.actual_volume,149)
        self.assertEqual(calls[-1],'play fixture from 0')
        self.assertLess(calls.index('setaudio fixture volume to 149'),calls.index('play fixture from 0'))
        player.close();self.assertEqual(calls[-1],'close fixture')

    def test_failed_volume_cap_closes_player_without_playing(self):
        player,calls=self.player([1000]*4)
        with self.assertRaises(RuntimeError):player.prepare(ASSET_DIR/SOUNDS['psd'])
        self.assertFalse(player.opened)
        self.assertFalse(any(command.startswith('play ') for command in calls))
        self.assertEqual(calls[-1],'close fixture')

    def test_missing_sound_never_opens_audio_device(self):
        player,calls=self.player([])
        with self.assertRaises(FileNotFoundError):player.prepare(ASSET_DIR/'missing.mp3')
        self.assertEqual(calls,[])

    def test_assets_and_mapping_match_requested_files(self):
        self.assertEqual(SOUNDS,{'ocr_all':'levelup.mp3','mask_all':'erro.mp3',
            'clean_all':'za-warudo-stop-time-sound.mp3','psd':'tuturu_1.mp3'})
        self.assertEqual(VOLUME,150)
        for name in SOUNDS.values():self.assertGreater((ASSET_DIR/name).stat().st_size,1000)

    def test_play_is_async_and_close_releases_backend(self):
        played=threading.Event();closed=threading.Event()
        backend=Mock();backend.play.side_effect=played.set;backend.playing.return_value=True
        backend.close.side_effect=closed.set
        sounds=SoundEffects(backend_factory=lambda:backend)
        sounds.play('mask_all')
        self.assertTrue(played.wait(2))
        backend.prepare.assert_called_once_with(ASSET_DIR/'erro.mp3')
        sounds.close();sounds.thread.join(2)
        self.assertTrue(closed.is_set());self.assertFalse(sounds.thread.is_alive())
        sounds.play('psd');backend.play.assert_called_once()

    def test_audio_failure_is_reported_without_playing(self):
        reported=threading.Event();messages=[]
        def log(message):messages.append(message);reported.set()
        backend=Mock();backend.prepare.side_effect=RuntimeError('decoder unavailable')
        sounds=SoundEffects(log=log,backend_factory=lambda:backend)
        sounds.play('clean_all')
        self.assertTrue(reported.wait(2))
        sounds.close();sounds.thread.join(2)
        self.assertIn('decoder unavailable',messages[0]);backend.play.assert_not_called()
