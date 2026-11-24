import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
import sys

from PIL import Image
import cli_pipeline as cli
import gdrive_sync as drive


class DriveJobs(unittest.TestCase):
    def test_links(self):
        for link in ['abc_1','https://drive.google.com/file/d/abc_1/view',
                     'https://drive.google.com/drive/folders/abc_1','https://drive.google.com/open?id=abc_1']:
            self.assertEqual(drive.drive_id(link),'abc_1')
        with self.assertRaises(ValueError):drive.drive_id('../outside')

    def test_translation_round_trip(self):
        calls=[]
        def folder_images(value,dest):
            dest.mkdir()
            im=dest/'01.png'
            Image.new('RGB',(200,150),'white').save(im)
            return 'chapter',[im]
        def translation(value,dest):
            dest.mkdir()
            txt=dest/'translated.txt'
            txt.write_text('1. สวัสดี',encoding='utf8')
            return txt,txt.name
        def scan(_):
            calls.append('scan')
            return [cli.app.Bubble([30,30,150,110],text='Hello',score=.9,detected_frame_type='normal')]
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            for obj,name,kw in [
                (drive,'ROOT',dict(new=Path(root))),
                (drive,'download_drive_folder_images',dict(side_effect=folder_images)),
                (drive,'download_drive_file',dict(side_effect=translation)),
                (drive,'create_job_subfolder',dict(side_effect=lambda parent,name:parent)),
                (drive,'upload_file_to_folder',dict(side_effect=lambda p,f:calls.append(p.suffix))),
                (cli,'export_psd',dict(side_effect=lambda im,pg,p: (p.touch() or {}))),
                (cli,'clean_checked',dict(side_effect=lambda c,r,**kw:(calls.append('clean') or
                        (r.image,r.mask,'solid',{'needs_review':False})))),
            ]:stack.enter_context(patch.object(obj,name,**kw))
            pipeline=stack.enter_context(patch.object(cli.app,'LocalPipeline'))
            pipeline.return_value.scan.side_effect=scan
            args=['cli','--input-mode','image_folder','--drive-link','source']
            with patch.object(sys,'argv',args):
                self.assertEqual(cli.run(cli.options()),0)
            self.assertEqual(pipeline.call_args.kwargs['mode'],'rapid')
            with patch.object(sys,'argv',args+['--translation-link','back']):
                self.assertEqual(cli.run(cli.options()),0)
            self.assertEqual(calls,['scan','.txt','.txt','clean','.psd','.psd'])

    def test_drive_uploads_selected_outputs(self):
        import json

        def images(source_id, dest):
            dest.mkdir()
            for name in ('01.png','02.png'):
                Image.new('RGB',(40,40),'white').save(dest/name)
            return 'ตอน 01',list(dest.iterdir())

        for fmt in ('mtp','all','psd+mtp'):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as root, ExitStack() as stack:
                uploads=[]
                def upload(path,folder):
                    self.assertTrue(path.is_file())
                    if folder=='outputs':
                        uploads.append(path.name)
                        if path.suffix=='.mtp':
                            data=json.loads(path.read_text('utf-8'))
                            self.assertEqual(len(data['files']),2)
                stack.enter_context(patch.object(drive,'ROOT',Path(root)))
                stack.enter_context(patch.object(drive,'download_drive_folder_images',side_effect=images))
                stack.enter_context(patch.object(drive,'create_job_subfolder',side_effect=['text','outputs']))
                stack.enter_context(patch.object(drive,'upload_file_to_folder',side_effect=upload))
                stack.enter_context(patch.object(cli.app,'LocalPipeline')).return_value.scan.return_value=[]
                session=stack.enter_context(patch.object(cli,'PhotopeaSession'))
                psd=stack.enter_context(patch.object(cli,'export_psd',side_effect=lambda im,page,path:(path.touch() or {})))
                argv=['cli','--drive-link','src','--input-mode','image_folder','--psd-backend','photopea','--export-format',fmt]
                with patch.object(sys,'argv',argv):
                    self.assertEqual(cli.run(cli.options()),0)
                if fmt=='mtp':
                    session.assert_not_called()
                    psd.assert_not_called()
                    self.assertEqual(uploads,['ตอน 01.mtp'])
                else:
                    self.assertEqual(uploads,['01.psd','02.psd','ตอน 01.mtp'])
