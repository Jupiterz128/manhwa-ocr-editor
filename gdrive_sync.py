"""Personal Google Drive transfers for the interactive Windows runner."""
from datetime import datetime
from functools import lru_cache
import os
from pathlib import Path, PureWindowsPath
import re
import time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

COMPONENT_VERSION='4.1'
PSD_OUTPUT_FOLDER_ID='1I7v6bIhIedfuKyonuyD2ddnDNxEwYY4w'
TEXT_EXPORT_FOLDER_ID='1X0jjn7vyQpRauLi7Y6z63nXCnEI07BH4'
ROOT=Path(os.environ['LOCALAPPDATA'])/'ManhwaOCREditor'
SCOPES=['https://www.googleapis.com/auth/drive']
FOLDER='application/vnd.google-apps.folder'
GDOC='application/vnd.google-apps.document'
RAW_TEXT_FILENAME='01_raw_text.txt'
TRANSLATED_TEXT_FILENAME='02_translated.txt'
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
        if parsed.scheme!='https' or parsed.hostname not in ('drive.google.com','docs.google.com'):
            raise ValueError('Expected a Google Drive sharing link or ID')
        match=re.search(r'/(?:folders|file/d|document/d)/([A-Za-z0-9_-]+)(?:/|$)',parsed.path)
        value=match[1] if match else parse_qs(parsed.query).get('id',[''])[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]+',value) or PureWindowsPath(value).is_reserved():
        raise ValueError('Invalid Drive ID')
    return value


def folder_url(folder_id):
    return f'https://drive.google.com/drive/folders/{drive_id(folder_id)}'


def file_url(file_id):
    return f'https://drive.google.com/file/d/{drive_id(file_id)}/view'


def local_name(name):
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',name).strip().rstrip('. ')
    if not name or PureWindowsPath(name).is_reserved():
        name='_'+name
    return name


def _download_request(request,dest):
    dest=Path(dest)
    dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=dest.parent/(uuid4().hex+'.part')
    try:
        with tmp.open('xb') as stream:
            transfer=MediaIoBaseDownload(stream,request,chunksize=CHUNK)
            done=False
            while not done:
                _,done=transfer.next_chunk(num_retries=4)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def download(record,dest_dir):
    dest_dir=Path(dest_dir)
    dest_dir.mkdir(parents=True,exist_ok=True)
    dest=dest_dir/local_name(record['name'])
    if dest.exists():
        raise FileExistsError(f'Duplicate Drive filename: {dest.name}')
    return _download_request(files().get_media(fileId=record['id'],supportsAllDrives=True),dest)


def metadata(value):
    return files().get(fileId=drive_id(value),fields='id,name,mimeType,size,modifiedTime,trashed',
                       supportsAllDrives=True).execute(num_retries=4)


def _read_text_bytes(raw_bytes):
    for encoding in ('utf-8-sig','cp874'):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode('utf-8',errors='replace')


def download_text_record(record,dest_path):
    """Download a plain-text Drive file or export a Google Doc as text/plain."""
    dest=Path(dest_path)
    if record.get('mimeType')==GDOC:
        req=files().export_media(fileId=record['id'],mimeType='text/plain')
    else:
        req=files().get_media(fileId=record['id'],supportsAllDrives=True)
    _download_request(req,dest)
    text=_read_text_bytes(dest.read_bytes())
    if dest.read_bytes()!=text.encode('utf-8'):
        dest.write_text(text,encoding='utf-8')
    return dest,text


def download_drive_file(file_id_or_url,dest_dir):
    record=metadata(file_id_or_url)
    if record['mimeType']==GDOC:
        dest_dir=Path(dest_dir)
        dest_dir.mkdir(parents=True,exist_ok=True)
        stem=Path(local_name(record['name'])).stem or 'translation'
        dest=dest_dir/(stem+'.txt')
        download_text_record(record,dest)
        return dest,dest.name
    if Path(record['name']).suffix.lower() not in ('.mtp','.txt') or record['mimeType']==FOLDER:
        raise ValueError('Expected an .mtp or .txt file')
    return download(record,dest_dir),record['name']


