"""Load the SandBox-Eval-Datasets manifests into normalized eval cases.

Scope: NON-IMAGE document-QA only. The standalone "Images" category is skipped
(those queries go through vision, not the bash/sandbox tool). PDF, Office, and
Combo are loaded. Cross-file ("Multiple files") is a follow-up (different layout).

Each manifest .xlsx has, per worksheet:
  - a header row whose B column == "File Name"
  - section header rows whose A column starts with the "▶" marker (skipped)
  - query rows: numeric column A, file name in B, and:
        D = Query Type, E = Test Query, F = Expected Behaviour,
        G = Gold Standard Response, H = Failure Example, I = Notes

We parse the xlsx directly from its zip/XML (no openpyxl dependency) so this
runs against the stock python in the AO venv.
"""

from __future__ import annotations

import html
import json
import re
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path

DATASET_ROOT = Path(__file__).resolve().parent / "dataset"

# category -> manifest path (relative to DATASET_ROOT).
IN_SCOPE_MANIFESTS = {
    "office": "office/Office_Eval_Dataset.xlsx",
    "pdf": "pdf/PDF_Eval_Dataset_Final.xlsx",
    "combo": "combo/Combo_Eval_Dataset.xlsx",
    "image": "image/Image_Eval_Dataset.xlsx",
}

# cross-file (multiple-files-per-query) manifest; only the non-image sheet is used.
CROSS_FILE_MANIFEST = "Multiple files/Cross_File_Eval_Questions.xlsx"
# folders searched when resolving a cross-file filename
RESOLVE_DIRS = ["Multiple files", "office", "pdf", "combo"]

MARKER = "▶"  # ▶ section-header marker


@dataclass
class EvalCase:
    id: str
    category: str
    sheet: str
    file_name: str
    file_path: str
    file_exists: bool
    query_type: str
    test_query: str
    expected_behaviour: str
    gold_standard: str
    notes: str
    files: list[str] = field(default_factory=list)  # all attachment paths (>=1; multiple for cross-file)


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
    return [html.unescape(re.sub("<[^>]+>", "", m)) for m in re.findall(r"<si>(.*?)</si>", sx, re.S)]


