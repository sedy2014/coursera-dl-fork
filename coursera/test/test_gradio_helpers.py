# -*- coding: utf-8 -*-
"""
Tests for pure helper functions in coursera/gradio_ui.py.

We import only the module-level helper functions, not the Gradio app itself,
so no Gradio server is started during tests.
"""

import os
import queue
import pytest
import pandas as pd
from types import SimpleNamespace
from unittest.mock import patch

from coursera.gradio_ui import (
    _make_table_rows,
    _rows_to_cli,
    _log_queue,
)

# _drain_queue is a local helper — import it if available, skip if not exported
try:
    from coursera.gradio_ui import _drain_queue
    HAS_DRAIN = True
except ImportError:
    HAS_DRAIN = False


# ---------------------------------------------------------------------------
# _make_table_rows
# ---------------------------------------------------------------------------

class TestMakeTableRows:

    def test_empty_slug_list_returns_empty(self):
        assert _make_table_rows([], "") == []

    def test_single_slug_structure(self):
        rows = _make_table_rows(["machine-learning"], "")
        assert len(rows) == 1
        row = rows[0]
        assert len(row) == 5
        badge, api_slug, display_name, folder, include = row
        assert badge == "1"
        assert api_slug == "machine-learning"
        assert isinstance(display_name, str) and len(display_name) > 0
        assert isinstance(folder, str) and len(folder) > 0
        assert include is True

    def test_multiple_slugs_badges_sequential(self):
        rows = _make_table_rows(["a", "b", "c"], "")
        badges = [r[0] for r in rows]
        assert badges == ["1", "2", "3"]

    def test_include_always_true(self):
        rows = _make_table_rows(["ml", "nlp"], "")
        assert all(r[4] is True for r in rows)

    def test_no_output_path_no_checkmark(self):
        rows = _make_table_rows(["ml"], "")
        assert "✅" not in rows[0][0]

    def test_existing_folder_shows_checkmark(self, tmp_path):
        # Create a folder that matches what compute_folder would produce
        # For slug "ml", display="Ml", folder="ml"
        folder_path = tmp_path / "ml"
        folder_path.mkdir()
        rows = _make_table_rows(["ml"], str(tmp_path))
        assert "✅" in rows[0][0]

    def test_nonexistent_folder_no_checkmark(self, tmp_path):
        rows = _make_table_rows(["ml"], str(tmp_path))
        assert "✅" not in rows[0][0]

    def test_display_name_derived_from_slug(self):
        rows = _make_table_rows(["intro-tensorflow"], "")
        display = rows[0][2]
        assert display == "Intro Tensorflow"


# ---------------------------------------------------------------------------
# _rows_to_cli
# ---------------------------------------------------------------------------

class _FakeCookieFile:
    """Minimal stub for a Gradio File object."""
    def __init__(self, name):
        self.name = name


