#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diario_pipeline.py — Stage 2 normalisation for the Diario de Sesiones corpus
(Honorable Cámara de Diputados de la Nación, períodos 124-144).

Turns a born-digital, two-column, rotated-page Diario PDF into a tidy
speech-level table:

    one row = one uninterrupted intervention by one speaker

Pipeline stages, each independently inspectable:

    1. HARVEST     lines in *display* coordinates, with typography
    2. COLUMNISE   find the gutter per page, emit single-column reading order
    3. PARAGRAPHS  rebuild paragraphs from the first-line indent
    4. FURNITURE   flag running heads, folios, rules, rosters, section titles
    5. CALIBRATE   learn this document's oratory font size from the paragraphs
                   that open with a taquigráfica speaker header
    6. STRIP       keep oratory-size paragraphs -> documentary inserts
                   (asuntos entrados, bill texts, resolutions, annexes,
                   nominal-vote listings, letters) fall away
    7. SEGMENT     state machine splitting on `Sr./Sra. APELLIDO (paren). –`
    8. EMIT        tidy/long rows -> Parquet + CSV, plus a per-file QA record

Why typography rather than keywords
-----------------------------------
In this corpus the shorthand record sets spoken oratory in one size (11.0pt
in the 2006 plates) and every interpolated document in a smaller one
(9.5pt). That distinction is far more reliable than content rules, and
because stage 5 *learns* the size per document, the same code survives the
2006-2025 layout changes without per-year constants.

Two PDF traps this code exists to avoid
---------------------------------------
* ``page.remove_rotation()`` clips text against the CropBox on these plates:
  syllables vanish mid-word ("Sr. Presidente" -> "Presidente") with nothing
  in the output to announce the loss. Never mutate the page; transform
  coordinates with ``page.rotation_matrix`` instead.
* A PyMuPDF *block* on these pages can span several paragraphs and therefore
  several speeches, so block-level speaker matching silently merges turns.
  Segmentation must run on paragraphs rebuilt from line indentation.

Nothing is dropped silently: every discarded paragraph is tallied by reason
in the QA record, so a layout the heuristics do not understand surfaces as
an anomalous discard ratio rather than as missing speeches.

`party` is emitted blank by design — bloc membership is joined later from an
external roster.

Usage
-----
    python diario_pipeline.py parse FILE.pdf --outdir out/ [--debug]
    python diario_pipeline.py batch  manifest.csv --outdir out/
    python diario_pipeline.py manifest ROOT --out manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import traceback
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pymupdf  # PyMuPDF

# --------------------------------------------------------------------------
# Conventions of the taquigráfica record
# --------------------------------------------------------------------------

DASHES = "–—‒‐-"

# "Sr. Presidente (Balestrini). – Tiene la palabra..."
# "Sra. Carrió. – Señor presidente: sólo quiero..."
# "Sra. González (N. S.). – ..."
SPEAKER_RE = re.compile(
    r"^\s*(?P<title>Sr\.|Sra\.|Srta\.|Señor|Señora)\s+"
    r"(?P<name>[^.\n(]{2,70}?)\s*"
    r"(?:\((?P<paren>[^)\n]{1,60})\)\s*)?"
    r"\.\s*[" + DASHES + r"]\s*"
    r"(?P<rest>.*)$",
    re.DOTALL,
)

# Chamber actions, set as their own paragraph: "(Aplausos.)", "–Se lee:",
# "–Varios señores diputados hablan a la vez.", "–Resulta afirmativa."
STAGE_RE = re.compile(r"^\s*(?:[" + DASHES + r"]\s*\w|\()")

RUNNING_HEAD_RE = re.compile(
    r"(C[ÁA]MARA\s+DE\s+DIPUTADOS\s+DE\s+LA\s+NACI[ÓO]N"
    r"|Reuni[óo]n\s+\d+[ªa]"
    r"|\d+[ªa]\s+REUNI[ÓO]N"
    r"|\d+[ªa]\s+SESI[ÓO]N)",
    re.IGNORECASE,
)