def children(folder):
    token=None
    while True:
        page=files().list(q=f"'{drive_id(folder)}' in parents and trashed=false",pageToken=token,
                          fields='nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum)',pageSize=1000,
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


def find_subfolder(parent_folder_id,folder_name):
    parent=drive_id(parent_folder_id)
    matches=[r for r in children(parent) if r.get('mimeType')==FOLDER and r.get('name')==folder_name]
    if not matches:
        return None
    matches.sort(key=lambda r:r.get('modifiedTime') or '',reverse=True)
    return matches[0]['id']


def get_or_create_subfolder(parent_folder_id,folder_name):
    existing=find_subfolder(parent_folder_id,folder_name)
    if existing:
        return existing
    parent=drive_id(parent_folder_id)
    return files().create(body=dict(name=folder_name,mimeType=FOLDER,parents=[parent]),
                          fields='id',supportsAllDrives=True).execute()['id']


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
    target_name=remote_name or path.name
    media=MediaFileUpload(str(path),resumable=True,chunksize=CHUNK)
    try:
        req=files().create(body=dict(name=target_name,parents=[drive_id(folder_id)]),
                           media_body=media,fields='id',supportsAllDrives=True)
        result=None
        while result is None:
            _,result=req.next_chunk(num_retries=4)
    finally:
        media.stream().close()
    print(f'Uploaded {target_name}: {folder_url(folder_id)}',flush=True)
    return result['id']


def upsert_file_to_folder(local_path,folder_id,remote_name=None,mimetype=None):
    """Upload a file into `folder_id`, overwriting any existing file with the same name."""
    path=Path(local_path)
    target_name=remote_name or path.name
    parent=drive_id(folder_id)
    existing=[r for r in children(parent) if r.get('mimeType')!=FOLDER and r.get('name')==target_name]
    media_kwargs=dict(resumable=True,chunksize=CHUNK)
    if mimetype:
        media_kwargs['mimetype']=mimetype
    media=MediaFileUpload(str(path),**media_kwargs)
    try:
        if existing:
            existing.sort(key=lambda r:r.get('modifiedTime') or '',reverse=True)
            req=files().update(fileId=existing[0]['id'],media_body=media,fields='id',supportsAllDrives=True)
        else:
            req=files().create(body=dict(name=target_name,parents=[parent]),
                               media_body=media,fields='id',supportsAllDrives=True)
        result=None
        while result is None:
            _,result=req.next_chunk(num_retries=4)
    finally:
        media.stream().close()
    print(f'Uploaded {target_name}: {folder_url(parent)}',flush=True)
    return result['id']


def find_translation_file(folder_id,filename=TRANSLATED_TEXT_FILENAME):
    """Find a translation file in `folder_id`.

    Priority 1: `02_translated.txt` (or `filename`) / Google Doc named `02_translated`.
    Priority 2 (fallback): any `.txt` file (`mimeType == 'text/plain'` or `.txt` suffix)
    or Google Doc in the same folder, excluding `01_raw_text.txt` / `01_raw_text` (case-insensitive).
    When multiple files match at the same priority, the most recently modified file wins.
    """
    target_full=filename.strip().casefold()
    target_stem=Path(filename).stem.strip().casefold()
    excluded={RAW_TEXT_FILENAME.strip().casefold(),Path(RAW_TEXT_FILENAME).stem.strip().casefold()}
    priority_matches=[]
    fallback_matches=[]
    for record in children(folder_id):
        mime=record.get('mimeType') or ''
        if mime==FOLDER:
            continue
        name=(record.get('name') or '').strip().casefold()
        if not name:
            continue
        if name==target_full or (mime==GDOC and name==target_stem):
            priority_matches.append(record)
            continue
        if name in excluded:
            continue
        if mime=='text/plain' or name.endswith('.txt') or mime==GDOC:
            fallback_matches.append(record)
    matches=priority_matches or fallback_matches
    if not matches:
        return None
    matches.sort(key=lambda r:(r.get('modifiedTime') or '',1 if r.get('mimeType')!=GDOC else 0),reverse=True)
    return matches[0]


def _record_is_empty(record):
    if record.get('mimeType')==GDOC:
        return False
    size=record.get('size')
    if size is None:
        return False
    try:
        return int(size)<=0
    except (TypeError,ValueError):
        return False


def wait_for_translation_file(
    folder_id,
    dest_path,
    *,
    filename=TRANSLATED_TEXT_FILENAME,
    timeout_minutes=60,
    poll_interval=15.0,
    settle_seconds=2.0,
    extra_folder_ids=(),
    log=print,
):
    """Poll Drive folder(s) until a stable, non-empty translation file appears or timeout expires."""
    search_folders=[]
    for fid in (folder_id,*extra_folder_ids):
        if fid and fid not in search_folders:
            search_folders.append(fid)
    deadline=time.monotonic()+max(0.0,float(timeout_minutes))*60.0
    poll_count=0
    while True:
        poll_count+=1
        for fid in search_folders:
            record=find_translation_file(fid,filename=filename)
            if not record or _record_is_empty(record):
                continue
            if settle_seconds>0:
                time.sleep(settle_seconds)
                try:
                    fresh=metadata(record['id'])
                except Exception:
                    continue
                if fresh.get('trashed'):
                    continue
                if (record.get('modifiedTime') and fresh.get('modifiedTime')
                        and fresh['modifiedTime']!=record['modifiedTime']):
                    continue
                if (record.get('size') is not None and fresh.get('size') is not None
                        and str(fresh['size'])!=str(record['size'])):
                    continue
                record=fresh
            try:
                path,text=download_text_record(record,dest_path)
            except Exception as exc:
                if log:
                    msg=f'พบ {record.get("name")} แต่ยังอ่านไม่สำเร็จ ({exc}) จะตรวจซ้ำรอบถัดไป...'
                    log(msg,flush=True) if log is print else log(msg)
                continue
            if text.strip():
                return path,record,text
        remaining=deadline-time.monotonic()
        if remaining<=0:
            return None
        if log and (poll_count==1 or poll_count%4==0):
            mins_left=max(1,int((remaining+59)//60))
            msg=f'กำลังรอไฟล์คำแปล (.txt ชื่อใดก็ได้ หรือ Google Doc) บน Drive... (เหลือเวลาอีก ~{mins_left} นาที)'
            log(msg,flush=True) if log is print else log(msg)
        time.sleep(min(max(0.0,float(poll_interval)),remaining))


if __name__=='__main__':
    setup()
