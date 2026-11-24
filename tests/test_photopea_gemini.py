import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_pipeline as cli
import gdrive_sync as drive
import gemini_translator as gt
import photoshop_export as pe
from manhwa_ocr import Bubble, Page


class BackendAndWorkflowTests(unittest.TestCase):
    def test_default_backend_remains_photoshop_and_photopea_mode_skips_photoshop(self):
        self.assertEqual(pe.DEFAULT_PSD_BACKEND, 'photoshop')
        self.assertEqual(pe.resolve_psd_backend(), 'photoshop')
        self.assertEqual(pe.resolve_psd_backend('photopea'), 'photopea')

        def fake_export_job(self_session, job, cancel=None):
            Path(job['output']).write_bytes(b'8BPSphotopea-fixture')
            return {'ok': True, 'backend': 'photopea', 'clean_layers': 0, 'text_layers': []}

        with tempfile.TemporaryDirectory() as folder, \
             patch('photoshop_export.photoshop_progid', side_effect=AssertionError('Must not touch Photoshop')), \
             patch('photoshop_export.run_script', side_effect=AssertionError('Must not run Photoshop COM')), \
             patch.object(pe.PhotopeaSession, 'start', return_value=None), \
             patch.object(pe.PhotopeaSession, 'close', return_value=None), \
             patch.object(pe.PhotopeaSession, 'export_job', new=fake_export_job):
            dest = Path(folder) / 'page01.psd'
            report = pe.export_psd(Image.new('RGB', (32, 32), 'white'), Page(''), dest, backend='photopea')
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_bytes(), b'8BPSphotopea-fixture')
            self.assertEqual(report['backend'], 'photopea')
            self.assertEqual(list(Path(folder).iterdir()), [dest])

    def test_cli_defaults_to_photoshop_and_accepts_photopea_and_auto_translate_gemini(self):
        with patch.object(sys, 'argv', ['cli_pipeline.py', '--input', 'dummy']):
            args = cli.options()
            self.assertEqual(args.psd_backend, 'photoshop')
            self.assertFalse(args.auto_translate_gemini)

        with patch.object(sys, 'argv', ['cli_pipeline.py', '--input', 'dummy', '--psd-backend', 'photopea', '--auto-translate-gemini']):
            args = cli.options()
            self.assertEqual(args.psd_backend, 'photopea')
            self.assertTrue(args.auto_translate_gemini)

        with patch.object(sys, 'argv', ['cli_pipeline.py', '--input', 'dummy', '--auto-translate-gemini', 'false']):
            args = cli.options()
            self.assertFalse(args.auto_translate_gemini)

    def test_workflow_uses_git_pull_photopea_and_auto_translate_gemini(self):
        wf = (Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'auto_manhwa_psd.yml').read_text(encoding='utf-8')
        self.assertIn('git pull', wf)
        self.assertLess(wf.index('git pull'), wf.index('cli_pipeline.py'))
        self.assertIn('default: photopea', wf)
        self.assertIn('--psd-backend', wf)
        self.assertIn('--auto-translate-gemini', wf)