def _sheet_files(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return [(sheet_name, worksheet_xml_filename), ...] in workbook order."""
    wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
    names = re.findall(r'<sheet[^>]*name="([^"]*)"', wb)
    # worksheets are sheet1.xml, sheet2.xml, ... in declared order
    return [(html.unescape(n), f"sheet{i + 1}.xml") for i, n in enumerate(names)]


def _rows(z: zipfile.ZipFile, ws_file: str, ss: list[str]) -> list[dict[str, str]]:
    """Parse a worksheet into [{col_letter: value}, ...]. Handles both shared
    strings (office/pdf/combo) and inline strings (cross-file workbook)."""
    sheet = z.read("xl/worksheets/" + ws_file).decode("utf-8", "replace")
    out = []
    for row in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
        d: dict[str, str] = {}
        for col, attrs, inner in re.findall(r'<c r="([A-Z]+)\d+"([^>]*)>(.*?)</c>', row, re.S):
            tmatch = re.search(r't="([^"]*)"', attrs)
            t = tmatch.group(1) if tmatch else ""
            if t == "inlineStr":
                m = re.search(r"<t[^>]*>(.*?)</t>", inner, re.S)
                val = re.sub(r"<[^>]+>", "", m.group(1)) if m else ""
            else:
                m = re.search(r"<v>(.*?)</v>", inner, re.S)
                raw = m.group(1) if m else ""
                if t == "s" and raw != "":
                    val = ss[int(raw)] if int(raw) < len(ss) else raw
                else:
                    val = raw
            d[col] = html.unescape(val)
        out.append(d)
    return out


def _resolve_multi(name: str) -> tuple[str, bool]:
    """Resolve a cross-file filename across the candidate dataset folders."""
    name = name.strip()
    for sub in RESOLVE_DIRS:
        direct = DATASET_ROOT / sub / name
        if direct.exists():
            return str(direct), True
    for sub in RESOLVE_DIRS:  # prefix fallback for truncated/renamed cells
        d = DATASET_ROOT / sub
        if not d.exists():
            continue
        matches = [p for p in d.glob("*") if p.is_file() and p.name.startswith(name[:18])]
        if len(matches) == 1:
            return str(matches[0]), True
    return name, False


def load_cross_file() -> list[EvalCase]:
    """Parse the cross-file manifest (multiple files per query), non-image sheet only.
    Columns: # | Cross-File Question | Files Required | Formats | Why | Gold Standard | ..."""
    manifest = DATASET_ROOT / CROSS_FILE_MANIFEST
    if not manifest.exists():
        print(f"[warn] cross-file manifest missing: {manifest}")
        return []
    z = zipfile.ZipFile(manifest)
    ss = _shared_strings(z)
    cases: list[EvalCase] = []
    for sheet_name, ws_file in _sheet_files(z):
        if "image" in sheet_name.lower():
            continue  # skip the image sheet — non-image only
        for d in _rows(z, ws_file, ss):
            a = d.get("A", "").strip()
            question = d.get("B", "").strip()
            files_raw = d.get("C", "").strip()
            if not a.isdigit() or not question or not files_raw:
                continue
            names = [n.strip() for n in re.split(r"[\n;,]+", files_raw) if n.strip()]
            resolved = [_resolve_multi(n) for n in names]
            paths = [p for p, _ in resolved]
            cases.append(
                EvalCase(
                    id=f"multiple-{a}",
                    category="multiple",
                    sheet=sheet_name,
                    file_name="; ".join(names),
                    file_path=paths[0] if paths else "",
                    file_exists=all(ex for _, ex in resolved),
                    query_type=d.get("D", "").strip(),
                    test_query=question,
                    expected_behaviour=d.get("E", "").strip(),
                    gold_standard=d.get("F", "").strip(),
                    notes=d.get("H", "").strip(),
                    files=paths,
                )
            )
    return cases


def _column_map(header: dict[str, str]) -> dict[str, str]:
    """Map logical fields to column letters from a manifest header row.

    Test Query rule: if the header labels more than one column "Test Query"
    (e.g. an original + a rewritten column), use column **B**; otherwise use
    whichever single column carries the label.
    """
    by_label: dict[str, list[str]] = {}
    for col, val in header.items():
        by_label.setdefault(val.strip(), []).append(col)

    def first(label: str) -> str:
        cols = sorted(by_label.get(label, []))
        return cols[0] if cols else ""

    tq_cols = sorted(by_label.get("Test Query", []))
    if len(tq_cols) > 1:
        test_query = "B" if "B" in tq_cols else tq_cols[0]
    else:
        test_query = tq_cols[0] if tq_cols else ""

    return {
        "file_name": first("File Name"),
        "test_query": test_query,
        "query_type": first("Query Type"),
        "expected": first("Expected Behaviour"),
        "gold": first("Gold Standard Response"),
        "notes": first("Notes"),
    }


def _resolve_file(category_dir: Path, file_name: str) -> tuple[str, bool]:
    """Find the actual file on disk for a manifest file-name cell."""
    name = file_name.strip()
    direct = category_dir / name
    if direct.exists():
        return str(direct), True
    # manifest cell may be truncated/renamed — match by basename prefix
    matches = [p for p in category_dir.rglob("*") if p.is_file() and p.name.startswith(name[:20])]
    if len(matches) == 1:
        return str(matches[0]), True
    return str(direct), False


def load_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    for category, rel in IN_SCOPE_MANIFESTS.items():
        manifest = DATASET_ROOT / rel
        category_dir = manifest.parent
        if not manifest.exists():
            print(f"[warn] manifest missing: {manifest}")
            continue
        z = zipfile.ZipFile(manifest)
        ss = _shared_strings(z)
        for sheet_name, ws_file in _sheet_files(z):
            rows = _rows(z, ws_file, ss)
            # Column layout is discovered from the header row (the row that
            # contains a "File Name" cell). This handles both manifest shapes
            # and any column reordering.
            cols: dict[str, str] = {}
            for d in rows:
                a = d.get("A", "").strip()
                # A header row is one that labels both File Name and Test Query.
                labels = {v.strip(): k for k, v in d.items()}
                if "File Name" in labels and "Test Query" in labels:
                    cols = _column_map(d)
                    continue
                if not cols:
                    continue
                if a.startswith(MARKER) or not a.isdigit():
                    continue  # section header / blank
                file_name = d.get(cols["file_name"], "").strip()
                query = d.get(cols["test_query"], "").strip()
                query_type = d.get(cols["query_type"], "").strip() if cols.get("query_type") else ""
                gold = d.get(cols["gold"], "").strip() if cols.get("gold") else ""
                expected = d.get(cols["expected"], "").strip() if cols.get("expected") else ""
                notes = d.get(cols["notes"], "").strip() if cols.get("notes") else ""
                if not file_name or not query:
                    continue
                fpath, exists = _resolve_file(category_dir, file_name)
                cases.append(
                    EvalCase(
                        id=f"{category}-{sheet_name[:4]}-{a}",
                        category=category,
                        sheet=sheet_name,
                        file_name=file_name,
                        file_path=fpath,
                        file_exists=exists,
                        query_type=query_type,
                        test_query=query,
                        expected_behaviour=expected,
                        gold_standard=gold,
                        notes=notes,
                        files=[fpath],
                    )
                )
    return cases


def main() -> None:
    cases = load_cases() + load_cross_file()
    out = Path(__file__).parent / "cases.json"
    out.write_text(json.dumps([asdict(c) for c in cases], indent=2))

    by_cat: dict[str, int] = {}
    missing = 0
    for c in cases:
        by_cat[c.category] = by_cat.get(c.category, 0) + 1
        if not c.file_exists:
            missing += 1
    print(f"Loaded {len(cases)} non-image cases -> {out}")
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat:8s}: {n}")
    if missing:
        print(f"  [warn] {missing} cases have unresolved file paths")


if __name__ == "__main__":
    main()
