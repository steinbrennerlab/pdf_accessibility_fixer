"""Tkinter GUI for the PDF accessibility fixer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk

from accessibility_core import (
    STRATEGIES,
    STRATEGY_AUTO,
    Heading,
    PdfInfo,
    derive_title,
    detect_headings,
)
from accessibility_workflow import (
    LOG_FILE,
    S_COMPLIANT,
    S_ERROR,
    S_FIXED,
    S_NEEDS_FIX,
    S_PROCESSING,
    ScanResult,
    log,
    log_scan_result,
    log_scan_summary,
    log_section,
    process_pdf_fix,
    requested_ocr_mode,
    row_tag_for_status,
    scan_folder,
)


@dataclass
class FileEntry:
    name: str
    path: Path
    info: PdfInfo
    status: str
    detail: str
    iid: str | None = None

    @property
    def tags_text(self) -> str:
        return self.info.tags_summary()

    @property
    def title(self) -> str:
        return self.info.current_title

    @property
    def row_tag(self) -> str:
        return row_tag_for_status(self.status)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PDF Accessibility Fixer")
        self.root.geometry("960x700")
        self.root.minsize(800, 550)

        self._file_data: dict[str, FileEntry] = {}
        self._heading_request_id = 0
        self._processing = False

        self._build_ui()
        self.root.after(100, self.scan)

    def _build_ui(self):
        btn_frame = ttk.Frame(self.root, padding=6)
        btn_frame.pack(fill=tk.X)

        self.btn_scan = ttk.Button(btn_frame, text="Scan Folder", command=self.scan)
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_fix = ttk.Button(
            btn_frame,
            text="Fix Selected",
            command=self._start_fix_selected,
        )
        self.btn_fix.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_log = ttk.Button(btn_frame, text="Open Log", command=self._open_log)
        self.btn_log.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(btn_frame, text="Heading method:").pack(side=tk.LEFT, padx=(0, 4))
        self.strategy_var = tk.StringVar(value=STRATEGY_AUTO)
        self.strategy_combo = ttk.Combobox(
            btn_frame,
            textvariable=self.strategy_var,
            values=STRATEGIES,
            state="readonly",
            width=20,
        )
        self.strategy_combo.pack(side=tk.LEFT, padx=(0, 4))

        self.lbl_summary = ttk.Label(btn_frame, text="", font=("Segoe UI", 9))
        self.lbl_summary.pack(side=tk.RIGHT)

        paned = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 0))

        file_frame = ttk.Frame(paned)
        paned.add(file_frame, weight=3)

        cols = ("pages", "status", "tags", "title", "details")
        self.tree = ttk.Treeview(
            file_frame,
            columns=cols,
            show="headings",
            selectmode="browse",
        )
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
        self.tree.bind("<<TreeviewSelect>>", self._on_file_select)

        heading_frame = ttk.LabelFrame(
            paned,
            text="Detected Heading Structure",
            padding=4,
        )
        paned.add(heading_frame, weight=2)

        h_cols = ("level", "font", "text")
        self.h_tree = ttk.Treeview(
            heading_frame,
            columns=h_cols,
            show="tree headings",
            selectmode="none",
            height=8,
        )
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
        self.h_tree.tag_configure("error", foreground="#721c24", background="#f8d7da")

        h_vsb = ttk.Scrollbar(heading_frame, orient=tk.VERTICAL, command=self.h_tree.yview)
        self.h_tree.configure(yscrollcommand=h_vsb.set)
        self.h_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        h_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill=tk.X, padx=6, pady=6)

    def _start_thread(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    def _queue(self, callback, *args):
        self.root.after(0, callback, *args)

    @staticmethod
    def _clear_tree(tree: ttk.Treeview):
        tree.delete(*tree.get_children())

    def _set_processing(self, is_processing: bool):
        self._processing = is_processing
        state = tk.DISABLED if is_processing else tk.NORMAL
        self.btn_scan.config(state=state)
        self.btn_fix.config(state=state)

    def _invalidate_heading_view(self):
        self._heading_request_id += 1
        self._clear_tree(self.h_tree)

    def _selected_filename(self) -> str | None:
        selection = self.tree.selection()
        if not selection:
            return None
        filename = self.tree.item(selection[0], "text")
        return filename or None

    def _selected_entry(self) -> FileEntry | None:
        filename = self._selected_filename()
        if not filename:
            return None
        return self._file_data.get(filename)

    def _render_entry(self, entry: FileEntry):
        if entry.iid is None:
            return
        self.tree.set(entry.iid, "pages", entry.info.page_count)
        self.tree.set(entry.iid, "status", entry.status)
        self.tree.set(entry.iid, "tags", entry.tags_text)
        self.tree.set(entry.iid, "title", entry.title)
        self.tree.set(entry.iid, "details", entry.detail)
        self.tree.item(entry.iid, tags=(entry.row_tag,))

    def _count_statuses(self) -> tuple[int, int]:
        compliant_statuses = {S_COMPLIANT, S_FIXED}
        compliant = sum(1 for entry in self._file_data.values() if entry.status in compliant_statuses)
        remaining = sum(1 for entry in self._file_data.values() if entry.status == S_NEEDS_FIX)
        return compliant, remaining

    def _on_file_select(self, _event=None):
        self._refresh_heading_view()

    def _refresh_heading_view(self):
        entry = self._selected_entry()
        if entry is None:
            self._invalidate_heading_view()
            return

        strategy = self.strategy_var.get()
        self._heading_request_id += 1
        request_id = self._heading_request_id

        def detect():
            try:
                headings = detect_headings(entry.path, strategy)
                self._queue(self._populate_heading_tree, request_id, entry.name, headings)
            except Exception as exc:
                self._queue(
                    self._show_detail_error,
                    request_id,
                    entry.name,
                    f"{type(exc).__name__}: {exc}",
                )

        self._start_thread(detect)

    def _populate_heading_tree(self, request_id: int, filename: str, headings: list[Heading]):
        if request_id != self._heading_request_id:
            return
        if filename != self._selected_filename():
            return

        self._clear_tree(self.h_tree)
        for heading in headings:
            tag = f"h{min(heading.level, 3)}"
            self.h_tree.insert(
                "",
                tk.END,
                text=f"Page {heading.page + 1}",
                values=(
                    f"H{heading.level}",
                    f"{heading.font_size}pt {heading.font_name}",
                    heading.text,
                ),
                tags=(tag,),
            )

    def _show_detail_error(self, request_id: int, filename: str, message: str):
        if request_id != self._heading_request_id:
            return
        if filename != self._selected_filename():
            return

        self._clear_tree(self.h_tree)
        self.h_tree.insert(
            "",
            tk.END,
            text="Error",
            values=("", "", message),
            tags=("error",),
        )

    def scan(self):
        if self._processing:
            return

        self._set_processing(True)
        self._invalidate_heading_view()
        self._clear_tree(self.tree)
        self._file_data.clear()
        self.lbl_summary.config(text="Scanning...")
        self._start_thread(self._scan_worker)

    def _scan_worker(self):
        log_section("SCAN SESSION STARTED")

        results, summary = scan_folder()
        if not results:
            log("No PDF files found in folder.")
            self._queue(self._scan_done, summary.total, summary.compliant, summary.needs_fix)
            return

        for result in results:
            log_scan_result(result)
            self._queue(self._scan_add_row, result)

        log_scan_summary(summary)
        self._queue(self._scan_done, summary.total, summary.compliant, summary.needs_fix)

    def _scan_add_row(self, result: ScanResult):
        entry = FileEntry(
            name=result.filename,
            path=result.display_path,
            info=result.info,
            status=result.status,
            detail=result.detail,
        )
        entry.iid = self.tree.insert(
            "",
            tk.END,
            text=entry.name,
            values=(
                entry.info.page_count,
                entry.status,
                entry.tags_text,
                entry.title,
                entry.detail,
            ),
            tags=(entry.row_tag,),
        )
        self._file_data[entry.name] = entry

    def _scan_done(self, total: int, compliant: int, needs_fix: int):
        if total == 0:
            self.lbl_summary.config(text="No PDFs found.")
        else:
            self.tree["displaycolumns"] = ("pages", "status", "tags", "title", "details")
            self.tree["show"] = ("tree", "headings")
            self.tree.heading("#0", text="File")
            self.tree.column("#0", width=250, stretch=False)
            self.lbl_summary.config(text=f"{compliant} compliant, {needs_fix} need fixing")

        self._set_processing(False)

    def _start_fix_selected(self):
        if self._processing:
            return

        entry = self._selected_entry()
        if entry is None or entry.status != S_NEEDS_FIX:
            return

        self._set_processing(True)
        self.progress["maximum"] = 1
        self.progress["value"] = 0

        strategy = self.strategy_var.get()
        log_section("FIX SESSION STARTED")
        log(f"File: {entry.name}")
        log(f"Heading strategy: {strategy}")

        self._start_thread(self._fix_worker, [entry], strategy)

    def _fix_worker(self, entries: list[FileEntry], strategy: str):
        fixed = 0
        errors = 0

        for index, entry in enumerate(entries, start=1):
            self._queue(self._update_entry, entry.name, S_PROCESSING, "Working...", None, None)

            requested_mode = requested_ocr_mode(entry.info)
            log(f"PROCESSING: {entry.name}")
            log(f"    Mode:     {requested_mode}")
            log(f"    Strategy: {strategy}")
            log(f"    Title:    '{derive_title(entry.name)}'")
            log(f"    Pages:    {entry.info.page_count}")

            started = time.time()
            try:
                result = process_pdf_fix(entry.path, entry.info, strategy)
                elapsed = time.time() - started
                log(f"    Completed in {elapsed:.1f}s (mode={result.mode})")

                if result.errors:
                    detail = "; ".join(result.errors)
                    log(f"    VERIFY FAILED: {detail}")
                    self._queue(self._update_entry, entry.name, S_ERROR, detail, None, None)
                    errors += 1
                else:
                    detail = f"Fixed in {elapsed:.1f}s - {result.mode}"
                    log("    VERIFY OK: all checks pass")
                    log(f"    Output: {result.output_path.stat().st_size / 1024:.0f} KB")
                    self._queue(
                        self._update_entry,
                        entry.name,
                        S_FIXED,
                        detail,
                        result.info,
                        result.output_path,
                    )
                    fixed += 1
            except Exception as exc:
                elapsed = time.time() - started
                log(f"    ERROR after {elapsed:.1f}s: {exc}")
                log(f"    TRACEBACK:\n{traceback.format_exc()}")
                self._queue(
                    self._update_entry,
                    entry.name,
                    S_ERROR,
                    str(exc)[:120],
                    None,
                    None,
                )
                errors += 1

            self._queue(self._set_progress, index)

        log(f"FIX SESSION COMPLETE: {fixed} fixed, {errors} errors")
        self._queue(self._fix_done, fixed, errors)

    def _update_entry(
        self,
        filename: str,
        status: str,
        detail: str,
        info: PdfInfo | None,
        path: Path | None,
    ):
        entry = self._file_data.get(filename)
        if entry is None:
            return

        entry.status = status
        entry.detail = detail
        if info is not None:
            entry.info = info
        if path is not None:
            entry.path = path

        self._render_entry(entry)
        if path is not None and filename == self._selected_filename():
            self._refresh_heading_view()

    def _set_progress(self, value: int):
        self.progress["value"] = value

    def _fix_done(self, fixed: int, errors: int):
        compliant, remaining = self._count_statuses()
        self.lbl_summary.config(
            text=(
                f"{compliant} compliant, {remaining} need fixing  |  "
                f"Last run: {fixed} fixed, {errors} errors"
            )
        )
        self._set_processing(False)

    def _open_log(self):
        if not LOG_FILE.exists():
            return
        if sys.platform == "win32":
            os.startfile(LOG_FILE)
        else:
            subprocess.Popen(["xdg-open", str(LOG_FILE)])


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