ROSTER_RE = re.compile(
    r"^(DIPUTADOS\s+PRESENTES|AUSENTES|NO\s+INCORPORAD|SUMARIO|"
    r"PRESIDENCIA|Secretari[oa]s?\b)",
    re.IGNORECASE,
)

# All-caps centred section titles: "ASUNTOS ENTRADOS", "DIARIO DE SESIONES",
# "APENDICE", "INSERCIONES". Never speech.
SECTION_TITLE_RE = re.compile(r"^[0-9IVXLC\s.ªº°–—-]*[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\s,.;:()ªº°–—-]{3,}$")

FILENAME_RE = re.compile(r"diario_(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?P<reunion>\d+)")

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

WORD_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)

INDENT_TOL = 4.0       # pt: how far past the margin counts as a first line
GUTTER_FRAC = 0.25     # a gap this fraction of text width is a real gutter
SIZE_TOL = 0.35        # pt tolerance around the calibrated oratory size


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


# --------------------------------------------------------------------------
# 1. Harvest
# --------------------------------------------------------------------------

@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    italic: bool
    text: str
    column: int = 0

    @property
    def width(self) -> float:
        return self.x1 - self.x0


def _line_stats(line: dict) -> tuple[float, bool, bool, str]:
    sizes: Counter = Counter()
    bold = italic = total = 0
    parts: list[str] = []
    for span in line.get("spans", []):
        txt = span["text"]
        if not txt:
            continue
        n = len(txt)
        sizes[round(span["size"], 1)] += n
        font = span["font"].lower()
        if "bold" in font or "black" in font:
            bold += n
        if "italic" in font or "oblique" in font:
            italic += n
        total += n
        parts.append(txt)
    size = sizes.most_common(1)[0][0] if sizes else 0.0
    return (size,
            bool(total and bold / total > 0.6),
            bool(total and italic / total > 0.6),
            "".join(parts))


def harvest(doc: pymupdf.Document) -> list[Line]:
    """Collect every text line, mapped into the printed page's coordinates.

    ``page.rotation_matrix`` maps the unrotated extraction space onto what a
    reader sees, without touching the page (see the module docstring on why
    mutating it corrupts the text).
    """
    out: list[Line] = []
    for page in doc:
        mat = page.rotation_matrix
        for blk in page.get_text("dict", sort=False)["blocks"]:
            if blk.get("type", 0) != 0:          # 1 = image
                continue
            for ln in blk.get("lines", []):
                size, bold, italic, text = _line_stats(ln)
                if not text.strip():
                    continue
                r = pymupdf.Rect(ln["bbox"]) * mat
                r.normalize()
                out.append(Line(page.number, r.x0, r.y0, r.x1, r.y1,
                                size, bold, italic, text))
    return out


# --------------------------------------------------------------------------
# 2. Columnise
# --------------------------------------------------------------------------

def _gutter(body: list[Line]) -> float | None:
    """Locate the gutter from the two column margins.

    The naive "widest gap in the sorted x0 values" fails on this corpus:
    centred section titles and vote tallies scatter x0 values across the
    measure, so the widest gap often falls *inside* a column and the two
    halves of the page get interleaved by the subsequent y-sort — two
    speakers' lines alternating in one paragraph.

    Both columns instead announce themselves as sharp modes in the x0
    histogram, because every continuation line in running prose starts
    exactly at its column's left margin. So: take the modes, require the
    outer two to be a column apart, and put the gutter between the left
    column's right edge and the right column's margin.
    """
    if len(body) < 8:
        return None
    x0s = [ln.x0 for ln in body]
    extent = max(ln.x1 for ln in body) - min(x0s)
    if extent <= 0:
        return None

    hist = Counter(round(x) for x in x0s)
    peak = max(hist.values())
    modes = sorted(x for x, n in hist.items() if n >= max(2, 0.2 * peak))
    if len(modes) < 2:
        return None

    # Each column contributes TWO adjacent modes — its margin and its
    # first-line indent, ~11pt apart. Cluster them, or the right column's
    # indent gets mistaken for its margin and the gutter lands inside the
    # column, sweeping most of the right column into the left one.
    clusters: list[list[int]] = [[modes[0]]]
    for x in modes[1:]:
        if x - clusters[-1][-1] <= 30:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    if len(clusters) < 2:
        return None

    # Take the two HEAVIEST clusters, not the outermost two. Attendance
    # rosters and nominal-vote tallies are set in many narrow columns, so the
    # outermost cluster on such a page belongs to the table, not the body;
    # trusting it pushes the gutter past the real right column and every line
    # there is misread as straddling. The body columns always carry the most
    # lines, so weight decides.
    weighted = sorted(clusters, key=lambda c: sum(hist[x] for x in c), reverse=True)
    left_c, right_c = sorted(weighted[:2], key=min)

    right_margin = float(min(right_c))
    if right_margin - min(left_c) < GUTTER_FRAC * extent:
        return None                                  # single-column page

    # Put the boundary just inside the right column's margin. Deriving it
    # from the left column's right edge instead invites contamination from
    # any full-width line (a running head spans both columns), which drags
    # the boundary into the right column; every right-column line then looks
    # like it straddles the gutter, loses its margin, and the page collapses
    # into one unbroken paragraph with no detectable speaker headers.
    return right_margin - 6.0


