# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pymupdf>=1.23.0",
#     "rich>=13.0.0",
# ]
# ///

"""
PDF Splitter (by bookmarks or page ranges) + TXT Converter + Direct Tesseract OCR
A single-file Python CLI tool for splitting PDFs into chapters, converting them to text,
and performing OCR with explicit Tesseract CLI integration.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, List, Optional, Tuple
import unicodedata
import urllib.error
import urllib.request

import pymupdf as fitz  # PyMuPDF
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

# Initialize rich console with UTF-8 enforcement
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console(highlight=False)

# Windows reserved device names
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """
    Sanitize a chapter or part title to produce a valid, safe Windows filename.
    - Strips control characters (\x00-\x1f and \x7f)
    - Replaces illegal Windows characters (<>:"/\\|?*) with underscores
    - Strips leading and trailing dots, spaces, and hyphens
    - Avoids reserved Windows names (CON, NUL, COM1, etc.)
    - Truncates to max_len (default ~80 chars) to prevent path overflow
    """
    if not name:
        return "Untitled"

    # Remove control characters
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name)

    # Replace illegal characters with underscore
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", cleaned)

    # Collapse repeated whitespace and underscores
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)

    # Strip leading/trailing spaces, dots, underscores, hyphens
    cleaned = cleaned.strip(" ._-")

    if not cleaned:
        cleaned = "Untitled"

    # Avoid reserved names
    base_stem = cleaned.split(".")[0].upper()
    if base_stem in WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned}_"

    # Truncate to max_len
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" ._-")

    return cleaned or "Untitled"


def load_toc(doc: fitz.Document) -> Tuple[List[dict], int]:
    """
    Extract table of contents (bookmarks) from the PDF document.
    Returns:
        (valid_bookmarks, dropped_count)
    Each bookmark is a dict: {'level': int, 'title': str, 'page': int} (1-based page).
    Entries with page <= 0 or page > len(doc) or malformed values are dropped.
    """
    raw_toc = doc.get_toc(simple=True)  # returns [[level, title, page], ...]
    total_pages = len(doc)

    valid_toc: List[dict] = []
    dropped_count = 0

    for item in raw_toc:
        try:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                dropped_count += 1
                continue

            level = int(item[0])
            title = str(item[1]).strip() if item[1] is not None else "Untitled"
            page = int(item[2])

            if level < 1 or page < 1 or page > total_pages:
                dropped_count += 1
                continue

            valid_toc.append({
                "level": level,
                "title": title or "Untitled",
                "page": page,
            })
        except (ValueError, TypeError):
            dropped_count += 1
            continue

    return valid_toc, dropped_count


def summarize_levels(toc: List[dict]) -> List[dict]:
    """
    Summarize bookmark statistics grouped by level.
    Returns a list of dicts with keys:
        - level: int
        - count: int
        - samples: list of first <=3 titles
        - min_page: int
        - max_page: int
    """
    levels_map: dict[int, dict[str, Any]] = {}

    for bm in toc:
        lvl = bm["level"]
        if lvl not in levels_map:
            levels_map[lvl] = {
                "level": lvl,
                "count": 0,
                "samples": [],
                "min_page": bm["page"],
                "max_page": bm["page"],
            }
        info = levels_map[lvl]
        info["count"] += 1
        if len(info["samples"]) < 3:
            info["samples"].append(bm["title"])
        if bm["page"] < info["min_page"]:
            info["min_page"] = bm["page"]
        if bm["page"] > info["max_page"]:
            info["max_page"] = bm["page"]

    return [levels_map[lvl] for lvl in sorted(levels_map.keys())]


def build_ranges_from_toc(
    toc: List[dict],
    chosen_level: int,
    total_pages: int,
    include_front_matter: bool = True,
) -> Tuple[List[dict], List[str]]:
    """
    Build chapter page ranges from bookmarks using the chosen hierarchy level.
    Boundaries include all bookmarks with level <= chosen_level.
    Deeper bookmarks stay inside their parent chapter.

    Returns:
        (chapters, warnings)
    Each chapter dict: {'title': str, 'start': int, 'end': int, 'level': int, 'warning': Optional[str]}
    """
    # Filter boundaries: bookmarks with level <= chosen_level
    boundaries = [bm for bm in toc if bm["level"] <= chosen_level]
    if not boundaries:
        return [], ["No bookmarks found at or above the chosen level."]

    # Sort boundaries by page, keeping original order for ties (stable sort)
    boundaries.sort(key=lambda b: b["page"])

    chapters: List[dict] = []
    warnings: List[str] = []

    # Check for front matter if first boundary starts after page 1
    first_page = boundaries[0]["page"]
    if first_page > 1 and include_front_matter:
        chapters.append({
            "title": "Front Matter",
            "start": 1,
            "end": first_page - 1,
            "level": 0,
            "warning": None,
        })

    num_boundaries = len(boundaries)
    for i in range(num_boundaries):
        curr = boundaries[i]
        start_p = curr["page"]
        chapter_warning: Optional[str] = None

        if i + 1 < num_boundaries:
            next_start = boundaries[i + 1]["page"]
            if next_start <= start_p:
                # Two boundaries on the same page
                end_p = start_p
                warn_msg = (
                    f"Bookmarks '{curr['title']}' and '{boundaries[i + 1]['title']}' "
                    f"both start on page {start_p}. Chapter given page {start_p}."
                )
                warnings.append(warn_msg)
                chapter_warning = f"Shared page {start_p}"
            else:
                end_p = next_start - 1
        else:
            # Last boundary runs to the final page
            end_p = total_pages

        chapters.append({
            "title": curr["title"],
            "start": start_p,
            "end": end_p,
            "level": curr["level"],
            "warning": chapter_warning,
        })

    return chapters, warnings


def parse_page_ranges(range_str: str, total_pages: int) -> Tuple[List[dict], List[str]]:
    """
    Parse a page range specification string into chapter chunks.
    Accepts:
      - 'every N' (e.g. 'every 20')
      - Comma-separated ranges (e.g. '1-10, 11-25, 26-end' or single pages like '5')
    Validates:
      - start <= end
      - 1 <= start, end <= total_pages
    Detects overlaps and gaps.

    Returns:
      (chunks, warnings)
    """
    range_clean = range_str.strip()
    if not range_clean:
        raise ValueError("Range string is empty.")

    chunks: List[dict] = []
    warnings: List[str] = []

    # Case 1: 'every N'
    every_match = re.match(r"^every\s+(\d+)$", range_clean, re.IGNORECASE)
    if every_match:
        step = int(every_match.group(1))
        if step <= 0:
            raise ValueError(f"Step size in 'every {step}' must be greater than 0.")

        curr = 1
        part_idx = 1
        while curr <= total_pages:
            end = min(curr + step - 1, total_pages)
            title = f"Part {part_idx:02d} (p{curr}-{end})"
            chunks.append({
                "title": title,
                "start": curr,
                "end": end,
                "level": 1,
                "warning": None,
            })
            curr = end + 1
            part_idx += 1
        return chunks, []

    # Case 2: Comma-separated ranges
    parts = [p.strip() for p in range_clean.split(",") if p.strip()]
    if not parts:
        raise ValueError("No valid range segments found.")

    for idx, part in enumerate(parts, 1):
        if "-" in part:
            s_str, e_str = part.split("-", 1)
            s_str = s_str.strip()
            e_str = e_str.strip().lower()

            if not s_str.isdigit():
                raise ValueError(f"Invalid start page in '{part}': '{s_str}' is not a positive integer.")
            start = int(s_str)

            if e_str == "end":
                end = total_pages
            elif e_str.isdigit():
                end = int(e_str)
            else:
                raise ValueError(f"Invalid end page in '{part}': '{e_str}' must be a number or 'end'.")
        else:
            # Single page chunk
            if not part.isdigit():
                raise ValueError(f"Invalid page token: '{part}' is not an integer or range.")
            start = int(part)
            end = start

        if start < 1:
            raise ValueError(f"Start page {start} in '{part}' is out of bounds (minimum is 1).")
        if end > total_pages:
            raise ValueError(f"End page {end} in '{part}' exceeds total pages ({total_pages}).")
        if start > end:
            raise ValueError(f"Invalid range in '{part}': start page {start} is greater than end page {end}.")

        title = f"Part {idx:02d} (p{start}-{end})" if start != end else f"Part {idx:02d} (p{start})"
        chunks.append({
            "title": title,
            "start": start,
            "end": end,
            "level": 1,
            "warning": None,
        })

    # Check for gaps and overlaps
    # Sort chunks by start page to detect sequential anomalies
    sorted_chunks = sorted(chunks, key=lambda c: (c["start"], c["end"]))

    if sorted_chunks[0]["start"] > 1:
        warnings.append(f"Gap detected: Pages 1 to {sorted_chunks[0]['start'] - 1} are omitted before the first range.")

    for i in range(1, len(sorted_chunks)):
        prev = sorted_chunks[i - 1]
        curr = sorted_chunks[i]

        if curr["start"] <= prev["end"]:
            warn_msg = (
                f"Overlap detected: '{prev['title']}' (p.{prev['start']}-{prev['end']}) "
                f"overlaps with '{curr['title']}' (p.{curr['start']}-{curr['end']})."
            )
            warnings.append(warn_msg)
        elif curr["start"] > prev["end"] + 1:
            gap_start = prev["end"] + 1
            gap_end = curr["start"] - 1
            warn_msg = (
                f"Gap detected: Pages {gap_start} to {gap_end} are omitted "
                f"between '{prev['title']}' and '{curr['title']}'."
            )
            warnings.append(warn_msg)

    if sorted_chunks[-1]["end"] < total_pages:
        gap_start = sorted_chunks[-1]["end"] + 1
        warnings.append(f"Gap detected: Pages {gap_start} to {total_pages} are omitted after the last range.")

    return chunks, warnings


def split_pdf(
    doc: fitz.Document,
    chapters: List[dict],
    out_dir: Path,
    txt_only: bool = False,
) -> None:
    """
    Split the input PDF into individual chapter PDFs using PyMuPDF insert_pdf.
    If txt_only is True, skips writing split PDF files.
    """
    if txt_only:
        return

    pdf_dir = out_dir / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    pad = max(2, len(str(len(chapters))))
    for idx, chap in enumerate(chapters, 1):
        safe_title = sanitize_filename(chap["title"])
        file_stem = f"{idx:0{pad}d} - {safe_title}"
        pdf_path = pdf_dir / f"{file_stem}.pdf"

        # PyMuPDF insert_pdf uses 0-based page indexes:
        from_page = chap["start"] - 1
        to_page = chap["end"] - 1

        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=from_page, to_page=to_page)
        new_doc.save(str(pdf_path))
        new_doc.close()


# ==============================================================================
# OCR Subsystem: Direct Tesseract CLI Integration
# ==============================================================================


def find_tesseract(custom_path: Optional[str] = None) -> Optional[Path]:
    """
    Locate Tesseract executable in resolution order:
    1. --tesseract-path
    2. Env var TESSERACT_CMD
    3. shutil.which("tesseract")
    4. Common Windows locations:
       - C:\\Program Files\\Tesseract-OCR\\tesseract.exe
       - C:\\Program Files (x86)\\Tesseract-OCR\\tesseract.exe
       - %LOCALAPPDATA%\\Programs\\Tesseract-OCR\\tesseract.exe

    Verify it by running `tesseract --version`.
    If not found, returns None.
    """
    candidates: List[Path] = []
    if custom_path:
        candidates.append(Path(os.path.expandvars(custom_path)).expanduser())
    else:
        env_cmd = os.environ.get("TESSERACT_CMD")
        if env_cmd:
            candidates.append(Path(os.path.expandvars(env_cmd)).expanduser())

        which_path = shutil.which("tesseract") or shutil.which("tesseract.exe")
        if which_path:
            candidates.append(Path(which_path))

        if sys.platform == "win32":
            candidates.append(Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"))
            candidates.append(Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"))
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                candidates.append(Path(local_appdata) / "Programs" / "Tesseract-OCR" / "tesseract.exe")

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    for cand in candidates:
        if cand.is_file():
            try:
                proc = subprocess.run(
                    [str(cand), "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    creationflags=flags,
                )
                if proc.returncode == 0:
                    return cand.resolve()
            except Exception:
                pass

    return None


def get_tessdata_candidates(
    tesseract_path: Optional[Path] = None,
    custom_dir: Optional[str] = None,
) -> List[Path]:
    """
    Return candidate tessdata directories in resolution order:
    1. --tessdata-dir
    2. Env var TESSDATA_PREFIX. Accept whether pointing to tessdata or its parent:
       if <prefix>/tessdata exists, use that.
    3. <tesseract.exe folder>/tessdata
    4. User-writable fallback: %LOCALAPPDATA%\\split_pdf\\tessdata
    """
    candidates: List[Path] = []

    # 1. --tessdata-dir
    if custom_dir:
        candidates.append(Path(os.path.expandvars(custom_dir)).expanduser())

    # 2. Env var TESSDATA_PREFIX
    prefix = os.environ.get("TESSDATA_PREFIX")
    if prefix:
        p_prefix = Path(os.path.expandvars(prefix)).expanduser()
        if (p_prefix / "tessdata").is_dir():
            candidates.append(p_prefix / "tessdata")
        else:
            candidates.append(p_prefix)

    # 3. <tesseract.exe folder>/tessdata
    if tesseract_path:
        candidates.append(tesseract_path.parent / "tessdata")

    # 4. User-writable fallback: %LOCALAPPDATA%\split_pdf\tessdata
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "split_pdf" / "tessdata")
    else:
        candidates.append(Path.home() / "AppData" / "Local" / "split_pdf" / "tessdata")

    # Deduplicate while preserving resolution order
    seen = set()
    result: List[Path] = []
    for c in candidates:
        try:
            r = str(c.resolve())
        except Exception:
            r = str(c)
        if r not in seen:
            seen.add(r)
            result.append(c)

    return result


def find_tessdata(
    tesseract_path: Optional[Path],
    custom_dir: Optional[str],
    langs: List[str],
) -> Optional[Path]:
    """
    Resolve the tessdata folder.
    A folder counts as valid only if it contains <lang>.traineddata for every requested language.
    """
    candidates = get_tessdata_candidates(tesseract_path, custom_dir)
    for c in candidates:
        if c.is_dir() and all((c / f"{lang}.traineddata").is_file() for lang in langs):
            return c.resolve()
    return None


def get_user_tessdata_dir() -> Path:
    """Return the user-writable tessdata directory path."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "split_pdf" / "tessdata"
    return Path.home() / "AppData" / "Local" / "split_pdf" / "tessdata"


def download_traineddata(lang: str, dest_dir: Path) -> bool:
    """Download <lang>.traineddata from GitHub tessdata_fast into dest_dir."""
    url = f"https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target_file = dest_dir / f"{lang}.traineddata"
    temp_file = dest_dir / f"{lang}.traineddata.tmp"

    console.print(f"[cyan]Downloading '{lang}.traineddata' from {url}...[/cyan]")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) split_pdf/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(temp_file, "wb") as out_f:
            shutil.copyfileobj(resp, out_f)
        if temp_file.exists() and temp_file.stat().st_size > 0:
            temp_file.replace(target_file)
            console.print(f"[green]Successfully downloaded '{lang}.traineddata'.[/green]")
            return True
        return False
    except Exception as e:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass
        console.print(f"[red]Download failed for '{lang}': {e}[/red]")
        console.print(
            f"[yellow]Manual fix: Download https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata "
            f"and place it in '{dest_dir.resolve()}'.[/yellow]"
        )
        return False


