"""Single-pass Thai manhwa/manga translator powered by google-genai."""
import json
import os
from pathlib import Path
import re
import time

from text_layout import FRAME_TYPE_LABELS, extract_frame_tag, normalize

COMPONENT_VERSION = '4.1'
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'
DEFAULT_MAX_BATCH_SIZE = 48
DEFAULT_MAX_OUTPUT_TOKENS = 16384
DEFAULT_THINKING_BUDGET = 1024


class TruncatedBatchError(ValueError):
    """Raised when Gemini stops due to MAX_TOKENS or truncates the JSON payload."""

_SYSTEM_PROMPT = """แปลการ์ตูนและเว็บตูนเป็นภาษาไทยสำหรับอ่านในบอลลูน อ่านบทตามลำดับทั้งตอนก่อนเลือกสำนวน
ให้ตัวละครพูดเหมือนคนไทยพูดกันจริง ถ่ายทอดเจตนาและอารมณ์แทนการเรียงคำตามต้นฉบับ
เขียนให้สั้น อ่านออกเสียงแล้วเข้าปาก ตัดคำฟุ่มเฟือยโดยเก็บใจความ มุก และจังหวะหยุดของผู้พูด
ใช้ “ครับ/ค่ะ/คะ” เฉพาะเมื่อความสัมพันธ์หรือสถานการณ์บ่งบอกว่าผู้พูดตั้งใจสุภาพ
อย่าเติมท้ายทุกประโยค โดยเฉพาะตอนต่อสู้ ฉากแฟนตาซี ย้อนยุค หรือคุยกันเอง
เลือกสรรพนามตามคู่สนทนา เช่น ข้า/เจ้า ฉัน/แก กู/มึง พี่/น้อง ละสรรพนามได้เมื่อคนไทยเข้าใจอยู่แล้ว
รักษาการสะกดชื่อ คำเรียก และระดับภาษาของแต่ละคู่ให้ต่อเนื่อง เปลี่ยนได้เมื่อความสัมพันธ์หรือสถานการณ์เปลี่ยน
ถ้ามี translation_note ให้ใช้เป็นคู่มือชื่อ แนวเรื่อง และน้ำเสียง หาก OCR ตกหล่นหรือเพี้ยนเล็กน้อย
ให้ดูช่องก่อนหน้าและถัดไปเพื่อกู้ความหมายเฉพาะที่พออนุมานได้ อย่าแต่งเหตุการณ์หรือชื่อเฉพาะเพิ่ม

เลือกน้ำเสียงตาม type ของกรอบ
normal พูดตามธรรมชาติและความสัมพันธ์ของตัวละคร
shout ใช้คำสั้น มีแรง สื่อความดุดันหรือตกใจตามฉาก
thought ให้เหมือนความคิดที่ผุดขึ้นในใจ
system ใช้ข้อความหน้าต่างเกมที่กระชับชัดเจน คงศัพท์สกิล สเตตัส ตัวเลข และหน่วย
narration บรรยายให้สละสลวย เข้ากับจังหวะเรื่อง
floating ใช้คำพึมพำ อุทาน หรือเสียงสั้นตามต้นฉบับ
terrible ให้น้ำเสียงข่มขู่หรือกดดัน happy ให้สดใส phone ให้เหมือนบทแชทหรือคำพูดทางโทรศัพท์

chapter_context ถ้ามี คือลำดับต้นฉบับทั้งตอนและคำแปลที่มีไว้เทียบสำนวน
แปลเฉพาะ items คืน JSON ตามสคีมา ส่ง id ของแต่ละรายการตรงตัวครั้งเดียวให้ครบ ห้ามข้าม รวม หรือย้ายเนื้อหาข้าม ID
ใน thai ใส่เฉพาะคำแปล ห้ามเติมเลขข้อ รหัส แท็กประเภทกรอบ หรือคำอธิบาย"""

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


def _response_hit_max_tokens(response):
    candidates = getattr(response, 'candidates', None) or []
    for cand in candidates:
        reason = getattr(cand, 'finish_reason', None)
        if reason is not None and 'MAX_TOKENS' in str(reason).upper():
            return True
    return False