class TestRowsToCli:

    COMMENT_NO_COURSES = "# Resolve"
    COMMENT_NONE_SELECTED = "# No courses"

    def _valid_rows(self, include=True):
        """One valid row as a list-of-lists."""
        return [["1", "ml", "Machine Learning", "machine-learning_ml", include]]

    # -- empty / None inputs --------------------------------------------------

    def test_none_rows_returns_comment(self):
        result = _rows_to_cli(None, None, ".", "en", "540p", ["all"], 1, False, False, False)
        assert result.startswith("#")

    def test_empty_list_returns_comment(self):
        result = _rows_to_cli([], None, ".", "en", "540p", ["all"], 1, False, False, False)
        assert result.startswith("#")

    def test_empty_dataframe_returns_comment(self):
        result = _rows_to_cli(pd.DataFrame(), None, ".", "en", "540p", ["all"], 1, False, False, False)
        assert result.startswith("#")

    # -- all excluded ---------------------------------------------------------

    def test_all_excluded_returns_comment(self):
        rows = self._valid_rows(include=False)
        result = _rows_to_cli(rows, None, ".", "en", "540p", ["all"], 1, False, False, False)
        assert result.startswith("#")

    # -- valid inputs ---------------------------------------------------------

    def test_valid_rows_contains_slug(self):
        rows = self._valid_rows()
        result = _rows_to_cli(rows, None, ".", "en", "540p", ["all"], 1, False, False, False)
        assert "ml" in result

    def test_no_cookies_file_uses_placeholder(self):
        rows = self._valid_rows()
        result = _rows_to_cli(rows, None, ".", "en", "540p", ["all"], 1, False, False, False)
        # When no cookies file object is provided, a "<cookies.txt>" placeholder
        # is shown so the user knows to fill it in
        assert "<cookies.txt>" in result

    def test_cookies_file_object_name_in_output(self):
        rows = self._valid_rows()
        fake = _FakeCookieFile("./my-cookies.txt")
        result = _rows_to_cli(rows, fake, ".", "en", "540p", ["all"], 1, False, False, False)
        assert "my-cookies.txt" in result
        assert "-c" in result

    def test_dataframe_input_same_as_list(self):
        rows_list = self._valid_rows()
        df = pd.DataFrame(rows_list, columns=["#", "API Slug", "Display Name", "Folder Name", "Include?"])
        result_list = _rows_to_cli(rows_list, None, ".", "en", "540p", ["all"], 1, False, False, False)
        result_df   = _rows_to_cli(df,        None, ".", "en", "540p", ["all"], 1, False, False, False)
        # Both should produce the same slug in the output
        assert "ml" in result_list
        assert "ml" in result_df

    def test_multiple_included_rows(self):
        rows = [
            ["1", "course-a", "Course A", "course-a_course-a", True],
            ["2", "course-b", "Course B", "course-b_course-b", True],
        ]
        result = _rows_to_cli(rows, None, ".", "en", "540p", ["all"], 1, False, False, False)
        assert "course-a" in result
        assert "course-b" in result

    def test_excluded_row_not_in_output(self):
        rows = [
            ["1", "course-a", "Course A", "course-a", True],
            ["2", "course-b", "Course B", "course-b", False],  # excluded
        ]
        result = _rows_to_cli(rows, None, ".", "en", "540p", ["all"], 1, False, False, False)
        assert "course-a" in result
        assert "course-b" not in result

    def test_output_path_in_command(self):
        rows = self._valid_rows()
        result = _rows_to_cli(rows, None, "./my-courses", "en", "540p", ["all"], 1, False, False, False)
        assert "my-courses" in result


# ---------------------------------------------------------------------------
# _drain_queue  (only if exported from the module)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_DRAIN, reason="_drain_queue not exported")
class TestDrainQueue:

    def setup_method(self):
        # Clear the module-level queue before each test
        while not _log_queue.empty():
            try:
                _log_queue.get_nowait()
            except queue.Empty:
                break

    def test_empty_queue_returns_false(self):
        log_lines = []
        changed = _drain_queue(log_lines)
        assert changed is False
        assert log_lines == []

    def test_messages_drained_and_returns_true(self):
        _log_queue.put("INFO     first message")
        _log_queue.put("INFO     second message")
        log_lines = []
        changed = _drain_queue(log_lines)
        assert changed is True
        assert len(log_lines) == 2
        assert "first message" in log_lines[0]

    def test_rolling_window_truncation(self):
        for i in range(600):
            _log_queue.put(f"line {i}")
        log_lines = []
        _drain_queue(log_lines, max_lines=500)
        assert len(log_lines) <= 500

    def test_existing_lines_preserved_up_to_max(self):
        existing = [f"old line {i}" for i in range(490)]
        _log_queue.put("new line 0")
        _log_queue.put("new line 1")
        log_lines = list(existing)
        _drain_queue(log_lines, max_lines=500)
        # Should have 492 total, under limit — all kept
        assert len(log_lines) == 492
