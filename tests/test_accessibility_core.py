from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import accessibility_core as core


class PdfInfoTests(unittest.TestCase):
    def test_tags_summary_and_issues_cover_missing_fields(self):
        info = core.PdfInfo(
            has_mark_info=False,
            has_struct_tree=False,
            has_headings=False,
            has_good_title=False,
            current_title="",
            has_text=False,
            page_count=3,
        )

        self.assertEqual(info.tags_summary(), "None")
        self.assertEqual(
            info.issues(),
            [
                "no MarkInfo",
                "no StructTreeRoot",
                "bad/missing title",
                "image-only (needs OCR)",
            ],
        )

    def test_tags_summary_includes_struct_tree_and_headings(self):
        info = core.PdfInfo(
            has_mark_info=True,
            has_struct_tree=True,
            has_headings=True,
            has_good_title=True,
            current_title="Sample",
            has_text=True,
            page_count=2,
        )

        self.assertEqual(info.tags_summary(), "MarkInfo StructTree Headings")
        self.assertEqual(info.issues(), [])


class FixPdfTests(unittest.TestCase):
    def test_fix_pdf_forwards_logger_to_ocr_step(self):
        info = core.PdfInfo(False, False, False, False, "", True, 4)

        with patch.object(core, "_run_ocr", return_value="redo-ocr") as run_ocr, patch.object(
            core, "add_tags_if_missing"
        ):
            logger = Mock()
            mode = core.fix_pdf(
                input_path=Path("input.pdf"),
                output_path=Path("output.pdf"),
                info=info,
                title="input",
                log_message=logger,
            )

        self.assertEqual(mode, "redo-ocr")
        run_ocr.assert_called_once_with(
            Path("input.pdf"),
            Path("output.pdf"),
            "input",
            True,
            logger,
        )


class DetectHeadingsTests(unittest.TestCase):
    def test_auto_falls_back_to_bold_candidates_when_font_sizes_do_not_separate(self):
        page_lines = {
            0: [
                core.TextLine("Course Title", "Arial-Bold", 10.0, 700, True),
                core.TextLine("Body text", "Arial", 10.0, 680, False),
                core.TextLine("Section Header", "Arial-Bold", 11.0, 660, True),
                core.TextLine("Glossary Term", "Arial-Bold", 9.0, 640, True),
            ]
        }

        with patch.object(core, "_extract_all_text_lines", return_value=page_lines):
            headings = core.detect_headings(Path("sample.pdf"), core.STRATEGY_AUTO)

        self.assertEqual(
            headings,
            [
                core.Heading(0, 1, "Course Title", 10.0, "Arial-Bold"),
                core.Heading(0, 2, "Section Header", 11.0, "Arial-Bold"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
