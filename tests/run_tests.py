# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pymupdf>=1.23.0",
#     "rich>=13.0.0",
# ]
# ///

"""
Automated test suite verifying all 5 test cases for split_pdf.py.
"""

import os
import subprocess
import sys
from pathlib import Path
import pymupdf as fitz

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

project_root = Path(__file__).parent.parent
test_data_dir = Path(__file__).parent / "test_data"
output_dir = Path(__file__).parent / "test_output"

passed_tests = 0
failed_tests = 0


def log_test(name: str, passed: bool, details: str = ""):
    global passed_tests, failed_tests
    if passed:
        passed_tests += 1
        print(f"  [PASS] {name} {details}")
    else:
        failed_tests += 1
        print(f"  [FAIL] {name} - {details}")


def run_cmd(args_list: list[str]) -> subprocess.CompletedProcess:
    script_path = str(project_root / "split_pdf.py")
    cmd = ["uv", "run", script_path] + args_list
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return res


def test_1_nested_bookmarks():
    print("\n--- Test 1: Nested Bookmarks (Split at Level 1 and Level 2) ---")
    pdf = str(test_data_dir / "nested_bookmarks.pdf")

    # 1A: Split at Level 1
    out_l1 = output_dir / "nested_l1"
    res1 = run_cmd([pdf, "--level", "1", "--out", str(out_l1), "--yes"])
    log_test("Level 1 Exit Code", res1.returncode == 0, f"Exit code: {res1.returncode}")

    pdf_files_l1 = sorted(list((out_l1 / "pdf").glob("*.pdf")))
    txt_files_l1 = sorted(list((out_l1 / "txt").glob("*.txt")))
    log_test("Level 1 File Count", len(pdf_files_l1) == 3 and len(txt_files_l1) == 3, f"Found {len(pdf_files_l1)} PDFs, {len(txt_files_l1)} TXTs")

    # Verify chapters coverage: Ch 1 (p1-8), Ch 2 (p9-12), Ch 3 (p13-15)
    doc_ch1 = fitz.open(str(pdf_files_l1[0]))
    doc_ch2 = fitz.open(str(pdf_files_l1[1]))
    doc_ch3 = fitz.open(str(pdf_files_l1[2]))
    log_test("Level 1 Page Counts", (len(doc_ch1) == 8 and len(doc_ch2) == 4 and len(doc_ch3) == 3),
             f"Pages: {len(doc_ch1)}, {len(doc_ch2)}, {len(doc_ch3)}")
    doc_ch1.close()
    doc_ch2.close()
    doc_ch3.close()

    # 1B: Split at Level 2
    out_l2 = output_dir / "nested_l2"
    res2 = run_cmd([pdf, "--level", "2", "--out", str(out_l2), "--yes"])
    log_test("Level 2 Exit Code", res2.returncode == 0, f"Exit code: {res2.returncode}")

    pdf_files_l2 = sorted(list((out_l2 / "pdf").glob("*.pdf")))
    txt_files_l2 = sorted(list((out_l2 / "txt").glob("*.txt")))
    # Level 2 boundaries:
    # 1. Ch1: Foundations (lvl 1, p1-2)
    # 2. Sec 1.1: Background (lvl 2, p3-5) [includes lvl 3 deep dive on p4]
    # 3. Sec 1.2: Architecture (lvl 2, p6-8)
    # 4. Ch2: Methodology (lvl 1, p9-10)
    # 5. Sec 2.1: Implementation (lvl 2, p11-12)
    # 6. Ch3: Results (lvl 1, p13-13)
    # 7. Sec 3.1: Discussion (lvl 2, p14-15)
    log_test("Level 2 File Count", len(pdf_files_l2) == 7 and len(txt_files_l2) == 7, f"Found {len(pdf_files_l2)} chapters (expected 7)")

    # Verify that deeper bookmark (lvl 3 on p4) stayed inside Section 1.1 (p3-5)
    sec_1_1 = fitz.open(str(pdf_files_l2[1]))
    log_test("Level 3 stays inside Level 2 parent", len(sec_1_1) == 3, f"Section 1.1 page count: {len(sec_1_1)} (p3-p5)")
    sec_1_1.close()


def test_2_no_bookmarks():
    print("\n--- Test 2: No Bookmarks (Ranges '1-5,6-end' and 'every 10') ---")
    pdf = str(test_data_dir / "no_bookmarks.pdf")

    # 2A: 1-5,6-end
    out_ranges = output_dir / "no_bm_ranges"
    res1 = run_cmd([pdf, "--ranges", "1-5,6-end", "--out", str(out_ranges), "--yes"])
    log_test("Ranges '1-5,6-end' Exit Code", res1.returncode == 0)

    pdf_files = sorted(list((out_ranges / "pdf").glob("*.pdf")))
    log_test("Ranges '1-5,6-end' Count", len(pdf_files) == 2)
    d1 = fitz.open(str(pdf_files[0]))
    d2 = fitz.open(str(pdf_files[1]))
    log_test("Ranges '1-5,6-end' Page Counts", len(d1) == 5 and len(d2) == 20, f"Part 1: {len(d1)}p, Part 2: {len(d2)}p")
    d1.close()
    d2.close()

    # 2B: every 10
    out_every = output_dir / "no_bm_every"
    res2 = run_cmd([pdf, "--ranges", "every 10", "--out", str(out_every), "--yes"])
    log_test("Ranges 'every 10' Exit Code", res2.returncode == 0)

    pdf_files_ev = sorted(list((out_every / "pdf").glob("*.pdf")))
    log_test("Ranges 'every 10' Count", len(pdf_files_ev) == 3, f"Found {len(pdf_files_ev)} parts (expected 3 for 25 pages)")
    counts = [len(fitz.open(str(p))) for p in pdf_files_ev]
    log_test("Ranges 'every 10' Chunk Sizes", counts == [10, 10, 5], f"Chunk sizes: {counts}")


