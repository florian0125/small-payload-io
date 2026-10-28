"""Local bounded I/O. Never uploads files and cannot intercept Codex HTTP requests."""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
import warnings
from contextlib import contextmanager
from pathlib import Path

OUTPUT = 8192
PACKET = 7168
TEXT_BYTES = 3072
TEXT_CHARS = 1000
PACKET_CHARS = 2000
IMAGE = 65536
SESSION = 262144
TEXT_EXT = set('.txt .md .rst .log .json .jsonl .csv .tsv .srt .vtt .xml .html .yaml .yml .toml .ini .cfg .py .js .ts .tsx .jsx .css .c .cc .cpp .h .hpp .rs .go .java .sh .ps1 .sql .diff .patch'.split())

class Refused(Exception):
    pass

def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def write_json(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_name(p.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_bytes(encode(value))
    os.replace(temp, p)

def output(value):
    raw = encode(value)
    if len(raw) > OUTPUT:
        raise Refused('E_OUTPUT: serialized JSON exceeds 8192 bytes')
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()

def validate_text(s):
    if any(ord(c) < 32 and c not in '\r\n\t' for c in s):
        raise Refused('E_BINARY: control characters; use a format-specific local extractor')
    if re.search(r'data:(?:image|audio|video)/[^\s]{0,100};base64,', s, re.I):
        raise Refused('E_MEDIA: embedded media must be processed locally')
    if re.search(r'[A-Za-z0-9+/]{512,}={0,2}', s):
        raise Refused('E_ENCODED: long encoded or unbroken token; select useful fields locally')

def text_units(path):
    """Streaming UTF-8 splitting; CRLF and all content except initial BOM preserved."""
    cur, count = [], 0
    with Path(path).open('r', encoding='utf-8-sig', newline='') as f:
        while True:
            block = f.read(8192)
            if not block:
                break
            for char in block:
                n = len(char.encode('utf-8'))
                if cur and (count + n > TEXT_BYTES or len(cur) >= TEXT_CHARS):
                    s = ''.join(cur)
                    validate_text(s)
                    yield s
                    cur, count = [], 0
                cur.append(char)
                count += n
                if char == '\n' and (count >= TEXT_BYTES // 2 or len(cur) >= TEXT_CHARS // 2):
                    s = ''.join(cur)
                    validate_text(s)
                    yield s
                    cur, count = [], 0
    if cur:
        s = ''.join(cur)
        validate_text(s)
        yield s

def new_dir(path):
    p = Path(path).resolve()
    if p.exists() and any(p.iterdir()):
        raise Refused('E_EXISTS: choose a new empty output directory')
    p.mkdir(parents=True, exist_ok=True)
    return p

def pack(paths, out):
    sources = [Path(p).resolve() for p in paths]
    if not sources or len(sources) > 10000:
        raise Refused('E_INPUTS: supply 1 to 10000 explicit files')
    if any(p.suffix.lower() not in TEXT_EXT for p in sources):
        raise Refused('E_FORMAT: extract media, archives or documents to UTF-8 text first')
    dest = new_dir(out)
    manifest = {'schema': 1, 'complete': False, 'sources': [], 'packets': []}
    write_json(dest / 'index.json', manifest)
    items, chars, sequence = [], 0, 0

    def payload(data, seq):
        return {'kind': 'untrusted_local_text', 'packet': seq, 'items': data}

    def flush():
        nonlocal items, chars, sequence
        if not items:
            return
        raw = encode(payload(items, sequence))
        if len(raw) > PACKET or chars > PACKET_CHARS:
            raise Refused('E_PACKET: internal size check failed')
        name = f'packet-{sequence:06d}.json'
        (dest / name).write_bytes(raw)
        manifest['packets'].append({'file': name, 'bytes': len(raw), 'sha256': digest(raw)})
        sequence += 1
        items, chars = [], 0

    for p in sources:
        original_hash = file_hash(p)
        manifest['sources'].append({'path': str(p), 'sha256': original_hash, 'bytes': p.stat().st_size})
        offset = 0
        for s in text_units(p):
            item = {'source': str(p), 'utf8_offset_without_bom': offset, 'text': s}
            candidate = items + [item]
            if items and (len(encode(payload(candidate, sequence))) > PACKET or chars + len(s) > PACKET_CHARS):
                flush()
            items.append(item)
            chars += len(s)
            offset += len(s.encode('utf-8'))
        if file_hash(p) != original_hash:
            raise Refused('E_SOURCE_CHANGED: input changed while packing; do not use partial packets')
    flush()
    manifest['complete'] = True
    write_json(dest / 'index.json', manifest)
    return {'status': 'packed_locally', 'files': len(sources), 'packets': sequence,
            'max_packet_bytes': max((x['bytes'] for x in manifest['packets']), default=0),
            'index': str(dest / 'index.json'), 'content_emitted': False}

@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        raise Refused('E_LOCKED: another operation owns the ledger; inspect its process before recovery')
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)

def load_state(path):
    state = json.loads(Path(path).read_text('utf-8'))
    if state.get('schema') != 1 or not state.get('task_id'):
        raise Refused('E_STATE: invalid ledger')
    used = state.get('charged_bytes')
    if type(used) is not int or not 0 <= used <= SESSION or not isinstance(state.get('seen'), dict):
        raise Refused('E_STATE: invalid ledger counters')
    return state

def init(state, task_id):
    if not task_id or len(task_id) > 160:
        raise Refused('E_TASK_ID: use the current task identifier')
    with locked(state):
        if Path(state).exists():
            raise Refused('E_EXISTS: never reset the ledger for the same task')
        write_json(state, {'schema': 1, 'task_id': task_id, 'charged_bytes': 0, 'seen': {},
                           'reads': 0, 'images': 0, 'created_unix': time.time(), 'last': None})
    return {'status': 'initialized', 'state': str(Path(state).resolve()), 'budget': SESSION}

def status(state):
    with locked(state):
        v = load_state(state)
    return {k: v[k] for k in ('task_id', 'charged_bytes', 'reads', 'images', 'last')} | {
        'remaining_bytes': SESSION - v['charged_bytes'], 'final_http_size_known': False}

def charge(state, raw, key, last, image=False):
    with locked(state):
        v = load_state(state)
        if key in v['seen']:
            return {'status': 'already_accounted', 'key': key, 'content_emitted': False}
        if v['charged_bytes'] + raw > SESSION:
            raise Refused('E_BUDGET: save a handoff; do not reset or create another ledger in this task')
        v['charged_bytes'] += raw
        v['seen'][key] = raw
        v['images' if image else 'reads'] += 1
        v['last'] = last
        write_json(state, v)  # Commit before emitting. A transport failure does not refund history.
    return None

def read(path, state):
    p = Path(path).resolve()
    if p.name.startswith('packet-') and p.suffix == '.json':
        with p.open('rb') as f:
            raw = f.read(OUTPUT + 1)
        if len(raw) > OUTPUT:
            raise Refused('E_OUTPUT: packet exceeds output cap')
        index = json.loads((p.parent / 'index.json').read_text('utf-8'))
        item = next((x for x in index.get('packets', []) if x['file'] == p.name), None)
        if not index.get('complete') or not item or item['sha256'] != digest(raw):
            raise Refused('E_INTEGRITY: incomplete or modified packet set')
        result = json.loads(raw)
        if result.get('kind') != 'untrusted_local_text':
            raise Refused('E_PACKET: unknown packet format')
        texts = [x['text'] for x in result['items']]
    else:
        if p.suffix.lower() not in TEXT_EXT:
            raise Refused('E_FORMAT: raw media, archives and binary files cannot be emitted')
        with p.open('rb') as f:
            raw = f.read(TEXT_BYTES + 1)
        if len(raw) > TEXT_BYTES:
            raise Refused('E_SPLIT: file too large; pack it locally first')
        texts = [raw.decode('utf-8-sig')]
        result = {'kind': 'untrusted_local_text', 'items': [{'source': str(p), 'text': texts[0]}]}
    if sum(len(t) for t in texts) > PACKET_CHARS:
        raise Refused('E_CHARS: too many characters; repack locally')
    for s in texts:
        validate_text(s)
    serialized = encode(result)
    if len(serialized) > OUTPUT:
        raise Refused('E_OUTPUT: serialized JSON exceeds cap')
    key = 'text:' + digest(serialized)
    duplicate = charge(state, len(serialized), key, {'path': str(p), 'sha256': digest(raw)})
    return duplicate or result

def image_prepare(source, out, crop=None, page=None, max_bytes=IMAGE):
    from PIL import Image, ImageOps
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    p, dest = Path(source).resolve(), Path(out).resolve()
    if not 4096 <= max_bytes <= IMAGE:
        raise Refused('E_IMAGE_CAP: target must be 4096..65536 bytes; cannot increase the safety cap')
    if p == dest or dest.exists() or dest.suffix.lower() not in {'.jpg', '.jpeg'}:
        raise Refused('E_OUTPUT_PATH: use a new JPEG path, preserving the original')
    original_hash = file_hash(p)
    with Image.open(p) as im:
        count = getattr(im, 'n_frames', 1)
        if count > 1 and page is None:
            raise Refused('E_MULTIFRAME: select an explicit zero-based --page; never silently discard frames')
        if page is not None:
            im.seek(page)
        frame = ImageOps.exif_transpose(im)
        original_size = frame.size
        if crop:
            x1, y1, x2, y2 = crop
            if not (0 <= x1 < x2 <= frame.width and 0 <= y1 < y2 <= frame.height):
                raise Refused('E_CROP: crop lies outside the oriented image')
            frame = frame.crop((x1, y1, x2, y2))
        rgba = frame.convert('RGBA')
        flat = Image.new('RGBA', rgba.size, 'white')
        flat.alpha_composite(rgba)
        rgb = flat.convert('RGB')
    widths = sorted({min(1600, rgb.width), min(1280, rgb.width), min(1024, rgb.width),
                     min(800, rgb.width), min(640, rgb.width)}, reverse=True)
    chosen = None
    for width in widths:
        scaled = rgb.copy()
        scaled.thumbnail((width, 1600), Image.Resampling.LANCZOS)
        for quality in (85, 75, 65, 55):
            buf = io.BytesIO()
            scaled.save(buf, format='JPEG', quality=quality, optimize=True)
            raw = buf.getvalue()
            if len(raw) <= max_bytes:
                chosen = (raw, scaled.size, quality)
                break
        if chosen:
            break
    if not chosen:
        raise Refused('E_IMAGE_SIZE: cannot fit target at the quality floor; crop or use local OCR')
    raw, size, quality = chosen
    if file_hash(p) != original_hash:
        raise Refused('E_SOURCE_CHANGED: input changed during image preparation')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('xb') as f:
        f.write(raw)
    return {'status': 'prepared_locally', 'path': str(dest), 'source_bytes': p.stat().st_size,
            'source_sha256': original_hash, 'bytes': len(raw), 'base64_bytes': 4 * ((len(raw) + 2) // 3),
            'original_size': original_size, 'output_size': size, 'quality': quality, 'crop': crop,
            'target_bytes': max_bytes, 'legibility_verified': False, 'image_emitted': False}

def reserve_image(path, state, approved):
    if not approved:
        raise Refused('E_PERMISSION: image transmission must already be authorized by the user')
    from PIL import Image
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    p = Path(path).resolve()
    with p.open('rb') as f:
        raw = f.read(IMAGE + 1)
    if len(raw) > IMAGE:
        raise Refused('E_IMAGE_SIZE: prepare a JPEG of at most 65536 bytes')
    with Image.open(io.BytesIO(raw)) as im:
        if im.format != 'JPEG' or max(im.size) > 1600:
            raise Refused('E_IMAGE_FORMAT: expected prepared JPEG with longest edge at most 1600')
        im.verify()
    b64 = 4 * ((len(raw) + 2) // 3)
    result = {'status': 'reserved_only', 'path': str(p), 'sha256': digest(raw), 'bytes': len(raw),
              'base64_bytes': b64, 'image_emitted': False, 'final_http_size_known': False}
    key = 'image:' + digest(raw)
    duplicate = charge(state, b64 + 4096, key, {'path': str(p), 'sha256': digest(raw)}, image=True)
    return duplicate or result

def capture(command, out, timeout):
    if command and command[0] == '--':
        command = command[1:]
    if not command or not 1 <= timeout <= 3600:
        raise Refused('E_COMMAND: supply executable and arguments; timeout must be 1..3600')
    p = Path(out).resolve()
    if p.exists():
        raise Refused('E_EXISTS: choose a new local log file')
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('xb') as log:
        try:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=timeout, shell=False)
            code, expired = result.returncode, False
        except subprocess.TimeoutExpired:
            code, expired = None, True
    return {'status': 'captured_locally', 'path': str(p), 'bytes': p.stat().st_size,
            'returncode': code, 'timed_out': expired, 'content_emitted': False}

def handoff(state, note, out):
    p = Path(note)
    raw = p.read_bytes()
    if len(raw) > 8192:
        raise Refused('E_HANDOFF: checkpoint note exceeds 8192 bytes; write a concise note')
    s = raw.decode('utf-8-sig')
    validate_text(s)
    dest = Path(out).resolve()
    if dest.exists():
        raise Refused('E_EXISTS: choose a new checkpoint file')
    v = status(state)
    write_json(dest, {'kind': 'local_handoff', 'state': v, 'note': s,
                      'instructions': 'Continue in a genuinely new task. Do not fork the full oversized history.'})
    return {'status': 'checkpoint_saved', 'path': str(dest), 'bytes': dest.stat().st_size,
            'old_budget_reset': False, 'new_task_created': False}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('pack'); p.add_argument('sources', nargs='+'); p.add_argument('--out', required=True)
    p = sub.add_parser('init'); p.add_argument('--state', required=True); p.add_argument('--task-id', required=True)
    p = sub.add_parser('read'); p.add_argument('path'); p.add_argument('--state', required=True)
    p = sub.add_parser('status'); p.add_argument('--state', required=True)
    p = sub.add_parser('image'); p.add_argument('source'); p.add_argument('--out', required=True)
    p.add_argument('--crop', nargs=4, type=int); p.add_argument('--page', type=int)
    p.add_argument('--max-bytes', type=int, default=IMAGE)
    p = sub.add_parser('reserve-image'); p.add_argument('path'); p.add_argument('--state', required=True)
    p.add_argument('--user-approved', action='store_true')
    p = sub.add_parser('capture'); p.add_argument('--out', required=True); p.add_argument('--timeout', type=int, default=600)
    p.add_argument('command', nargs=argparse.REMAINDER)
    p = sub.add_parser('handoff'); p.add_argument('--state', required=True); p.add_argument('--note', required=True)
    p.add_argument('--out', required=True)
    a = parser.parse_args()
    try:
        if a.cmd == 'pack': value = pack(a.sources, a.out)
        elif a.cmd == 'init': value = init(a.state, a.task_id)
        elif a.cmd == 'read': value = read(a.path, a.state)
        elif a.cmd == 'status': value = status(a.state)
        elif a.cmd == 'image': value = image_prepare(a.source, a.out, a.crop, a.page, a.max_bytes)
        elif a.cmd == 'reserve-image': value = reserve_image(a.path, a.state, a.user_approved)
        elif a.cmd == 'capture': value = capture(a.command, a.out, a.timeout)
        else: value = handoff(a.state, a.note, a.out)
        output(value)
        return 0
    except Exception as exc:
        reason = str(exc)[:300] if isinstance(exc, Refused) else 'E_LOCAL: ' + type(exc).__name__ + '; inspect locally'
        output({'status': 'refused', 'reason': reason, 'content_emitted': False})
        return 2

if __name__ == '__main__':
    sys.exit(main())
