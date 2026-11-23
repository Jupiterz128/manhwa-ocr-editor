"""Personal Google Drive transfers for the interactive Windows runner."""
from datetime import datetime
from functools import lru_cache
import os
from pathlib import Path, PureWindowsPath
import re
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

COMPONENT_VERSION='3.1'
PSD_OUTPUT_FOLDER_ID='1I7v6bIhIedfuKyonuyD2ddnDNxEwYY4w'
TEXT_EXPORT_FOLDER_ID='1X0jjn7vyQpRauLi7Y6z63nXCnEI07BH4'
ROOT=Path(os.environ['LOCALAPPDATA'])/'ManhwaOCREditor'
SCOPES=['https://www.googleapis.com/auth/drive']
FOLDER='application/vnd.google-apps.folder'
CHUNK=8*1024*1024


def setup():
    secret=ROOT/'client_secret.json'
    if not secret.is_file():
        raise FileNotFoundError(f'Place the Desktop OAuth client file at {secret}')
    flow=InstalledAppFlow.from_client_secrets_file(str(secret),SCOPES)
    creds=flow.run_local_server(host='localhost',port=0,access_type='offline',prompt='consent')
    if not creds.refresh_token:
        raise RuntimeError('Google did not return a refresh token')
    save_credentials(creds)


def save_credentials(creds):
    tmp=ROOT/('gdrive_token.'+uuid4().hex+'.tmp')
    try:
        tmp.write_text(creds.to_json(),encoding='utf-8')
        tmp.replace(ROOT/'gdrive_token.json')
    finally:
        tmp.unlink(missing_ok=True)


@lru_cache(maxsize=1)
def files():
    path=ROOT/'gdrive_token.json'
    if not path.is_file():
        raise RuntimeError('Run SETUP_GDRIVE_AUTH.bat on the runner first')
    creds=Credentials.from_authorized_user_file(str(path),SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        save_credentials(creds)
    return build('drive','v3',credentials=creds,cache_discovery=False).files()


def drive_id(value):
    value=value.strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+',value):
        parsed=urlparse(value)
        if parsed.scheme!='https' or parsed.hostname!='drive.google.com':
            raise ValueError('Expected a Google Drive sharing link or ID')
        match=re.search(r'/(?:folders|file/d)/([A-Za-z0-9_-]+)(?:/|$)',parsed.path)
        value=match[1] if match else parse_qs(parsed.query).get('id',[''])[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]+',value) or PureWindowsPath(value).is_reserved():
        raise ValueError('Invalid Drive ID')
    return value


def local_name(name):
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',name).strip().rstrip('. ')
    if not name or PureWindowsPath(name).is_reserved():
        name='_'+name
    return name


def download(record,dest_dir):
    dest_dir=Path(dest_dir)
    dest_dir.mkdir(parents=True,exist_ok=True)
    dest=dest_dir/local_name(record['name'])
    if dest.exists():
        raise FileExistsError(f'Duplicate Drive filename: {dest.name}')
    tmp=dest_dir/(uuid4().hex+'.part')
    try:
        with tmp.open('xb') as stream:
            transfer=MediaIoBaseDownload(stream,files().get_media(fileId=record['id'],supportsAllDrives=True),chunksize=CHUNK)
            done=False
            while not done:
                _,done=transfer.next_chunk(num_retries=4)
        tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def metadata(value):
    return files().get(fileId=drive_id(value),fields='id,name,mimeType',supportsAllDrives=True).execute(num_retries=4)


def download_drive_file(file_id_or_url,dest_dir):
    record=metadata(file_id_or_url)
    if Path(record['name']).suffix.lower() not in ('.mtp','.txt') or record['mimeType']==FOLDER:
        raise ValueError('Expected an .mtp or .txt file')
    return download(record,dest_dir),record['name']


def children(folder):
    token=None
    while True:
        page=files().list(q=f"'{drive_id(folder)}' in parents and trashed=false",pageToken=token,
                          fields='nextPageToken,files(id,name,mimeType)',pageSize=1000,
                          supportsAllDrives=True,includeItemsFromAllDrives=True).execute(num_retries=4)
        yield from page.get('files',[])
        token=page.get('nextPageToken')
        if not token:break


def download_drive_folder_images(folder_id_or_url,dest_dir):
    from manhwa_ocr import natural_sort_key
    folder=metadata(folder_id_or_url)
    if folder['mimeType']!=FOLDER:raise ValueError('Expected a Google Drive folder')
    images=[r for r in children(folder['id']) if r['mimeType']!=FOLDER and Path(r['name']).suffix.lower() in
            ('.png','.jpg','.jpeg','.webp','.bmp')]
    if not images:raise ValueError('No supported images in the Drive folder')
    images.sort(key=lambda r:natural_sort_key(r['name']))
    return folder['name'],[download(r,dest_dir) for r in images]


def create_job_subfolder(parent_folder_id,folder_name):
    parent=drive_id(parent_folder_id)
    names={r['name'] for r in children(parent)}
    name=folder_name
    if name in names:
        base=folder_name+datetime.now().strftime('_%Y%m%d_%H%M')
        name=base
        n=2
        while name in names:
            name=f'{base}_{n}'
            n+=1
    return files().create(body=dict(name=name,mimeType=FOLDER,parents=[parent]),
                          fields='id',supportsAllDrives=True).execute()['id']


def upload_file_to_folder(local_path,folder_id,remote_name=None):
    path=Path(local_path)
    media=MediaFileUpload(str(path),resumable=True,chunksize=CHUNK)
    try:
        req=files().create(body=dict(name=remote_name or path.name,parents=[drive_id(folder_id)]),
                           media_body=media,fields='id',supportsAllDrives=True)
        result=None
        while result is None:
            _,result=req.next_chunk(num_retries=4)
    finally:
        media.stream().close()
    print(f'Uploaded {path.name}: https://drive.google.com/drive/folders/{folder_id}',flush=True)
    return result['id']


if __name__=='__main__':
    setup()