class GeminiSinglePassTranslatorTests(unittest.TestCase):
    def test_notes_and_context_across_batches(self):
        page = Page('p', [Bubble([0,0,30,30], id=str(i), text=f'line {i}') for i in range(3)])
        page.bubbles[0].translation = 'คำเดิม'
        prompts = []
        def respond(**kwargs):
            payload = json.loads(kwargs['contents'].split('\n',1)[1])
            prompts.append(payload)
            bid = payload['items'][0]['id']
            return SimpleNamespace(text=json.dumps({'translations':[{'id':bid,'thai':'คำใหม่'}]}))
        client = MagicMock()
        client.models.generate_content.side_effect = respond
        with patch('google.genai.Client', return_value=client):
            gt.translate_pages_with_gemini([page], api_key='test', max_batch_size=1,
                                           translation_note=' ใช้ข้า/เจ้า ', log=None)
        self.assertEqual([p['translation_note'] for p in prompts], ['ใช้ข้า/เจ้า']*2)
        self.assertEqual([r['id'] for r in prompts[0]['chapter_context']], ['0','1','2'])
        self.assertEqual([r['thai'] for r in prompts[1]['chapter_context']], ['คำเดิม','คำใหม่',''])

    def test_large_batch_repairs_only_missing_or_ambiguous_ids(self):
        bubbles = [Bubble([0, 0, 40, 40], id=f'b{i}', text='Hi') for i in range(152)]
        requests = []
        def respond(**kwargs):
            payload = json.loads(kwargs['contents'].split('\n', 1)[1])
            ids = [item['id'] for item in payload['items']]
            requests.append(ids)
            rows = [{'id': bid, 'thai': 'แปล ' + bid} for bid in reversed(ids)]
            if len(requests) == 1:
                rows = rows[1:] + [rows[1], {'id': 'unknown', 'thai': 'ผิดกรอบ'}]
            return SimpleNamespace(text=json.dumps({'translations': rows}))
        client = MagicMock()
        client.models.generate_content.side_effect = respond
        with patch('google.genai.Client', return_value=client):
            gt.translate_pages_with_gemini([Page('p', bubbles)], api_key='test', log=None)
        self.assertEqual(requests[1:], [['b150', 'b151']])
        self.assertTrue(all(b.translation == 'แปล ' + b.id for b in bubbles))

    def test_duplicate_result_id_rejected_before_applying(self):
        page = Page('p', [Bubble([0,0,30,30], id='a', text='Run!')])
        client = MagicMock()
        row = {'id':'a','thai':'วิ่ง!'}
        client.models.generate_content.return_value.text = json.dumps({'translations':[row,row]})
        with patch('google.genai.Client', return_value=client), self.assertRaises(ValueError):
            gt.translate_pages_with_gemini([page], api_key='test', log=None)
        self.assertFalse(page.bubbles[0].translation)

    def test_single_pass_batch_translation_maps_all_pages_in_one_call(self):
        p1 = Page('01.png', [
            Bubble([10, 10, 120, 80], text='안녕하세요', id='b1', detected_frame_type='normal'),
            Bubble([10, 90, 120, 160], text='멈춰!', id='b2', detected_frame_type='shout'),
        ])
        p2 = Page('02.png', [
            Bubble([10, 10, 120, 80], text='[Level Up!]', id='b3', detected_frame_type='system'),
        ])

        mock_client = MagicMock()
        mock_response = SimpleNamespace(text=json.dumps({
            'translations': [
                {'id': 'b1', 'thai': 'สวัสดี'},
                {'id': 'b2', 'thai': 'หยุดนะ!'},
                {'id': 'b3', 'thai': '[ระบบ] เลเวลอัป!'},
            ]
        }, ensure_ascii=False))
        mock_client.models.generate_content.return_value = mock_response

        with patch('google.genai.Client', return_value=mock_client) as client_cls:
            report = gt.translate_pages_with_gemini([p1, p2], source_lang='ko+en', api_key='test-key', log=None)

        self.assertEqual(client_cls.call_args.kwargs['api_key'], 'test-key')
        self.assertEqual(client_cls.call_args.kwargs['http_options'].api_version, 'v1beta')
        self.assertEqual(mock_client.models.generate_content.call_count, 1)
        self.assertEqual(report['batches'], 1)
        self.assertEqual(report['translated_count'], 3)
        self.assertEqual(report['missing_ids'], [])
        self.assertEqual(p1.bubbles[0].translation, 'สวัสดี')
        self.assertEqual(p1.bubbles[1].translation, 'หยุดนะ!')
        self.assertEqual(p2.bubbles[0].translation, 'เลเวลอัป!')
        self.assertEqual(p2.bubbles[0].frame_type, 'system')

    def test_cli_single_pass_drive_with_auto_translate_gemini_and_photopea(self):
        calls = []

        def folder_images(value, dest):
            dest.mkdir()
            im = dest / '01.png'
            Image.new('RGB', (240, 180), 'white').save(im)
            return 'ep01', [im]

        def scan(_):
            calls.append('scan')
            return [cli.app.Bubble([20, 20, 200, 140], text='Hello!', score=.95, lines=[{'text':'Hello!', 'box':[44,40,118,68]}], id='bub1', detected_frame_type='normal')]

        def fake_translate(pages, **kw):
            calls.append('gemini')
            for p in pages:
                for b in p.bubbles:
                    b.translation = 'สวัสดี!'
            return {
                'translated_count': 1,
                'total_candidates': 1,
                'missing_ids': [],
                'model': 'gemini-2.5-flash',
                'batches': 1,
                'changes': {'bub1': 'สวัสดี!'},
            }

        def fake_pp_export(self_session, job, cancel=None):
            calls.append(('photopea', len(job['texts'])))
            Path(job['output']).write_bytes(b'8BPSsinglepass')
            return {'ok': True, 'backend': 'photopea', 'clean_layers': 1, 'text_layers': job['texts']}

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            for obj, name, kw in [
                (drive, 'ROOT', dict(new=Path(root))),
                (drive, 'download_drive_folder_images', dict(side_effect=folder_images)),
                (drive, 'create_job_subfolder', dict(side_effect=lambda parent, name: parent)),
                (drive, 'upload_file_to_folder', dict(side_effect=lambda p, f: calls.append(p.suffix))),
                (cli, 'translate_pages_with_gemini', dict(side_effect=fake_translate)),
                (cli, 'clean_checked', dict(side_effect=lambda c, r, **kw: (calls.append('clean') or
                    (r.image, r.mask, 'solid', {'needs_review': False})))),
                (pe, 'photoshop_progid', dict(side_effect=AssertionError('Must not open Photoshop'))),
                (pe.PhotopeaSession, 'start', dict(return_value=None)),
                (pe.PhotopeaSession, 'close', dict(return_value=None)),
                (pe.PhotopeaSession, 'export_job', dict(new=fake_pp_export)),
            ]:
                stack.enter_context(patch.object(obj, name, **kw))
            pipeline = stack.enter_context(patch.object(cli.app, 'LocalPipeline'))
            pipeline.return_value.scan.side_effect = scan

            argv = [
                'cli_pipeline.py',
                '--input-mode', 'image_folder',
                '--drive-link', 'src1',
                '--psd-backend', 'photopea',
                '--auto-translate-gemini',
            ]
            with patch.object(sys, 'argv', argv):
                rc = cli.run(cli.options())
            self.assertEqual(rc, 0)
            self.assertEqual(calls, ['scan', 'gemini', '.txt', '.txt', 'clean', ('photopea', 1), '.psd'])


if __name__ == '__main__':
    unittest.main()