def columnise(lines: list[Line]) -> list[Line]:
    """Assign a column per page and return single-column reading order.

    A line straddling the gutter (running head, full-width rule) gets column
    -1 and sorts to the top of the page.

    Per-page estimates are cross-checked against a document-level consensus.
    The plates carry varying CropBoxes, so margins drift from page to page
    and the gutter has to be measured per page — but a page whose own
    estimate is an outlier (a page that is mostly roster or tally, with only
    a few lines of debate) is better served by the consensus position,
    expressed as a fraction of that page's own text extent.
    """
    by_page: dict[int, list[Line]] = defaultdict(list)
    for ln in lines:
        by_page[ln.page].append(ln)

    # Pass 1: per-page estimate, recorded as a fraction of the text extent.
    est: dict[int, float | None] = {}
    frac: dict[int, float] = {}
    for page_no, page_lines in by_page.items():
        body = [ln for ln in page_lines if ln.size >= 8.0]
        mid = _gutter(body) if body else None
        est[page_no] = mid
        if mid is not None:
            x0 = min(ln.x0 for ln in body)
            x1 = max(ln.x1 for ln in body)
            if x1 > x0:
                frac[page_no] = (mid - x0) / (x1 - x0)

    consensus = None
    if len(frac) >= 3:
        vals = sorted(frac.values())
        consensus = vals[len(vals) // 2]

    ordered: list[Line] = []
    for page_no in sorted(by_page):
        page_lines = by_page[page_no]
        body = [ln for ln in page_lines if ln.size >= 8.0]
        mid = est[page_no]
        if body and consensus is not None:
            x0 = min(ln.x0 for ln in body)
            x1 = max(ln.x1 for ln in body)
            if x1 > x0 and (mid is None or abs(frac.get(page_no, 9) - consensus) > 0.12):
                mid = x0 + consensus * (x1 - x0)
        extent = (max(ln.x1 for ln in body) - min(ln.x0 for ln in body)) if body else 0.0
        for ln in page_lines:
            if mid is None:
                ln.column = 0
            elif ln.x0 < mid < ln.x1 - 2 and ln.width > 0.6 * extent:
                # Genuinely full-measure: a running head or a rule. Crossing
                # the estimated gutter is not enough on its own — when the
                # estimate is a little off, ordinary body lines cross it too,
                # and treating those as full-width strips them of their
                # column and of any paragraph structure.
                ln.column = -1
            else:
                ln.column = 0 if ln.x0 < mid else 1
        page_lines.sort(key=lambda l: (l.column, round(l.y0, 1), l.x0))
        ordered.extend(page_lines)
    return ordered


# --------------------------------------------------------------------------
# 3. Paragraphs
# --------------------------------------------------------------------------

@dataclass
class Para:
    page_start: int
    page_end: int
    column: int
    x0: float
    size: float
    bold: bool
    italic: bool
    lines: list[str]
    centred: bool = False
    drop_reason: str | None = None

    @property
    def text(self) -> str:
        return dehyphenate(self.lines)


def _margins(lines: list[Line]) -> dict[tuple[int, int], tuple[float, float]]:
    """Per (page, column): the body left margin and the column's right edge."""
    buckets: dict[tuple[int, int], list[Line]] = defaultdict(list)
    for ln in lines:
        if ln.size >= 8.0 and ln.column >= 0:
            buckets[(ln.page, ln.column)].append(ln)
    out: dict[tuple[int, int], tuple[float, float]] = {}
    for key, group in buckets.items():
        # The margin is the LEFTMOST well-populated left edge, not the most
        # common one. Taking the modal x0 looks right in running prose, where
        # continuation lines outnumber indented first lines — but it inverts
        # on pages of short presidential interventions ("Tiene la palabra el
        # señor diputado por Buenos Aires."), where almost every line is a
        # first line. The indent then wins the vote, no line looks indented,
        # and every speaker header on the page is absorbed into the previous
        # speech. Two well-populated modes a hair apart = margin and indent;
        # the smaller is the margin.
        hist = Counter(round(l.x0) for l in group)
        peak = max(hist.values())
        modes = [x for x, n in hist.items() if n >= max(2, 0.15 * peak)]
        margin = float(min(modes)) if modes else min(l.x0 for l in group)
        out[key] = (margin, max(l.x1 for l in group))
    return out


def paragraphs(lines: list[Line]) -> list[Para]:
    """Rebuild paragraphs from the first-line indent.

    A new paragraph begins when a line's left edge sits past the column's
    body margin (the Diario indents first lines by ~11pt), when the font size
    or emphasis changes, or across a column or page break.
    """
    marg = _margins(lines)
    out: list[Para] = []
    cur: Para | None = None
    prev: Line | None = None

    for ln in lines:
        margin, right = marg.get((ln.page, ln.column), (ln.x0, ln.x1))
        indented = ln.x0 > margin + INDENT_TOL
        centred = indented and (right - ln.x1) > INDENT_TOL * 2

        # Note what is NOT a paragraph break: a column or page change. A
        # paragraph routinely runs from the foot of one column to the head of
        # the next, often mid-word ("dipu-" / "tados"). Breaking there would
        # strand the fragment and leave a dangling hyphen in the output. The
        # indent alone decides; a continuation line simply carries on.
        new = (
            cur is None
            or indented
            # A speaker header opens a paragraph by the convention of the
            # record, so this does not depend on measuring the indent
            # correctly. Geometry is best-effort across two decades of
            # changing plates; this rule is not, and it is what guarantees no
            # intervention is ever absorbed into its predecessor.
            or SPEAKER_RE.match(ln.text.strip()) is not None
            or abs(ln.size - cur.size) > 0.2
            or ln.italic != cur.italic
            or ln.bold != cur.bold
        )
        if new:
            cur = Para(ln.page, ln.page, ln.column, ln.x0, ln.size,
                       ln.bold, ln.italic, [ln.text], centred)
            out.append(cur)
        else:
            cur.lines.append(ln.text)
            cur.page_end = ln.page
        prev = ln
    return out


LIGATURES = "ﬀﬁﬂﬃﬄﬅﬆ"   # ff fi fl ffi ffl ft st
LIG_SPLIT_RE = re.compile(r"([" + LIGATURES + r"])[  ]+(?=[a-záéíóúüñ])")

# Some plates emit the same ligature already decomposed, so the stray space
# sits between ordinary letters: "modifi caciones", "ofi cialismo". Requiring
# a letter before and a lower-case letter after keeps this off word
# boundaries; only -fi and -fl are joined, because Spanish has essentially no
# words ending in those, while -ff does occur ("staff").
LIG_PLAIN_RE = re.compile(r"(f[il])[  ]+(?=[a-záéíóúüñ])")


# Chamber actions the shorthand writer inserts inline, inside the speaker's
# own paragraph: "(Aplausos.)", "(Risas.)", "(Murmullos en las bancas.)".
# They are not spoken words, so they are removed before the counts are taken
# — otherwise they inflate the word total and enter the vocabulary. Only this
# closed, conventional set is touched; a deputy's own parenthetical remark
# stays in the text.
INLINE_ACTION_RE = re.compile(
    r"\s*\((?:[¡!]*\s*)?(?:Aplausos|Risas|Murmullos|Hilaridad|Asentimiento|"
    r"Varios\s+señores\s+diputados|Manifestaciones|Silbidos|Abucheos|"
    r"Aplausos\s+prolongados)\b[^)]{0,80}\)\s*",
    re.IGNORECASE)


def normalise_text(s: str) -> str:
    """Repair the text-layer artefacts of these plates.

    The typesetter's ligatures arrive as single code points followed by a
    stray space, so words come out of the PDF split in two: "identiﬁ cación",
    "reﬂ ejadas", "Caﬁ ero". Left alone this inflates the word count,
    pollutes the vocabulary with fragments, and therefore skews every
    stage-3 measure — readability, diversity and homogeneity alike. Over
    1,200 words in a single 2016 session are affected.

    The space is removed only before a lower-case letter, so a ligature that
    genuinely ends a word keeps its word boundary. NFKC then expands the
    ligatures themselves, and the usual PDF whitespace oddities go with them.
    """
    s = s.replace("­", "")                  # soft hyphen
    s = LIG_SPLIT_RE.sub(r"\1", s)
    s = unicodedata.normalize("NFKC", s)
    s = LIG_PLAIN_RE.sub(r"\1", s)
    s = s.replace(" ", " ").replace("​", "")
    s = INLINE_ACTION_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def dehyphenate(lines: list[str]) -> str:
    """Join the hard line breaks the two-column measure forces.

    `conside-` + `ración` -> `consideración`; a hyphen kept before an
    upper-case word or a digit (ranges, article numbers, compound names).
    """
    out: list[str] = []
    for raw in (normalise_text(l) for l in lines):
        if not raw:
            continue
        if out and out[-1].endswith(("-", "‐")) and raw[:1].islower():
            out[-1] = out[-1][:-1] + raw
        elif out:
            out[-1] = out[-1] + " " + raw
        else:
            out.append(raw)
    # Normalise once more over the joined text: a ligature split across a
    # line or column break only becomes visible after the pieces are joined.
    return normalise_text(" ".join(out))


# --------------------------------------------------------------------------
# 4. Furniture
# --------------------------------------------------------------------------

def mark_furniture(paras: list[Para]) -> None:
    page_tops: dict[int, float] = {}
    for p in paras:
        page_tops[p.page_start] = min(page_tops.get(p.page_start, 1e9), 0.0)

    for p in paras:
        t = p.text.strip()
        if not t:
            p.drop_reason = "empty"
        elif RUNNING_HEAD_RE.search(t) and len(t) < 140:
            p.drop_reason = "running_head"
        elif re.fullmatch(r"[\s\d.,;:ªº°IVXLC" + DASHES + r"]{1,24}", t):
            p.drop_reason = "folio_or_rule"
        elif p.size <= 8.0:
            p.drop_reason = "roster_or_signature"
        elif ROSTER_RE.match(t):
            p.drop_reason = "roster_header"
        elif p.centred and len(t) < 90 and SECTION_TITLE_RE.match(t):
            p.drop_reason = "section_title"


# --------------------------------------------------------------------------
# 5-6. Calibrate, strip
# --------------------------------------------------------------------------

def calibrate(paras: list[Para]) -> tuple[float | None, dict]:
    """Learn the font size the record uses for spoken oratory.

    Paragraphs opening with a speaker header are oratory by construction, so
    their modal size is this document's oratory size.
    """
    hits: Counter = Counter()
    for p in paras:
        if p.drop_reason:
            continue
        if SPEAKER_RE.match(p.text.strip()):
            hits[p.size] += 1
    if not hits:
        return None, {"speaker_paras_by_size": {}}
    size, n = hits.most_common(1)[0]
    return size, {
        "speaker_paras_by_size": {str(k): v for k, v in sorted(hits.items())},
        "oratory_size": size,
        "oratory_size_support": n,
        "oratory_size_purity": round(n / sum(hits.values()), 4),
    }


def strip_inserts(paras: list[Para], oratory: float | None) -> None:
    if oratory is None:
        return
    for p in paras:
        if p.drop_reason:
            continue
        if abs(p.size - oratory) > SIZE_TOL:
            p.drop_reason = ("insert_smaller_type" if p.size < oratory
                             else "display_larger_type")


# --------------------------------------------------------------------------
# 7. Segment
# --------------------------------------------------------------------------

def classify_role(title: str, name: str, paren: str | None) -> tuple[str, str, str]:
    """(role, surname, chair_name) from a speaker header.

    "Sr. Presidente (Balestrini)" -> ('presidente', 'Balestrini', 'Balestrini')
    "Sra. Carrió"                 -> ('diputada',   'Carrió',     '')
    """
    n = name.strip().rstrip(";,")
    low = _strip_accents(n).lower()
    if low.startswith("president"):
        chair = (paren or "").strip()
        return ("presidente", chair or n, chair)
    if low.startswith("secretari"):
        return ("secretario", (paren or "").strip() or n, "")
    if low.startswith(("ministr", "jefe de gabinete", "señor ministr")):
        return ("funcionario", (paren or "").strip() or n, "")
    gender = "diputada" if title.lower().startswith(("sra", "señora")) else "diputado"
    return (gender, n, "")


@dataclass
class Speech:
    seq: int
    page_start: int
    page_end: int
    title: str
    speaker_raw: str
    speaker_surname: str
    role: str
    chair_name: str
    paren: str
    chunks: list[str]
    n_paras: int = 1
    n_stage_directions: int = 0

    @property
    def text(self) -> str:
        # Hyphen-aware join: an intervention resumed after an interpolated
        # document can resume mid-word, so the same rule that stitches lines
        # has to stitch chunks.
        return dehyphenate(self.chunks)


def segment(paras: list[Para]) -> tuple[list[Speech], int, int]:
    """Paragraph state machine -> speeches, preamble count, orphan count.

    An interpolated document or a chamber action interrupts an intervention
    without ending it: the paragraphs that follow continue the same speaker
    until the next header appears. Paragraphs before the first header are
    preamble (sumario tail, opening formalities) and are counted, not emitted.
    """
    speeches: list[Speech] = []
    cur: Speech | None = None
    preamble = orphan = 0

    for p in paras:
        if p.drop_reason:
            continue
        raw = p.text.strip()
        m = SPEAKER_RE.match(raw)
        if m:
            role, surname, chair = classify_role(
                m.group("title"), m.group("name"), m.group("paren"))
            cur = Speech(
                seq=len(speeches) + 1,
                page_start=p.page_start, page_end=p.page_end,
                title=m.group("title"),
                speaker_raw=f"{m.group('title')} {m.group('name').strip()}",
                speaker_surname=surname, role=role, chair_name=chair,
                paren=(m.group("paren") or "").strip(),
                chunks=[m.group("rest")],
            )
            speeches.append(cur)
            continue
        if cur is None:
            preamble += 1
            continue
        if p.italic or STAGE_RE.match(raw):
            cur.n_stage_directions += 1
            cur.page_end = max(cur.page_end, p.page_end)
            continue
        cur.chunks.append(p.text)
        cur.n_paras += 1
        cur.page_end = max(cur.page_end, p.page_end)

    speeches = [s for s in speeches if s.text]
    for i, s in enumerate(speeches, 1):
        s.seq = i
    return speeches, preamble, orphan


# --------------------------------------------------------------------------
# Session metadata
# --------------------------------------------------------------------------

def session_meta(doc: pymupdf.Document, path: Path) -> dict:
    meta: dict = {
        "source_file": path.name, "periodo": None, "fecha": None,
        "reunion": None, "sesion_nro": None, "sesion_tipo": None,
        "n_pages": doc.page_count,
    }
    m = FILENAME_RE.search(path.name)
    if m:
        meta["fecha"] = f"{m.group('y')}-{m.group('m')}-{m.group('d')}"
        meta["reunion"] = int(m.group("reunion"))
    for part in path.parts[::-1]:
        pm = re.search(r"Periodo[_\s-]*(\d{3})", part, re.IGNORECASE)
        if pm:
            meta["periodo"] = int(pm.group(1))
            break

    head = doc[0].get_text()
    hm = re.search(
        r"(\d+)\s*[ªa]\s*REUNI[ÓO]N\s*[" + DASHES + r"]?\s*"
        r"(\d+)\s*[ªa]\s*SESI[ÓO]N\s*([A-ZÁÉÍÓÚ()\s]*)",
        head, re.IGNORECASE)
    if hm:
        meta["reunion"] = meta["reunion"] or int(hm.group(1))
        meta["sesion_nro"] = int(hm.group(2))
        # The plate runs the session type straight into the month
        # ("ORDINARIA (ESPECIAL) JUNIO"), so keep only the type words.
        kinds = re.findall(
            r"ORDINARIA|EXTRAORDINARIA|ESPECIAL|PREPARATORIA|INFORMATIVA|"
            r"CONTINUACI[ÓO]N|MINUTA",
            hm.group(3), re.IGNORECASE)
        meta["sesion_tipo"] = " ".join(k.title() for k in kinds) or None
    if not meta["fecha"]:
        dm = re.search(r"(\d{1,2})\s+DE\s+([A-ZÁÉÍÓÚ]+)\s+DE\s+(\d{4})", head, re.IGNORECASE)
        if dm:
            mon = MONTHS.get(_strip_accents(dm.group(2)).lower())
            if mon:
                meta["fecha"] = f"{dm.group(3)}-{mon:02d}-{int(dm.group(1)):02d}"
    return meta


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def parse_pdf(path: Path, debug: bool = False) -> tuple[list[dict], dict]:
    doc = pymupdf.open(path)
    meta = session_meta(doc, path)

    lines = columnise(harvest(doc))
    paras = paragraphs(lines)
    mark_furniture(paras)
    oratory, calib = calibrate(paras)
    strip_inserts(paras, oratory)
    speeches, preamble, orphan = segment(paras)

    rows: list[dict] = []
    for sp in speeches:
        text = sp.text
        rows.append({
            **{k: meta[k] for k in
               ("periodo", "fecha", "reunion", "sesion_nro", "sesion_tipo", "source_file")},
            "speech_seq": sp.seq,
            "speaker_raw": sp.speaker_raw,
            "speaker_surname": sp.speaker_surname,
            "speaker_paren": sp.paren,
            # Join key for the bloc roster: the record disambiguates
            # same-surname deputies by initials, "Sra. García (M. T.)", and
            # the surname alone would collapse them into one legislator.
            "speaker_key": (f"{sp.speaker_surname} ({sp.paren})"
                            if sp.paren and sp.role in ("diputado", "diputada")
                            else sp.speaker_surname),
            "role": sp.role,
            "chair_name": sp.chair_name,
            "party": "",                      # joined later from the roster
            "page_start": sp.page_start,
            "page_end": sp.page_end,
            "n_paragraphs": sp.n_paras,
            "n_stage_directions": sp.n_stage_directions,
            "n_chars": len(text),
            "n_words": len(WORD_RE.findall(text)),
            "text": text,
        })

    # Self-check, run on every file. A speaker header occupies a whole line
    # of the record, so the number of oratory lines that open with one is an
    # independent count of the interventions on the page — derived from the
    # raw line stream, before any columnisation, paragraph rebuilding or
    # furniture rule has had a chance to go wrong. If it disagrees with the
    # number of speeches emitted, segmentation lost or invented turns on this
    # file, and the row count cannot be trusted. This is the invariant that
    # caught every parsing bug found while building the pipeline.
    expected = sum(
        1 for ln in lines
        if oratory is not None and abs(ln.size - oratory) <= SIZE_TOL
        and SPEAKER_RE.match(ln.text.strip()))

    kept = sum(1 for p in paras if not p.drop_reason)
    qa = {
        **meta,
        "n_paras_total": len(paras),
        "n_paras_kept": kept,
        "n_paras_discarded": dict(Counter(p.drop_reason for p in paras if p.drop_reason)),
        "n_speeches": len(rows),
        "n_words_total": sum(r["n_words"] for r in rows),
        "n_distinct_speakers": len({r["speaker_surname"] for r in rows}),
        "n_preamble_paras": preamble,
        "n_header_lines_expected": expected,
        "segmentation_exact": expected == len(rows),
        **calib,
        "status": ("ok" if rows and oratory and expected == len(rows)
                   else "suspect"),
    }
    if debug:
        qa["debug_paras"] = [
            {"page": p.page_start, "col": p.column, "x0": round(p.x0, 1),
             "size": p.size, "it": p.italic, "centred": p.centred,
             "drop": p.drop_reason, "text": p.text[:140]} for p in paras]
    return rows, qa


def write_outputs(rows: list[dict], qa: dict, outdir: Path, stem: str) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "qa").mkdir(exist_ok=True)
    (outdir / "qa" / f"{stem}.json").write_text(
        json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    if not rows:
        return
    try:
        import pandas as pd
        df = pd.DataFrame(rows)
        df.to_parquet(outdir / f"{stem}.parquet", index=False)
        df.to_csv(outdir / f"{stem}.csv", index=False, encoding="utf-8")
    except ImportError:
        with (outdir / f"{stem}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def build_manifest(root: Path, out: Path) -> int:
    pdfs = sorted(root.rglob("*.pdf"))
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "periodo", "size_bytes"])
        for p in pdfs:
            pm = re.search(r"Periodo[_\s-]*(\d{3})", str(p), re.IGNORECASE)
            w.writerow([str(p), pm.group(1) if pm else "", p.stat().st_size])
    return len(pdfs)


