"""
cassazione_pdf_extractor.py

Extracts clean, layered content from Corte di Cassazione PDF decisions.

Layers identified from layout analysis:
  - WATERMARK:   vertical rotated text at x > 555  ("Corte di Cassazione - copia non ufficiale")
  - FOOTER:      y < 55  ("Cons. Est. NAME - N")
  - HEADER_META: page 1 only, top text block with case identifiers (snciv layout)
                 NOTE: snpen embeds this block as an image — metadata will be None in that case.
  - SUBJECT:     page 1 only, right-aligned "Oggetto:" block (snciv only)
  - BODY:        everything else — the actual decision text
"""

import re
from dataclasses import dataclass, field
from typing import Optional
from io import BytesIO

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextBox


# ── Thresholds (derived from bounding-box analysis) ──────────────────────────
WATERMARK_X_MIN = 555   # rotated chars live at x0 ≥ 560
FOOTER_Y_MAX    = 55    # "Cons. Est. ..." line sits at y ≈ 42
SUBJECT_X_MIN   = 390   # "Oggetto:" block on page 1 is right-aligned (snciv)
HEADER_Y_MIN    = 700   # top metadata block on page 1 (snciv)


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class DecisionLayers:
    # structured header fields — populated only when header is text (snciv)
    # will be None for snpen where the header is rendered as an image
    sezione:      Optional[str] = None
    numero:       Optional[str] = None
    anno:         Optional[str] = None
    tipo:         Optional[str] = None      # ORDINANZA / SENTENZA / DECRETO
    presidente:   Optional[str] = None
    relatore:     Optional[str] = None
    data_pubbl:   Optional[str] = None      # snciv: Data pubblicazione
    data_udienza: Optional[str] = None      # snpen: Data Udienza (if ever in text)

    # subject matter right column (snciv only)
    oggetto:      Optional[str] = None

    # full clean body text
    body:         str = ""

    # section breakdown
    sections:     dict = field(default_factory=dict)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _is_watermark(box) -> bool:
    return box.x0 >= WATERMARK_X_MIN

def _is_footer(box) -> bool:
    return box.y1 <= FOOTER_Y_MAX

def _is_page1_header(box, page_num: int) -> bool:
    return page_num == 1 and box.y0 >= HEADER_Y_MIN

def _is_page1_subject(box, page_num: int) -> bool:
    return page_num == 1 and box.x0 >= SUBJECT_X_MIN and box.y0 < HEADER_Y_MIN

def _clean(text: str) -> str:
    text = text.replace('\xa0', ' ')
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()

def _parse_header_block(text: str) -> dict:
    """
    Parse snciv top metadata block:
        Civile Ord. Sez. 5   Num. 13080  Anno 2026
        Presidente: LUCIOTTI LUCIO
        Relatore: SUCCIO ROBERTO
        Data pubblicazione: 07/05/2026
    """
    result = {}
    m = re.search(r'Sez\.\s*(\S+)', text)
    if m: result['sezione'] = m.group(1)
    m = re.search(r'Num\.\s*(\d+)', text)
    if m: result['numero'] = m.group(1)
    m = re.search(r'Anno\s*(\d{4})', text)
    if m: result['anno'] = m.group(1)
    m = re.search(r'Presidente:\s*(.+)', text)
    if m: result['presidente'] = m.group(1).strip()
    m = re.search(r'Relatore:\s*(.+)', text)
    if m: result['relatore'] = m.group(1).strip()
    m = re.search(r'Data pubblicazione:\s*(.+)', text)
    if m: result['data_pubbl'] = m.group(1).strip()
    m = re.search(r'Data [Uu]dienza:\s*(.+)', text)
    if m: result['data_udienza'] = m.group(1).strip()
    for t in ('ORDINANZA', 'SENTENZA', 'DECRETO'):
        if t in text.upper():
            result['tipo'] = t
            break
    return result


# ── Section splitting ─────────────────────────────────────────────────────────
SECTION_HEADINGS = [
    'Fatti di causa',
    'Ragioni della decisione',
    'Motivi della decisione',
    'Svolgimento del processo',
    'RITENUTO IN FATTO',
    'CONSIDERATO IN DIRITTO',
    'In fatto',
    'In diritto',
    r'P\.Q\.M\.',
]

