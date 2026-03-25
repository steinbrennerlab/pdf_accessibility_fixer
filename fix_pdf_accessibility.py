"""PDF Accessibility Fixer — tkinter GUI with smart heading detection."""

from collections import Counter
from pathlib import Path
from datetime import datetime
import os
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk

import pikepdf
import ocrmypdf
from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTTextBox, LTTextLine, LTChar


BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "updated"
LOG_FILE = BASE_DIR / "accessibility_log.txt"
KNOWN_GOOD = {"Wk1_Janeway_Ch1_Sec1-5.pdf"}

# Heading detection strategies
STRATEGY_AUTO = "Auto (font size)"
STRATEGY_FIRST_LINE = "First line = H1"
STRATEGY_BOLD = "Bold text = headings"
STRATEGIES = [STRATEGY_AUTO, STRATEGY_FIRST_LINE, STRATEGY_BOLD]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}]  {msg}\n")


def log_section(title: str) -> None:
    log(f"{'=' * 60}")
    log(f"  {title}")
    log(f"{'=' * 60}")


# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------

def _extract_text_lines(path: Path, page_num: int) -> list[dict]:
    """Extract text lines with font info from a single page using pdfminer."""
    lines = []
    try:
        for pg_idx, page_layout in enumerate(extract_pages(
            str(path), page_numbers=[page_num], laparams=LAParams()
        )):
            for elem in page_layout:
                if isinstance(elem, LTTextBox):
                    for line in elem:
                        if not isinstance(line, LTTextLine):
                            continue
                        text = line.get_text().strip()
                        if not text or len(text) < 2:
                            continue
                        font_name = font_size = None
                        for ch in line:
                            if isinstance(ch, LTChar):
                                font_name = ch.fontname
                                font_size = round(ch.size, 1)
                                break
                        if font_size is None or font_size < 5:
                            continue  # skip tiny/rotated text
                        lines.append({
                            "text": text,
                            "font_name": font_name or "",
                            "font_size": font_size,
                            "y": round(line.y0),
                            "is_bold": font_name and ("Bold" in font_name
                                                       or "bold" in font_name),
                        })
    except Exception:
        pass
    return lines


def detect_headings(path: Path, strategy: str = STRATEGY_AUTO) -> list[dict]:
    """Detect headings across all pages. Returns list of heading dicts."""
    # Get page count — let exceptions propagate so callers can report them
    with pikepdf.open(path) as pdf:
        n_pages = len(pdf.pages)

    all_headings = []

    if strategy == STRATEGY_AUTO:
        # First pass: collect all font sizes across all pages to find body size
        all_sizes = []
        page_lines = {}
        for pg in range(n_pages):
            lines = _extract_text_lines(path, pg)
            page_lines[pg] = lines
            all_sizes.extend(ln["font_size"] for ln in lines)

        if not all_sizes:
            return []

        # Body font = most common size
        size_counts = Counter(all_sizes)
        body_size = size_counts.most_common(1)[0][0]

        # Heading sizes = sizes significantly larger than body (>= 1.2x)
        heading_sizes = sorted(
            set(s for s in all_sizes if s >= body_size * 1.2),
            reverse=True,
        )
        # Map sizes to levels: largest = H1, next = H2, etc.
        # Cap at 3 levels for academic papers (H1=title, H2=subtitle, H3=section)
        size_to_level = {}
        for i, sz in enumerate(heading_sizes):
            size_to_level[sz] = min(i + 1, 3)

        for pg in range(n_pages):
            for ln in page_lines.get(pg, []):
                if ln["font_size"] in size_to_level:
                    all_headings.append({
                        "page": pg,
                        "level": size_to_level[ln["font_size"]],
                        "text": ln["text"],
                        "font_size": ln["font_size"],
                        "font_name": ln["font_name"],
                    })

    elif strategy == STRATEGY_FIRST_LINE:
        for pg in range(n_pages):
            lines = _extract_text_lines(path, pg)
            if lines:
                # Topmost line (highest y)
                top = max(lines, key=lambda l: l["y"])
                all_headings.append({
                    "page": pg,
                    "level": 1 if pg == 0 else 2,
                    "text": top["text"],
                    "font_size": top["font_size"],
                    "font_name": top["font_name"],
                })

    elif strategy == STRATEGY_BOLD:
        for pg in range(n_pages):
            lines = _extract_text_lines(path, pg)
            for ln in lines:
                if ln["is_bold"]:
                    all_headings.append({
                        "page": pg,
                        "level": 2,
                        "text": ln["text"],
                        "font_size": ln["font_size"],
                        "font_name": ln["font_name"],
                    })
        # Promote first heading to H1
        if all_headings:
            all_headings[0]["level"] = 1

    return all_headings


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def derive_title(filename: str) -> str:
    return Path(filename).stem.replace("_", " ")