def run_batch(manifest: Path, outdir: Path, resume: bool = True) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "qa").mkdir(exist_ok=True)
    qa_all: list[dict] = []
    entries = list(csv.DictReader(manifest.open(encoding="utf-8")))
    for i, e in enumerate(entries, 1):
        path, stem = Path(e["path"]), Path(e["path"]).stem
        qa_path = outdir / "qa" / f"{stem}.json"
        if resume and qa_path.exists():
            qa_all.append(json.loads(qa_path.read_text(encoding="utf-8")))
            print(f"[{i}/{len(entries)}] skip {stem}", flush=True)
            continue
        try:
            rows, qa = parse_pdf(path)
            write_outputs(rows, qa, outdir, stem)
            qa_all.append(qa)
            print(f"[{i}/{len(entries)}] {stem}: {qa['n_speeches']} speeches, "
                  f"{qa['n_words_total']} words, {qa['status']}", flush=True)
        except Exception as exc:                       # keep the batch alive
            qa = {"source_file": path.name, "status": "error",
                  "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()}
            qa_path.write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
            qa_all.append(qa)
            print(f"[{i}/{len(entries)}] ERROR {stem}: {exc}", file=sys.stderr, flush=True)
    try:
        import pandas as pd
        pd.DataFrame(qa_all).to_csv(outdir / "_qa_summary.csv", index=False)
    except ImportError:
        pass
    print(f"\ndone: {len(qa_all)} files, "
          f"{sum(q.get('n_speeches', 0) for q in qa_all)} speeches")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("parse", help="normalise one PDF")
    p1.add_argument("pdf")
    p1.add_argument("--outdir", default="out")
    p1.add_argument("--debug", action="store_true")

    p2 = sub.add_parser("batch", help="normalise every PDF in a manifest")
    p2.add_argument("manifest")
    p2.add_argument("--outdir", default="out")
    p2.add_argument("--no-resume", action="store_true")

    p3 = sub.add_parser("manifest", help="build a manifest from a folder tree")
    p3.add_argument("root")
    p3.add_argument("--out", default="manifest.csv")

    a = ap.parse_args(argv)
    if a.cmd == "parse":
        path = Path(a.pdf)
        rows, qa = parse_pdf(path, debug=a.debug)
        write_outputs(rows, qa, Path(a.outdir), path.stem)
        print(json.dumps({k: v for k, v in qa.items() if k != "debug_paras"},
                         ensure_ascii=False, indent=2))
    elif a.cmd == "batch":
        run_batch(Path(a.manifest), Path(a.outdir), resume=not a.no_resume)
    else:
        print(f"{build_manifest(Path(a.root), Path(a.out))} PDFs -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
