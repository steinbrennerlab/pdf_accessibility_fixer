import unittest
from pathlib import Path

import accessibility_core as core
import accessibility_gui as gui


class FakeTree:
    def __init__(self):
        self.rows = []

    def get_children(self):
        return [str(index) for index in range(len(self.rows))]

    def delete(self, *items):
        self.rows = []

    def insert(self, parent, index, text="", values=(), tags=()):
        self.rows.append(
            {
                "parent": parent,
                "index": index,
                "text": text,
                "values": values,
                "tags": tags,
            }
        )
        return str(len(self.rows) - 1)


class FakeVar:
    def __init__(self, value: str):
        self.value = value

    def get(self):
        return self.value


class FakeLabel:
    def __init__(self):
        self.text = ""

    def config(self, *, text: str):
        self.text = text


class FakeText:
    def __init__(self):
        self.content = ""
        self._state = "normal"

    def config(self, **kwargs):
        if "state" in kwargs:
            self._state = kwargs["state"]

    def delete(self, start, end):
        self.content = ""

    def insert(self, index, text, *tags):
        self.content += text

    def tag_configure(self, *args, **kwargs):
        pass


class FakeCombo:
    def __init__(self, value: str):
        self.value = value

    def get(self):
        return self.value

    def bind(self, *_args, **_kwargs):
        return None


class HeadingViewTests(unittest.TestCase):
    def _make_app(self):
        app = object.__new__(gui.App)
        app.h_tree = FakeTree()
        app.props_text = FakeText()
        app.preview_text = FakeText()
        app.strategy_var = FakeVar(core.STRATEGY_AUTO)
        app.strategy_combo = FakeCombo(core.STRATEGY_AUTO)
        app._heading_request_id = 0
        return app

    def test_refresh_heading_view_shows_detecting_status_immediately(self):
        app = self._make_app()
        entry = gui.FileEntry(
            name="sample.pdf",
            path=Path("sample.pdf"),
            source_path=Path("sample.pdf"),
            info=core.PdfInfo(True, True, True, True, "sample", True, 1),
            status="Fixed",
            detail="ok",
        )
        app._selected_entry = lambda: entry
        app._start_thread = lambda target, *args: None

        app._refresh_heading_view()

        self.assertEqual(len(app.h_tree.rows), 1)
        self.assertEqual(app.h_tree.rows[0]["text"], "Detecting...")
        self.assertEqual(
            app.h_tree.rows[0]["values"],
            ("", "", f"Running {core.STRATEGY_AUTO}"),
        )
        self.assertIn("sample.pdf", app.props_text.content)

    def test_on_headings_ready_shows_no_headings_message(self):
        app = self._make_app()
        entry = gui.FileEntry(
            name="sample.pdf",
            path=Path("sample.pdf"),
            source_path=Path("sample.pdf"),
            info=core.PdfInfo(True, True, True, True, "sample", True, 1),
            status="Needs Fix",
            detail="issues",
        )
        app._selected_filename = lambda: "sample.pdf"
        app._selected_entry = lambda: entry
        app._heading_request_id = 3

        app._on_headings_ready(3, "sample.pdf", [])

        self.assertEqual(len(app.h_tree.rows), 1)
        self.assertEqual(app.h_tree.rows[0]["text"], "No headings detected")
        self.assertEqual(
            app.h_tree.rows[0]["values"],
            ("", "", f"{core.STRATEGY_AUTO} found no heading candidates"),
        )

    def test_start_fix_selected_uses_combo_strategy_and_allows_retry_from_error(self):
        app = self._make_app()
        app.strategy_combo = FakeCombo(core.STRATEGY_BOLD)
        entry = gui.FileEntry(
            name="sample.pdf",
            path=Path("sample.pdf"),
            source_path=Path("sample.pdf"),
            info=core.PdfInfo(True, False, False, False, "", True, 1),
            status=gui.S_ERROR,
            detail="Verification failed",
        )
        app._processing = False
        app.progress = {}
        app._selected_entry = lambda: entry
        app._set_processing = lambda is_processing: setattr(app, "_processing", is_processing)

        started = {}
        worker = object()
        app._fix_worker = worker
        app._start_thread = lambda target, *args: started.update(target=target, args=args)

        with unittest.mock.patch.object(gui, "log_section"), unittest.mock.patch.object(gui, "log"):
            app._start_fix_selected()

        self.assertTrue(app._processing)
        self.assertEqual(started["target"], worker)
        self.assertEqual(started["args"], ([entry], core.STRATEGY_BOLD))
        self.assertEqual(app.progress["maximum"], 1)
        self.assertEqual(app.progress["value"], 0)


if __name__ == "__main__":
    unittest.main()