def ensure_languages(
    tesseract_path: Optional[Path],
    custom_dir: Optional[str],
    ocr_lang: str,
    assume_yes: bool = False,
) -> Tuple[Optional[Path], List[str]]:
    """
    Check requested languages and offer auto-download for missing ones.
    Returns:
        (resolved_tessdata_dir, active_languages)
    """
    req_langs = [l.strip() for l in ocr_lang.split("+") if l.strip()]
    if not req_langs:
        req_langs = ["eng"]

    # If already fully satisfied in an existing candidate folder, return immediately
    resolved = find_tessdata(tesseract_path, custom_dir, req_langs)
    if resolved:
        return resolved, req_langs

    user_dir = get_user_tessdata_dir()
    candidates = get_tessdata_candidates(tesseract_path, custom_dir)

    active_langs: List[str] = []

    for lang in req_langs:
        found_in: Optional[Path] = None
        for c in candidates:
            if (c / f"{lang}.traineddata").is_file():
                found_in = c
                break

        if found_in is not None:
            active_langs.append(lang)
            # If found elsewhere and user_dir is going to be used, copy into user_dir
            if found_in != user_dir and not (user_dir / f"{lang}.traineddata").is_file():
                user_dir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(found_in / f"{lang}.traineddata", user_dir / f"{lang}.traineddata")
                except Exception:
                    pass
        else:
            # Language missing
            should_download = False
            if assume_yes:
                should_download = True
            else:
                should_download = Confirm.ask(
                    f"Language '{lang}' not found. Download it now?",
                    default=True,
                )

            if should_download:
                if download_traineddata(lang, user_dir):
                    active_langs.append(lang)
            else:
                console.print(
                    f"[yellow]Manual fix: Download https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata "
                    f"and place it in '{user_dir.resolve()}'.[/yellow]"
                )

    if not active_langs:
        return None, []

    # Ensure eng.traineddata and osd.traineddata are copied into user_dir so it works on its own
    for aux in ["eng", "osd"]:
        if not (user_dir / f"{aux}.traineddata").is_file():
            copied = False
            for c in candidates:
                if c != user_dir and (c / f"{aux}.traineddata").is_file():
                    user_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(c / f"{aux}.traineddata", user_dir / f"{aux}.traineddata")
                        copied = True
                        break
                    except Exception:
                        pass
            if not copied and aux in active_langs:
                download_traineddata(aux, user_dir)

    # Check if user_dir now satisfies all active_langs
    if all((user_dir / f"{l}.traineddata").is_file() for l in active_langs):
        return user_dir.resolve(), active_langs

    # Fallback search across candidates
    final_dir = find_tessdata(tesseract_path, custom_dir, active_langs)
    return final_dir, active_langs


