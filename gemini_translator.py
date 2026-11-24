"""Single-pass Thai manhwa/manga translator powered by google-genai."""
import json
import os
from pathlib import Path
import re
import time

from text_layout import FRAME_TYPE_LABELS, extract_frame_tag, normalize

COMPONENT_VERSION = '3.1'
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'

_SYSTEM_PROMPT = """คุณคือนักแปลมังฮวา/มังงะมืออาชีพจากต้นฉบับเป็นภาษาไทย
กติกาการแปล:
1. แปลทุกกรอบตามลำดับบริบทของทั้งตอนให้ต่อเนื่องกัน ใช้ภาษาพูดที่เป็นธรรมชาติ เหมาะกับการ์ตูน ไม่แปลแข็งทื่อแบบเครื่องแปล
2. รักษาชื่อตัวละคร สรรพนาม (เช่น ฉัน/นาย/เธอ/ผม/คุณ/ข้า/เจ้า) และระดับภาษาให้สม่ำเสมอตลอดทั้งตอน
3. กระชับคำให้พอดีกับกรอบคำพูดการ์ตูน ไม่เยิ่นเย้อ
4. ปรับน้ำเสียงให้ตรงกับประเภทกรอบ (`type`):
   - `normal`: บทพูดทั่วไป เป็นธรรมชาติ
   - `shout`: ตะโกน / แอ็กชัน ดุดัน กระชับ มีพลัง
   - `thought`: ความคิดในใจ
   - `system`: กล่องระบบ/สเตตัสเกม ใช้สำนวนเกม RPG ภาษาไทย (เช่น เลเวล, สกิล, ภารกิจ, แจ้งเตือน)
   - `narration`: ช่องบรรยาย ภาษาสละสลวย
   - `terrible`: ข่มขู่ / ออร่ามืด
   - `happy`: สดใส ร่าเริง
   - `phone`: ข้อความแชท/โทรศัพท์
   - `floating`: คำลอยนอกกรอบหรือเสียงเอฟเฟกต์ (แปลกระชับ)
5. ห้ามใส่รหัสกรอบ เลขข้อ หรือแท็กประเภทเช่น [พูด] นำหน้าในค่า `thai`
6. ส่งคืนเป็น JSON ตามสคีมาที่กำหนดเท่านั้น โดยต้องมีรายการครบทุก `id` ที่ส่งมา"""

_RESPONSE_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'translations': {
            'type': 'ARRAY',
            'items': {
                'type': 'OBJECT',
                'properties': {
                    'id': {'type': 'STRING'},
                    'thai': {'type': 'STRING'},
                },
                'required': ['id', 'thai'],
            },
        },
    },
    'required': ['translations'],
}


def _read_dotenv_key(dotenv_path):
    if not dotenv_path.is_file():
        return ''
    try:
        for raw in dotenv_path.read_text(encoding='utf-8-sig').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            k = k.strip()
            if k in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
                val = v.strip().strip('"').strip("'")
                if val:
                    return val
    except OSError:
        return ''
    return ''


def resolve_api_key(api_key=None):
    """Resolve Gemini API key from argument, env vars, local .env, or app data."""
    if api_key and str(api_key).strip():
        return str(api_key).strip()
    for env_name in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        val = (os.environ.get(env_name) or '').strip()
        if val:
            return val
    dotenv_val = _read_dotenv_key(Path(__file__).resolve().parent / '.env')
    if dotenv_val:
        return dotenv_val
    local_app = os.environ.get('LOCALAPPDATA')
    if local_app:
        key_file = Path(local_app) / 'ManhwaOCREditor' / 'gemini_api_key.txt'
        try:
            if key_file.is_file():
                val = key_file.read_text(encoding='utf-8-sig').strip()
                if val:
                    return val
        except OSError:
            pass
    raise RuntimeError(
        'ไม่พบ GEMINI_API_KEY สำหรับแปลภาษาไทยอัตโนมัติ '
        '(กรุณาตั้งค่า environment variable GEMINI_API_KEY หรือใส่ไว้ในไฟล์ .env / '
        '%LOCALAPPDATA%\\ManhwaOCREditor\\gemini_api_key.txt)'
    )