def _call_gemini_batch(client, model, items, source_lang='ko+en', context_hint='', max_retries=3,
                       translation_note: str = '', chapter_context=None,
                       max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
                       thinking_budget=None):
    from google.genai import types

    payload = {
        'source_language': source_lang,
        'target_language': 'th',
        'items': items,
    }
    if translation_note.strip():
        payload['translation_note'] = translation_note.strip()
    if chapter_context:
        payload['chapter_context'] = chapter_context
    if context_hint:
        payload['context'] = context_hint
    prompt = (
        'แปลข้อความในรายการ `items` ต่อไปนี้เป็นภาษาไทยให้ครบทุก `id` ในรอบเดียว '
        'คัดลอก id ตามตัวอักษร แปลเฉพาะ items ส่วน chapter_context ใช้อ้างอิงเท่านั้น '
        'โดยรักษาความต่อเนื่องของบทสนทนาทั้งตอน:\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    config_kwargs = dict(
        system_instruction=_SYSTEM_PROMPT,
        response_mime_type='application/json',
        response_schema=_RESPONSE_SCHEMA,
        temperature=0.3,
        max_output_tokens=max(DEFAULT_MAX_OUTPUT_TOKENS, int(max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS)),
    )
    budget = thinking_budget
    if budget is None and 'gemini-2.5-flash' in (model or '').lower():
        budget = DEFAULT_THINKING_BUDGET
    if budget is not None and hasattr(types, 'ThinkingConfig'):
        config_kwargs['thinking_config'] = types.ThinkingConfig(thinking_budget=int(budget))
    config = types.GenerateContentConfig(**config_kwargs)

    last_exc = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            if _response_hit_max_tokens(response):
                raise TruncatedBatchError('Gemini ตอบกลับถูกตัดกลางคัน (MAX_TOKENS)')
            data = _extract_json_payload(response.text)
            rows = data.get('translations') if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError('Gemini ไม่ส่งรายการ translations กลับมา')
            expected = {item['id'] for item in items}
            result = {}
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                    continue
                bid = row['id']
                if bid not in expected:
                    continue
                if bid in seen:
                    result.pop(bid, None)
                    continue
                seen.add(bid)
                thai = row.get('thai')
                if isinstance(thai, str) and extract_frame_tag(thai.strip())[1].strip():
                    result[bid] = thai.strip()
            return result
        except (json.JSONDecodeError, TruncatedBatchError) as exc:
            last_exc = exc
            if len(items) > 1 or attempt + 1 >= max_retries:
                raise
            time.sleep(min(5.0, 1.5 * (attempt + 1)))
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= max_retries or not _is_retryable_error(exc):
                raise
            time.sleep(min(15.0, 2.0 * (2 ** attempt)))
    raise last_exc


def _call_gemini_with_autosplit(
    client,
    model,
    items,
    *,
    source_lang='ko+en',
    context_hint='',
    translation_note: str = '',
    chapter_context=None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    thinking_budget=None,
    log=None,
):
    """Call Gemini for `items`, automatically halving the batch on JSON truncation / MAX_TOKENS."""
    try:
        res = _call_gemini_batch(
            client,
            model,
            items,
            source_lang=source_lang,
            context_hint=context_hint,
            translation_note=translation_note,
            chapter_context=chapter_context,
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
        )
        return res, 1
    except (json.JSONDecodeError, TruncatedBatchError) as exc:
        if len(items) <= 1:
            raise
        mid = len(items) // 2
        left, right = items[:mid], items[mid:]
        if log:
            msg = (
                f'Gemini JSON ถูกตัดกลางคัน ({exc}) — '
                f'แบ่งครึ่ง batch {len(items)} กรอบ เป็น {len(left)} + {len(right)} กรอบอัตโนมัติ'
            )
            log(msg, flush=True) if log is print else log(msg)
        res_left, calls_left = _call_gemini_with_autosplit(
            client,
            model,
            left,
            source_lang=source_lang,
            context_hint=context_hint,
            translation_note=translation_note,
            chapter_context=chapter_context,
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
            log=log,
        )
        right_context = chapter_context
        if chapter_context is not None and res_left:
            right_context = [
                dict(row, thai=res_left.get(row['id']) or row.get('thai', ''))
                for row in chapter_context
            ]
        res_right, calls_right = _call_gemini_with_autosplit(
            client,
            model,
            right,
            source_lang=source_lang,
            context_hint=context_hint,
            translation_note=translation_note,
            chapter_context=right_context,
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
            log=log,
        )
        merged = dict(res_left)
        merged.update(res_right)
        return merged, 1 + calls_left + calls_right