def _struct_tree_is_empty(root) -> bool:
    st = root.get("/StructTreeRoot")
    if st is None:
        return True
    k = st.get("/K")
    if k is None:
        return True
    if isinstance(k, pikepdf.Array) and len(k) == 0:
        return True
    return False


def _has_heading_with_text(root) -> bool:
    """Check if structure tree has heading elements with /ActualText."""
    st = root.get("/StructTreeRoot")
    if st is None:
        return False
    heading_names = {"/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}
    try:
        k = st.get("/K")
        if k is None:
            return False
        docs = k if isinstance(k, pikepdf.Array) else [k]
        for doc in docs:
            if not isinstance(doc, pikepdf.Dictionary):
                continue
            sects = doc.get("/K")
            if sects is None:
                continue
            sects = sects if isinstance(sects, pikepdf.Array) else [sects]
            for sect in sects:
                if not isinstance(sect, pikepdf.Dictionary):
                    continue
                children = sect.get("/K")
                if children is None:
                    continue
                children = children if isinstance(children, pikepdf.Array) else [children]
                for child in children:
                    if isinstance(child, pikepdf.Dictionary):
                        s = str(child.get("/S", ""))
                        if s in heading_names and "/ActualText" in child:
                            return True
    except Exception:
        pass
    return False


def inspect_pdf(path: Path) -> dict:
    with pikepdf.open(path) as pdf:
        root = pdf.Root

        title_raw = ""
        try:
            title_raw = str(pdf.docinfo.get("/Title", ""))
        except Exception:
            pass

        has_text = False
        n = len(pdf.pages)
        sample_indices = sorted(set([0, n // 2, n - 1]) & set(range(n)))
        for i in sample_indices:
            page = pdf.pages[i]
            if "/Contents" in page:
                contents = page["/Contents"]
                try:
                    if isinstance(contents, pikepdf.Array):
                        raw = b"".join(c.read_bytes() for c in contents)
                    else:
                        raw = contents.read_bytes()
                    if b"BT" in raw:
                        has_text = True
                        break
                except Exception:
                    pass

        is_good_title = bool(title_raw.strip()) and ".." not in title_raw

        return {
            "has_mark_info": "/MarkInfo" in root,
            "has_struct_tree": not _struct_tree_is_empty(root),
            "has_headings": _has_heading_with_text(root),
            "has_good_title": is_good_title,
            "current_title": title_raw,
            "has_text": has_text,
            "page_count": n,
        }


def add_tags_if_missing(path: Path, title: str,
                        strategy: str = STRATEGY_AUTO) -> list:
    """Add MarkInfo, structure tree with real headings, and title."""
    changes = []
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        root = pdf.Root

        if "/MarkInfo" not in root:
            root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
            changes.append("Added /MarkInfo{Marked:true}")

        needs_struct = (_struct_tree_is_empty(root)
                        or not _has_heading_with_text(root))
        if needs_struct:
            # Detect headings from the PDF content
            headings = detect_headings(path, strategy)
            # Group by page
            headings_by_page: dict[int, list] = {}
            for h in headings:
                headings_by_page.setdefault(h["page"], []).append(h)

            struct_root = pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructTreeRoot"),
            }))
            root["/StructTreeRoot"] = struct_root

            doc_elem = pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Document"),
                "/P": struct_root,
                "/K": pikepdf.Array([]),
            }))

            page_elems = []
            parent_tree_nums = []

            for page_idx, page in enumerate(pdf.pages):
                sect_elem = pdf.make_indirect(pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/Sect"),
                    "/P": doc_elem,
                }))

                sect_children = []

                # Add heading elements with /ActualText for this page
                page_headings = headings_by_page.get(page_idx, [])
                for h in page_headings:
                    tag = f"/H{h['level']}"
                    h_elem = pdf.make_indirect(pikepdf.Dictionary({
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name(tag),
                        "/P": sect_elem,
                        "/ActualText": pikepdf.String(h["text"]),
                    }))
                    sect_children.append(h_elem)

                # Body paragraph with MCID wrapping all page content
                para_elem = pdf.make_indirect(pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/P"),
                    "/P": sect_elem,
                    "/K": pikepdf.Dictionary({
                        "/Type": pikepdf.Name("/MCR"),
                        "/Pg": page.obj,
                        "/MCID": 0,
                    }),
                }))
                sect_children.append(para_elem)

                sect_elem["/K"] = pikepdf.Array(sect_children)
                page_elems.append(sect_elem)

                # Wrap page content in single BDC/EMC
                if "/Contents" in page:
                    contents = page["/Contents"]
                    if isinstance(contents, pikepdf.Array):
                        old_streams = list(contents)
                    else:
                        old_streams = [contents]

                    bdc = pdf.make_stream(b"/P <</MCID 0>> BDC\n")
                    emc = pdf.make_stream(b"\nEMC\n")
                    page["/Contents"] = pikepdf.Array(
                        [bdc] + old_streams + [emc]
                    )

                page.obj["/StructParents"] = page_idx

                parent_tree_nums.append(page_idx)
                parent_tree_nums.append(pikepdf.Array([para_elem]))

            doc_elem["/K"] = pikepdf.Array(page_elems)
            struct_root["/K"] = pikepdf.Array([doc_elem])
            struct_root["/ParentTree"] = pdf.make_indirect(pikepdf.Dictionary({
                "/Nums": pikepdf.Array(parent_tree_nums),
            }))
            struct_root["/ParentTreeNextKey"] = len(pdf.pages)

            n_h = sum(len(v) for v in headings_by_page.values())
            changes.append(
                f"Built StructTreeRoot: {len(pdf.pages)} pages, "
                f"{n_h} headings detected ({strategy})"
            )
            for h in headings[:10]:
                changes.append(
                    f"  H{h['level']} p{h['page']+1}: "
                    f"{h['text'][:60]} ({h['font_size']}pt {h['font_name']})"
                )
            if len(headings) > 10:
                changes.append(f"  ... and {len(headings)-10} more")

        # Title
        with pdf.open_metadata() as meta:
            meta["dc:title"] = title
        pdf.docinfo["/Title"] = title
        changes.append(f"Set title to '{title}'")

        pdf.save(path)
    return changes


