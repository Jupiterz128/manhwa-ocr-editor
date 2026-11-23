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
            return [cli.app.Bubble([30,30,150,110],text='Hello',detected_frame_type='normal')]
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
