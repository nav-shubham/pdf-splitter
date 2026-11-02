# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pymupdf>=1.23.0",
# ]
# ///

"""
Script to generate test PDFs for split_pdf.py verification.
"""

from pathlib import Path
import pymupdf as fitz

test_dir = Path(__file__).parent / "test_data"
test_dir.mkdir(exist_ok=True, parents=True)


def create_nested_pdf():
    doc = fitz.open()
    for i in range(1, 16):
        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (50, 72),
            f"Nested Bookmarks Document - Page {i}\n\nContent for testing bookmark splitting at levels 1 and 2.\n"
            f"This paragraph contains regular text for page {i}.",
            fontsize=12,
        )
    # TOC: [lvl, title, page] (1-based)
    toc = [
        [1, "Chapter 1: Foundations", 1],
        [2, "Section 1.1: Background", 3],
        [3, "Subsection 1.1.1: Deep Dive", 4],
        [2, "Section 1.2: Architecture", 6],
        [1, "Chapter 2: Methodology", 9],
        [2, "Section 2.1: Implementation", 11],
        [1, "Chapter 3: Results", 13],
        [2, "Section 3.1: Discussion", 14],
    ]
    doc.set_toc(toc)
    path = test_dir / "nested_bookmarks.pdf"
    doc.save(str(path))
    doc.close()
    print(f"Created {path}")


def create_no_bookmarks_pdf():
    doc = fitz.open()
    for i in range(1, 26):
        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (50, 72),
            f"No Bookmarks Document - Page {i}\n\nTesting range-based splitting like '1-5,6-end' and 'every 10'.\n"
            f"Here is content for page {i}.",
            fontsize=12,
        )
    doc.set_toc([])
    path = test_dir / "no_bookmarks.pdf"
    doc.save(str(path))
    doc.close()
    print(f"Created {path}")


def create_invalid_and_shared_pdf():
    # Create 15 pages first
    doc = fitz.open()
    for i in range(1, 16):
        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (50, 72),
            f"Invalid & Shared Bookmarks Document - Page {i}\n\nTesting error handling for bad pages and shared boundaries.",
            fontsize=12,
        )
    # Set TOC with bookmarks pointing to pages 1..15, including duplicates on page 5
    toc = [
        [1, "Chapter 1: Start", 1],
        [1, "Shared Chapter 2A", 5],
        [1, "Shared Chapter 2B", 5],
        [1, "Chapter 3: Middle", 8],
        [1, "Beyond Range Bookmark 1", 12],
        [1, "Beyond Range Bookmark 2", 15],
    ]
    doc.set_toc(toc)
    pdf_bytes = doc.tobytes()
    doc.close()

    # Re-open and delete pages 10 to 15 (0-based: 9 to 14) so total_pages becomes 9
    # The TOC entries for page 12 and 15 will now be beyond total_pages!
    doc2 = fitz.open("pdf", pdf_bytes)
    doc2.delete_pages(9, 14)
    path = test_dir / "invalid_and_shared.pdf"
    doc2.save(str(path))
    doc2.close()
    print(f"Created {path}")


def create_illegal_titles_pdf():
    doc = fitz.open()
    for i in range(1, 9):
        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (50, 72),
            f"Illegal Titles Document - Page {i}\n\nTesting filename sanitization for Windows filesystems.",
            fontsize=12,
        )
    long_title = "Chapter 3 - Very Long Title " + ("ExtremelyDescriptiveAnalysisAndFindings" * 4) + " Details"
    toc = [
        [1, 'What is <Python>? / Why "PyMuPDF": Part *1* | Version: 2.0?', 1],
        [1, 'CON', 3],  # Windows reserved device name
        [1, long_title, 5],
        [1, 'Chapter 4: Trailing Dots and Spaces...   ', 7],
    ]
    doc.set_toc(toc)
    path = test_dir / "illegal_titles.pdf"
    doc.save(str(path))
    doc.close()
    print(f"Created {path}")


def create_scanned_pdf():
    doc = fitz.open()
    for i in range(1, 4):
        page = doc.new_page(width=595, height=842)
        # Draw vector graphics (shapes, rects) with no text layer at all
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(50, 50, 500, 750))
        shape.finish(color=(0.2, 0.2, 0.2), fill=(0.95, 0.95, 0.95))
        shape.draw_circle(fitz.Point(275, 400), 100)
        shape.finish(color=(0.8, 0.1, 0.1), fill=(0.9, 0.3, 0.3))
        shape.commit()
    toc = [
        [1, "Scanned Section 1", 1],
        [1, "Scanned Section 2", 2],
        [1, "Scanned Section 3", 3],
    ]
    doc.set_toc(toc)
    path = test_dir / "scanned_pdf.pdf"
    doc.save(str(path))
    doc.close()
    print(f"Created {path}")


def main():
    create_nested_pdf()
    create_no_bookmarks_pdf()
    create_invalid_and_shared_pdf()
    create_illegal_titles_pdf()
    create_scanned_pdf()
    print("All test PDFs created successfully.")


if __name__ == "__main__":
    main()
