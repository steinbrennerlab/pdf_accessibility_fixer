"""Core PDF accessibility logic."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import ocrmypdf
import pikepdf
from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTTextBox, LTTextLine


STRATEGY_AUTO = "Auto (font size)"
STRATEGY_FIRST_LINE = "First line = H1"
STRATEGY_BOLD = "Bold text = headings"
STRATEGIES = [STRATEGY_AUTO, STRATEGY_FIRST_LINE, STRATEGY_BOLD]


@dataclass(frozen=True)
class TextLine:
    text: str
    font_name: str
    font_size: float
    y: int
    is_bold: bool


@dataclass(frozen=True)
class Heading:
    page: int
    level: int
    text: str
    font_size: float
    font_name: str


@dataclass(frozen=True)
class PdfInfo:
    has_mark_info: bool
    has_struct_tree: bool
    has_headings: bool
    has_good_title: bool
    current_title: str
    has_text: bool
    page_count: int

    def issues(self) -> list[str]:
        issues: list[str] = []
        if not self.has_mark_info:
            issues.append("no MarkInfo")
        if not self.has_struct_tree:
            issues.append("no StructTreeRoot")
        elif not self.has_headings:
            issues.append("no headings with text")
        if not self.has_good_title:
            issues.append("bad/missing title")
        if not self.has_text:
            issues.append("image-only (needs OCR)")
        return issues

    def tags_summary(self) -> str:
        parts: list[str] = []
        if self.has_mark_info:
            parts.append("MarkInfo")
        if self.has_struct_tree:
            parts.append("StructTree")
        if self.has_headings:
            parts.append("Headings")
        return " ".join(parts) or "None"


def derive_title(filename: str) -> str:
    return Path(filename).stem.replace("_", " ")


def _as_pdf_list(value):
    if value is None:
        return []
    if isinstance(value, pikepdf.Array):
        return list(value)
    return [value]


def _extract_all_text_lines(path: Path) -> dict[int, list[TextLine]]:
    """Extract text lines with font info from all pages in a single pass."""
    page_lines: dict[int, list[TextLine]] = {}
    try:
        for page_idx, page_layout in enumerate(
            extract_pages(str(path), laparams=LAParams())
        ):
            lines: list[TextLine] = []
            for elem in page_layout:
                if not isinstance(elem, LTTextBox):
                    continue
                for line in elem:
                    if not isinstance(line, LTTextLine):
                        continue
                    text = line.get_text().strip()
                    if not text or len(text) < 2:
                        continue
                    font_name = None
                    font_size = None
                    for char in line:
                        if isinstance(char, LTChar):
                            font_name = char.fontname
                            font_size = round(char.size, 1)
                            break
                    if font_size is None or font_size < 5:
                        continue
                    lines.append(
                        TextLine(
                            text=text,
                            font_name=font_name or "",
                            font_size=font_size,
                            y=round(line.y0),
                            is_bold=bool(
                                font_name
                                and ("Bold" in font_name or "bold" in font_name)
                            ),
                        )
                    )
            page_lines[page_idx] = lines
    except Exception:
        pass
    return page_lines


def _heading_from_line(page: int, level: int, line: TextLine) -> Heading:
    return Heading(
        page=page,
        level=level,
        text=line.text,
        font_size=line.font_size,
        font_name=line.font_name,
    )


def _detect_first_line_headings(page_lines: dict[int, list[TextLine]]) -> list[Heading]:
    headings: list[Heading] = []
    for page in sorted(page_lines):
        lines = page_lines[page]
        if not lines:
            continue
        top_line = max(lines, key=lambda item: item.y)
        level = 1 if page == 0 else 2
        headings.append(_heading_from_line(page, level, top_line))
    return headings


def _detect_bold_headings(
    page_lines: dict[int, list[TextLine]],
    *,
    min_font_size: float | None = None,
) -> list[Heading]:
    headings: list[Heading] = []
    for page in sorted(page_lines):
        for line in page_lines[page]:
            if not line.is_bold:
                continue
            if min_font_size is not None and line.font_size < min_font_size:
                continue
            headings.append(_heading_from_line(page, 2, line))

    if headings:
        first = headings[0]
        headings[0] = Heading(
            page=first.page,
            level=1,
            text=first.text,
            font_size=first.font_size,
            font_name=first.font_name,
        )

    return headings


def detect_headings(path: Path, strategy: str = STRATEGY_AUTO) -> list[Heading]:
    """Detect headings across all pages."""
    page_lines = _extract_all_text_lines(path)
    headings: list[Heading] = []

    if strategy == STRATEGY_AUTO:
        all_sizes = [line.font_size for lines in page_lines.values() for line in lines]
        if not all_sizes:
            return []

        body_size = Counter(all_sizes).most_common(1)[0][0]
        heading_sizes = sorted(
            {size for size in all_sizes if size >= body_size * 1.2},
            reverse=True,
        )
        if heading_sizes:
            size_to_level = {
                size: min(index + 1, 3)
                for index, size in enumerate(heading_sizes)
            }

            for page in sorted(page_lines):
                for line in page_lines[page]:
                    level = size_to_level.get(line.font_size)
                    if level is not None:
                        headings.append(_heading_from_line(page, level, line))

            return headings

        total_lines = sum(len(lines) for lines in page_lines.values())
        bold_headings = _detect_bold_headings(page_lines, min_font_size=body_size)
        if bold_headings and len(bold_headings) <= max(1, total_lines // 2):
            return bold_headings

        return _detect_first_line_headings(page_lines)

    elif strategy == STRATEGY_FIRST_LINE:
        return _detect_first_line_headings(page_lines)

    elif strategy == STRATEGY_BOLD:
        return _detect_bold_headings(page_lines)

    return headings


def _struct_tree_is_empty(root) -> bool:
    struct_tree = root.get("/StructTreeRoot")
    if struct_tree is None:
        return True
    children = struct_tree.get("/K")
    if children is None:
        return True
    return isinstance(children, pikepdf.Array) and len(children) == 0


def _has_heading_with_text(root) -> bool:
    """Check if the structure tree has heading elements with /ActualText."""
    struct_tree = root.get("/StructTreeRoot")
    if struct_tree is None:
        return False

    heading_names = {"/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}
    try:
        for document in _as_pdf_list(struct_tree.get("/K")):
            if not isinstance(document, pikepdf.Dictionary):
                continue
            for section in _as_pdf_list(document.get("/K")):
                if not isinstance(section, pikepdf.Dictionary):
                    continue
                for child in _as_pdf_list(section.get("/K")):
                    if not isinstance(child, pikepdf.Dictionary):
                        continue
                    tag_name = str(child.get("/S", ""))
                    if tag_name in heading_names and "/ActualText" in child:
                        return True
    except Exception:
        pass
    return False


def _page_has_text(page) -> bool:
    if "/Contents" not in page:
        return False

    contents = page["/Contents"]
    try:
        if isinstance(contents, pikepdf.Array):
            raw_bytes = b"".join(stream.read_bytes() for stream in contents)
        else:
            raw_bytes = contents.read_bytes()
    except Exception:
        return False
    return b"BT" in raw_bytes


def inspect_pdf(path: Path) -> PdfInfo:
    with pikepdf.open(path) as pdf:
        root = pdf.Root

        try:
            title_raw = str(pdf.docinfo.get("/Title", ""))
        except Exception:
            title_raw = ""

        page_count = len(pdf.pages)
        sample_indices = sorted({0, page_count // 2, page_count - 1} & set(range(page_count)))
        has_text = any(_page_has_text(pdf.pages[index]) for index in sample_indices)
        has_good_title = bool(title_raw.strip()) and ".." not in title_raw

        return PdfInfo(
            has_mark_info="/MarkInfo" in root,
            has_struct_tree=not _struct_tree_is_empty(root),
            has_headings=_has_heading_with_text(root),
            has_good_title=has_good_title,
            current_title=title_raw,
            has_text=has_text,
            page_count=page_count,
        )


def add_tags_if_missing(
    path: Path,
    title: str,
    strategy: str = STRATEGY_AUTO,
) -> list[str]:
    """Add MarkInfo, structure tree with real headings, and title."""
    changes: list[str] = []
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        root = pdf.Root

        if "/MarkInfo" not in root:
            root["/MarkInfo"] = pikepdf.Dictionary({"/Marked": True})
            changes.append("Added /MarkInfo{Marked:true}")

        needs_struct = _struct_tree_is_empty(root) or not _has_heading_with_text(root)
        if needs_struct:
            headings = detect_headings(path, strategy)
            headings_by_page: dict[int, list[Heading]] = {}
            for heading in headings:
                headings_by_page.setdefault(heading.page, []).append(heading)

            struct_root = pdf.make_indirect(
                pikepdf.Dictionary({"/Type": pikepdf.Name("/StructTreeRoot")})
            )
            root["/StructTreeRoot"] = struct_root

            document_elem = pdf.make_indirect(
                pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/StructElem"),
                        "/S": pikepdf.Name("/Document"),
                        "/P": struct_root,
                        "/K": pikepdf.Array([]),
                    }
                )
            )

            page_elems = []
            parent_tree_nums = []

            for page_idx, page in enumerate(pdf.pages):
                section_elem = pdf.make_indirect(
                    pikepdf.Dictionary(
                        {
                            "/Type": pikepdf.Name("/StructElem"),
                            "/S": pikepdf.Name("/Sect"),
                            "/P": document_elem,
                        }
                    )
                )

                section_children = []

                for heading in headings_by_page.get(page_idx, []):
                    heading_elem = pdf.make_indirect(
                        pikepdf.Dictionary(
                            {
                                "/Type": pikepdf.Name("/StructElem"),
                                "/S": pikepdf.Name(f"/H{heading.level}"),
                                "/P": section_elem,
                                "/ActualText": pikepdf.String(heading.text),
                            }
                        )
                    )
                    section_children.append(heading_elem)

                paragraph_elem = pdf.make_indirect(
                    pikepdf.Dictionary(
                        {
                            "/Type": pikepdf.Name("/StructElem"),
                            "/S": pikepdf.Name("/P"),
                            "/P": section_elem,
                            "/K": pikepdf.Dictionary(
                                {
                                    "/Type": pikepdf.Name("/MCR"),
                                    "/Pg": page.obj,
                                    "/MCID": 0,
                                }
                            ),
                        }
                    )
                )
                section_children.append(paragraph_elem)

                section_elem["/K"] = pikepdf.Array(section_children)
                page_elems.append(section_elem)

                if "/Contents" in page:
                    contents = page["/Contents"]
                    old_streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
                    bdc = pdf.make_stream(b"/P <</MCID 0>> BDC\n")
                    emc = pdf.make_stream(b"\nEMC\n")
                    page["/Contents"] = pikepdf.Array([bdc] + old_streams + [emc])

                page.obj["/StructParents"] = page_idx
                parent_tree_nums.append(page_idx)
                parent_tree_nums.append(pikepdf.Array([paragraph_elem]))

            document_elem["/K"] = pikepdf.Array(page_elems)
            struct_root["/K"] = pikepdf.Array([document_elem])
            struct_root["/ParentTree"] = pdf.make_indirect(
                pikepdf.Dictionary({"/Nums": pikepdf.Array(parent_tree_nums)})
            )
            struct_root["/ParentTreeNextKey"] = len(pdf.pages)

            heading_count = sum(len(page_headings) for page_headings in headings_by_page.values())
            changes.append(
                f"Built StructTreeRoot: {len(pdf.pages)} pages, "
                f"{heading_count} headings detected ({strategy})"
            )
            for heading in headings[:10]:
                changes.append(
                    f"  H{heading.level} p{heading.page + 1}: "
                    f"{heading.text[:60]} ({heading.font_size}pt {heading.font_name})"
                )
            if len(headings) > 10:
                changes.append(f"  ... and {len(headings) - 10} more")

        with pdf.open_metadata() as meta:
            meta["dc:title"] = title
        pdf.docinfo["/Title"] = title
        changes.append(f"Set title to '{title}'")

        pdf.save(path)
    return changes


def _run_ocr(
    input_path: Path,
    output_path: Path,
    title: str,
    has_text: bool,
    log_message: Callable[[str], None] | None = None,
) -> str:
    """Run OCRmyPDF, falling back to plain PDF on color-space errors."""
    ocr_flag = {"redo_ocr": True} if has_text else {"force_ocr": True}
    mode = "redo-ocr" if has_text else "force-ocr"

    try:
        ocrmypdf.ocr(input_path, output_path, output_type="pdfa-2", title=title, **ocr_flag)
    except ocrmypdf.exceptions.ColorConversionNeededError:
        if log_message is not None:
            log_message("    Color space issue - retrying with output_type=pdf")
        ocrmypdf.ocr(input_path, output_path, output_type="pdf", title=title, **ocr_flag)
        mode += " (skipped PDF/A - unusual color space)"

    return mode


def fix_pdf(
    input_path: Path,
    output_path: Path,
    info: PdfInfo,
    title: str,
    strategy: str = STRATEGY_AUTO,
    log_message: Callable[[str], None] | None = None,
) -> str:
    mode = _run_ocr(input_path, output_path, title, info.has_text, log_message)
    add_tags_if_missing(output_path, title, strategy)
    return mode


def verify_output(path: Path, expected_title: str) -> list[str]:
    errors: list[str] = []
    info = inspect_pdf(path)
    if not info.has_mark_info:
        errors.append("Missing /MarkInfo")
    if not info.has_struct_tree:
        errors.append("Missing /StructTreeRoot")
    if not info.has_headings:
        errors.append("Missing headings with /ActualText")
    if not info.has_text:
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