def is_garbage_char(c: str) -> bool:
    """Check if a character is U+FFFD, a control character, or private-use character."""
    if c == "\ufffd":
        return True
    cat = unicodedata.category(c)
    if cat == "Co":  # Private use
        return True
    if cat.startswith("C") and c not in ("\r", "\n", "\t"):
        return True
    return False


def page_needs_ocr(page: fitz.Page, ocr_mode: str = "auto") -> Tuple[bool, str]:
    """
    Decide whether a page needs OCR (per-page evaluation).
    Returns:
        (needs_ocr, reason_code)
    Reason codes: 'disabled', 'forced', 'blank', 'scanned_or_sparse', 'garbage_text', 'text'
    """
    if ocr_mode == "off":
        return False, "disabled"

    text = page.get_text("text")
    non_ws = re.sub(r"\s+", "", text)
    num_non_ws = len(non_ws)

    images = page.get_images()
    drawings = page.get_drawings()
    has_images = len(images) > 0
    has_drawings = len(drawings) > 0

    # Truly blank pages (no text, no images, no drawings) get no OCR.
    if num_non_ws == 0 and not has_images and not has_drawings:
        return False, "blank"

    if ocr_mode == "force":
        return True, "forced"

    # auto mode:
    # 1. Fewer than about 25 non-whitespace characters and contains images (or has no text at all)
    if num_non_ws < 25 and (has_images or num_non_ws == 0):
        return True, "scanned_or_sparse"

    # 2. Text layer looks like garbage: more than 30% U+FFFD, control characters or private-use characters
    if num_non_ws > 0:
        bad_count = sum(1 for c in non_ws if is_garbage_char(c))
        if (bad_count / num_non_ws) > 0.30:
            return True, "garbage_text"

    return False, "text"


