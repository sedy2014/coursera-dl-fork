import json
from pathlib import Path

import pytest

from slide_gen import pipeline


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def stream(self, system, user):
        self.calls.append((system, user))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        yield response


@pytest.fixture
def snapshot(tmp_path):
    source = tmp_path / "course"
    source.mkdir()
    return {"folder": str(source.resolve()), "complete": True, "entries": [
        {"source_id": "S0001", "path": str(source / "lecture.en.txt"),
         "rel": "lecture.en.txt", "module": "", "section": "", "lecture": "lecture",
         "text": "A distinctive source fact about supervised learning."},
    ]}


def install_client(monkeypatch, responses):
    client = FakeClient(responses)
    monkeypatch.setattr(pipeline, "make_client", lambda *args: client)
    return client


def tutorial_args(snapshot, tmp_path):
    return ("## Learning\n- Supervision [S0001]", 3, "OpenAI", "test", "dummy",
            str(tmp_path / "output"), "lesson", snapshot, snapshot["folder"])


def test_synthesis_consolidates_previous_notes(monkeypatch, tmp_path):
    (tmp_path / "first.en.txt").write_text("first source " * 300, encoding="utf-8")
    (tmp_path / "second.en.txt").write_text("second source " * 300, encoding="utf-8")
    client = install_client(monkeypatch, [
        "## Topic A\nFirst fact [S0001]",
        "## Topic B\nSecond fact [S0002]",
        "## Combined\nFirst fact [S0001]\nSecond fact [S0002]",
    ])
    updates = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy"))
    assert len(client.calls) == 3
    assert "First fact [S0001]" in client.calls[2][1]
    assert "Second fact [S0002]" in client.calls[2][1]
    assert updates[-1][2]["complete"]
    assert len(updates[-1][2]["entries"]) == 2


def test_synthesis_rejects_lost_source_references(monkeypatch, tmp_path):
    (tmp_path / "lecture.en.txt").write_text("source content", encoding="utf-8")
    install_client(monkeypatch, ["## Topic\nNo references"])
    updates = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy"))
    assert updates[-1][2] == {}
    assert "Incomplete outline" in updates[-1][1]


def test_tutorial_receives_original_sources(monkeypatch, tmp_path, snapshot):
    client = install_client(monkeypatch, ["### Explanation\nSource-backed fact [S0001]"])
    updates = list(pipeline.tutorial(*tutorial_args(snapshot, tmp_path)))
    assert snapshot["entries"][0]["text"] in client.calls[0][1]
    saved = Path(updates[-1][2]).read_text(encoding="utf-8")
    assert "Source Index" in saved
    assert "lecture.en.txt" in saved
    assert "Supplemental explanations require factual review" in saved


@pytest.mark.parametrize("bad_outline", ["no headings", "## Topic\nNo citation", "## Topic\n[S9999]"])
def test_invalid_edited_outline_fails_before_api(monkeypatch, tmp_path, snapshot, bad_outline):
    client = install_client(monkeypatch, [])
    args = list(tutorial_args(snapshot, tmp_path))
    args[0] = bad_outline
    updates = list(pipeline.tutorial(*args))
    assert not client.calls
    assert updates[-1][2] is None


def test_changed_folder_rejects_snapshot(monkeypatch, tmp_path, snapshot):
    client = install_client(monkeypatch, [])
    args = list(tutorial_args(snapshot, tmp_path))
    args[-1] = str(tmp_path / "different_course")
    updates = list(pipeline.tutorial(*args))
    assert "folder changed" in updates[-1][1]
    assert not client.calls


def test_missing_snapshot_rejected(monkeypatch, tmp_path, snapshot):
    client = install_client(monkeypatch, [])
    args = list(tutorial_args(snapshot, tmp_path))
    args[-2] = {}
    updates = list(pipeline.tutorial(*args))
    assert updates[-1][2] is None
    assert not client.calls


def test_unwritable_destination_checked_before_generation(monkeypatch, tmp_path, snapshot):
    client = install_client(monkeypatch, [])
    blocked = tmp_path / "output"
    blocked.write_text("existing file", encoding="utf-8")
    updates = list(pipeline.tutorial(*tutorial_args(snapshot, tmp_path)))
    assert updates[-1][2] is None
    assert not client.calls
    assert blocked.read_text(encoding="utf-8") == "existing file"


def test_incomplete_tutorial_preserves_existing_output(monkeypatch, tmp_path, snapshot):
    target = tmp_path / "output" / "lesson_tutorial.md"
    target.parent.mkdir()
    target.write_text("previous artifact", encoding="utf-8")
    install_client(monkeypatch, [RuntimeError("truncated response")])
    updates = list(pipeline.tutorial(*tutorial_args(snapshot, tmp_path)))
    assert updates[-1][2] is None
    assert "truncated" in updates[-1][1]
    assert target.read_text(encoding="utf-8") == "previous artifact"


def test_generated_tutorial_requires_source_coverage(monkeypatch, tmp_path, snapshot):
    install_client(monkeypatch, ["A confident but uncited answer"])
    updates = list(pipeline.tutorial(*tutorial_args(snapshot, tmp_path)))
    assert updates[-1][2] is None
    assert not (tmp_path / "output" / "lesson_tutorial.md").exists()


@pytest.mark.parametrize("name", ["../escape", "bad/name", "C:\\outside", "CON", "with.ext", ""])
def test_filename_stays_inside_output(tmp_path, name):
    with pytest.raises(ValueError):
        pipeline.prepare_destination(str(tmp_path), name, ".pptx")


def test_failed_atomic_write_preserves_old_file(monkeypatch, tmp_path):
    path = tmp_path / "tutorial.md"
    path.write_text("old", encoding="utf-8")

    def fail_replace(*args):
        raise PermissionError("locked file")

    monkeypatch.setattr(pipeline.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        pipeline.atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.iterdir()) == [path]


def test_slide_pipeline_renders_real_presentation(monkeypatch, tmp_path, snapshot):
    presentation = pytest.importorskip("pptx").Presentation
    payload = {"title": "Learning", "slides": [
        {"title": "Supervision", "bullets": ["Learn from labels"], "notes": "Source [S0001]"},
    ]}
    install_client(monkeypatch, [json.dumps(payload)])
    updates = list(pipeline.slides("## Learning\n[S0001]", "", "OpenAI", "test", "dummy",
                                   str(tmp_path / "output"), "lesson", snapshot, snapshot["folder"]))
    assert updates[-1][1] is not None, updates[-1][0]
    deck = presentation(updates[-1][1])
    assert len(deck.slides) >= 1
    assert "[S0001]" in deck.slides[0].notes_slide.notes_text_frame.text


def test_invalid_slide_data_retries_once_without_touching_old_deck(monkeypatch, tmp_path, snapshot):
    pytest.importorskip("pptx")
    output = tmp_path / "output"
    output.mkdir()
    target = output / "lesson.pptx"
    target.write_bytes(b"previous artifact")
    client = install_client(monkeypatch, ["print('not data')", "print('still not data')"])
    updates = list(pipeline.slides("## Learning\n[S0001]", "", "OpenAI", "test", "dummy",
                                   str(output), "lesson", snapshot, snapshot["folder"]))
    assert len(client.calls) == 2
    assert updates[-1][1] is None
    assert target.read_bytes() == b"previous artifact"
    assert (output / "lesson_slides_error.txt").exists()
    assert not list(output.glob("*.py"))
