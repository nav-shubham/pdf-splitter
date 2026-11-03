"""
Comprehensive test suite verifying the 8 OCR requirements:
1. --check-ocr passes on a clean setup.
2. Fully scanned PDF (made by rendering text to image-only pages): txt contains expected text with (OCR) page markers.
3. Mixed PDF (some text pages, some scanned): only scanned pages are OCR'd.
4. TESSDATA_PREFIX set wrongly (parent folder, nonexistent folder): tool resolves correct tessdata.
5. Tesseract missing (bad --tesseract-path): clear install message, no crash, split PDFs produced.
6. Missing language (--ocr-lang hin without Hindi data): offers download, downloads into LOCALAPPDATA, works.
7. Page shared by two chapters is OCR'd once (cache hit).
8. Output .txt files are valid UTF-8 (including with Devanagari text if hin is installed).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import pymupdf as fitz

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

project_root = Path(__file__).parent.parent
test_data_dir = Path(__file__).parent / "test_data"
output_dir = Path(__file__).parent / "test_output"
test_data_dir.mkdir(exist_ok=True, parents=True)
output_dir.mkdir(exist_ok=True, parents=True)

python_exe = sys.executable
script_path = str(project_root / "split_pdf.py")

passed = 0
failed = 0


def log(name: str, ok: bool, details: str = ""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name} {details}")
    else:
        failed += 1
        print(f"  [FAIL] {name} - {details}")


def run_split(args: list[str], env_overrides: dict[str, str] = None) -> subprocess.CompletedProcess:
    cmd = [python_exe, script_path] + args
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


# ------------------------------------------------------------------------------
# Test 1: --check-ocr passes on clean setup
# ------------------------------------------------------------------------------
def test_1_check_ocr():
    print("\n--- Test 1: --check-ocr passes on a clean setup ---")
    res = run_split(["--check-ocr"])
    log("Check OCR Exit Code", res.returncode == 0, f"Exit code: {res.returncode}")
    log("Tesseract found reported", "Tesseract executable:" in res.stdout)
    log("Self-test passed reported", "Self-test passed:" in res.stdout)
    log("Summary success reported", "All OCR checks passed successfully!" in res.stdout)


# ------------------------------------------------------------------------------
# Test 2: Fully scanned PDF
# ------------------------------------------------------------------------------
def create_fully_scanned_pdf(path: Path):
    temp_doc = fitz.open()
    texts = [
        "The quick brown fox jumps over the lazy dog 101",
        "Sphinx of black quartz judge my vow 102",
        "Pack my box with five dozen liquor jugs 103",
        "Bright vixens jump dozy fowl quack 104",
    ]
    for t in texts:
        p = temp_doc.new_page(width=400, height=200)
        p.insert_text((40, 100), t, fontsize=16)

    scanned_doc = fitz.open()
    for i, p in enumerate(temp_doc):
        pix = p.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")
        new_page = scanned_doc.new_page(width=400, height=200)
        new_page.insert_image(new_page.rect, stream=img_bytes)

    temp_doc.close()

    scanned_doc.set_toc([
        [1, "Scanned Chapter 1", 1],
        [1, "Scanned Chapter 2", 3],
    ])
    scanned_doc.save(str(path))
    scanned_doc.close()


def test_2_fully_scanned():
    print("\n--- Test 2: Fully scanned PDF with (OCR) page markers ---")
    pdf_path = test_data_dir / "fully_scanned.pdf"
    create_fully_scanned_pdf(pdf_path)

    out = output_dir / "scanned_out"
    shutil.rmtree(out, ignore_errors=True)
    res = run_split([str(pdf_path), "--level", "1", "--out", str(out), "--yes"])
    log("Exit code 0", res.returncode == 0, f"code: {res.returncode}")

    txt_files = sorted(list((out / "txt").glob("*.txt")))
    log("TXT files created", len(txt_files) == 2, f"Found {len(txt_files)}")

    content_ch1 = txt_files[0].read_text(encoding="utf-8")
    content_ch2 = txt_files[1].read_text(encoding="utf-8")

    log("Ch 1 contains Page 1 (OCR)", "--- Page 1 (OCR) ---" in content_ch1)
    log("Ch 1 contains Page 2 (OCR)", "--- Page 2 (OCR) ---" in content_ch1)
    log("Ch 2 contains Page 3 (OCR)", "--- Page 3 (OCR) ---" in content_ch2)
    log("Ch 2 contains Page 4 (OCR)", "--- Page 4 (OCR) ---" in content_ch2)

    log("Ch 1 recognized 'quick brown fox'", "quick" in content_ch1.lower() or "brown" in content_ch1.lower())
    log("Ch 2 recognized 'liquor' or 'jugs'", "liquor" in content_ch2.lower() or "jugs" in content_ch2.lower() or "dozen" in content_ch2.lower())


# ------------------------------------------------------------------------------
# Test 3: Mixed PDF (some text, some scanned)
# ------------------------------------------------------------------------------
def create_mixed_pdf(path: Path):
    doc = fitz.open()

    p1 = doc.new_page(width=400, height=200)
    p1.insert_text((40, 100), "Digital Chapter One Page One 201 - plenty of text to exceed 25 characters easily.", fontsize=14)

    temp_doc = fitz.open()
    tp = temp_doc.new_page(width=400, height=200)
    tp.insert_text((40, 100), "Scanned Chapter One Page Two 202 OCR Target", fontsize=16)
    pix = tp.get_pixmap(dpi=150)
    img_bytes = pix.tobytes("png")
    temp_doc.close()

    p2 = doc.new_page(width=400, height=200)
    p2.insert_image(p2.rect, stream=img_bytes)

    p3 = doc.new_page(width=400, height=200)
    p3.insert_text((40, 100), "Digital Chapter Two Page Three 203 - another digital page with plenty of text.", fontsize=14)

    temp_doc = fitz.open()
    tp = temp_doc.new_page(width=400, height=200)
    tp.insert_text((40, 100), "Scanned Chapter Two Page Four 204 OCR Target", fontsize=16)
    pix = tp.get_pixmap(dpi=150)
    img_bytes = pix.tobytes("png")
    temp_doc.close()

    p4 = doc.new_page(width=400, height=200)
    p4.insert_image(p4.rect, stream=img_bytes)

    doc.set_toc([
        [1, "Mixed Chapter 1", 1],
        [1, "Mixed Chapter 2", 3],
    ])
    doc.save(str(path))
    doc.close()


def test_3_mixed_pdf():
    print("\n--- Test 3: Mixed PDF (only scanned pages are OCR'd) ---")
    pdf_path = test_data_dir / "mixed_doc.pdf"
    create_mixed_pdf(pdf_path)

    out = output_dir / "mixed_out"
    shutil.rmtree(out, ignore_errors=True)
    res = run_split([str(pdf_path), "--level", "1", "--out", str(out), "--yes"])
    log("Exit code 0", res.returncode == 0)

    txt_files = sorted(list((out / "txt").glob("*.txt")))
    content_ch1 = txt_files[0].read_text(encoding="utf-8")
    content_ch2 = txt_files[1].read_text(encoding="utf-8")

    log("Page 1 is regular --- Page 1 ---", "--- Page 1 ---" in content_ch1 and "--- Page 1 (OCR) ---" not in content_ch1)
    log("Page 2 has --- Page 2 (OCR) ---", "--- Page 2 (OCR) ---" in content_ch1)
    log("Page 3 is regular --- Page 3 ---", "--- Page 3 ---" in content_ch2 and "--- Page 3 (OCR) ---" not in content_ch2)
    log("Page 4 has --- Page 4 (OCR) ---", "--- Page 4 (OCR) ---" in content_ch2)


# ------------------------------------------------------------------------------
# Test 4: TESSDATA_PREFIX wrong
# ------------------------------------------------------------------------------
def test_4_tessdata_prefix_wrong():
    print("\n--- Test 4: TESSDATA_PREFIX wrong (parent folder, and nonexistent folder) ---")
    pdf_path = test_data_dir / "fully_scanned.pdf"

    # 4A: Point to parent folder of tessdata (C:\Program Files\Tesseract-OCR)
    out_parent = output_dir / "prefix_parent"
    shutil.rmtree(out_parent, ignore_errors=True)
    res_parent = run_split(
        [str(pdf_path), "--level", "1", "--out", str(out_parent), "--yes"],
        env_overrides={"TESSDATA_PREFIX": r"C:\Program Files\Tesseract-OCR"},
    )
    log("TESSDATA_PREFIX parent folder exit code 0", res_parent.returncode == 0)
    txt_files_a = sorted(list((out_parent / "txt").glob("*.txt")))
    log("Parent folder resolved tessdata and OCR'd", len(txt_files_a) == 2 and "--- Page 1 (OCR) ---" in txt_files_a[0].read_text(encoding="utf-8"))

    # 4B: Point to nonexistent folder
    out_nonexist = output_dir / "prefix_nonexist"
    shutil.rmtree(out_nonexist, ignore_errors=True)
    res_nonexist = run_split(
        [str(pdf_path), "--level", "1", "--out", str(out_nonexist), "--yes"],
        env_overrides={"TESSDATA_PREFIX": r"C:\nonexistent_prefix_dir_xyz"},
    )
    log("TESSDATA_PREFIX nonexistent exit code 0", res_nonexist.returncode == 0)
    txt_files_b = sorted(list((out_nonexist / "txt").glob("*.txt")))
    log("Nonexistent prefix falls back and OCR succeeds", len(txt_files_b) == 2 and "--- Page 1 (OCR) ---" in txt_files_b[0].read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------
# Test 5: Tesseract missing
# ------------------------------------------------------------------------------
def test_5_tesseract_missing():
    print("\n--- Test 5: Tesseract missing (bad --tesseract-path, stripped PATH) ---")
    pdf_path = test_data_dir / "fully_scanned.pdf"
    out = output_dir / "missing_tess_out"
    shutil.rmtree(out, ignore_errors=True)

    res = run_split(
        [str(pdf_path), "--level", "1", "--out", str(out), "--tesseract-path", r"C:\bad_dir\tesseract.exe", "--yes"],
        env_overrides={"PATH": ""},
    )
    log("Exit code 0 when Tesseract missing", res.returncode == 0, f"code: {res.returncode}")
    log("Install guidance printed", "winget install UB-Mannheim.TesseractOCR" in res.stdout)
    log("PDF files still created", len(list((out / "pdf").glob("*.pdf"))) == 2)
    log("TXT files still created", len(list((out / "txt").glob("*.txt"))) == 2)
    log("OCR unavailable warning in stdout", "OCR unavailable: Tesseract not found" in res.stdout)


# ------------------------------------------------------------------------------
# Test 6: Missing language & auto-download
# ------------------------------------------------------------------------------
def test_6_missing_language_download():
    print("\n--- Test 6: Missing language (--ocr-lang hin) auto-download ---")
    pdf_path = test_data_dir / "fully_scanned.pdf"
    out = output_dir / "hin_download_out"
    shutil.rmtree(out, ignore_errors=True)

    local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    user_hin = Path(local_appdata) / "split_pdf" / "tessdata" / "hin.traineddata"
    if user_hin.exists():
        user_hin.unlink()

    res = run_split(
        [str(pdf_path), "--level", "1", "--out", str(out), "--ocr-lang", "hin", "--yes"]
    )
    log("Exit code 0 on hin download run", res.returncode == 0, f"code: {res.returncode}")
    log("hin.traineddata exists in user tessdata folder", user_hin.is_file(), f"Path: {user_hin}")
    log("Download notice or success in stdout", "hin.traineddata" in res.stdout)


# ------------------------------------------------------------------------------
# Test 7: Shared page OCR'd once (cache hit)
# ------------------------------------------------------------------------------
def create_shared_ocr_pdf(path: Path):
    doc = fitz.open()

    p1 = doc.new_page(width=400, height=200)
    p1.insert_text((40, 100), "Chapter 1 Page 1 Digital Text Content Here.", fontsize=14)

    temp_doc = fitz.open()
    tp = temp_doc.new_page(width=400, height=200)
    tp.insert_text((40, 100), "Shared Scanned Page Two 99999", fontsize=16)
    pix = tp.get_pixmap(dpi=150)
    img_bytes = pix.tobytes("png")
    temp_doc.close()

    p2 = doc.new_page(width=400, height=200)
    p2.insert_image(p2.rect, stream=img_bytes)

    p3 = doc.new_page(width=400, height=200)
    p3.insert_text((40, 100), "Chapter 2 Page 3 Digital Text Content Here.", fontsize=14)

    doc.save(str(path))
    doc.close()


def test_7_shared_page_cache():
    print("\n--- Test 7: Page shared by two chapters OCR'd once ---")
    pdf_path = test_data_dir / "shared_ocr.pdf"
    create_shared_ocr_pdf(pdf_path)

    out = output_dir / "shared_cache_out"
    shutil.rmtree(out, ignore_errors=True)
    res = run_split([str(pdf_path), "--ranges", "1-2,2-3", "--out", str(out), "--yes"])
    log("Exit code 0", res.returncode == 0)

    log("Preview shows OCR needed on 1 pages", "OCR needed on 1 pages" in res.stdout)
    log("Tesseract only ran once (OCR page 1/1)", "OCR page 1/1" in res.stdout)

    txt_files = sorted(list((out / "txt").glob("*.txt")))
    log("Two txt files produced", len(txt_files) == 2, f"Got: {[f.name for f in txt_files]}")

    content_ch1 = txt_files[0].read_text(encoding="utf-8")
    content_ch2 = txt_files[1].read_text(encoding="utf-8")

    log("Ch 1 has Page 2 (OCR)", "--- Page 2 (OCR) ---" in content_ch1)
    log("Ch 2 has Page 2 (OCR)", "--- Page 2 (OCR) ---" in content_ch2)
    log("Both got '99999'", "99999" in content_ch1 and "99999" in content_ch2)


# ------------------------------------------------------------------------------
# Test 8: Output .txt files valid UTF-8 including Devanagari
# ------------------------------------------------------------------------------
def create_devanagari_pdf(path: Path):
    doc = fitz.open()
    temp_doc = fitz.open()
    tp = temp_doc.new_page(width=400, height=200)
    tp.insert_text((40, 100), "नमस्ते भारत 2026", fontsize=20)
    pix = tp.get_pixmap(dpi=150)
    img_bytes = pix.tobytes("png")
    temp_doc.close()

    p = doc.new_page(width=400, height=200)
    p.insert_image(p.rect, stream=img_bytes)
    doc.set_toc([[1, "Hindi Chapter", 1]])
    doc.save(str(path))
    doc.close()


def test_8_utf8_devanagari():
    print("\n--- Test 8: Valid UTF-8 output with Devanagari text ---")
    pdf_path = test_data_dir / "devanagari_ocr.pdf"
    create_devanagari_pdf(pdf_path)

    out = output_dir / "devanagari_out"
    shutil.rmtree(out, ignore_errors=True)
    res = run_split([str(pdf_path), "--level", "1", "--out", str(out), "--ocr-lang", "hin+eng", "--yes"])
    log("Exit code 0 with Devanagari", res.returncode == 0)

    txt_file = next((out / "txt").glob("*.txt"))
    try:
        raw_bytes = txt_file.read_bytes()
        decoded = raw_bytes.decode("utf-8", errors="strict")
        log("File is strictly valid UTF-8", True)
    except Exception as e:
        log("File is strictly valid UTF-8", False, str(e))


def main():
    print("==================================================")
    print("RUNNING COMPREHENSIVE OCR REQUIREMENTS VERIFICATION")
    print("==================================================")

    test_1_check_ocr()
    test_2_fully_scanned()
    test_3_mixed_pdf()
    test_4_tessdata_prefix_wrong()
    test_5_tesseract_missing()
    test_6_missing_language_download()
    test_7_shared_page_cache()
    test_8_utf8_devanagari()

    print("\n==================================================")
    print(f"OCR TEST RESULTS: {passed} PASSED, {failed} FAILED")
    print("==================================================")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