def render_page_for_ocr(page: fitz.Page, dpi: int = 300) -> bytes:
    """
    Render page to a grayscale pixmap at given dpi.
    If the longer side would exceed about 5000 px, lower the DPI to fit.
    """
    rect = page.rect
    max_side_pts = max(rect.width, rect.height)
    if max_side_pts * (dpi / 72.0) > 5000.0:
        actual_dpi = int((5000.0 * 72.0) / max_side_pts)
    else:
        actual_dpi = dpi

    zoom = actual_dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    return pix.tobytes("png")


def run_tesseract_cli(
    tesseract_path: Path,
    tessdata_dir: Path,
    lang: str,
    png_bytes: bytes,
    timeout: int = 120,
) -> Tuple[bool, str]:
    """
    Call Tesseract CLI directly via subprocess.
    Command: tesseract stdin stdout -l <lang> --tessdata-dir <dir> --psm 3
    120-second timeout per page, with one retry on failure.
    """
    cmd = [
        str(tesseract_path),
        "stdin",
        "stdout",
        "-l",
        lang,
        "--tessdata-dir",
        str(tessdata_dir),
        "--psm",
        "3",
    ]
    env = os.environ.copy()
    env["OMP_THREAD_LIMIT"] = "1"
    env["TESSDATA_PREFIX"] = str(tessdata_dir)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    # Try up to 2 times (initial + 1 retry)
    for _ in range(2):
        try:
            proc = subprocess.run(
                cmd,
                input=png_bytes,
                capture_output=True,
                timeout=timeout,
                env=env,
                creationflags=flags,
            )
            if proc.returncode == 0:
                text = proc.stdout.decode("utf-8", errors="replace")
                return True, text
        except (subprocess.TimeoutExpired, Exception):
            pass

    return False, ""


def run_self_test(
    tesseract_path: Path,
    tessdata_dir: Path,
    lang: str = "eng",
) -> Tuple[bool, str]:
    """
    Self-test: create an in-memory PDF page with text 'OCR TEST 12345',
    render it, run it through ocr_page, and confirm the output contains '12345'.
    """
    try:
        doc = fitz.open()
        page = doc.new_page(width=300, height=100)
        page.insert_text((30, 55), "OCR TEST 12345", fontsize=24)
        mat = fitz.Matrix(150 / 72.0, 150 / 72.0)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        png_bytes = pix.tobytes("png")
        doc.close()
    except Exception as e:
        return False, f"Failed to generate in-memory test page: {e}"

    success, text = run_tesseract_cli(
        tesseract_path=tesseract_path,
        tessdata_dir=tessdata_dir,
        lang=lang,
        png_bytes=png_bytes,
        timeout=30,
    )
    if not success:
        return False, "Tesseract invocation failed during self-test."
    if "12345" not in text:
        return False, f"Output did not contain expected text '12345'. Got: {text.strip()!r}"

    return True, "Self-test passed."