def _run_ocr(input_path: Path, output_path: Path, title: str,
             has_text: bool) -> str:
    """Run ocrmypdf, falling back to plain PDF output on color space errors."""
    ocr_flag = {"redo_ocr": True} if has_text else {"force_ocr": True}
    mode = "redo-ocr" if has_text else "force-ocr"

    try:
        ocrmypdf.ocr(
            input_path, output_path,
            output_type="pdfa-2", title=title, **ocr_flag,
        )
    except ocrmypdf.exceptions.ColorConversionNeededError:
        log(f"    Color space issue — retrying with output_type=pdf")
        ocrmypdf.ocr(
            input_path, output_path,
            output_type="pdf", title=title, **ocr_flag,
        )
        mode += " (skipped PDF/A — unusual color space)"

    return mode


def fix_pdf(input_path: Path, output_path: Path, info: dict, title: str,
            strategy: str = STRATEGY_AUTO) -> str:
    mode = _run_ocr(input_path, output_path, title, info["has_text"])
    add_tags_if_missing(output_path, title, strategy)
    return mode


def verify_output(path: Path, expected_title: str) -> list:
    errors = []
    info = inspect_pdf(path)
    if not info["has_mark_info"]:
        errors.append("Missing /MarkInfo")
    if not info["has_struct_tree"]:
        errors.append("Missing /StructTreeRoot")
    if not info["has_headings"]:
        errors.append("Missing headings with /ActualText")
    if not info["has_text"]:
        errors.append("No text content (OCR may have failed)")
    with pikepdf.open(path) as pdf:
        found_title = False
        try:
            docinfo_title = str(pdf.docinfo.get("/Title", ""))
            if expected_title in docinfo_title:
                found_title = True
        except Exception:
            pass
        if not found_title:
            try:
                with pdf.open_metadata() as meta:
                    xmp_title = meta.get("dc:title", "")
                    if expected_title in str(xmp_title):
                        found_title = True
            except Exception:
                pass
        if not found_title:
            errors.append(f"Title mismatch (expected '{expected_title}')")
    return errors


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

