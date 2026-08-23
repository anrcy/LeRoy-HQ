#!/usr/bin/env python3
"""
Add fillable AcroForm text fields to a flat PDF contract.

HMB standard: every client-facing contract ships FILLABLE. The client should be
able to tab through boxes in free Adobe Reader — never asked to place floating
text on a drawn line with Fill & Sign.

Requires: PyMuPDF  (`pip install pymupdf`) — already present on Brian's box.

USAGE
-----
1.  Render the contract HTML -> flat PDF (Chrome headless):

        & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
            --headless --disable-gpu --no-pdf-header-footer `
            --print-to-pdf="OUT.pdf" "file:///C:/path/to/contract.html"

2.  DISCOVER the coordinates of every ruled line (do NOT guess them):

        python pdf-add-form-fields.py --discover OUT.pdf

    Ruled lines in the HMB contract template are CSS `border-bottom` rules, which
    render as thin filled rectangles (height < 1.4pt). --discover prints every one
    with its page, x-range and y, plus nearby text anchors so you can tell which
    line is which.

3.  Edit the FIELDS table below, then build:

        python pdf-add-form-fields.py --build OUT.pdf OUT_FILLABLE.pdf

4.  VERIFY VISUALLY — always. Crop-render each field region to PNG and look at it:

        python pdf-add-form-fields.py --preview OUT_FILLABLE.pdf <page> <x0> <y0> <x1> <y1>

GEOMETRY RULE (learned the hard way 2026-07-31)
-----------------------------------------------
PyMuPDF TOP-ALIGNS text inside a widget. For a ruled line at y=L with 9pt text:

        field rect = (x0, L - 12, x1, L - 0.5)

Do NOT extend y1 past L — the field's fill tint will paint OVER the ruled line and
the underline disappears, which looks unfinished. Keep the box just above the rule
so the line stays visible and the typed text sits on it like handwriting.

Also: check horizontal bounds against neighbouring TEXT, not just the line. An
initials box sized to the underscore run can still cover the first letter of the
next label.
"""

import sys
import fitz

# --- Appearance: HMB house style ------------------------------------------
TINT = (0.937, 0.961, 0.988)   # pale blue — makes fields discoverable
FONT_SIZE = 9
BORDER_WIDTH = 0               # the printed rule IS the border

# --- Field table ----------------------------------------------------------
# (page_index, field_name, x0, y0, x1, y1, default_value, tooltip)
# Populate from --discover output. Prefill a value when the fact is known but
# should stay client-correctable (e.g. a name sourced from public record).
FIELDS = [
    # (0, 'client_owner_last_name', 88.5, 201.0, 171.0, 212.5, '', "Owner's last name"),
]


def discover(path):
    doc = fitz.open(path)
    for page in doc:
        rules = []
        for d in page.get_drawings():
            for it in d['items']:
                if it[0] == 're':
                    r = it[1]
                    if r.height < 1.4 and r.width > 25:
                        rules.append(r)
                elif it[0] == 'l':
                    p1, p2 = it[1], it[2]
                    if abs(p1.y - p2.y) < 0.6 and abs(p2.x - p1.x) > 25:
                        rules.append(fitz.Rect(min(p1.x, p2.x), p1.y, max(p1.x, p2.x), p1.y))
        if not rules:
            continue
        print(f'\n===== PAGE {page.number + 1}  (size {page.rect.width:.0f}x{page.rect.height:.0f})')
        for r in sorted(rules, key=lambda r: (r.y0, r.x0)):
            near = [w[4] for w in page.get_text('words')
                    if abs(w[3] - r.y0) < 16 and w[0] < r.x1 + 90 and w[2] > r.x0 - 90]
            ctx = ' '.join(near[:9])
            print(f'  RULE y={r.y0:7.2f}  x={r.x0:7.2f}->{r.x1:7.2f}  w={r.width:6.1f}   ~ {ctx[:70]}')
            print(f'        suggested rect: ({r.x0:.2f}, {r.y0 - 12:.2f}, {r.x1:.2f}, {r.y0 - 0.5:.2f})')
    doc.close()


def build(src, out):
    if not FIELDS:
        sys.exit('FIELDS table is empty — run --discover first and fill it in.')
    doc = fitz.open(src)
    for pi, name, x0, y0, x1, y1, val, tip in FIELDS:
        w = fitz.Widget()
        w.field_name = name
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.rect = fitz.Rect(x0, y0, x1, y1)
        w.field_value = val
        w.field_label = tip
        w.text_fontsize = FONT_SIZE
        w.text_color = (0, 0, 0)
        w.fill_color = TINT
        w.border_width = BORDER_WIDTH
        doc[pi].add_widget(w)
    doc.save(out, garbage=3, deflate=True)
    doc.close()

    v = fitz.open(out)
    n = 0
    for p in v:
        for wd in p.widgets():
            n += 1
            print(f'  p{p.number + 1}  {wd.field_name:28} {wd.field_type_string:6} value={wd.field_value!r}')
    print(f'\n{n} fields across {v.page_count} pages -> {out}')
    print('NOW RENDER AND LOOK AT IT. Do not ship an unverified form.')
    v.close()


def preview(path, page, x0, y0, x1, y1, dst='preview.png'):
    doc = fitz.open(path)
    doc[int(page)].get_pixmap(dpi=150, clip=fitz.Rect(x0, y0, x1, y1)).save(dst)
    print('wrote', dst)
    doc.close()


if __name__ == '__main__':
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    mode = sys.argv[1]
    if mode == '--discover':
        discover(sys.argv[2])
    elif mode == '--build':
        build(sys.argv[2], sys.argv[3])
    elif mode == '--preview':
        preview(sys.argv[2], sys.argv[3], *map(float, sys.argv[4:8]))
    else:
        sys.exit(__doc__)