def test_3_invalid_and_shared_bookmarks():
    print("\n--- Test 3: Invalid Pages and Shared Page Bookmarks ---")
    pdf = str(test_data_dir / "invalid_and_shared.pdf")
    out_dir = output_dir / "invalid_shared"

    res = run_cmd([pdf, "--level", "1", "--out", str(out_dir), "--yes"])
    log_test("Invalid & Shared Exit Code", res.returncode == 0)

    # Check that invalid bookmarks were reported as dropped
    log_test("Dropped Invalid Bookmarks Reported", "Dropped 2 invalid bookmark(s)" in res.stdout)

    # Check shared bookmark warning was printed
    log_test("Shared Bookmark Warning Reported", "both start on page 5" in res.stdout or "shared page" in res.stdout.lower())

    pdf_files = sorted(list((out_dir / "pdf").glob("*.pdf")))
    # Expected chapters:
    # 1. Ch1: Start (p 1-4)
    # 2. Shared Chapter 2A (p 5-5) -> at least that one page!
    # 3. Shared Chapter 2B (p 5-7)
    # 4. Ch3: Middle (p 8-9)
    log_test("Chapter Count for Shared", len(pdf_files) == 4, f"Found {len(pdf_files)} chapters")

    # Verify Shared Chapter 2A has exactly 1 page (page 5)
    doc_shared_a = fitz.open(str(pdf_files[1]))
    log_test("Shared Chapter 2A given at least that one page", len(doc_shared_a) == 1, f"Pages: {len(doc_shared_a)}")
    doc_shared_a.close()


def test_4_illegal_titles():
    print("\n--- Test 4: Illegal Filename Characters Sanitization ---")
    pdf = str(test_data_dir / "illegal_titles.pdf")
    out_dir = output_dir / "illegal_titles"

    res = run_cmd([pdf, "--level", "1", "--out", str(out_dir), "--yes"])
    log_test("Illegal Titles Exit Code", res.returncode == 0, f"Error if any: {res.stderr}")

    pdf_files = sorted(list((out_dir / "pdf").glob("*.pdf")))
    txt_files = sorted(list((out_dir / "txt").glob("*.txt")))
    log_test("Files Created without OS Error", len(pdf_files) == 4 and len(txt_files) == 4,
             f"{len(pdf_files)} PDFs and {len(txt_files)} TXTs created")

    # Verify CON reserved word was sanitized (e.g. CON_)
    con_files = [f.name for f in pdf_files if "CON" in f.name]
    log_test("Reserved word 'CON' sanitized", len(con_files) == 1 and not con_files[0].endswith("CON.pdf"),
             f"CON filename: {con_files}")

    # Verify illegal characters <>:"/\|?* are not present in filenames
    illegal_chars = set('<>:"/\\|?*')
    has_illegal = any(any(c in f.name for c in illegal_chars) for f in pdf_files + txt_files)
    log_test("No illegal characters in filenames", not has_illegal)

    # Verify long filename length <= 80 chars for title
    long_file = [f.name for f in pdf_files if "Very Long Title" in f.name]
    log_test("Long title truncated", len(long_file) == 1 and len(long_file[0]) < 120, f"Length: {len(long_file[0]) if long_file else 0}")


def test_5_scanned_pdf():
    print("\n--- Test 5: Scanned PDF (No-Text Layer Warning) ---")
    pdf = str(test_data_dir / "scanned_pdf.pdf")
    out_dir = output_dir / "scanned"

    res = run_cmd([pdf, "--level", "1", "--out", str(out_dir), "--yes"])
    log_test("Scanned PDF Exit Code", res.returncode == 0)

    # Check for warning in output
    has_warning = "no text layer, OCR needed" in res.stdout or "OCR needed" in res.stdout
    log_test("No-text warning detected in stdout", has_warning, "Found expected OCR needed warning")

    txt_files = sorted(list((out_dir / "txt").glob("*.txt")))
    log_test("Text files still generated", len(txt_files) == 3)


def test_6_txt_only():
    print("\n--- Test 6: --txt-only Flag ---")
    pdf = str(test_data_dir / "nested_bookmarks.pdf")
    out_dir = output_dir / "txt_only_test"

    res = run_cmd([pdf, "--level", "1", "--out", str(out_dir), "--txt-only", "--yes"])
    log_test("--txt-only Exit Code", res.returncode == 0)

    pdf_folder_exists = (out_dir / "pdf").exists()
    txt_files = list((out_dir / "txt").glob("*.txt"))
    log_test("PDF folder omitted when --txt-only", not pdf_folder_exists)
    log_test("TXT files written when --txt-only", len(txt_files) == 3)


def main():
    print("==================================================")
    print("RUNNING PDF SPLITTER COMPREHENSIVE TEST SUITE")
    print("==================================================")

    # Ensure test PDFs exist
    if not (test_data_dir / "nested_bookmarks.pdf").exists():
        import create_test_data
        create_test_data.main()

    test_1_nested_bookmarks()
    test_2_no_bookmarks()
    test_3_invalid_and_shared_bookmarks()
    test_4_illegal_titles()
    test_5_scanned_pdf()
    test_6_txt_only()

    print("\n==================================================")
    print(f"FINAL TEST RESULTS: {passed_tests} PASSED, {failed_tests} FAILED")
    print("==================================================")

    if failed_tests > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
