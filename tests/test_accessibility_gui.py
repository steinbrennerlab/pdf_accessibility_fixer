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


class HeadingViewTests(unittest.TestCase):
    def _make_app(self):
        app = object.__new__(gui.App)
        app.h_tree = FakeTree()
        app.strategy_var = FakeVar(core.STRATEGY_AUTO)
        app._heading_request_id = 0
        return app

    def test_refresh_heading_view_shows_detecting_status_immediately(self):
        app = self._make_app()
        app._selected_entry = lambda: gui.FileEntry(
            name="sample.pdf",
            path=Path("sample.pdf"),
            info=core.PdfInfo(True, True, True, True, "sample", True, 1),
            status="Fixed",
            detail="ok",
        )
        app._start_thread = lambda target, *args: None

        app._refresh_heading_view()

        self.assertEqual(len(app.h_tree.rows), 1)
        self.assertEqual(app.h_tree.rows[0]["text"], "Detecting...")
        self.assertEqual(
            app.h_tree.rows[0]["values"],
            ("", "", f"Running {core.STRATEGY_AUTO}"),
        )

    def test_populate_heading_tree_shows_no_headings_message(self):
        app = self._make_app()
        app._selected_filename = lambda: "sample.pdf"
        app._heading_request_id = 3

        app._populate_heading_tree(3, "sample.pdf", [])

        self.assertEqual(len(app.h_tree.rows), 1)
        self.assertEqual(app.h_tree.rows[0]["text"], "No headings detected")
        self.assertEqual(
            app.h_tree.rows[0]["values"],
            ("", "", f"{core.STRATEGY_AUTO} found no heading candidates"),
        )


if __name__ == "__main__":
    unittest.main()
