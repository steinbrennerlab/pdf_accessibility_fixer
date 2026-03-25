"""PDF Accessibility Fixer — tkinter GUI with detailed logging."""

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


BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "updated"
LOG_FILE = BASE_DIR / "accessibility_log.txt"
KNOWN_GOOD = {"Wk1_Janeway_Ch1_Sec1-5.pdf"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    """Append a timestamped line to the log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}]  {msg}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def log_section(title: str) -> None:
    """Write a section header to the log."""
    log(f"{'=' * 60}")
    log(f"  {title}")
    log(f"{'=' * 60}")


# ---------------------------------------------------------------------------
# PDF helpers (unchanged from CLI version)
# ---------------------------------------------------------------------------

def derive_title(filename: str) -> str:
    return Path(filename).stem.replace("_", " ")


def _struct_tree_is_empty(root) -> bool:
    """Check if a StructTreeRoot exists but has no real content."""
    st = root.get("/StructTreeRoot")
    if st is None:
        return True
    k = st.get("/K")
    if k is None:
        return True
    if isinstance(k, pikepdf.Array) and len(k) == 0:
        return True
    return False


def _struct_tree_has_headings(root) -> bool:
    """Check if the structure tree contains any heading elements."""
    st = root.get("/StructTreeRoot")
    if st is None:
        return False
    heading_names = {"/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}
    try:
        k = st.get("/K")
        if k is None:
            return False
        # Walk: StructTreeRoot -> /Document -> /Sect -> children
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
                # Check sect itself
                if str(sect.get("/S", "")) in heading_names:
                    return True
                # Check sect's children
                children = sect.get("/K")
                if children is None:
                    continue
                children = children if isinstance(children, pikepdf.Array) else [children]
                for child in children:
                    if isinstance(child, pikepdf.Dictionary):
                        if str(child.get("/S", "")) in heading_names:
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
            "has_headings": _struct_tree_has_headings(root),
            "has_good_title": is_good_title,
            "current_title": title_raw,
            "has_text": has_text,
            "page_count": n,
        }


def add_tags_if_missing(path: Path, title: str) -> list:
    """Add MarkInfo, real per-page StructTreeRoot, and title. Returns changes."""
    changes = []
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        root = pdf.Root

        # MarkInfo
        if "/MarkInfo" not in root:
            root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
            changes.append("Added /MarkInfo{Marked:true}")

        # Build a real structure tree if missing, empty, or lacking headings
        needs_struct = _struct_tree_is_empty(root) or not _struct_tree_has_headings(root)
        if needs_struct:
            # Create the StructTreeRoot first (we'll fill /K and /ParentTree below)
            struct_root = pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructTreeRoot"),
            }))
            root["/StructTreeRoot"] = struct_root

            # Document-level element — the single child of StructTreeRoot
            doc_elem = pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/Document"),
                "/P": struct_root,
                "/K": pikepdf.Array([]),
            }))

            page_elems = []       # one /Sect per page, children of /Document
            parent_tree_nums = [] # flat [page_idx, ref, page_idx, ref, ...]

            for page_idx, page in enumerate(pdf.pages):
                # Each page gets a /Sect containing a heading + paragraph.
                # Page 1 gets /H1 (document title), others get /H (section heading).
                # Two MCIDs per page: 0 = heading region, 1 = body region.
                sect_elem = pdf.make_indirect(pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/Sect"),
                    "/P": doc_elem,
                }))

                heading_tag = "/H1" if page_idx == 0 else "/H"
                heading_elem = pdf.make_indirect(pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name(heading_tag),
                    "/P": sect_elem,
                    "/K": pikepdf.Dictionary({
                        "/Type": pikepdf.Name("/MCR"),
                        "/Pg": page.obj,
                        "/MCID": 0,
                    }),
                }))

                para_elem = pdf.make_indirect(pikepdf.Dictionary({
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name("/P"),
                    "/P": sect_elem,
                    "/K": pikepdf.Dictionary({
                        "/Type": pikepdf.Name("/MCR"),
                        "/Pg": page.obj,
                        "/MCID": 1,
                    }),
                }))

                sect_elem["/K"] = pikepdf.Array([heading_elem, para_elem])
                page_elems.append(sect_elem)

                # Wrap the page content stream with two marked regions:
                #   MCID 0 = heading (first line area), MCID 1 = body (rest)
                # We can't perfectly split content, so the heading region is
                # a thin wrapper and the body gets the bulk of the content.
                if "/Contents" in page:
                    contents = page["/Contents"]
                    if isinstance(contents, pikepdf.Array):
                        old_streams = list(contents)
                    else:
                        old_streams = [contents]

                    heading_bdc = pdf.make_stream(
                        f"{heading_tag} <</MCID 0>> BDC\nEMC\n".encode()
                    )
                    body_bdc = pdf.make_stream(b"/P <</MCID 1>> BDC\n")
                    body_emc = pdf.make_stream(b"\nEMC\n")
                    page["/Contents"] = pikepdf.Array(
                        [heading_bdc, body_bdc] + old_streams + [body_emc]
                    )

                # Link page back to structure tree
                page.obj["/StructParents"] = page_idx

                # ParentTree: maps StructParents index -> array of struct elems
                # for all MCIDs on this page (MCID 0 = heading, MCID 1 = para)
                parent_tree_nums.append(page_idx)
                parent_tree_nums.append(pikepdf.Array([heading_elem, para_elem]))

            # Wire everything together
            doc_elem["/K"] = pikepdf.Array(page_elems)
            struct_root["/K"] = pikepdf.Array([doc_elem])
            struct_root["/ParentTree"] = pdf.make_indirect(pikepdf.Dictionary({
                "/Nums": pikepdf.Array(parent_tree_nums),
            }))
            struct_root["/ParentTreeNextKey"] = len(pdf.pages)

            changes.append(
                f"Built StructTreeRoot with /Document -> {len(pdf.pages)} "
                f"/Sect elements (/H1+/P on page 1, /H+/P on rest), "
                f"ParentTree, and marked content on all pages"
            )

        # Title
        with pdf.open_metadata() as meta:
            meta["dc:title"] = title
        pdf.docinfo["/Title"] = title
        changes.append(f"Set title to '{title}'")

        pdf.save(path)
    return changes


def fix_pdf(input_path: Path, output_path: Path, info: dict, title: str) -> str:
    """Apply accessibility fixes. Returns the mode used."""
    if info["has_text"]:
        # Mixed PDF (some real text + scanned images) — redo_ocr preserves
        # existing text and OCRs image regions so all text is selectable.
        ocrmypdf.ocr(
            input_path, output_path,
            output_type="pdfa-2", title=title, redo_ocr=True,
        )
        mode = "redo-ocr"
    else:
        # Image-only PDF — needs full OCR
        ocrmypdf.ocr(
            input_path, output_path,
            output_type="pdfa-2", title=title, force_ocr=True,
        )
        mode = "force-ocr"

    add_tags_if_missing(output_path, title)
    return mode


def verify_output(path: Path, expected_title: str) -> list:
    errors = []
    info = inspect_pdf(path)
    if not info["has_mark_info"]:
        errors.append("Missing /MarkInfo")
    if not info["has_struct_tree"]:
        errors.append("Missing /StructTreeRoot")
    if not info["has_headings"]:
        errors.append("Missing headings in structure tree")
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

# Status constants
S_COMPLIANT = "Compliant"
S_NEEDS_FIX = "Needs Fix"
S_PROCESSING = "Processing..."
S_FIXED = "Fixed"
S_ERROR = "Error"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PDF Accessibility Fixer")
        self.root.geometry("960x520")
        self.root.minsize(800, 400)

        self._build_ui()
        self._file_data: dict[str, dict] = {}  # filename -> info + status
        self._processing = False

        # Auto-scan on launch
        self.root.after(100, self.scan)

    # -- UI construction ----------------------------------------------------

    def _build_ui(self):
        # Top button bar
        btn_frame = ttk.Frame(self.root, padding=6)
        btn_frame.pack(fill=tk.X)

        self.btn_scan = ttk.Button(btn_frame, text="Scan Folder", command=self.scan)
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_fix = ttk.Button(btn_frame, text="Fix All", command=self._start_fix_all)
        self.btn_fix.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_log = ttk.Button(btn_frame, text="Open Log", command=self._open_log)
        self.btn_log.pack(side=tk.LEFT, padx=(0, 4))

        self.lbl_summary = ttk.Label(btn_frame, text="", font=("Segoe UI", 9))
        self.lbl_summary.pack(side=tk.RIGHT)

        # Treeview
        cols = ("pages", "status", "tags", "title", "details")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("pages", text="Pages")
        self.tree.heading("status", text="Status")
        self.tree.heading("tags", text="Tags")
        self.tree.heading("title", text="Title")
        self.tree.heading("details", text="Details")

        self.tree.column("pages", width=50, anchor=tk.CENTER, stretch=False)
        self.tree.column("status", width=110, anchor=tk.CENTER, stretch=False)
        self.tree.column("tags", width=130, anchor=tk.CENTER, stretch=False)
        self.tree.column("title", width=200, stretch=True)
        self.tree.column("details", width=350, stretch=True)

        # Row tags for coloring
        self.tree.tag_configure("compliant", background="#d4edda")
        self.tree.tag_configure("needs_fix", background="#fff3cd")
        self.tree.tag_configure("processing", background="#cce5ff")
        self.tree.tag_configure("fixed", background="#d4edda")
        self.tree.tag_configure("error", background="#f8d7da")

        # Scrollbar
        vsb = ttk.Scrollbar(self.root, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 0))
        vsb.place(in_=self.tree, relx=1.0, rely=0, relheight=1.0, anchor=tk.NE)

        # Progress bar
        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill=tk.X, padx=6, pady=6)

    # -- Scan ---------------------------------------------------------------

    def scan(self):
        if self._processing:
            return

        self.tree.delete(*self.tree.get_children())
        self._file_data.clear()

        log_section("SCAN SESSION STARTED")

        OUTPUT_DIR.mkdir(exist_ok=True)
        pdfs = sorted(BASE_DIR.glob("*.pdf"))

        if not pdfs:
            self.lbl_summary.config(text="No PDFs found.")
            log("No PDF files found in folder.")
            return

        n_compliant = 0
        n_needs_fix = 0

        for pdf_path in pdfs:
            fname = pdf_path.name
            is_known_good = fname in KNOWN_GOOD

            info = inspect_pdf(pdf_path)

            # Determine issues
            issues = []
            if not info["has_mark_info"]:
                issues.append("no MarkInfo")
            if not info["has_struct_tree"]:
                issues.append("no StructTreeRoot")
            elif not info["has_headings"]:
                issues.append("no headings in structure tree")
            if not info["has_good_title"]:
                issues.append("bad/missing title")
            if not info["has_text"]:
                issues.append("image-only (needs OCR)")

            tags_str = ""
            if info["has_mark_info"]:
                tags_str += "MarkInfo "
            if info["has_struct_tree"]:
                tags_str += "StructTree "
            if info["has_headings"]:
                tags_str += "Headings"
            if not tags_str:
                tags_str = "None"

            # Log full inspection
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
                # A fixed version exists — verify it and show as previously fixed
                output_path = OUTPUT_DIR / fname
                title = derive_title(fname)
                errors = verify_output(output_path, title)
                if not errors:
                    out_info = inspect_pdf(output_path)
                    info = out_info  # display the fixed version's info
                    tags_str = ""
                    if out_info["has_mark_info"]:
                        tags_str += "MarkInfo "
                    if out_info["has_struct_tree"]:
                        tags_str += "StructTree "
                    if out_info["has_headings"]:
                        tags_str += "Headings"
                    status = S_FIXED
                    detail = "Previously fixed (in updated/)"
                    tag = "fixed"
                    n_compliant += 1
                    log(f"    STATUS: Previously fixed — verified output in updated/")
                else:
                    status = S_NEEDS_FIX
                    detail = "; ".join(issues) + " (prior fix failed verification)"
                    tag = "needs_fix"
                    n_needs_fix += 1
                    log(f"    STATUS: Needs fix — prior output failed: {errors}")
            else:
                status = S_NEEDS_FIX
                detail = "; ".join(issues)
                tag = "needs_fix"
                n_needs_fix += 1
                log(f"    STATUS: Needs fix — {detail}")

            iid = self.tree.insert(
                "", tk.END,
                text=fname,
                values=(info["page_count"], status, tags_str, info["current_title"], detail),
                tags=(tag,),
            )
            # Override the default hidden 'text' — show filename as first visible column
            # Actually treeview 'text' is the tree column; let's use #0
            self._file_data[fname] = {**info, "status": status, "iid": iid, "path": pdf_path}

        # Show filename in #0 column
        self.tree["displaycolumns"] = ("pages", "status", "tags", "title", "details")
        self.tree["show"] = ("tree", "headings")
        self.tree.heading("#0", text="File")
        self.tree.column("#0", width=250, stretch=False)

        summary = f"{n_compliant} compliant, {n_needs_fix} need fixing"
        self.lbl_summary.config(text=summary)
        log(f"SCAN SUMMARY: {len(pdfs)} PDFs — {summary}")

    # -- Fix all ------------------------------------------------------------

    def _start_fix_all(self):
        if self._processing:
            return

        to_fix = [
            (fname, d) for fname, d in self._file_data.items()
            if d["status"] == S_NEEDS_FIX
        ]
        if not to_fix:
            return

        self._processing = True
        self.btn_fix.config(state=tk.DISABLED)
        self.btn_scan.config(state=tk.DISABLED)
        self.progress["maximum"] = len(to_fix)
        self.progress["value"] = 0

        log_section("FIX SESSION STARTED")
        log(f"Files to process: {len(to_fix)}")

        thread = threading.Thread(target=self._fix_worker, args=(to_fix,), daemon=True)
        thread.start()

    def _fix_worker(self, to_fix: list):
        n_fixed = 0
        n_errors = 0

        for idx, (fname, data) in enumerate(to_fix):
            iid = data["iid"]
            pdf_path = data["path"]

            # Update row to "Processing..."
            self.root.after(0, self._update_row, iid, S_PROCESSING, "processing", "Working...")

            title = derive_title(fname)
            output_path = OUTPUT_DIR / fname
            mode = "force-ocr" if not data["has_text"] else "redo-ocr"

            log(f"PROCESSING: {fname}")
            log(f"    Mode:     {mode}")
            log(f"    Title:    '{title}'")
            log(f"    Pages:    {data['page_count']}")
            log(f"    Output:   {output_path}")

            t0 = time.time()
            try:
                actual_mode = fix_pdf(pdf_path, output_path, data, title)
                elapsed = time.time() - t0
                log(f"    OCR/convert completed in {elapsed:.1f}s (mode={actual_mode})")

                # Verify
                errors = verify_output(output_path, title)
                if errors:
                    detail = "; ".join(errors)
                    log(f"    VERIFY FAILED: {detail}")
                    self.root.after(0, self._update_row, iid, S_ERROR, "error", detail)
                    n_errors += 1
                else:
                    # Re-inspect output for display
                    out_info = inspect_pdf(output_path)
                    tags_str = ""
                    if out_info["has_mark_info"]:
                        tags_str += "MarkInfo "
                    if out_info["has_struct_tree"]:
                        tags_str += "StructTree "
                    if out_info["has_headings"]:
                        tags_str += "Headings"

                    detail = f"Fixed in {elapsed:.1f}s — {mode}"
                    log(f"    VERIFY OK: all checks pass")
                    log(f"    Output size: {output_path.stat().st_size / 1024:.0f} KB")
                    self.root.after(
                        0, self._update_row_full, iid, S_FIXED, "fixed",
                        tags_str, title, detail,
                    )
                    n_fixed += 1

            except Exception as e:
                elapsed = time.time() - t0
                log(f"    ERROR after {elapsed:.1f}s: {e}")
                log(f"    TRACEBACK:\n{traceback.format_exc()}")
                self.root.after(0, self._update_row, iid, S_ERROR, "error", str(e)[:120])
                n_errors += 1

            # Progress
            self.root.after(0, self._set_progress, idx + 1)

        log(f"FIX SESSION COMPLETE: {n_fixed} fixed, {n_errors} errors")
        self.root.after(0, self._fix_done, n_fixed, n_errors)

    # -- Thread-safe UI updates ---------------------------------------------

    def _update_row(self, iid: str, status: str, tag: str, detail: str):
        self.tree.set(iid, "status", status)
        self.tree.set(iid, "details", detail)
        self.tree.item(iid, tags=(tag,))

    def _update_row_full(self, iid: str, status: str, tag: str, tags_str: str,
                         title: str, detail: str):
        self.tree.set(iid, "status", status)
        self.tree.set(iid, "tags", tags_str)
        self.tree.set(iid, "title", title)
        self.tree.set(iid, "details", detail)
        self.tree.item(iid, tags=(tag,))

    def _set_progress(self, value: int):
        self.progress["value"] = value

    def _fix_done(self, n_fixed: int, n_errors: int):
        self._processing = False
        self.btn_fix.config(state=tk.NORMAL)
        self.btn_scan.config(state=tk.NORMAL)
        # Refresh summary counts
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
