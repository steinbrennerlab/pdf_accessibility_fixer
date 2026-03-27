from pathlib import Path
import unittest
from unittest.mock import patch

import accessibility_core as core
import accessibility_workflow as flow


class ScanSummaryTests(unittest.TestCase):
    def test_from_results_counts_fixed_as_compliant(self):
        info = core.PdfInfo(True, True, True, True, "sample", True, 2)
        results = [
            flow.ScanResult("a.pdf", Path("a.pdf"), Path("a.pdf"), info, flow.S_COMPLIANT, "ok"),
            flow.ScanResult("b.pdf", Path("b.pdf"), Path("updated/b.pdf"), info, flow.S_FIXED, "fixed"),
            flow.ScanResult("c.pdf", Path("c.pdf"), Path("c.pdf"), info, flow.S_NEEDS_FIX, "needs fix"),
        ]

        summary = flow.ScanSummary.from_results(results)

        self.assertEqual(summary.total, 3)
        self.assertEqual(summary.compliant, 2)
        self.assertEqual(summary.needs_fix, 1)


class ScanPdfTests(unittest.TestCase):
    def test_scan_pdf_marks_known_good_as_compliant(self):
        source = Path("Wk1_Janeway_Ch1_Sec1-5.pdf")
        info = core.PdfInfo(True, False, False, False, "", True, 11)

        with patch.object(flow, "inspect_pdf", return_value=info):
            result = flow.scan_pdf(source, known_good={source.name})

        self.assertEqual(result.status, flow.S_COMPLIANT)
        self.assertTrue(result.known_good)
        self.assertEqual(result.display_path, source)

    def test_scan_pdf_prefers_verified_updated_output(self):
        source = Path("sample.pdf")
        output = flow.OUTPUT_DIR / source.name
        source_info = core.PdfInfo(False, False, False, False, "", True, 5)
        output_info = core.PdfInfo(True, True, True, True, "sample", True, 5)

        with patch.object(flow, "inspect_pdf", side_effect=[source_info, output_info]), patch.object(
            flow, "verify_output", return_value=[]
        ), patch("pathlib.Path.exists", return_value=True):
            result = flow.scan_pdf(source)

        self.assertEqual(result.status, flow.S_FIXED)
        self.assertTrue(result.checked_existing_output)
        self.assertTrue(result.from_updated_output)
        self.assertEqual(result.display_path, output)
        self.assertEqual(result.info, output_info)

    def test_scan_pdf_returns_needs_fix_when_existing_output_fails_verification(self):
        source = Path("sample.pdf")
        source_info = core.PdfInfo(False, False, False, False, "", True, 5)

        with patch.object(flow, "inspect_pdf", return_value=source_info), patch.object(
            flow, "verify_output", return_value=["Missing /MarkInfo"]
        ), patch("pathlib.Path.exists", return_value=True):
            result = flow.scan_pdf(source)

        self.assertEqual(result.status, flow.S_NEEDS_FIX)
        self.assertTrue(result.checked_existing_output)
        self.assertFalse(result.from_updated_output)
        self.assertEqual(result.display_path, source)
        self.assertEqual(result.detail, "Missing /MarkInfo")


class ScanFolderTests(unittest.TestCase):
    def test_scan_folder_uses_folder_updated_dir_and_summarizes_results(self):
        info = core.PdfInfo(True, True, True, True, "sample", True, 2)
        base_dir = Path("course")
        captured_calls = []

        def fake_scan_pdf(pdf_path: Path, *, output_dir: Path | None = None, known_good=None):
            captured_calls.append((pdf_path.name, output_dir))
            status = flow.S_COMPLIANT if pdf_path.name == "a.pdf" else flow.S_NEEDS_FIX
            return flow.ScanResult(
                filename=pdf_path.name,
                source_path=pdf_path,
                display_path=pdf_path,
                info=info,
                status=status,
                detail="ok",
            )

        with patch("pathlib.Path.glob", return_value=[base_dir / "b.pdf", base_dir / "a.pdf"]), patch(
            "pathlib.Path.mkdir"
        ) as mkdir, patch.object(flow, "scan_pdf", side_effect=fake_scan_pdf):
            results, summary = flow.scan_folder(base_dir)

        self.assertEqual([result.filename for result in results], ["a.pdf", "b.pdf"])
        self.assertEqual(
            captured_calls,
            [
                ("a.pdf", base_dir / "updated"),
                ("b.pdf", base_dir / "updated"),
            ],
        )
        mkdir.assert_called_once_with(exist_ok=True)
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.compliant, 1)
        self.assertEqual(summary.needs_fix, 1)


class DescribeFixErrorsTests(unittest.TestCase):
    def test_describe_fix_errors_explains_missing_heading_candidates(self):
        with patch.object(flow, "detect_headings", return_value=[]):
            detail = flow.describe_fix_errors(
                Path("sample.pdf"),
                core.STRATEGY_AUTO,
                ["Missing headings with /ActualText"],
            )

        self.assertEqual(
            detail,
            "Verification failed: Missing headings with /ActualText | "
            "Auto (font size) found no heading candidates",
        )


class ProcessPdfFixTests(unittest.TestCase):
    def test_process_pdf_fix_returns_verification_errors(self):
        info = core.PdfInfo(False, False, False, False, "", True, 4)
        source = Path("sample.pdf")

        with patch.object(flow, "fix_pdf", return_value="redo-ocr"), patch.object(
            flow, "verify_output", return_value=["Missing headings with /ActualText"]
        ), patch.object(
            flow, "describe_fix_errors",
            return_value="Verification failed: Missing headings with /ActualText",
        ):
            result = flow.process_pdf_fix(source, info)

        self.assertEqual(result.title, "sample")
        self.assertEqual(result.mode, "redo-ocr")
        self.assertEqual(result.errors, ("Missing headings with /ActualText",))
        self.assertEqual(
            result.error_detail,
            "Verification failed: Missing headings with /ActualText",
        )
        self.assertIsNone(result.info)

    def test_process_pdf_fix_returns_output_info_on_success(self):
        info = core.PdfInfo(False, False, False, False, "", True, 4)
        output_info = core.PdfInfo(True, True, True, True, "sample", True, 4)
        source = Path("sample.pdf")

        with patch.object(flow, "fix_pdf", return_value="redo-ocr"), patch.object(
            flow, "verify_output", return_value=[]
        ), patch.object(flow, "inspect_pdf", return_value=output_info):
            result = flow.process_pdf_fix(source, info)

        self.assertEqual(result.errors, ())
        self.assertEqual(result.info, output_info)
        self.assertEqual(result.output_path, flow.OUTPUT_DIR / source.name)
        self.assertIsNone(result.error_detail)


if __name__ == "__main__":
    unittest.main()