def _parse_sections(body: str) -> dict:
    pattern = r'(?<!\w)(' + '|'.join(SECTION_HEADINGS) + r')(?!\w)'
    parts = re.split(pattern, body)

    sections = {}
    if parts[0].strip():
        sections['introduzione'] = parts[0].strip()

    i = 1
    while i < len(parts) - 1:
        heading = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ''
        key = heading.lower()
        if key not in {k.lower() for k in sections}:
            sections[heading] = content
        else:
            last_key = list(sections.keys())[-1]
            sections[last_key] += ' ' + content
        i += 2

    return sections


# ── Main extractor ────────────────────────────────────────────────────────────
def extract_decision(source, parse_sections: bool = True) -> DecisionLayers:
    """
    Extract layered content from a Cassazione PDF.

    Args:
        source: file path (str) or bytes object
        parse_sections: if True, split body into named sections

    Returns:
        DecisionLayers dataclass.
        Note: metadata fields (sezione, numero, etc.) are None for snpen docs
        where the header is rendered as an embedded image rather than text.
        Use the Solr fields (numdec, anno, szdec, presidente, relatore) instead.
    """
    if isinstance(source, bytes):
        source = BytesIO(source)

    layers = DecisionLayers()
    body_chunks = []
    header_text = ''
    subject_text = ''

    for page_num, page_layout in enumerate(extract_pages(source), 1):
        boxes = sorted(
            [el for el in page_layout if isinstance(el, LTTextBox)],
            key=lambda b: -b.y0
        )

        for box in boxes:
            text = box.get_text().strip()
            if not text:
                continue

            # 1. drop watermark
            if _is_watermark(box):
                continue

            # 2. drop running footer
            if _is_footer(box):
                continue

            # 3. capture page-1 header metadata (snciv text block)
            if _is_page1_header(box, page_num):
                header_text += ' ' + text
                continue

            # 4. capture page-1 subject block (snciv right column)
            if _is_page1_subject(box, page_num):
                subject_text += ' ' + text
                continue

            # 5. detect tipo from standalone heading box
            if not layers.tipo and text.strip().upper() in ('ORDINANZA', 'SENTENZA', 'DECRETO'):
                layers.tipo = text.strip().upper()

            # 6. body
            body_chunks.append(text)

    # ── Parse header (snciv only) ─────────────────────────────────────────────
    if header_text:
        parsed = _parse_header_block(header_text)
        layers.sezione      = parsed.get('sezione')
        layers.numero       = parsed.get('numero')
        layers.anno         = parsed.get('anno')
        layers.tipo         = parsed.get('tipo') or layers.tipo
        layers.presidente   = parsed.get('presidente')
        layers.relatore     = parsed.get('relatore')
        layers.data_pubbl   = parsed.get('data_pubbl')
        layers.data_udienza = parsed.get('data_udienza')

    # ── Parse subject (snciv only) ────────────────────────────────────────────
    if subject_text:
        layers.oggetto = re.sub(r'^oggetto\s*:?\s*', '', subject_text.strip(), flags=re.IGNORECASE).strip()

    # ── Assemble body ─────────────────────────────────────────────────────────
    layers.body = _clean('\n'.join(body_chunks))

    # ── Split into sections ───────────────────────────────────────────────────
    if parse_sections:
        layers.sections = _parse_sections(layers.body)

    return layers


# ── CLI / demo ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Usage: python cassazione_pdf_extractor.py <path/to/decision.pdf>")
        sys.exit(1)

    doc = extract_decision(path)

    print("=" * 60)
    print("METADATA  (None = header embedded as image, use Solr fields)")
    print("=" * 60)
    print(f"  Tipo:        {doc.tipo}")
    print(f"  Sezione:     {doc.sezione}")
    print(f"  Numero:      {doc.numero}")
    print(f"  Anno:        {doc.anno}")
    print(f"  Presidente:  {doc.presidente}")
    print(f"  Relatore:    {doc.relatore}")
    print(f"  Pubblicata:  {doc.data_pubbl}")
    print(f"  Udienza:     {doc.data_udienza}")
    print(f"  Oggetto:     {doc.oggetto}")

    print("\n" + "=" * 60)
    print("SECTIONS")
    print("=" * 60)
    for name, text in doc.sections.items():
        print(f"\n[{name}]")
        print(text[:300] + ("..." if len(text) > 300 else ""))

    print("\n" + "=" * 60)
    print(f"BODY ({len(doc.body)} chars total)")
    print("=" * 60)
    print(doc.body[:500] + "...")
