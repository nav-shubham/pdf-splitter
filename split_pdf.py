# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pymupdf>=1.23.0",
#     "rich>=13.0.0",
# ]
# ///

"""
PDF Splitter (by bookmarks or page ranges) + TXT Converter
A single-file Python CLI tool for splitting PDFs into chapters and converting them to text.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pymupdf as fitz  # PyMuPDF
from rich.console import Console
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


def pdf_to_txt(
    doc: fitz.Document,
    chapters: List[dict],
    out_dir: Path,
    source_name: str,
) -> List[dict]:
    """
    Convert each chapter into a UTF-8 encoded text file with headers and page separators.
    Checks for text layer and warns if almost no text is extracted (e.g. scanned PDF).
    Returns a list of summary dicts for final reporting.
    """
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

        # Build text content
        lines = [
            f"Chapter: {chap['title']}",
            f"Source: {source_name}",
            f"Pages: {pages_str} (Total: {page_count})",
            "=" * 80,
            "",
        ]

        total_chars = 0
        for p_num in range(chap["start"], chap["end"] + 1):
            page = doc[p_num - 1]  # 0-based index
            page_text = page.get_text("text", sort=True)
            chars_on_page = len(page_text.strip())
            total_chars += chars_on_page

            lines.append(f"--- Page {p_num} ---")
            lines.append(page_text)
            lines.append("")

        full_content = "\n".join(lines)
        txt_path.write_text(full_content, encoding="utf-8")

        # Check for empty text layer (scanned PDF)
        chapter_warnings: List[str] = []
        if chap.get("warning"):
            chapter_warnings.append(chap["warning"])

        if total_chars < 10:
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


def print_preview_table(chapters: List[dict]) -> None:
    """Print the preview table of chapters/parts before splitting."""
    table = Table(title="Split Plan Preview", title_style="bold cyan")
    table.add_column("No.", style="dim", width=6, justify="right")
    table.add_column("Title", style="bold", min_width=25)
    table.add_column("Pages", justify="center", width=14)
    table.add_column("Page count", justify="right", width=12)

    for idx, chap in enumerate(chapters, 1):
        pages_str = f"{chap['start']} - {chap['end']}" if chap["start"] != chap["end"] else f"{chap['start']}"
        count = chap["end"] - chap["start"] + 1
        table.add_row(str(idx), chap["title"], pages_str, str(count))

    console.print(table)


def print_report_table(report_rows: List[dict], out_dir: Path, txt_only: bool) -> None:
    """Print the final summary table and destination directory."""
    table = Table(title="Split & Extraction Summary", title_style="bold green")
    table.add_column("File", style="bold")
    table.add_column("Pages", justify="center", width=16)
    table.add_column("Characters", justify="right", width=12)
    table.add_column("Warnings", style="yellow")

    for row in report_rows:
        table.add_row(
            row["file"],
            row["pages"],
            f"{row['chars']:,}",
            row["warnings"],
        )

    console.print()
    console.print(table)
    console.print()
    console.print(f"[bold green]✓ Processing complete![/bold green]")
    console.print(f"Output folder: [bold cyan]{out_dir.resolve()}[/bold cyan]")
    if txt_only:
        console.print("[dim](Note: --txt-only active; PDF splits skipped.)[/dim]")


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

    args = parser.parse_args()

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
            # Try empty password first
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
            console.print(f"[bold red]Error:[/bold red] PDF has 0 pages.")
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

        # Determine splitting method
        # If user explicitly passed --ranges, use ranges even if bookmarks exist
        use_ranges = args.ranges is not None or len(toc) == 0

        if not use_ranges:
            # Step 2: Bookmarks exist and user did not force --ranges
            level_summaries = summarize_levels(toc)
            available_levels = [s["level"] for s in level_summaries]

            # Print summary table of bookmark levels
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

            # Determine level
            chosen_level: int
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

            # Check for front matter
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
            # Step 3: Split by page range
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
                        # CLI argument was invalid, do not loop infinitely
                        sys.exit(1)
                    range_input = None

        if not chapters:
            console.print("[bold red]Error:[/bold red] No chapters or ranges defined to split.")
            sys.exit(1)

        # Print any warnings from range building or shared pages
        for w in workflow_warnings:
            console.print(f"[bold yellow]Warning:[/bold yellow] {w}", soft_wrap=True)

        if workflow_warnings and not args.yes:
            if not Confirm.ask("Anomalies/warnings were detected. Proceed anyway?", default=True):
                console.print("[yellow]Operation aborted by user.[/yellow]")
                sys.exit(0)

        # Show preview table
        print_preview_table(chapters)

        # Confirmation prompt
        if not args.yes:
            if not Confirm.ask("[bold green]Proceed with split?[/bold green]", default=True):
                console.print("[yellow]Split cancelled by user.[/yellow]")
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

        # Split PDF
        split_pdf(doc, chapters, out_dir, txt_only=args.txt_only)

        # Convert to TXT
        report_rows = pdf_to_txt(doc, chapters, out_dir, source_name=pdf_path.name)

        # Step 6: Final report
        print_report_table(report_rows, out_dir, txt_only=args.txt_only)

    finally:
        doc.close()


if __name__ == "__main__":
    main()