def _extract_json_payload(raw_text):
    text = (raw_text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def _is_retryable_error(exc):
    msg = str(exc).lower()
    return any(tok in msg for tok in (
        '429', '500', '502', '503', '504',
        'resource_exhausted', 'rate limit', 'unavailable', 'timeout', 'timed out',
    ))


def _call_gemini_batch(client, model, items, source_lang='ko+en', context_hint='', max_retries=3):
    from google.genai import types

    payload = {
        'source_language': source_lang,
        'target_language': 'th',
        'items': items,
    }
    if context_hint:
        payload['context'] = context_hint
    prompt = (
        'แปลข้อความในรายการ `items` ต่อไปนี้เป็นภาษาไทยให้ครบทุก `id` ในรอบเดียว '
        'โดยรักษาความต่อเนื่องของบทสนทนาทั้งตอน:\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        response_mime_type='application/json',
        response_schema=_RESPONSE_SCHEMA,
        temperature=0.3,
    )

    last_exc = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            data = _extract_json_payload(response.text)
            rows = data.get('translations') if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError('Gemini ไม่ส่งรายการ translations กลับมา')
            result = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                bid = str(row.get('id') or '').strip()
                thai = str(row.get('thai') or '').strip()
                if bid and thai:
                    result[bid] = thai
            return result
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= max_retries or not _is_retryable_error(exc):
                raise
            time.sleep(min(15.0, 2.0 * (2 ** attempt)))
    raise last_exc


def translate_pages_with_gemini(
    pages,
    source_lang='ko+en',
    model=None,
    api_key=None,
    overwrite=False,
    context_hint='',
    max_batch_size=200,
    log=print,
):
    """Translate all bubbles across `pages` into Thai in a single google-genai pass.

    Normal chapters (<= `max_batch_size` bubbles) are translated in one single API request
    so Gemini sees the full chapter context.
    """
    model_name = (model or os.environ.get('GEMINI_MODEL') or DEFAULT_GEMINI_MODEL).strip()
    candidates = []
    bubble_by_id = {}
    for page_idx, page in enumerate(pages, 1):
        for b in page.bubbles:
            src = normalize(getattr(b, 'text', '') or '')
            if not src:
                continue
            if not overwrite and (getattr(b, 'translation', '') or '').strip():
                continue
            ftype = (
                getattr(b, 'frame_type', '')
                or getattr(b, 'detected_frame_type', '')
                or 'normal'
            )
            if ftype not in FRAME_TYPE_LABELS:
                ftype = 'normal'
            candidates.append({
                'id': b.id,
                'page': page_idx,
                'type': ftype,
                'text': src,
            })
            bubble_by_id[b.id] = b

    if not candidates:
        return {
            'translated_count': 0,
            'total_candidates': 0,
            'missing_ids': [],
            'model': model_name,
            'batches': 0,
            'changes': {},
        }

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            'ยังไม่ได้ติดตั้ง google-genai ใน .venv กรุณารัน: '
            '.venv\\Scripts\\python.exe -m pip install google-genai'
        ) from exc

    key = resolve_api_key(api_key)
    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(api_version='v1beta'),
    )

    changes = {}
    batch_size = max(1, int(max_batch_size or 200))
    batches = [candidates[i:i + batch_size] for i in range(0, len(candidates), batch_size)]
    for batch_idx, batch in enumerate(batches, 1):
        if log:
            log(
                f'Gemini translate ({model_name}) batch {batch_idx}/{len(batches)}: '
                f'{len(batch)} bubbles...',
                flush=True,
            ) if log is print else log(
                f'Gemini translate ({model_name}) batch {batch_idx}/{len(batches)}: '
                f'{len(batch)} bubbles...'
            )
        translated = _call_gemini_batch(
            client,
            model_name,
            batch,
            source_lang=source_lang,
            context_hint=context_hint,
        )
        for item in batch:
            bid = item['id']
            val = translated.get(bid, '').strip()
            if not val:
                continue
            tag, clean = extract_frame_tag(val)
            b = bubble_by_id[bid]
            b.translation = clean
            if tag and not getattr(b, 'frame_type_locked', False):
                b.frame_type = tag
            changes[bid] = clean

    missing = [item['id'] for item in candidates if item['id'] not in changes]
    if log:
        msg = (
            f'Gemini translated {len(changes)}/{len(candidates)} bubbles '
            f'using {model_name} ({len(batches)} pass)'
        )
        log(msg, flush=True) if log is print else log(msg)
    return {
        'translated_count': len(changes),
        'total_candidates': len(candidates),
        'missing_ids': missing,
        'model': model_name,
        'batches': len(batches),
        'changes': changes,
    }
