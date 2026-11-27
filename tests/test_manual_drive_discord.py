import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error as urllib_error

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_pipeline as cli
import gdrive_sync as drive


def _fake_layout(work, bubble, **kw):
    return {
        'text': bubble.translation,
        'translation': bubble.translation,
        'box': list(bubble.box),
        'bubble_box': list(bubble.box),
        'font': 'THSarabunNew-Bold',
        'font_size': 24,
        'color': [0, 0, 0],
        'auto_leading': True,
        'alignment': 'center',
    }


class _FakeHTTPResponse:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def getcode(self):
        return self.status


class DiscordWebhookTests(unittest.TestCase):
    def test_send_discord_notification_posts_embed_json(self):
        captured = {}

        def fake_urlopen(req, timeout=15):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['headers'] = dict(req.header_items())
            captured['body'] = json.loads(req.data.decode('utf-8'))
            captured['timeout'] = timeout
            return _FakeHTTPResponse(204)

        with patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/123/abc'}, clear=False), \
             patch.object(cli.urllib_request, 'urlopen', side_effect=fake_urlopen):
            ok = cli.send_discord_notification(
                title='📝 พร้อมแปลแล้ว: EP01',
                description='วาง 02_translated.txt ลง Drive เดิมได้เลย',
                fields=[{'name': 'จำนวนบับเบิล', 'value': '12 บับเบิล', 'inline': True}],
                url='https://drive.google.com/drive/folders/folder123',
            )

        self.assertTrue(ok)
        self.assertEqual(captured['url'], 'https://discord.com/api/webhooks/123/abc')
        self.assertEqual(captured['method'], 'POST')
        self.assertIn('User-agent', captured['headers'])
        embed = captured['body']['embeds'][0]
        self.assertEqual(embed['title'], '📝 พร้อมแปลแล้ว: EP01')
        self.assertEqual(embed['url'], 'https://drive.google.com/drive/folders/folder123')
        self.assertEqual(embed['fields'][0]['value'], '12 บับเบิล')

    def test_missing_discord_webhook_url_does_not_crash_or_call_http(self):
        env = dict(os.environ)
        env.pop('DISCORD_WEBHOOK_URL', None)
        with patch.dict(os.environ, env, clear=True), \
             patch.object(cli.urllib_request, 'urlopen', side_effect=AssertionError('Must not call HTTP')):
            ok = cli.send_discord_notification('title', 'desc')
        self.assertFalse(ok)

    def test_discord_http_error_is_swallowed_gracefully(self):
        with patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/123/abc'}, clear=False), \
             patch.object(cli.urllib_request, 'urlopen', side_effect=urllib_error.URLError('network down')):
            ok = cli.send_discord_notification('title', 'desc')
        self.assertFalse(ok)