S_COMPLIANT = "Compliant"
S_NEEDS_FIX = "Needs Fix"
S_PROCESSING = "Processing..."
S_FIXED = "Fixed"
S_ERROR = "Error"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PDF Accessibility Fixer")
        self.root.geometry("960x700")
        self.root.minsize(800, 550)

        self._build_ui()
        self._file_data: dict[str, dict] = {}
        self._processing = False

        self.root.after(100, self.scan)

    def _build_ui(self):
        # Top button bar
        btn_frame = ttk.Frame(self.root, padding=6)
        btn_frame.pack(fill=tk.X)

        self.btn_scan = ttk.Button(btn_frame, text="Scan Folder", command=self.scan)
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_fix = ttk.Button(btn_frame, text="Fix Selected",
                                   command=self._start_fix_selected)
        self.btn_fix.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_log = ttk.Button(btn_frame, text="Open Log", command=self._open_log)
        self.btn_log.pack(side=tk.LEFT, padx=(0, 12))

        # Strategy dropdown
        ttk.Label(btn_frame, text="Heading method:").pack(side=tk.LEFT, padx=(0, 4))
        self.strategy_var = tk.StringVar(value=STRATEGY_AUTO)
        self.strategy_combo = ttk.Combobox(
            btn_frame, textvariable=self.strategy_var,
            values=STRATEGIES, state="readonly", width=20,
        )
        self.strategy_combo.pack(side=tk.LEFT, padx=(0, 4))

        self.lbl_summary = ttk.Label(btn_frame, text="", font=("Segoe UI", 9))
        self.lbl_summary.pack(side=tk.RIGHT)

        # --- Main file table (top half) ---
        paned = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 0))

        # File treeview frame
        file_frame = ttk.Frame(paned)
        paned.add(file_frame, weight=3)

        cols = ("pages", "status", "tags", "title", "details")
        self.tree = ttk.Treeview(file_frame, columns=cols, show="headings",
                                 selectmode="browse")
        self.tree.heading("pages", text="Pages")
        self.tree.heading("status", text="Status")
        self.tree.heading("tags", text="Tags")
        self.tree.heading("title", text="Title")
        self.tree.heading("details", text="Details")

        self.tree.column("pages", width=50, anchor=tk.CENTER, stretch=False)
        self.tree.column("status", width=110, anchor=tk.CENTER, stretch=False)
        self.tree.column("tags", width=160, anchor=tk.CENTER, stretch=False)
        self.tree.column("title", width=200, stretch=True)
        self.tree.column("details", width=350, stretch=True)

        self.tree.tag_configure("compliant", background="#d4edda")
        self.tree.tag_configure("needs_fix", background="#fff3cd")
        self.tree.tag_configure("processing", background="#cce5ff")
        self.tree.tag_configure("fixed", background="#d4edda")
        self.tree.tag_configure("error", background="#f8d7da")

        vsb = ttk.Scrollbar(file_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Bind selection to show headings
        self.tree.bind("<<TreeviewSelect>>", self._on_file_select)

        # --- Heading structure viewer (bottom half) ---
        heading_frame = ttk.LabelFrame(paned, text="Detected Heading Structure",
                                       padding=4)
        paned.add(heading_frame, weight=2)

        h_cols = ("level", "font", "text")
        self.h_tree = ttk.Treeview(heading_frame, columns=h_cols,
                                   show="tree headings", selectmode="none",
                                   height=8)
        self.h_tree.heading("#0", text="Page")
        self.h_tree.heading("level", text="Level")
        self.h_tree.heading("font", text="Font")
        self.h_tree.heading("text", text="Text")

        self.h_tree.column("#0", width=70, stretch=False)
        self.h_tree.column("level", width=50, anchor=tk.CENTER, stretch=False)
        self.h_tree.column("font", width=180, stretch=False)
        self.h_tree.column("text", width=500, stretch=True)

        self.h_tree.tag_configure("h1", background="#d1ecf1")
        self.h_tree.tag_configure("h2", background="#e2e3e5")
        self.h_tree.tag_configure("h3", background="#f8f9fa")
        self.h_tree.tag_configure("error", foreground="#721c24",
                                  background="#f8d7da")

        h_vsb = ttk.Scrollbar(heading_frame, orient=tk.VERTICAL,
                               command=self.h_tree.yview)
        self.h_tree.configure(yscrollcommand=h_vsb.set)
        self.h_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        h_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Progress bar
        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill=tk.X, padx=6, pady=6)

    # -- Heading viewer -----------------------------------------------------

    def _on_file_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        fname = self.tree.item(iid, "text")
        if not fname:
            return

        # Prefer output PDF if it exists, else input
        output_path = OUTPUT_DIR / fname
        input_path = BASE_DIR / fname
        pdf_path = output_path if output_path.exists() else input_path

        strategy = self.strategy_var.get()

        # Run heading detection in background to avoid freezing the GUI
        def _detect():
            try:
                headings = detect_headings(pdf_path, strategy)
                self.root.after(0, self._populate_heading_tree, headings)
            except Exception as e:
                self.root.after(0, self._show_detail_error,
                                f"{type(e).__name__}: {e}")

        threading.Thread(target=_detect, daemon=True).start()

    def _populate_heading_tree(self, headings: list[dict]):
        self.h_tree.delete(*self.h_tree.get_children())
        for h in headings:
            level = h["level"]
            tag = f"h{min(level, 3)}"
            self.h_tree.insert(
                "", tk.END,
                text=f"Page {h['page'] + 1}",
                values=(f"H{level}", f"{h['font_size']}pt {h['font_name']}",
                        h["text"]),
                tags=(tag,),
            )

    def _show_detail_error(self, message: str):
        self.h_tree.delete(*self.h_tree.get_children())
        self.h_tree.insert(
            "", tk.END, text="Error",
            values=("", "", message),
            tags=("error",),
        )

    # -- Scan ---------------------------------------------------------------

    def scan(self):
        if self._processing:
            return

        self._processing = True
        self.btn_scan.config(state=tk.DISABLED)
        self.btn_fix.config(state=tk.DISABLED)
        self.tree.delete(*self.tree.get_children())
        self.h_tree.delete(*self.h_tree.get_children())
        self._file_data.clear()
        self.lbl_summary.config(text="Scanning...")

        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        log_section("SCAN SESSION STARTED")

        OUTPUT_DIR.mkdir(exist_ok=True)
        pdfs = sorted(BASE_DIR.glob("*.pdf"))

        if not pdfs:
            self.root.after(0, self._scan_done, 0, 0, 0)
            log("No PDF files found in folder.")
            return

        n_compliant = 0
        n_needs_fix = 0

        for pdf_path in pdfs:
            fname = pdf_path.name
            is_known_good = fname in KNOWN_GOOD

            info = inspect_pdf(pdf_path)

            issues = []
            if not info["has_mark_info"]:
                issues.append("no MarkInfo")
            if not info["has_struct_tree"]:
                issues.append("no StructTreeRoot")
            elif not info["has_headings"]:
                issues.append("no headings with text")
            if not info["has_good_title"]:
                issues.append("bad/missing title")
            if not info["has_text"]:
                issues.append("image-only (needs OCR)")

            tags_str = self._make_tags_str(info)

            log(f"INSPECTED: {fname}")
            log(f"    Pages:          {info['page_count']}")
            log(f"    Has text:       {info['has_text']}")
            log(f"    MarkInfo:       {info['has_mark_info']}")
            log(f"    StructTreeRoot: {info['has_struct_tree']}")
            log(f"    Headings:       {info['has_headings']}")
            log(f"    Title:          {info['current_title']!r}")
            log(f"    Good title:     {info['has_good_title']}")

            if is_known_good or not issues:
                status = S_COMPLIANT
                detail = "Known good" if is_known_good else "All checks pass"
                tag = "compliant"
                n_compliant += 1
                if is_known_good:
                    log(f"    KNOWN_GOOD: {fname} — already compliant, skipped")
                else:
                    log(f"    STATUS: Already compliant — no fixes needed")
            elif (OUTPUT_DIR / fname).exists():
                output_path = OUTPUT_DIR / fname
                title = derive_title(fname)
                errors = verify_output(output_path, title)
                if not errors:
                    out_info = inspect_pdf(output_path)
                    info = out_info
                    tags_str = self._make_tags_str(out_info)
                    status = S_FIXED
                    detail = "Previously fixed (in updated/)"
                    tag = "fixed"
                    n_compliant += 1
                    log(f"    STATUS: Previously fixed — verified output in updated/")
                else:
                    status = S_NEEDS_FIX
                    detail = "; ".join(errors)
                    tag = "needs_fix"
                    n_needs_fix += 1
                    log(f"    STATUS: Needs re-fix — output failed: {errors}")
            else:
                status = S_NEEDS_FIX
                detail = "; ".join(issues)
                tag = "needs_fix"
                n_needs_fix += 1
                log(f"    STATUS: Needs fix — {detail}")

            # Insert row on main thread
            self.root.after(0, self._scan_add_row, fname, info, status,
                            tags_str, detail, tag, pdf_path)

        log(f"SCAN SUMMARY: {len(pdfs)} PDFs — {n_compliant} compliant, {n_needs_fix} need fixing")
        self.root.after(0, self._scan_done, len(pdfs), n_compliant, n_needs_fix)

    def _scan_add_row(self, fname, info, status, tags_str, detail, tag, pdf_path):
        iid = self.tree.insert(
            "", tk.END, text=fname,
            values=(info["page_count"], status, tags_str,
                    info["current_title"], detail),
            tags=(tag,),
        )
        self._file_data[fname] = {
            **info, "status": status, "iid": iid, "path": pdf_path,
        }

    def _scan_done(self, n_pdfs, n_compliant, n_needs_fix):
        if n_pdfs == 0:
            self.lbl_summary.config(text="No PDFs found.")
        else:
            self.tree["displaycolumns"] = ("pages", "status", "tags", "title", "details")
            self.tree["show"] = ("tree", "headings")
            self.tree.heading("#0", text="File")
            self.tree.column("#0", width=250, stretch=False)
            self.lbl_summary.config(
                text=f"{n_compliant} compliant, {n_needs_fix} need fixing"
            )
        self._processing = False
        self.btn_scan.config(state=tk.NORMAL)
        self.btn_fix.config(state=tk.NORMAL)

    @staticmethod
    def _make_tags_str(info: dict) -> str:
        parts = []
        if info["has_mark_info"]:
            parts.append("MarkInfo")
        if info["has_struct_tree"]:
            parts.append("StructTree")
        if info["has_headings"]:
            parts.append("Headings")
        return " ".join(parts) or "None"

    # -- Fix selected -------------------------------------------------------

    def _start_fix_selected(self):
        if self._processing:
            return

        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        fname = self.tree.item(iid, "text")
        if not fname or fname not in self._file_data:
            return
        data = self._file_data[fname]
        if data["status"] not in (S_NEEDS_FIX,):
            return

        to_fix = [(fname, data)]

        self._processing = True
        self.btn_fix.config(state=tk.DISABLED)
        self.btn_scan.config(state=tk.DISABLED)
        self.progress["maximum"] = 1
        self.progress["value"] = 0

        strategy = self.strategy_var.get()

        log_section("FIX SESSION STARTED")
        log(f"File: {fname}")
        log(f"Heading strategy: {strategy}")

        thread = threading.Thread(
            target=self._fix_worker, args=(to_fix, strategy), daemon=True,
        )
        thread.start()

    def _fix_worker(self, to_fix: list, strategy: str):
        n_fixed = 0
        n_errors = 0

        for idx, (fname, data) in enumerate(to_fix):
            iid = data["iid"]
            pdf_path = data["path"]

            self.root.after(0, self._update_row, iid, S_PROCESSING,
                            "processing", "Working...")

            title = derive_title(fname)
            output_path = OUTPUT_DIR / fname
            mode = "force-ocr" if not data["has_text"] else "redo-ocr"

            log(f"PROCESSING: {fname}")
            log(f"    Mode:     {mode}")
            log(f"    Strategy: {strategy}")
            log(f"    Title:    '{title}'")
            log(f"    Pages:    {data['page_count']}")

            t0 = time.time()
            try:
                actual_mode = fix_pdf(pdf_path, output_path, data, title,
                                      strategy)
                elapsed = time.time() - t0
                log(f"    Completed in {elapsed:.1f}s (mode={actual_mode})")

                errors = verify_output(output_path, title)
                if errors:
                    detail = "; ".join(errors)
                    log(f"    VERIFY FAILED: {detail}")
                    self.root.after(0, self._update_row, iid, S_ERROR,
                                    "error", detail)
                    n_errors += 1
                else:
                    out_info = inspect_pdf(output_path)
                    tags_str = self._make_tags_str(out_info)

                    detail = f"Fixed in {elapsed:.1f}s — {mode}"
                    log(f"    VERIFY OK: all checks pass")
                    log(f"    Output: {output_path.stat().st_size / 1024:.0f} KB")
                    self.root.after(
                        0, self._update_row_full, iid, S_FIXED, "fixed",
                        tags_str, title, detail,
                    )
                    n_fixed += 1

            except Exception as e:
                elapsed = time.time() - t0
                log(f"    ERROR after {elapsed:.1f}s: {e}")
                log(f"    TRACEBACK:\n{traceback.format_exc()}")
                self.root.after(0, self._update_row, iid, S_ERROR,
                                "error", str(e)[:120])
                n_errors += 1

            self.root.after(0, self._set_progress, idx + 1)

        log(f"FIX SESSION COMPLETE: {n_fixed} fixed, {n_errors} errors")
        self.root.after(0, self._fix_done, n_fixed, n_errors)

    # -- Thread-safe UI updates ---------------------------------------------

    def _update_row(self, iid, status, tag, detail):
        self.tree.set(iid, "status", status)
        self.tree.set(iid, "details", detail)
        self.tree.item(iid, tags=(tag,))

    def _update_row_full(self, iid, status, tag, tags_str, title, detail):
        self.tree.set(iid, "status", status)
        self.tree.set(iid, "tags", tags_str)
        self.tree.set(iid, "title", title)
        self.tree.set(iid, "details", detail)
        self.tree.item(iid, tags=(tag,))

    def _set_progress(self, value):
        self.progress["value"] = value

    def _fix_done(self, n_fixed, n_errors):
        self._processing = False
        self.btn_fix.config(state=tk.NORMAL)
        self.btn_scan.config(state=tk.NORMAL)
        n_compliant = sum(
            1 for d in self._file_data.values()
            if self.tree.set(d["iid"], "status") in (S_COMPLIANT, S_FIXED)
        )
        n_remaining = sum(
            1 for d in self._file_data.values()
            if self.tree.set(d["iid"], "status") == S_NEEDS_FIX
        )
        self.lbl_summary.config(
            text=f"{n_compliant} compliant, {n_remaining} need fixing  |  "
                 f"Last run: {n_fixed} fixed, {n_errors} errors"
        )

    # -- Open log -----------------------------------------------------------

    def _open_log(self):
        if not LOG_FILE.exists():
            return
        if sys.platform == "win32":
            os.startfile(LOG_FILE)
        else:
            subprocess.Popen(["xdg-open", str(LOG_FILE)])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
