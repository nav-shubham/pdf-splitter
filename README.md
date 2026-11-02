# PDF Splitter & TXT Converter

A high-performance, single-file Python CLI tool to split PDF documents into logical chapters (via bookmarks/TOC or custom page ranges) and convert each chapter into clean, UTF-8 text files.

Powered by **PyMuPDF** (`pymupdf`) and styled with **Rich**. Runs anywhere with `uv` with zero manual dependency installation (PEP 723 inline metadata).

---

## Features

- **PEP 723 Single-File Execution**: Run directly via `uv run split_pdf.py <input.pdf>` without manual virtualenv setup.
- **Bookmark / TOC Hierarchy Splitting**:
  - Automatically inspects and tabulates bookmarks by level (**Level | Count | Sample titles | Page span**).
  - Keeps deeper bookmarks organized within parent chapters.
  - Automatically offers an optional **Front Matter** chunk for pages preceding the first bookmark.
  - Handles bookmarks sharing the same page gracefully with automatic warnings and guarantees each chapter receives at least that page.
- **Custom Page Range Splitting**:
  - Supports comma-separated ranges: `1-10, 11-25, 26-end`.
  - Supports fixed-size chunking: `every 20`, `every 10`.
  - Automatically detects and warns on page gaps or overlaps.
- **High-Fidelity Text Extraction**:
  - Extracts text page-by-page with layout-aware sorting (`get_text("text", sort=True)`).
  - Produces structured headers with chapter title, source file, and page range.
  - Uses `--- Page N ---` page delimiters with the original 1-based page numbers.
  - Detects scanned/empty text layers and alerts the user when OCR is required.
- **Robust Windows Filename Sanitization**:
  - Strips illegal Windows characters (`<>:"/\|?*`) and control characters.
  - Safely handles reserved Windows device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`).
  - Truncates long titles to 80 characters to prevent path overflow issues.
- **Encrypted PDF Handling**:
  - Automatically prompts for PDF passwords with interactive authentication.
- **Collision-Safe Output Handling**:
  - Prompts to overwrite, create incremented folders (`_2`, `_3`), or abort if output folder already exists.

---

## Installation & Quick Start

### Using `uv` (Recommended)

No manual install needed. Just run:

```bash
uv run split_pdf.py document.pdf
```

### Traditional Virtualenv / Pip

```bash
git clone https://github.com/nav-shubham/pdf-splitter.git
cd pdf-splitter
uv venv
uv pip install -e .
python split_pdf.py document.pdf
```

---

## Command Line Interface (CLI)

```bash
uv run split_pdf.py [input.pdf] [OPTIONS]
```

| Argument | Description |
|---|---|
| `input_pdf` | Path to the PDF file (prompted interactively if omitted). |
| `--level N` | Split by bookmark hierarchy level (e.g., `1` for top-level chapters, `2` for sections). |
| `--ranges "RANGES"` | Split by page range (e.g., `"1-10,11-25,26-end"` or `"every 20"`). |
| `--out DIR` | Custom destination directory (defaults to `<pdfname>_split`). |
| `--txt-only` | Skips writing split PDF files and only outputs `.txt` files. |
| `-y`, `--yes` | Non-interactive mode: skips confirmation prompts and accepts defaults. |

---

## Examples

### 1. Split a book by Level 1 chapters
```bash
uv run split_pdf.py textbook.pdf --level 1 --yes
```

### 2. Split by Level 2 sections and output only text files
```bash
uv run split_pdf.py manual.pdf --level 2 --txt-only --out ./output_text
```

### 3. Split an unbookmarked PDF into custom page ranges
```bash
uv run split_pdf.py report.pdf --ranges "1-5, 6-20, 21-end" --yes
```

### 4. Split a PDF into equal 25-page chunks
```bash
uv run split_pdf.py large_doc.pdf --ranges "every 25" --yes
```

---

## Output Layout

```
<pdfname>_split/
├── pdf/
│   ├── 01 - Chapter 1 Foundations.pdf
│   ├── 02 - Chapter 2 Methods.pdf
│   └── 03 - Chapter 3 Results.pdf
└── txt/
    ├── 01 - Chapter 1 Foundations.txt
    ├── 02 - Chapter 2 Methods.txt
    └── 03 - Chapter 3 Results.txt
```

Each `.txt` file contains:

```text
Chapter: Chapter 1 Foundations
Source: textbook.pdf
Pages: 1-8 (Total: 8)
================================================================================

--- Page 1 ---
[Extracted text from page 1...]

--- Page 2 ---
[Extracted text from page 2...]
```

---

## Running Automated Tests

A comprehensive test suite is included covering nested bookmarks, custom ranges, dropped invalid pages, shared page ties, filename sanitization, scanned PDF warnings, and `--txt-only` mode:

```bash
uv run tests/run_tests.py
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