class GDriveManualHelpersTests(unittest.TestCase):
    def test_find_translation_file_supports_txt_and_google_doc(self):
        txt_files = [
            {'id': 'f1', 'name': '01_raw_text.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:00:00Z'},
            {'id': 'f2', 'name': '02_translated.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:05:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(txt_files)):
            found = drive.find_translation_file('folder1')
        self.assertEqual(found['id'], 'f2')

        gdoc_files = [
            {'id': 'g1', 'name': '02_translated', 'mimeType': drive.GDOC, 'modifiedTime': '2026-11-27T10:06:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(gdoc_files)):
            found_doc = drive.find_translation_file('folder1')
        self.assertEqual(found_doc['id'], 'g1')
        self.assertEqual(found_doc['mimeType'], drive.GDOC)

    def test_find_translation_file_fallback_accepts_thai_or_any_txt_and_gdoc(self):
        thai_txt_files = [
            {'id': 'sub1', 'name': 'nested', 'mimeType': drive.FOLDER, 'modifiedTime': '2026-11-27T10:20:00Z'},
            {'id': 'img1', 'name': '01.png', 'mimeType': 'image/png', 'modifiedTime': '2026-11-27T10:15:00Z'},
            {'id': 'raw1', 'name': '01_raw_text.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:00:00Z'},
            {'id': 'th66', 'name': 'แปลตอนที่66.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:05:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(thai_txt_files)):
            found = drive.find_translation_file('folder1')
        self.assertIsNotNone(found)
        self.assertEqual(found['id'], 'th66')
        self.assertEqual(found['name'], 'แปลตอนที่66.txt')

        # Fallback also matches .txt by extension even with generic mimeType
        octet_txt = [
            {'id': 'raw1', 'name': '01_raw_text.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:00:00Z'},
            {'id': 'oct1', 'name': 'chapter66_translated.TXT', 'mimeType': 'application/octet-stream', 'modifiedTime': '2026-11-27T10:06:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(octet_txt)):
            found_oct = drive.find_translation_file('folder1')
        self.assertEqual(found_oct['id'], 'oct1')

        # Fallback also matches text/plain without .txt suffix and any-name Google Doc
        plain_no_ext = [
            {'id': 'p1', 'name': 'คำแปลตอน66', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:07:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(plain_no_ext)):
            self.assertEqual(drive.find_translation_file('folder1')['id'], 'p1')

        thai_gdoc = [
            {'id': 'raw1', 'name': '01_raw_text.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:00:00Z'},
            {'id': 'gd66', 'name': 'คำแปลตอนที่ 66', 'mimeType': drive.GDOC, 'modifiedTime': '2026-11-27T10:08:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(thai_gdoc)):
            found_gdoc = drive.find_translation_file('folder1')
        self.assertEqual(found_gdoc['id'], 'gd66')
        self.assertEqual(found_gdoc['mimeType'], drive.GDOC)

    def test_find_translation_file_never_picks_raw_text_file(self):
        only_raw_and_non_text = [
            {'id': 'raw1', 'name': '01_raw_text.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:00:00Z'},
            {'id': 'raw2', 'name': '01_RAW_TEXT.TXT', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:05:00Z'},
            {'id': 'raw3', 'name': '01_raw_text', 'mimeType': drive.GDOC, 'modifiedTime': '2026-11-27T10:06:00Z'},
            {'id': 'raw4', 'name': '01_Raw_Text', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:07:00Z'},
            {'id': 'img1', 'name': '01.png', 'mimeType': 'image/png', 'modifiedTime': '2026-11-27T10:08:00Z'},
            {'id': 'psd1', 'name': '01.psd', 'mimeType': 'image/vnd.adobe.photoshop', 'modifiedTime': '2026-11-27T10:09:00Z'},
            {'id': 'dir1', 'name': 'sub.txt', 'mimeType': drive.FOLDER, 'modifiedTime': '2026-11-27T10:10:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(only_raw_and_non_text)):
            self.assertIsNone(drive.find_translation_file('folder1'))

    def test_find_translation_file_picks_latest_modified_and_prioritizes_02_translated(self):
        multiple_fallbacks = [
            {'id': 'raw1', 'name': '01_raw_text.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T11:00:00Z'},
            {'id': 'old1', 'name': 'แปลรอบแรก.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:05:00Z'},
            {'id': 'new1', 'name': 'แปลตอนที่66_แก้แล้ว.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:25:00Z'},
            {'id': 'mid1', 'name': 'คำแปลชั่วคราว', 'mimeType': drive.GDOC, 'modifiedTime': '2026-11-27T10:15:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(multiple_fallbacks)):
            picked = drive.find_translation_file('folder1')
        self.assertEqual(picked['id'], 'new1')

        # Priority 1 (`02_translated.txt` or Google Doc `02_translated`) always wins over fallback files
        with_priority_txt = multiple_fallbacks + [
            {'id': 'prio_txt', 'name': '02_translated.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T10:01:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(with_priority_txt)):
            picked_prio = drive.find_translation_file('folder1')
        self.assertEqual(picked_prio['id'], 'prio_txt')

        with_priority_gdoc = multiple_fallbacks + [
            {'id': 'prio_doc', 'name': '02_translated', 'mimeType': drive.GDOC, 'modifiedTime': '2026-11-27T10:02:00Z'},
        ]
        with patch.object(drive, 'children', return_value=iter(with_priority_gdoc)):
            picked_prio_doc = drive.find_translation_file('folder1')
        self.assertEqual(picked_prio_doc['id'], 'prio_doc')

    def test_download_text_record_exports_google_doc_as_text_plain(self):
        mock_files = MagicMock()
        mock_req = MagicMock()
        mock_files.export_media.return_value = mock_req

        def fake_download_request(req, dest):
            self.assertIs(req, mock_req)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_text('1. สวัสดีจาก Google Docs', encoding='utf-8')
            return Path(dest)

        record = {'id': 'doc123', 'name': '02_translated', 'mimeType': drive.GDOC}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(drive, 'files', return_value=mock_files), \
             patch.object(drive, '_download_request', side_effect=fake_download_request):
            dest, text = drive.download_text_record(record, Path(tmp) / '02_translated.txt')
            self.assertEqual(dest.read_text('utf-8'), '1. สวัสดีจาก Google Docs')

        mock_files.export_media.assert_called_once_with(fileId='doc123', mimeType='text/plain')
        self.assertEqual(text, '1. สวัสดีจาก Google Docs')

    def test_upsert_file_to_folder_overwrites_existing_file(self):
        mock_files = MagicMock()
        mock_update_req = MagicMock()
        mock_update_req.next_chunk.return_value = (None, {'id': 'existing_id'})
        mock_files.update.return_value = mock_update_req

        existing = [{'id': 'existing_id', 'name': '01_raw_text.txt', 'mimeType': 'text/plain', 'modifiedTime': '2026-11-27T09:00:00Z'}]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(drive, 'files', return_value=mock_files), \
             patch.object(drive, 'children', return_value=iter(existing)):
            local_file = Path(tmp) / '01_raw_text.txt'
            local_file.write_text('hello', encoding='utf-8')
            file_id = drive.upsert_file_to_folder(local_file, 'folderA', remote_name='01_raw_text.txt')

        self.assertEqual(file_id, 'existing_id')
        mock_files.update.assert_called_once()
        mock_files.create.assert_not_called()

    def test_wait_for_translation_file_waits_for_stability_and_non_empty(self):
        # Poll 1: not found
        # Poll 2: 0-byte file (skipped)
        # Poll 3: Google Doc found & stable & non-empty
        gdoc = {'id': 'doc9', 'name': '02_translated', 'mimeType': drive.GDOC, 'modifiedTime': '2026-11-27T10:10:00Z'}
        find_results = [
            None,
            {'id': 'empty1', 'name': '02_translated.txt', 'mimeType': 'text/plain', 'size': '0', 'modifiedTime': '2026-11-27T10:09:00Z'},
            gdoc,
        ]
        now = [0.0]

        def fake_monotonic():
            return now[0]

        def fake_sleep(sec):
            now[0] += sec

        def fake_download(record, dest_path):
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            Path(dest_path).write_text('1. แปลเสร็จแล้ว', encoding='utf-8')
            return Path(dest_path), '1. แปลเสร็จแล้ว'

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(drive, 'find_translation_file', side_effect=find_results), \
             patch.object(drive, 'metadata', return_value=dict(gdoc, trashed=False)), \
             patch.object(drive, 'download_text_record', side_effect=fake_download), \
             patch.object(drive.time, 'monotonic', side_effect=fake_monotonic), \
             patch.object(drive.time, 'sleep', side_effect=fake_sleep):
            res = drive.wait_for_translation_file(
                'folder1',
                Path(tmp) / '02_translated.txt',
                timeout_minutes=5,
                poll_interval=15.0,
                settle_seconds=2.0,
                log=None,
            )

        self.assertIsNotNone(res)
        path, rec, text = res
        self.assertEqual(rec['id'], 'doc9')
        self.assertEqual(text, '1. แปลเสร็จแล้ว')


class ManualDrivePipelineTests(unittest.TestCase):
    def _setup_common_patches(self, stack, root, summary_file, events, *, webhook='https://discord.com/api/webhooks/1/tok'):
        env = {'GITHUB_STEP_SUMMARY': str(summary_file)}
        if webhook is not None:
            env['DISCORD_WEBHOOK_URL'] = webhook
        else:
            env['DISCORD_WEBHOOK_URL'] = ''
        stack.enter_context(patch.dict(os.environ, env, clear=False))
        stack.enter_context(patch.object(drive, 'ROOT', Path(root)))
        stack.enter_context(patch.object(cli, 'POLL_INTERVAL_SECONDS', 15.0))
        stack.enter_context(patch.object(cli, 'resolve_font', return_value=None))
        stack.enter_context(patch.object(cli, 'layout_text', side_effect=_fake_layout))
        stack.enter_context(patch.object(cli, 'export_psd', side_effect=lambda im, pg, p: (events.append(('export_psd', p.name, [b.translation for b in pg.bubbles])) or p.touch() or {})))
        stack.enter_context(patch.object(cli, 'clean_checked', side_effect=lambda c, r, **kw: (events.append('clean') or (r.image, r.mask, 'solid', {'needs_review': False}))))

        discord_posts = []

        def fake_urlopen(req, timeout=15):
            discord_posts.append(json.loads(req.data.decode('utf-8'))['embeds'][0])
            return _FakeHTTPResponse(204)

        stack.enter_context(patch.object(cli.urllib_request, 'urlopen', side_effect=fake_urlopen))
        return discord_posts

    def test_manual_drive_polls_and_exports_when_translation_arrives(self):
        events = []

        def folder_images(source_id, dest):
            events.append('download_images')
            dest.mkdir(parents=True, exist_ok=True)
            im = dest / '01.png'
            Image.new('RGB', (200, 150), 'white').save(im)
            return 'EP10', [im]

        def scan(_):
            events.append('scan')
            return [cli.app.Bubble([20, 20, 150, 110], id='b1', text='Hello!', score=0.95, detected_frame_type='normal')]

        def fake_wait(folder_id, dest_path, **kw):
            events.append(('poll', folder_id, kw['timeout_minutes']))
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            Path(dest_path).write_text('รหัส: b1\nคำต้นฉบับ: Hello!\nคำแปลไทย: สวัสดี!', encoding='utf-8')
            return Path(dest_path), {'id': 'tr_th66', 'name': 'แปลตอนที่66.txt', 'mimeType': 'text/plain'}, 'รหัส: b1\nคำต้นฉบับ: Hello!\nคำแปลไทย: สวัสดี!'

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            summary_file = Path(root) / 'step_summary.md'
            discord_posts = self._setup_common_patches(stack, root, summary_file, events)
            stack.enter_context(patch.object(drive, 'metadata', return_value={'id': 'src_ep10', 'name': 'EP10', 'mimeType': drive.FOLDER}))
            stack.enter_context(patch.object(drive, 'find_subfolder', return_value=None))
            stack.enter_context(patch.object(drive, 'get_or_create_subfolder', return_value='txt_folder_10'))
            stack.enter_context(patch.object(drive, 'find_translation_file', return_value=None))
            stack.enter_context(patch.object(drive, 'download_drive_folder_images', side_effect=folder_images))
            stack.enter_context(patch.object(drive, 'upsert_file_to_folder', side_effect=lambda p, f, **kw: (events.append(('upsert', kw.get('remote_name'), f)) or 'raw_file_10')))
            stack.enter_context(patch.object(drive, 'wait_for_translation_file', side_effect=fake_wait))
            stack.enter_context(patch.object(drive, 'create_job_subfolder', return_value='psd_folder_10'))
            stack.enter_context(patch.object(drive, 'upload_file_to_folder', side_effect=lambda p, f: (events.append(('upload_psd', p.name, f)) or 'psd_id_1')))
            stack.enter_context(patch.object(cli.app, 'LocalPipeline')).return_value.scan.side_effect = scan

            argv = ['cli_pipeline.py', '--input-mode', 'image_folder', '--drive-link', 'src_ep10', '--translation-mode', 'manual_drive', '--wait-timeout-minutes', '30']
            with patch.object(sys, 'argv', argv):
                rc = cli.run(cli.options())

            self.assertEqual(rc, 0)
            self.assertIn(('upsert', '01_raw_text.txt', 'txt_folder_10'), events)
            self.assertIn(('poll', 'txt_folder_10', 30.0), events)
            self.assertIn(('export_psd', '01.psd', ['สวัสดี!']), events)
            self.assertIn(('upload_psd', '01.psd', 'psd_folder_10'), events)

            # Verify 3 Discord embeds: waiting + translation found + completion
            self.assertEqual(len(discord_posts), 3)
            self.assertIn('EP10', discord_posts[0]['title'])
            self.assertIn(
                'นำ `01_raw_text.txt` ไปแปล แล้วอัปโหลดไฟล์ `.txt` ชื่ออะไรก็ได้ (หรือสร้าง Google Doc) '
                'ลงโฟลเดอร์ Drive เดิมได้เลย (ขอแค่ไม่ใช่ชื่อ `01_raw_text.txt`)',
                discord_posts[0]['description'],
            )
            self.assertIn('txt_folder_10', discord_posts[0]['url'])
            self.assertIn('แปลตอนที่66.txt', discord_posts[1]['description'])
            self.assertIn('✅ เสร็จเรียบร้อย! ส่งออก PSD ขึ้น Drive แล้ว', discord_posts[2]['title'])
            self.assertIn('psd_folder_10', discord_posts[2]['url'])
            self.assertTrue(any('แปลตอนที่66.txt' in f['value'] for f in discord_posts[2]['fields']))

            # Verify $GITHUB_STEP_SUMMARY contains links, new guidance, and picked filename
            summary = summary_file.read_text('utf-8')
            self.assertIn('https://drive.google.com/drive/folders/txt_folder_10', summary)
            self.assertIn('https://drive.google.com/file/d/raw_file_10/view', summary)
            self.assertIn('https://drive.google.com/drive/folders/psd_folder_10', summary)
            self.assertIn('อัปโหลดไฟล์ `.txt` ชื่ออะไรก็ได้ (หรือสร้าง Google Doc)', summary)
            self.assertIn('แปลตอนที่66.txt', summary)

    def test_manual_drive_timeout_exits_0_and_notifies_discord(self):
        events = []

        def folder_images(source_id, dest):
            dest.mkdir(parents=True, exist_ok=True)
            im = dest / '01.png'
            Image.new('RGB', (200, 150), 'white').save(im)
            return 'EP11', [im]

        def scan(_):
            return [cli.app.Bubble([20, 20, 150, 110], id='b1', text='Wait me', score=0.95, detected_frame_type='normal')]

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            summary_file = Path(root) / 'step_summary.md'
            discord_posts = self._setup_common_patches(stack, root, summary_file, events)
            stack.enter_context(patch.object(drive, 'metadata', return_value={'id': 'src_ep11', 'name': 'EP11', 'mimeType': drive.FOLDER}))
            stack.enter_context(patch.object(drive, 'find_subfolder', return_value=None))
            stack.enter_context(patch.object(drive, 'get_or_create_subfolder', return_value='txt_folder_11'))
            stack.enter_context(patch.object(drive, 'find_translation_file', return_value=None))
            stack.enter_context(patch.object(drive, 'download_drive_folder_images', side_effect=folder_images))
            stack.enter_context(patch.object(drive, 'upsert_file_to_folder', return_value='raw_file_11'))
            stack.enter_context(patch.object(drive, 'wait_for_translation_file', return_value=None))
            stack.enter_context(patch.object(drive, 'create_job_subfolder', side_effect=AssertionError('Must not create PSD folder on timeout')))
            stack.enter_context(patch.object(cli.app, 'LocalPipeline')).return_value.scan.side_effect = scan

            argv = ['cli_pipeline.py', '--input-mode', 'image_folder', '--drive-link', 'src_ep11', '--translation-mode', 'manual_drive', '--wait-timeout-minutes', '60']
            with patch.object(sys, 'argv', argv):
                rc = cli.run(cli.options())

            self.assertEqual(rc, 0)
            self.assertFalse(any(isinstance(e, tuple) and e[0] == 'export_psd' for e in events))
            # Cache must be saved cleanly so Re-run skips OCR/Clean!
            cache_file = Path(root) / 'runner_cache' / 'src_ep11' / 'project_cleaned.json'
            self.assertTrue(cache_file.is_file())
            self.assertFalse((cache_file.parent / 'pending').exists())
            cached_data = json.loads(cache_file.read_text('utf-8'))
            self.assertEqual(cached_data['drive_text_folder_id'], 'txt_folder_11')

            self.assertEqual(len(discord_posts), 2)
            self.assertIn('⏳ หมดเวลารอแล้ว', discord_posts[1]['description'])

    def test_manual_drive_zero_timeout_skips_polling_and_exits_0_without_discord_secret(self):
        events = []

        def folder_images(source_id, dest):
            dest.mkdir(parents=True, exist_ok=True)
            im = dest / '01.png'
            Image.new('RGB', (200, 150), 'white').save(im)
            return 'EP12', [im]

        def scan(_):
            return [cli.app.Bubble([20, 20, 150, 110], id='b1', text='No wait', score=0.95, detected_frame_type='normal')]

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            summary_file = Path(root) / 'step_summary.md'
            discord_posts = self._setup_common_patches(stack, root, summary_file, events, webhook=None)
            stack.enter_context(patch.object(drive, 'metadata', return_value={'id': 'src_ep12', 'name': 'EP12', 'mimeType': drive.FOLDER}))
            stack.enter_context(patch.object(drive, 'find_subfolder', return_value=None))
            stack.enter_context(patch.object(drive, 'get_or_create_subfolder', return_value='txt_folder_12'))
            stack.enter_context(patch.object(drive, 'find_translation_file', return_value=None))
            stack.enter_context(patch.object(drive, 'download_drive_folder_images', side_effect=folder_images))
            stack.enter_context(patch.object(drive, 'upsert_file_to_folder', return_value='raw_file_12'))
            stack.enter_context(patch.object(drive, 'wait_for_translation_file', side_effect=AssertionError('Must not poll when wait_timeout_minutes=0')))
            stack.enter_context(patch.object(cli.app, 'LocalPipeline')).return_value.scan.side_effect = scan

            argv = ['cli_pipeline.py', '--input-mode', 'image_folder', '--drive-link', 'src_ep12', '--translation-mode', 'manual_drive', '--wait-timeout-minutes', '0']
            with patch.object(sys, 'argv', argv):
                rc = cli.run(cli.options())

            self.assertEqual(rc, 0)
            self.assertEqual(discord_posts, [])
            summary = summary_file.read_text('utf-8')
            self.assertIn('https://drive.google.com/drive/folders/txt_folder_12', summary)
            self.assertIn('https://drive.google.com/file/d/raw_file_12/view', summary)

    def test_manual_drive_rerun_with_cache_and_existing_translation_skips_ocr_clean_and_polling(self):
        events = []

        def folder_images(source_id, dest):
            events.append('download_images')
            dest.mkdir(parents=True, exist_ok=True)
            im = dest / '01.png'
            Image.new('RGB', (200, 150), 'white').save(im)
            return 'EP13', [im]

        def scan(_):
            events.append('scan')
            return [cli.app.Bubble([20, 20, 150, 110], id='b1', text='Fight!', score=0.95, detected_frame_type='shout')]

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            summary_file = Path(root) / 'step_summary.md'
            discord_posts = self._setup_common_patches(stack, root, summary_file, events)
            # Run 1: times out after OCR + Clean + saving cache
            stack.enter_context(patch.object(drive, 'metadata', return_value={'id': 'src_ep13', 'name': 'EP13', 'mimeType': drive.FOLDER}))
            stack.enter_context(patch.object(drive, 'find_subfolder', return_value=None))
            stack.enter_context(patch.object(drive, 'get_or_create_subfolder', return_value='txt_folder_13'))
            find_mock = stack.enter_context(patch.object(drive, 'find_translation_file', return_value=None))
            dl_imgs = stack.enter_context(patch.object(drive, 'download_drive_folder_images', side_effect=folder_images))
            upsert_mock = stack.enter_context(patch.object(drive, 'upsert_file_to_folder', return_value='raw_file_13'))
            wait_mock = stack.enter_context(patch.object(drive, 'wait_for_translation_file', return_value=None))
            stack.enter_context(patch.object(drive, 'create_job_subfolder', return_value='psd_folder_13'))
            stack.enter_context(patch.object(drive, 'upload_file_to_folder', side_effect=lambda p, f: (events.append(('upload_psd', p.name, f)) or 'psd_id_13')))
            stack.enter_context(patch.object(cli.app, 'LocalPipeline')).return_value.scan.side_effect = scan

            argv = ['cli_pipeline.py', '--input-mode', 'image_folder', '--drive-link', 'src_ep13', '--translation-mode', 'manual_drive', '--wait-timeout-minutes', '60']
            with patch.object(sys, 'argv', argv):
                self.assertEqual(cli.run(cli.options()), 0)

            events.clear()
            discord_posts.clear()

            # Run 2 (Re-run): a custom-named translation file now exists on Drive!
            find_mock.return_value = {'id': 'tr_13', 'name': 'แปลตอนที่13.txt', 'mimeType': 'text/plain'}
            dl_imgs.side_effect = AssertionError('Re-run with cache must not re-download images')
            upsert_mock.side_effect = AssertionError('Re-run with existing translation must not re-upload 01_raw_text.txt')
            wait_mock.side_effect = AssertionError('Re-run with existing translation must not enter polling loop')

            def fake_download_text(record, dest_path):
                Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
                content = 'รหัส: b1\nคำต้นฉบับ: Fight!\nคำแปลไทย: สู้เลย!'
                Path(dest_path).write_text(content, encoding='utf-8')
                return Path(dest_path), content

            stack.enter_context(patch.object(drive, 'download_text_record', side_effect=fake_download_text))

            with patch.object(sys, 'argv', argv):
                rc2 = cli.run(cli.options())

            self.assertEqual(rc2, 0)
            self.assertNotIn('scan', events)
            self.assertNotIn('clean', events)
            self.assertIn(('export_psd', '01.psd', ['สู้เลย!']), events)
            self.assertIn(('upload_psd', '01.psd', 'psd_folder_13'), events)
            self.assertEqual(len(discord_posts), 2)
            self.assertIn('แปลตอนที่13.txt', discord_posts[0]['description'])
            self.assertIn('✅ เสร็จเรียบร้อย! ส่งออก PSD ขึ้น Drive แล้ว', discord_posts[1]['title'])
            self.assertTrue(any('แปลตอนที่13.txt' in f['value'] for f in discord_posts[1]['fields']))
            self.assertIn('แปลตอนที่13.txt', summary_file.read_text('utf-8'))

    def test_pipeline_error_sends_discord_error_notification(self):
        events = []
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            summary_file = Path(root) / 'step_summary.md'
            discord_posts = self._setup_common_patches(stack, root, summary_file, events)
            stack.enter_context(patch.object(drive, 'metadata', side_effect=RuntimeError('Drive API boom')))

            argv = ['cli_pipeline.py', '--input-mode', 'image_folder', '--drive-link', 'src_err', '--translation-mode', 'manual_drive']
            with patch.object(sys, 'argv', argv), self.assertRaises(RuntimeError):
                cli.run(cli.options())

            self.assertEqual(len(discord_posts), 1)
            self.assertIn('❌', discord_posts[0]['title'])
            self.assertIn('Drive API boom', discord_posts[0]['description'])

    def test_workflow_yaml_defaults_to_manual_drive_with_timeout_and_discord_secret(self):
        wf = (Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'auto_manhwa_psd.yml').read_text(encoding='utf-8')
        self.assertIn('translation_mode:', wf)
        self.assertIn('default: manual_drive', wf)
        self.assertIn('options: [manual_drive, gemini_auto]', wf)
        self.assertIn('wait_timeout_minutes:', wf)
        self.assertIn('default: 60', wf)
        self.assertIn('DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}', wf)
        self.assertIn('--translation-mode', wf)
        self.assertIn('--wait-timeout-minutes', wf)


if __name__ == '__main__':
    unittest.main()