def translate_pages_with_gemini(
    pages,
    source_lang='ko+en',
    model=None,
    api_key=None,
    overwrite=False,
    context_hint='',
    max_batch_size=DEFAULT_MAX_BATCH_SIZE,
    log=print,
    translation_note: str = "",
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    thinking_budget=None,
):
    """Translate all bubbles across `pages` into Thai using google-genai with full chapter context.

    Defaults to `DEFAULT_MAX_BATCH_SIZE` (48) bubbles per request with `max_output_tokens >= 16384`
    and automatic recursive batch splitting on truncated JSON / MAX_TOKENS responses.
    """
    model_name = (model or os.environ.get('GEMINI_MODEL') or DEFAULT_GEMINI_MODEL).strip()
    chapter = []
    candidates = []
    bubble_by_id = {}
    for page_idx, page in enumerate(pages, 1):
        for b in page.bubbles:
            src = normalize(getattr(b, 'text', '') or '')
            if not src:
                continue
            ftype = (
                getattr(b, 'frame_type', '')
                or getattr(b, 'detected_frame_type', '')
                or 'normal'
            )
            if ftype not in FRAME_TYPE_LABELS:
                ftype = 'normal'
            item = {
                'id': b.id,
                'page': page_idx,
                'type': ftype,
                'text': src,
            }
            if b.id in bubble_by_id:
                raise ValueError('พบรหัสกรอบซ้ำในต้นฉบับ')
            bubble_by_id[b.id] = b
            chapter.append(item)
            if overwrite or not (getattr(b, 'translation', '') or '').strip():
                candidates.append(item)

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
    tags = {}
    batch_size = max(1, int(max_batch_size or DEFAULT_MAX_BATCH_SIZE))
    batches = [candidates[i:i + batch_size] for i in range(0, len(candidates), batch_size)]
    request_count = 0
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
        translated = {}
        pending = batch
        for round_index in range(3):
            limit = len(batch) if round_index == 0 else 16
            for offset in range(0, len(pending), limit):
                subset = pending[offset:offset + limit]
                context = None
                if len(subset) < len(chapter):
                    context = [dict(item, thai=translated.get(item['id']) or changes.get(item['id'])
                                    or getattr(bubble_by_id[item['id']], 'translation', ''))
                               for item in chapter]
                chunk_res, chunk_calls = _call_gemini_with_autosplit(
                    client,
                    model_name,
                    subset,
                    source_lang=source_lang,
                    context_hint=context_hint,
                    translation_note=translation_note,
                    chapter_context=context,
                    max_output_tokens=max_output_tokens,
                    thinking_budget=thinking_budget,
                    log=log,
                )
                translated.update(chunk_res)
                request_count += chunk_calls
            pending = [item for item in batch if item['id'] not in translated]
            if not pending:
                break
            if log and round_index < 2:
                log(f'Gemini จับคู่ได้ {len(translated)}/{len(batch)} กรอบ '
                    f'กำลังขอเฉพาะ {len(pending)} กรอบที่ยังจับคู่ไม่ได้ (รอบแก้ {round_index + 1}/2)')
        if pending:
            sample = ', '.join(item['id'] for item in pending[:5])
            raise ValueError(f'Gemini ยังจับคู่ไม่ได้ {len(pending)}/{len(batch)} กรอบหลังขอซ้ำ '
                             f'(ตัวอย่าง ID: {sample})')
        for item in batch:
            bid = item['id']
            val = translated.get(bid, '').strip()
            if not val:
                continue
            tag, clean = extract_frame_tag(val)
            if not clean.strip():
                raise ValueError('Gemini ส่งเฉพาะแท็กโดยไม่มีคำแปล')
            changes[bid] = clean
            if tag:
                tags[bid] = tag

    for bid, clean in changes.items():
        bubble = bubble_by_id[bid]
        bubble.translation = clean
        if bid in tags and not getattr(bubble, 'frame_type_locked', False):
            bubble.frame_type = tags[bid]

    missing = [item['id'] for item in candidates if item['id'] not in changes]
    if log:
        msg = (
            f'Gemini translated {len(changes)}/{len(candidates)} bubbles '
            f'using {model_name} ({request_count} pass)'
        )
        log(msg, flush=True) if log is print else log(msg)
    return {
        'translated_count': len(changes),
        'total_candidates': len(candidates),
        'missing_ids': missing,
        'model': model_name,
        'batches': request_count,
        'changes': changes,
    }