def batch_ocr_pages(
    doc: fitz.Document,
    pages_to_ocr: List[int],  # 1-based page numbers
    tesseract_path: Path,
    tessdata_dir: Path,
    ocr_lang: str,
    ocr_dpi: int = 300,
    ocr_cache: Optional[dict[int, Tuple[bool, str]]] = None,
) -> dict[int, Tuple[bool, str]]:
    """
    Run OCR in a ThreadPoolExecutor with workers = max(1, min(4, cpu_count - 1)).
    Renders pages sequentially in the main thread (thread-safe).
    Keeps at most 2 * workers images in flight.
    Caches OCR result per source page number.
    Shows a rich progress bar.
    """
    if ocr_cache is None:
        ocr_cache = {}

    to_process = [p for p in sorted(list(set(pages_to_ocr))) if p not in ocr_cache]
    if not to_process:
        return ocr_cache

    cpu_count = os.cpu_count() or 1
    workers = max(1, min(4, cpu_count - 1))
    max_in_flight = 2 * workers

    total = len(to_process)
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures: dict[Any, int] = {}
        page_iter = iter(to_process)

        def submit_next() -> bool:
            try:
                p_num = next(page_iter)
            except StopIteration:
                return False
            png_bytes = render_page_for_ocr(doc[p_num - 1], dpi=ocr_dpi)
            fut = executor.submit(
                run_tesseract_cli,
                tesseract_path,
                tessdata_dir,
                ocr_lang,
                png_bytes,
            )
            futures[fut] = p_num
            return True

        # Pre-fill up to max_in_flight
        for _ in range(max_in_flight):
            if not submit_next():
                break

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task(f"OCR page 0/{total}", total=total)

            while futures:
                done, _ = concurrent.futures.wait(
                    futures.keys(),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    p_num = futures.pop(fut)
                    completed += 1
                    try:
                        success, text = fut.result()
                    except Exception:
                        success, text = False, ""
                    ocr_cache[p_num] = (success, text)
                    progress.update(
                        task_id,
                        advance=1,
                        description=f"OCR page {completed}/{total}",
                    )
                    submit_next()

    return ocr_cache


def run_check_ocr(args: argparse.Namespace) -> bool:
    """
    Diagnostics routine for --check-ocr.
    Prints status and returns True if all pass, False otherwise.
    """
    console.print("\n[bold cyan]=== OCR Diagnostics & Configuration Check ===[/bold cyan]")
    all_passed = True

    # 1. Tesseract path & version
    tess_path = find_tesseract(args.tesseract_path)
    if not tess_path:
        console.print("[red]✗ Tesseract executable: NOT FOUND[/red]")
        console.print(
            "  [yellow]Install: winget install UB-Mannheim.TesseractOCR  (then reopen the terminal), "
            "or pass --tesseract-path.[/yellow]"
        )
        return False
    else:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            ver_proc = subprocess.run(
                [str(tess_path), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
            )
            ver_line = ver_proc.stdout.splitlines()[0] if ver_proc.stdout else "unknown version"
        except Exception as e:
            ver_line = f"error checking version: {e}"
        console.print(f"[green]✓ Tesseract executable:[/green] {tess_path}")
        console.print(f"  Version: {ver_line}")

    # 2. Resolved tessdata dir
    req_langs = [l.strip() for l in args.ocr_lang.split("+") if l.strip()]
    if not req_langs:
        req_langs = ["eng"]

    candidates = get_tessdata_candidates(tess_path, args.tessdata_dir)
    tessdata_dir: Optional[Path] = None
    for c in candidates:
        if c.is_dir():
            tessdata_dir = c
            break

    if not tessdata_dir:
        console.print("[red]✗ Tessdata directory: NOT FOUND[/red]")
        console.print(f"  [yellow]Checked locations: {[str(c) for c in candidates]}[/yellow]")
        return False
    else:
        console.print(f"[green]✓ Tessdata directory:[/green] {tessdata_dir}")

    # 3. Installed languages found there
    installed_files = sorted(list(tessdata_dir.glob("*.traineddata")))
    installed_langs = [f.stem for f in installed_files]
    console.print(f"  Installed languages: {', '.join(installed_langs) if installed_langs else 'None'}")

    # 4. Whether requested languages are present
    missing_langs = [l for l in req_langs if l not in installed_langs]
    if missing_langs:
        console.print(f"[red]✗ Requested language(s) missing: {', '.join(missing_langs)}[/red]")
        user_dir = get_user_tessdata_dir()
        for ml in missing_langs:
            console.print(
                f"  Fix: Download https://github.com/tesseract-ocr/tessdata_fast/raw/main/{ml}.traineddata "
                f"and place it in '{user_dir.resolve()}' or '{tessdata_dir.resolve()}'."
            )
        all_passed = False
    else:
        console.print(f"[green]✓ Requested language(s) present:[/green] {', '.join(req_langs)}")

    # 5. Self-test
    test_lang = "eng" if "eng" in installed_langs else (installed_langs[0] if installed_langs else req_langs[0])
    console.print(f"  Running self-test (rendering 'OCR TEST 12345' with lang='{test_lang}')...")
    test_ok, test_msg = run_self_test(tess_path, tessdata_dir, lang=test_lang)
    if test_ok:
        console.print(f"[green]✓ Self-test passed:[/green] Successfully recognized '12345'.")
    else:
        console.print(f"[red]✗ Self-test failed:[/red] {test_msg}")
        all_passed = False

    console.print("[bold cyan]=============================================[/bold cyan]")
    if all_passed:
        console.print("[bold green]All OCR checks passed successfully![/bold green]\n")
    else:
        console.print("[bold red]OCR configuration checks failed.[/bold red]\n")

    return all_passed


# ==============================================================================
# TXT Output Generation and Reporting
# ==============================================================================


def pdf_to_txt(
    doc: fitz.Document,
    chapters: List[dict],
    out_dir: Path,
    source_name: str,
    ocr_cache: Optional[dict[int, Tuple[bool, str]]] = None,
    ocr_unavailable_pages: Optional[List[int]] = None,
    ocr_unavailable_reason: Optional[str] = None,
) -> List[dict]:
    """
    Convert each chapter into a UTF-8 encoded text file with headers and page separators.
    For OCR'd pages, uses '--- Page N (OCR) ---'.
    Returns a list of summary dicts for final reporting.
    """
    if ocr_cache is None:
        ocr_cache = {}
    if ocr_unavailable_pages is None:
        ocr_unavailable_pages = []

    txt_dir = out_dir / "txt"
    txt_dir.mkdir(parents=True, exist_ok=True)

    pad = max(2, len(str(len(chapters))))
    report_rows: List[dict] = []

    for idx, chap in enumerate(chapters, 1):
        safe_title = sanitize_filename(chap["title"])
        file_stem = f"{idx:0{pad}d} - {safe_title}"
        txt_path = txt_dir / f"{file_stem}.txt"

        page_count = chap["end"] - chap["start"] + 1
        pages_str = f"{chap['start']}-{chap['end']}" if chap["start"] != chap["end"] else f"{chap['start']}"

        lines = [
            f"Chapter: {chap['title']}",
            f"Source: {source_name}",
            f"Pages: {pages_str} (Total: {page_count})",
            "=" * 80,
            "",
        ]

        total_chars = 0
        chapter_ocr_pages: List[int] = []
        chapter_ocr_failed: List[int] = []
        chapter_ocr_unavail: List[int] = []

        for p_num in range(chap["start"], chap["end"] + 1):
            page = doc[p_num - 1]

            if p_num in ocr_cache:
                success, ocr_text = ocr_cache[p_num]
                if success:
                    chapter_ocr_pages.append(p_num)
                    page_text = ocr_text
                    lines.append(f"--- Page {p_num} (OCR) ---")
                else:
                    chapter_ocr_failed.append(p_num)
                    page_text = page.get_text("text", sort=True)
                    lines.append(f"--- Page {p_num} ---")
            else:
                page_text = page.get_text("text", sort=True)
                lines.append(f"--- Page {p_num} ---")
                if p_num in ocr_unavailable_pages:
                    chapter_ocr_unavail.append(p_num)

            chars_on_page = len(page_text.strip())
            total_chars += chars_on_page
            lines.append(page_text)
            lines.append("")

        full_content = "\n".join(lines)
        txt_path.write_text(full_content, encoding="utf-8")

        chapter_warnings: List[str] = []
        if chap.get("warning"):
            chapter_warnings.append(chap["warning"])

        if chapter_ocr_failed:
            pages_s = ", ".join(str(p) for p in chapter_ocr_failed)
            chapter_warnings.append(f"OCR failed on pages: {pages_s}")

        if chapter_ocr_unavail:
            pages_s = ", ".join(str(p) for p in chapter_ocr_unavail)
            reason = ocr_unavailable_reason or "Tesseract not found"
            chapter_warnings.append(f"OCR unavailable: {reason} (pages: {pages_s})")
        elif not chapter_ocr_pages and total_chars < 10:
            console.print(
                f"[yellow]Warning: Chapter '{file_stem}' has almost no extractable text "
                f"(no text layer, OCR needed).[/yellow]",
                soft_wrap=True,
            )
            chapter_warnings.append("no text layer, OCR needed")

        report_rows.append({
            "file": f"{file_stem}.txt",
            "pages": f"{pages_str} ({page_count})",
            "chars": total_chars,
            "ocr_pages": len(chapter_ocr_pages),
            "ocr_failed": len(chapter_ocr_failed),
            "warnings": "; ".join(chapter_warnings) if chapter_warnings else "-",
        })

    return report_rows


def resolve_output_dir(base_dir: Path, assume_yes: bool = False) -> Optional[Path]:
    """
    Ensure the output directory is properly handled if it already exists.
    Asks user to overwrite, create _2, or abort (unless assume_yes is True).
    """
    if not base_dir.exists():
        return base_dir

    if assume_yes:
        return base_dir

    console.print(f"[yellow]Output folder '{base_dir}' already exists.[/yellow]")
    choice = Prompt.ask(
        "Choose an action: [bold][O][/bold]verwrite / create [bold][N][/bold]ew folder (_2) / [bold][A][/bold]bort",
        choices=["o", "O", "n", "N", "a", "A"],
        default="O",
    ).upper()

    if choice == "O":
        return base_dir
    elif choice == "N":
        counter = 2
        while True:
            candidate = Path(f"{base_dir}_{counter}")
            if not candidate.exists():
                return candidate
            counter += 1
    else:
        return None


def print_preview_table(
    chapters: List[dict],
    total_ocr_pages: int,
    ocr_dpi: int,
    ocr_mode: str,
) -> None:
    """Print the preview table of chapters/parts before splitting, including OCR columns."""
    table = Table(title="Split Plan Preview", title_style="bold cyan")
    table.add_column("No.", style="dim", width=6, justify="right")
    table.add_column("Title", style="bold", min_width=25)
    table.add_column("Pages", justify="center", width=14)
    table.add_column("Page count", justify="right", width=12)
    table.add_column("OCR pages", justify="right", width=11)

    for idx, chap in enumerate(chapters, 1):
        pages_str = f"{chap['start']} - {chap['end']}" if chap["start"] != chap["end"] else f"{chap['start']}"
        count = chap["end"] - chap["start"] + 1
        ocr_cnt = chap.get("ocr_pages_count", 0)
        table.add_row(str(idx), chap["title"], pages_str, str(count), str(ocr_cnt))

    console.print(table)

    if ocr_mode != "off" and total_ocr_pages > 0:
        workers = max(1, min(4, (os.cpu_count() or 1) - 1))
        est_sec = total_ocr_pages * (2.5 / workers)
        min_min = max(1, int(est_sec / 60))
        max_min = max(min_min + 1, int((est_sec * 1.5) / 60) + 1)
        console.print(
            f"[bold cyan]OCR needed on {total_ocr_pages} pages "
            f"(about {min_min}-{max_min} min at {ocr_dpi} dpi)[/bold cyan]"
        )
    elif ocr_mode == "off":
        console.print("[dim]OCR is off.[/dim]")
    else:
        console.print("[dim]OCR needed on 0 pages.[/dim]")


def print_report_table(report_rows: List[dict], out_dir: Path, txt_only: bool) -> None:
    """Print the final summary table and destination directory."""
    table = Table(title="Split & Extraction Summary", title_style="bold green")
    table.add_column("File", style="bold", min_width=20)
    table.add_column("Pages", justify="center", width=12)
    table.add_column("Characters", justify="right", width=12)
    table.add_column("OCR pages", justify="right", width=10)
    table.add_column("OCR failed", justify="right", width=10)
    table.add_column("Warnings", style="yellow", overflow="fold")

    for row in report_rows:
        table.add_row(
            row["file"],
            row["pages"],
            f"{row['chars']:,}",
            str(row["ocr_pages"]),
            str(row["ocr_failed"]),
            row["warnings"],
        )

    console.print()
    console.print(table)
    console.print()

    all_warnings: List[str] = []
    for r in report_rows:
        w = r.get("warnings", "")
        if w and w != "-":
            for part in w.split("; "):
                if part not in all_warnings:
                    all_warnings.append(part)

    if all_warnings:
        console.print("[bold yellow]Warnings summary:[/bold yellow]")
        for w in all_warnings:
            console.print(f"  • {w}")
        console.print()

    console.print("[bold green]✓ Processing complete![/bold green]")
    console.print(f"Output folder: [bold cyan]{out_dir.resolve()}[/bold cyan]")
    if txt_only:
        console.print("[dim](Note: --txt-only active; PDF splits skipped.)[/dim]")


# ==============================================================================
# Main Entry Point
# ==============================================================================


def main() -> None:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Split a PDF into chapters/parts and convert each to a UTF-8 text file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input_pdf",
        nargs="?",
        default=None,
        help="Path to the input PDF file (prompted if missing).",
    )
    parser.add_argument(
        "--level",
        type=int,
        default=None,
        help="Bookmark hierarchy level to split by (e.g. 1 or 2).",
    )
    parser.add_argument(
        "--ranges",
        type=str,
        default=None,
        help="Page ranges (e.g. '1-10,11-25,26-end' or 'every 20').",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Custom output directory (defaults to <pdfname>_split).",
    )
    parser.add_argument(
        "--txt-only",
        action="store_true",
        help="Skip creating split PDF files and write only .txt files.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompts and accept defaults.",
    )
    # OCR Options
    parser.add_argument(
        "--ocr",
        choices=["auto", "off", "force"],
        default="auto",
        help="OCR mode: auto (default), off, or force.",
    )
    parser.add_argument(
        "--ocr-lang",
        type=str,
        default="eng",
        help="OCR language(s), e.g. 'eng' or 'eng+hin' (default: eng).",
    )
    parser.add_argument(
        "--ocr-dpi",
        type=int,
        default=300,
        help="DPI for page rendering during OCR (default: 300).",
    )
    parser.add_argument(
        "--tesseract-path",
        type=str,
        default=None,
        help="Explicit path to tesseract executable.",
    )
    parser.add_argument(
        "--tessdata-dir",
        type=str,
        default=None,
        help="Explicit path to tessdata directory.",
    )
    parser.add_argument(
        "--check-ocr",
        action="store_true",
        help="Run OCR diagnostics and self-test, print result, and exit.",
    )

    args = parser.parse_args()

    # If --check-ocr is invoked, run diagnostics and exit immediately
    if args.check_ocr:
        success = run_check_ocr(args)
        sys.exit(0 if success else 1)

    # Step 1: Resolve PDF path
    pdf_path_str = args.input_pdf
    if not pdf_path_str:
        pdf_path_str = Prompt.ask("[bold cyan]Enter path to PDF file[/bold cyan]").strip().strip('"').strip("'")

    pdf_path = Path(pdf_path_str)
    if not pdf_path.is_file():
        console.print(f"[bold red]Error:[/bold red] File not found: '{pdf_path}'")
        sys.exit(1)

    # Open PDF
    try:
        doc = fitz.open(str(pdf_path))
    except fitz.FileDataError as err:
        console.print(f"[bold red]Error:[/bold red] Not a valid PDF file '{pdf_path}': {err}")
        sys.exit(1)
    except Exception as err:
        console.print(f"[bold red]Error opening PDF:[/bold red] {err}")
        sys.exit(1)

    try:
        # Encryption check
        if doc.is_encrypted:
            console.print("[yellow]PDF is encrypted.[/yellow]")
            authenticated = False
            if doc.authenticate(""):
                authenticated = True
            else:
                while not authenticated:
                    password = Prompt.ask("[bold cyan]Enter PDF password[/bold cyan]", password=True)
                    if not password:
                        console.print("[bold red]Error:[/bold red] Password is required to open this encrypted PDF.")
                        sys.exit(1)
                    if doc.authenticate(password):
                        authenticated = True
                        console.print("[green]PDF successfully unlocked.[/green]")
                        break
                    else:
                        console.print("[red]Incorrect password. Please try again.[/red]")
                        if args.yes:
                            console.print("[bold red]Error:[/bold red] Authentication failed in non-interactive mode.")
                            sys.exit(1)

        total_pages = len(doc)
        if total_pages == 0:
            console.print("[bold red]Error:[/bold red] PDF has 0 pages.")
            sys.exit(1)

        console.print(f"[cyan]Loaded PDF:[/cyan] [bold]{pdf_path.name}[/bold] ({total_pages} pages)")

        # Load bookmarks
        toc, dropped_count = load_toc(doc)
        if dropped_count > 0:
            console.print(
                f"[yellow]Dropped {dropped_count} invalid bookmark(s) "
                f"(page <= 0 or beyond page count {total_pages}).[/yellow]"
            )

        chapters: List[dict] = []
        workflow_warnings: List[str] = []

        use_ranges = args.ranges is not None or len(toc) == 0

        if not use_ranges:
            level_summaries = summarize_levels(toc)
            available_levels = [s["level"] for s in level_summaries]

            lvl_table = Table(title="Bookmark Levels Found", title_style="bold cyan")
            lvl_table.add_column("Level", justify="center", width=8)
            lvl_table.add_column("No. of bookmarks", justify="right", width=18)
            lvl_table.add_column("Sample titles (first 3)", min_width=30)
            lvl_table.add_column("Page span", justify="center", width=16)

            for s in level_summaries:
                samples_text = ", ".join(f"'{title}'" for title in s["samples"])
                span_text = f"p. {s['min_page']} - {s['max_page']}" if s["min_page"] != s["max_page"] else f"p. {s['min_page']}"
                lvl_table.add_row(str(s["level"]), str(s["count"]), samples_text, span_text)

            console.print(lvl_table)

            if args.level is not None:
                if args.level not in available_levels:
                    console.print(
                        f"[bold red]Error:[/bold red] Chosen level {args.level} does not exist in bookmarks. "
                        f"Available levels: {available_levels}"
                    )
                    sys.exit(1)
                chosen_level = args.level
            else:
                level_choices = [str(l) for l in available_levels]
                chosen_str = Prompt.ask(
                    "Split by which level?",
                    choices=level_choices,
                    default=str(available_levels[0]),
                )
                chosen_level = int(chosen_str)

            boundaries = [bm for bm in toc if bm["level"] <= chosen_level]
            boundaries.sort(key=lambda b: b["page"])
            include_front_matter = True

            if boundaries and boundaries[0]["page"] > 1:
                first_page = boundaries[0]["page"]
                if args.yes:
                    include_front_matter = True
                else:
                    include_front_matter = Confirm.ask(
                        f"First bookmark starts on page {first_page}. "
                        f"Include Front matter (pages 1-{first_page - 1})?",
                        default=True,
                    )

            chapters, workflow_warnings = build_ranges_from_toc(
                toc=toc,
                chosen_level=chosen_level,
                total_pages=total_pages,
                include_front_matter=include_front_matter,
            )

        else:
            if len(toc) == 0 and args.ranges is None:
                console.print("[yellow]No bookmarks found.[/yellow]")

            range_input = args.ranges
            while True:
                if not range_input:
                    range_input = Prompt.ask(
                        "[bold cyan]Enter page ranges (e.g. '1-10, 11-25, 26-end' or 'every 20')[/bold cyan]"
                    ).strip()

                try:
                    chapters, workflow_warnings = parse_page_ranges(range_input, total_pages)
                    break
                except ValueError as err:
                    console.print(f"[bold red]Range syntax error:[/bold red] {err}")
                    if args.ranges is not None:
                        sys.exit(1)
                    range_input = None

        if not chapters:
            console.print("[bold red]Error:[/bold red] No chapters or ranges defined to split.")
            sys.exit(1)

        for w in workflow_warnings:
            console.print(f"[bold yellow]Warning:[/bold yellow] {w}", soft_wrap=True)

        if workflow_warnings and not args.yes:
            if not Confirm.ask("Anomalies/warnings were detected. Proceed anyway?", default=True):
                console.print("[yellow]Operation aborted by user.[/yellow]")
                sys.exit(0)

        # Decide which pages need OCR
        page_ocr_status: dict[int, Tuple[bool, str]] = {}
        for p in range(1, total_pages + 1):
            page_ocr_status[p] = page_needs_ocr(doc[p - 1], ocr_mode=args.ocr)

        # Compute OCR count per chapter and overall unique OCR pages
        for chap in chapters:
            c_cnt = sum(1 for p in range(chap["start"], chap["end"] + 1) if page_ocr_status[p][0])
            chap["ocr_pages_count"] = c_cnt

        all_needed_pages = sorted(
            list(set(p for chap in chapters for p in range(chap["start"], chap["end"] + 1) if page_ocr_status[p][0]))
        )
        total_ocr_needed = len(all_needed_pages)

        # Preview table
        print_preview_table(chapters, total_ocr_needed, args.ocr_dpi, args.ocr)

        # Confirmation step
        if not args.yes:
            if total_ocr_needed > 0 and args.ocr != "off":
                choice = Prompt.ask(
                    "[bold green]Proceed with split?[/bold green] ([bold]Y[/bold]es / [bold]n[/bold]o / [bold]off[/bold] to disable OCR)",
                    default="Y",
                ).strip().lower()

                if choice in ("n", "no"):
                    console.print("[yellow]Split cancelled by user.[/yellow]")
                    sys.exit(0)
                elif choice == "off":
                    args.ocr = "off"
                    all_needed_pages = []
                    total_ocr_needed = 0
                    console.print("[yellow]Switched to --ocr off. OCR will not be performed.[/yellow]")
            else:
                if not Confirm.ask("[bold green]Proceed with split?[/bold green]", default=True):
                    console.print("[yellow]Split cancelled by user.[/yellow]")
                    sys.exit(0)

        # Perform OCR if needed
        ocr_cache: dict[int, Tuple[bool, str]] = {}
        ocr_unavailable_pages: List[int] = []
        ocr_unavailable_reason: Optional[str] = None

        if total_ocr_needed > 0 and args.ocr != "off":
            tess_path = find_tesseract(args.tesseract_path)
            if not tess_path:
                console.print(
                    "[bold yellow]Tesseract not found. Install: winget install UB-Mannheim.TesseractOCR  "
                    "(then reopen the terminal), or pass --tesseract-path.[/bold yellow]"
                )
                ocr_unavailable_pages = list(all_needed_pages)
                ocr_unavailable_reason = "Tesseract not found"
            else:
                tessdata_dir, active_langs = ensure_languages(
                    tess_path,
                    args.tessdata_dir,
                    args.ocr_lang,
                    assume_yes=args.yes,
                )
                if not tessdata_dir or not active_langs:
                    console.print("[bold yellow]OCR unavailable: Tessdata folder or requested languages missing.[/bold yellow]")
                    ocr_unavailable_pages = list(all_needed_pages)
                    ocr_unavailable_reason = "Tessdata folder or language missing"
                else:
                    # Run self-test automatically once before real OCR job
                    test_ok, test_msg = run_self_test(tess_path, tessdata_dir, lang=active_langs[0])
                    if not test_ok:
                        console.print(f"[bold red]OCR self-test failed:[/bold red] {test_msg}")
                        console.print("[yellow]OCR cannot proceed reliably on this setup.[/yellow]")
                        ocr_unavailable_pages = list(all_needed_pages)
                        ocr_unavailable_reason = "OCR self-test failed"
                    else:
                        # Self-test passed, run OCR
                        ocr_lang_str = "+".join(active_langs)
                        batch_ocr_pages(
                            doc=doc,
                            pages_to_ocr=all_needed_pages,
                            tesseract_path=tess_path,
                            tessdata_dir=tessdata_dir,
                            ocr_lang=ocr_lang_str,
                            ocr_dpi=args.ocr_dpi,
                            ocr_cache=ocr_cache,
                        )

            if ocr_unavailable_pages:
                if not args.yes:
                    if not Confirm.ask("Continue without OCR?", default=False):
                        console.print("[yellow]Operation aborted by user.[/yellow]")
                        sys.exit(0)

        # Step 4: Resolve output directory
        if args.out:
            base_out_dir = Path(args.out)
        else:
            base_out_dir = Path(f"{pdf_path.stem}_split")

        out_dir = resolve_output_dir(base_out_dir, assume_yes=args.yes)
        if out_dir is None:
            console.print("[yellow]Operation aborted by user.[/yellow]")
            sys.exit(0)

        console.print(f"[cyan]Writing output to:[/cyan] {out_dir}")

        # Split PDF (never modified by OCR)
        split_pdf(doc, chapters, out_dir, txt_only=args.txt_only)

        # Convert to TXT
        report_rows = pdf_to_txt(
            doc=doc,
            chapters=chapters,
            out_dir=out_dir,
            source_name=pdf_path.name,
            ocr_cache=ocr_cache,
            ocr_unavailable_pages=ocr_unavailable_pages,
            ocr_unavailable_reason=ocr_unavailable_reason,
        )

        # Step 6: Final report
        print_report_table(report_rows, out_dir, txt_only=args.txt_only)

    finally:
        doc.close()


if __name__ == "__main__":
    main()
