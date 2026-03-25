# PDF Accessibility Fixer

![PDF Accessibility Fixer](logo.png)

A tkinter GUI tool that scans course PDFs for accessibility compliance and automatically fixes common issues flagged by the University of Washington's accessibility requirements.

## What it fixes

| Issue | Detection | Fix |
|---|---|---|
| **Untagged PDF** | Missing `/MarkInfo` dictionary | Adds `/MarkInfo{Marked:true}` |
| **No document structure** | Missing `/StructTreeRoot` | Adds structure tree for screen reader navigation |
| **Image-only pages** (scanned docs) | No text operators in content streams | Full OCR via Tesseract |
| **Missing or bad title** | Empty, missing, or gibberish `/Title` metadata | Sets title derived from filename |

PDFs are converted to PDF/A-2 format with full tagging via [ocrmypdf](https://github.com/ocrmypdf/OCRmyPDF), then patched with [pikepdf](https://github.com/pikepdf/pikepdf) to ensure `/MarkInfo` and `/StructTreeRoot` are present.

## Screenshot

The GUI shows a color-coded table of all PDFs in the folder:

- **Green** — already compliant (including known-good files)
- **Yellow** — needs fixing
- **Blue** — currently processing
- **Red** — error during processing

Click **Fix All** to process all pending files. Fixed PDFs are saved to the `updated/` subfolder.

## Setup

Requires Python 3.10+ with a [micromamba](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html) environment.

```bash
# Install system dependencies
micromamba install -n myenv -c conda-forge tesseract ghostscript pikepdf

# Install ocrmypdf (pip, since conda-forge has a missing dependency on Windows)
micromamba run -n myenv pip install ocrmypdf
```

### Dependencies

- **tesseract** — OCR engine (installed via conda-forge)
- **ghostscript** — PDF/A conversion backend
- **ocrmypdf** — orchestrates OCR and PDF/A tagging
- **pikepdf** — PDF inspection and metadata patching
- **tkinter** — GUI (bundled with Python)

## Usage

```bash
micromamba run -n myenv python fix_pdf_accessibility.py
```

1. The GUI opens and automatically scans the current folder for PDFs
2. Each PDF is inspected and shown in the table with its accessibility status
3. Click **Fix All** to process all files that need fixing
4. Fixed PDFs appear in the `updated/` subfolder
5. Click **Open Log** to view the detailed processing log

### Known-good files

Files listed in the `KNOWN_GOOD` set at the top of the script are always shown as compliant and skipped during processing. Edit this set to add files you've already verified externally:

```python
KNOWN_GOOD = {"Wk1_Janeway_Ch1_Sec1-5.pdf"}
```

### Log file

Every run appends to `accessibility_log.txt` with timestamped entries covering:

- Full inspection details for every PDF (pages, text content, tags, title)
- Known-good file acknowledgments
- Fix mode used (OCR vs skip-text), duration, and output size
- Verification pass/fail with specific errors
- Full tracebacks for any failures

## Example files

Three example PDFs are included in the repo:

| File | Status | Notes |
|---|---|---|
| `Wk1_Janeway_Ch1_Sec1-5.pdf` | Compliant | Already tagged with proper title — used as reference |
| `Wk1_Janeway1989.pdf` | Needs fix | Has text but missing tags and has a gibberish title |
| `Wk1_HerronFreeman_Chapters.pdf` | Needs fix | Scanned images only — requires full OCR |

New PDFs added to the folder are git-ignored by default. Only the three example files are tracked.
