"""Extract document text locally; stdout contains metadata only, never document text."""
import argparse
import contextlib
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_XML = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024

def extract(source, out, pages=None):
    source, out = Path(source).resolve(), Path(out).resolve()
    if source == out or out.exists():
        raise ValueError('Use a new output path')
    blocks, selected, total = [], 0, 0
    if source.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(str(source))
        if reader.is_encrypted:
            raise ValueError('Encrypted PDF requires explicit local decryption')
        count = len(reader.pages)
        wanted = list(range(count)) if not pages else pages
        if any(i < 0 or i >= count for i in wanted):
            raise ValueError('Page index out of range')
        for i in wanted:
            text = reader.pages[i].extract_text() or ''
            blocks.append(f'\n[PDF page {i + 1}]\n{text}\n')
            total += len(text.encode('utf-8'))
            if total > MAX_TOTAL:
                raise ValueError('Extract fewer pages')
        selected = len(wanted)
    elif source.suffix.lower() in {'.pptx', '.docx'}:
        with zipfile.ZipFile(source) as z:
            names = z.namelist()
            if source.suffix.lower() == '.pptx':
                ns = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
                      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
                for name in ('ppt/_rels/presentation.xml.rels', 'ppt/presentation.xml'):
                    if z.getinfo(name).file_size > MAX_XML:
                        raise ValueError('Relationship XML too large')
                rels = ET.fromstring(z.read('ppt/_rels/presentation.xml.rels'))
                targets = {r.attrib['Id']: r.attrib['Target'] for r in rels
                           if r.attrib.get('TargetMode') != 'External'}
                root = ET.fromstring(z.read('ppt/presentation.xml'))
                ordered = []
                for slide in root.findall('p:sldIdLst/p:sldId', ns):
                    target = targets[slide.attrib['{' + ns['r'] + '}id']]
                    name = target.lstrip('/') if target.startswith('/') else 'ppt/' + target
                    if '..' in Path(name).parts or name not in names:
                        raise ValueError('Unsupported slide relationship')
                    ordered.append(name)
            else:
                ordered = ['word/document.xml']
            wanted = list(range(len(ordered))) if not pages else pages
            if any(i < 0 or i >= len(ordered) for i in wanted):
                raise ValueError('Page/slide index out of range')
            for i in wanted:
                info = z.getinfo(ordered[i])
                if info.file_size > MAX_XML:
                    raise ValueError('XML member too large; select locally')
                total += info.file_size
                if total > MAX_TOTAL:
                    raise ValueError('Select fewer parts')
                root = ET.fromstring(z.read(info))
                lines = []
                for paragraph in root.iter():
                    if paragraph.tag.rsplit('}', 1)[-1] == 'p':
                        texts = [n.text or '' for n in paragraph.iter()
                                 if n.tag.rsplit('}', 1)[-1] == 't']
                        if texts:
                            lines.append(''.join(texts))
                blocks.append(f'\n[{ordered[i]}]\n' + '\n'.join(lines) + '\n')
            selected = len(wanted)
    else:
        raise ValueError('Supported formats: PDF, PPTX, DOCX')
    text = ''.join(blocks)
    source_chars = sum(len(re.sub(r'\[[^\n]+\]', '', x).strip()) for x in blocks)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return {'status': 'extracted_locally', 'out': str(out), 'bytes': out.stat().st_size,
            'parts': selected, 'source_characters': source_chars,
            'needs_local_ocr': source_chars == 0, 'content_emitted': False,
            'limitation': 'Native text only; layout, images, notes and OCR are not included'}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source'); p.add_argument('--out', required=True)
    p.add_argument('--pages', nargs='+', type=int, help='Zero-based page/slide indices; DOCX has one document part')
    a = p.parse_args()
    try:
        log_path = Path(a.out).resolve().with_name(Path(a.out).name + '.extract.log')
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('x', encoding='utf-8') as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                value = extract(a.source, a.out, a.pages)
        code = 0
    except Exception as exc:
        value = {'status': 'refused', 'reason': type(exc).__name__, 'content_emitted': False}
        code = 2
    sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
    return code

if __name__ == '__main__':
    sys.exit(main())
