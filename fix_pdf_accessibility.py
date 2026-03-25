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
            "has_struct_tree": "/StructTreeRoot" in root,
            "has_good_title": is_good_title,
            "current_title": title_raw,
            "has_text": has_text,
            "page_count": n,
        }


def add_tags_if_missing(path: Path, title: str) -> list:
    """Add MarkInfo, StructTreeRoot, and title. Returns list of changes made."""
    changes = []
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        root = pdf.Root

        if "/MarkInfo" not in root:
            root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
            changes.append("Added /MarkInfo{Marked:true}")

        if "/StructTreeRoot" not in root:
            struct_root = pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructTreeRoot"),
                "/K": pikepdf.Array([]),
                "/ParentTree": pikepdf.Dictionary({"/Nums": pikepdf.Array([])}),
                "/ParentTreeNextKey": 0,
            })
            root["/StructTreeRoot"] = pdf.make_indirect(struct_root)
            changes.append("Added /StructTreeRoot")

        with pdf.open_metadata() as meta:
            meta["dc:title"] = title
        pdf.docinfo["/Title"] = title
        changes.append(f"Set title to '{title}'")

        pdf.save(path)
    return changes


def fix_pdf(input_path: Path, output_path: Path, info: dict, title: str) -> str:
    """Apply accessibility fixes. Returns the OCR mode used."""
    kwargs = dict(output_type="pdfa-2", title=title)
    if info["has_text"]:
        kwargs["skip_text"] = True
        mode = "skip-text"
    else:
        kwargs["force_ocr"] = True
        mode = "force-ocr"

    ocrmypdf.ocr(input_path, output_path, **kwargs)

    add_tags_if_missing(output_path, title)
    return mode


def verify_output(path: Path, expected_title: str) -> list:
    errors = []
    info = inspect_pdf(path)
    if not info["has_mark_info"]:
        errors.append("Missing /MarkInfo")
    if not info["has_struct_tree"]:
        errors.append("Missing /StructTreeRoot")
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
            if not info["has_good_title"]:
                issues.append("bad/missing title")
            if not info["has_text"]:
                issues.append("image-only (needs OCR)")

            tags_str = ""
            if info["has_mark_info"]:
                tags_str += "MarkInfo "
            if info["has_struct_tree"]:
                tags_str += "StructTree"
            if not tags_str:
                tags_str = "None"

            # Log full inspection
            log(f"INSPECTED: {fname}")
            log(f"    Pages:          {info['page_count']}")
            log(f"    Has text:       {info['has_text']}")
            log(f"    MarkInfo:       {info['has_mark_info']}")
            log(f"    StructTreeRoot: {info['has_struct_tree']}")
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
                        tags_str += "StructTree"
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
            mode = "force-ocr" if not data["has_text"] else "skip-text"

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
                        tags_str += "StructTree"

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
