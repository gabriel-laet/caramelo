"""Minimal, dependency-free .xlsx reader (first-party gov spreadsheets).

Inep (Ideb) and SINISA publish municipal tables only as .xlsx. Rather than
pull openpyxl into the container for two files, read the OOXML directly:
shared strings + one worksheet, streamed with iterparse so a 12 MB sheet
stays cheap. Returns rows as lists of strings (numbers keep their raw
text; callers parse what they need). Formulas, styles and dates are
ignored — none of the sources we read rely on them.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Iterator
from xml.etree.ElementTree import iterparse

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    out: list[str] = []
    with zf.open("xl/sharedStrings.xml") as fh:
        for _, el in iterparse(fh):
            if el.tag == f"{NS}si":
                out.append("".join(t.text or "" for t in el.iter(f"{NS}t")))
                el.clear()
    return out


def _sheet_path(zf: zipfile.ZipFile, sheet: int | str) -> str:
    """Worksheet part for a 0-based index or a sheet name."""
    from xml.etree.ElementTree import fromstring
    wb = fromstring(zf.read("xl/workbook.xml"))
    sheets = wb.find(f"{NS}sheets").findall(f"{NS}sheet")
    chosen = (sheets[sheet] if isinstance(sheet, int)
              else next(s for s in sheets if s.get("name") == sheet))
    rid = chosen.get(f"{_REL_NS}id")
    rels = fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    target = next(r.get("Target") for r in rels if r.get("Id") == rid)
    return target.lstrip("/") if target.startswith("/") else f"xl/{target}"


def rows(path: Path | zipfile.ZipFile, sheet: int | str = 0) -> Iterator[list[str]]:
    zf = path if isinstance(path, zipfile.ZipFile) else zipfile.ZipFile(path)
    strings = _shared_strings(zf)
    with zf.open(_sheet_path(zf, sheet)) as fh:
        for _, el in iterparse(fh):
            if el.tag != f"{NS}row":
                continue
            cells: list[str] = []
            for c in el.findall(f"{NS}c"):
                idx = _col_index(c.get("r")) if c.get("r") else len(cells)
                while len(cells) < idx:
                    cells.append("")
                kind = c.get("t")
                if kind == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(f"{NS}t"))
                else:
                    v = c.find(f"{NS}v")
                    val = v.text if v is not None and v.text else ""
                    if kind == "s" and val:
                        val = strings[int(val)]
                cells.append(val.strip())
            yield cells
            el.clear()


def table(path: Path | zipfile.ZipFile, header_match: str,
          sheet: int | str = 0) -> Iterator[dict[str, str]]:
    """Rows as dicts, keyed by the first row containing `header_match`.

    Gov spreadsheets open with title/notes rows; the header is found by
    content, not position, so a re-issued file with one more note line
    still parses.
    """
    header: list[str] | None = None
    for r in rows(path, sheet):
        if header is None:
            if header_match in r:
                header = r
            continue
        if not any(r):
            continue
        yield {h: (r[i] if i < len(r) else "") for i, h in enumerate(header) if h}
