"""Application workflow for PDF accessibility processing."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from accessibility_core import (
    PdfInfo,
    STRATEGY_AUTO,
    detect_headings,
    derive_title,
    fix_pdf,
    inspect_pdf,
    verify_output,
)


BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "updated"
LOG_FILE = BASE_DIR / "accessibility_log.txt"
KNOWN_GOOD = frozenset({"Wk1_Janeway_Ch1_Sec1-5.pdf"})

S_COMPLIANT = "Compliant"
S_NEEDS_FIX = "Needs Fix"
S_PROCESSING = "Processing..."
S_FIXED = "Fixed"
S_ERROR = "Error"

ROW_TAG_BY_STATUS = {
    S_COMPLIANT: "compliant",
    S_NEEDS_FIX: "needs_fix",
    S_PROCESSING: "processing",
    S_FIXED: "fixed",
    S_ERROR: "error",
}


@dataclass(frozen=True)
class ScanResult:
    filename: str
    source_path: Path
    display_path: Path
    info: PdfInfo
    status: str
    detail: str
    known_good: bool = False
    checked_existing_output: bool = False
    from_updated_output: bool = False


@dataclass(frozen=True)
class ScanSummary:
    total: int
    compliant: int
    needs_fix: int

    @classmethod
    def from_results(cls, results: Sequence[ScanResult]) -> "ScanSummary":
        compliant = sum(
            1 for result in results if result.status in {S_COMPLIANT, S_FIXED}
        )
        return cls(
            total=len(results),
            compliant=compliant,
            needs_fix=len(results) - compliant,
        )


@dataclass(frozen=True)
class FixResult:
    title: str
    output_path: Path
    mode: str
    errors: tuple[str, ...] = ()
    info: PdfInfo | None = None
    error_detail: str | None = None


def row_tag_for_status(status: str) -> str:
    return ROW_TAG_BY_STATUS[status]


def requested_ocr_mode(info: PdfInfo) -> str:
    return "redo-ocr" if info.has_text else "force-ocr"


def describe_fix_errors(
    input_path: Path,
    strategy: str,
    errors: Sequence[str],
) -> str:
    if not errors:
        return ""

    parts = [f"Verification failed: {'; '.join(errors)}"]
    if "Missing headings with /ActualText" in errors:
        try:
            heading_count = len(detect_headings(input_path, strategy))
        except Exception:
            heading_count = None

        if heading_count == 0:
            parts.append(f"{strategy} found no heading candidates")
        elif heading_count is not None:
            label = "candidate" if heading_count == 1 else "candidates"
            parts.append(f"{strategy} found {heading_count} heading {label}")

    return " | ".join(parts)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(f"[{ts}]  {msg}\n")


def log_section(title: str) -> None:
    log(f"{'=' * 60}")
    log(f"  {title}")
    log(f"{'=' * 60}")


def log_scan_result(result: ScanResult) -> None:
    info = result.info
    log(f"INSPECTED: {result.filename}")
    log(f"    Pages:          {info.page_count}")
    log(f"    Has text:       {info.has_text}")
    log(f"    MarkInfo:       {info.has_mark_info}")
    log(f"    StructTreeRoot: {info.has_struct_tree}")
    log(f"    Headings:       {info.has_headings}")
    log(f"    Title:          {info.current_title!r}")
    log(f"    Good title:     {info.has_good_title}")

    if result.known_good:
        log(f"    KNOWN_GOOD: {result.filename} - already compliant, skipped")
    elif result.from_updated_output:
        log("    STATUS: Previously fixed - verified output in updated/")
    elif result.checked_existing_output:
        log(f"    STATUS: Needs re-fix - output failed: {result.detail}")
    elif result.status == S_COMPLIANT:
        log("    STATUS: Already compliant - no fixes needed")
    else:
        log(f"    STATUS: Needs fix - {result.detail}")


def log_scan_summary(summary: ScanSummary) -> None:
    log(
        f"SCAN SUMMARY: {summary.total} PDFs - {summary.compliant} compliant, "
        f"{summary.needs_fix} need fixing"
    )


def scan_pdf(
    pdf_path: Path,
    *,
    output_dir: Path | None = None,
    known_good: Collection[str] | None = None,
) -> ScanResult:
    filename = pdf_path.name
    info = inspect_pdf(pdf_path)
    issues = info.issues()
    known_good_names = KNOWN_GOOD if known_good is None else known_good
    is_known_good = filename in known_good_names

    if is_known_good or not issues:
        detail = "Known good" if is_known_good else "All checks pass"
        return ScanResult(
            filename=filename,
            source_path=pdf_path,
            display_path=pdf_path,
            info=info,
            status=S_COMPLIANT,
            detail=detail,
            known_good=is_known_good,
        )

    actual_output_dir = OUTPUT_DIR if output_dir is None else output_dir
    output_path = actual_output_dir / filename
    if output_path.exists():
        title = derive_title(filename)
        errors = verify_output(output_path, title)
        if not errors:
            output_info = inspect_pdf(output_path)
            return ScanResult(
                filename=filename,
                source_path=pdf_path,
                display_path=output_path,
                info=output_info,
                status=S_FIXED,
                detail="Previously fixed (in updated/)",
                checked_existing_output=True,
                from_updated_output=True,
            )

        return ScanResult(
            filename=filename,
            source_path=pdf_path,
            display_path=pdf_path,
            info=info,
            status=S_NEEDS_FIX,
            detail="; ".join(errors),
            checked_existing_output=True,
        )

    return ScanResult(
        filename=filename,
        source_path=pdf_path,
        display_path=pdf_path,
        info=info,
        status=S_NEEDS_FIX,
        detail="; ".join(issues),
    )


def scan_folder(
    base_dir: Path = BASE_DIR,
    *,
    output_dir: Path | None = None,
    known_good: Collection[str] | None = None,
) -> tuple[list[ScanResult], ScanSummary]:
    actual_output_dir = base_dir / "updated" if output_dir is None else output_dir
    actual_output_dir.mkdir(exist_ok=True)

    results = [
        scan_pdf(
            pdf_path,
            output_dir=actual_output_dir,
            known_good=known_good,
        )
        for pdf_path in sorted(base_dir.glob("*.pdf"))
    ]
    return results, ScanSummary.from_results(results)


def process_pdf_fix(
    input_path: Path,
    info: PdfInfo,
    strategy: str = STRATEGY_AUTO,
    *,
    output_dir: Path = OUTPUT_DIR,
) -> FixResult:
    output_dir.mkdir(exist_ok=True)
    title = derive_title(input_path.name)
    output_path = output_dir / input_path.name
    mode = fix_pdf(
        input_path,
        output_path,
        info,
        title,
        strategy,
        log_message=log,
    )
    errors = tuple(verify_output(output_path, title))
    if errors:
        return FixResult(
            title=title,
            output_path=output_path,
            mode=mode,
            errors=errors,
            error_detail=describe_fix_errors(input_path, strategy, errors),
        )
    return FixResult(
        title=title,
        output_path=output_path,
        mode=mode,
        info=inspect_pdf(output_path),
    )
