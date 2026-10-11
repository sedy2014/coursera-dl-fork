from pathlib import Path

import pytest

from slide_gen.transcript_scanner import (
    build_llm_context,
    group_by_folder,
    load_texts,
    scan_transcripts,
    summarise,
)


@pytest.fixture
def transcript_tree(tmp_path):
    contents = {
        "02_module/01_section/02_second.en.txt": "Second lecture",
        "01_module/02_section/01_first.en.txt": "First lecture",
        "01_module/intro.en.txt": "Introduction",
        "root.en.txt": "Root lecture",
    }
    for relative, text in contents.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (tmp_path / "ignored.fr.txt").write_text("French", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("Other", encoding="utf-8")
    (tmp_path / "directory.en.txt").mkdir()
    return tmp_path, contents


def make_entry(text, relative="lecture.en.txt", source_id="S0001"):
    return {"rel": relative, "source_id": source_id, "text": text}


def test_scan_resolves_paths_and_assigns_deterministic_ids(transcript_tree, monkeypatch):
    root, contents = transcript_tree
    monkeypatch.chdir(root.parent)
    entries = scan_transcripts(str(Path(root.name) / ".." / root.name))
    assert [entry["rel"] for entry in entries] == sorted(contents)
    assert [entry["source_id"] for entry in entries] == [
        "S0001", "S0002", "S0003", "S0004"
    ]
    assert entries == scan_transcripts(str(root))
    for entry in entries:
        assert set(entry) == {
            "path", "rel", "folder_parts", "folder", "lecture", "source_id", "text"
        }
        assert Path(entry["path"]).is_absolute()
        assert Path(entry["path"]) == root / entry["rel"]
        assert entry["text"] is None
    assert entries[0]["folder_parts"] == ["01_module", "02_section"]
    assert entries[0]["folder"] == "01_module/02_section"
    assert entries[0]["lecture"] == "01_first"
    assert entries[1]["folder_parts"] == ["01_module"]
    assert entries[-1]["folder_parts"] == []
    assert entries[-1]["folder"] == ""


def test_scan_empty_directory(tmp_path):
    assert scan_transcripts(str(tmp_path)) == []


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 6, 12])
def test_scan_preserves_every_folder_level(tmp_path, depth):
    folder_parts = [f"level_{index:02d}" for index in range(depth)]
    path = tmp_path.joinpath(*folder_parts, "01_lecture.en.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Lecture text", encoding="utf-8")

    entries = scan_transcripts(str(tmp_path))
    assert len(entries) == 1
    entry = entries[0]
    assert entry["folder_parts"] == folder_parts
    assert entry["folder"] == "/".join(folder_parts)
    assert entry["rel"] == "/".join(folder_parts + [path.name])
    assert entry["lecture"] == "01_lecture"
    assert entry["source_id"] == "S0001"
    assert "module" not in entry and "section" not in entry
    assert list(group_by_folder(entries)) == [entry["folder"]]
    assert f"maximum depth: **{depth}**" in summarise(entries)


@pytest.fixture
def specialization_transcript(tmp_path):
    folder_parts = [
        "candlestick-float-analysis-trading",
        "01_foundations-of-float-financial-planning",
        "01_understanding-float-basics",
    ]
    path = tmp_path.joinpath(*folder_parts, "01_float.en.txt")
    path.parent.mkdir(parents=True)
    path.write_text("Synthetic float lecture", encoding="utf-8")
    return tmp_path, path, folder_parts


@pytest.mark.parametrize("root_depth", [0, 1, 2, 3])
def test_specialization_can_be_scanned_from_any_root(specialization_transcript, root_depth):
    root, path, folder_parts = specialization_transcript
    selected_root = root.joinpath(*folder_parts[:root_depth])
    entries = load_texts(scan_transcripts(str(selected_root)))
    remaining_parts = folder_parts[root_depth:]
    relative = "/".join(remaining_parts + [path.name])

    assert len(entries) == 1
    entry = entries[0]
    assert Path(entry["path"]) == path
    assert entry["folder_parts"] == remaining_parts
    assert entry["folder"] == "/".join(remaining_parts)
    assert entry["rel"] == relative
    assert entry["lecture"] == "01_float"
    assert build_llm_context(entries) == [f"[S0001] {relative}\nSynthetic float lecture"]


def test_mixed_depths_and_repeated_names_remain_distinct(tmp_path):
    relatives = [
        "root.en.txt",
        "course_a/01_module/01_section/01_first.en.txt",
        "course_a/01_module/01_section/02_second.en.txt",
        "course_b/01_module/01_section/01_first.en.txt",
        "archive/batch/course_c/01_module/01_section/extra/01_deep.en.txt",
        "course_b/intro.en.txt",
    ]
    for relative in relatives:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic lecture", encoding="utf-8")

    entries = scan_transcripts(str(tmp_path))
    assert [entry["rel"] for entry in entries] == sorted(relatives)
    assert [entry["source_id"] for entry in entries] == [
        "S0001", "S0002", "S0003", "S0004", "S0005", "S0006"
    ]
    groups = group_by_folder(entries)
    assert len(groups) == 5
    assert len(groups["course_a/01_module/01_section"]) == 2
    assert len(groups["course_b/01_module/01_section"]) == 1
    assert len(groups[""]) == 1
    assert summarise(entries) == (
        "Found **6** transcripts in **5** folders containing transcripts "
        "(maximum depth: **6** below selected root)."
    )


@pytest.mark.parametrize("existing_file", [False, True])
def test_scan_requires_existing_directory(tmp_path, existing_file):
    root = tmp_path / "invalid-root"
    if existing_file:
        root.write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid-root"):
        scan_transcripts(str(root))


def test_scan_reports_resolution_failure(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "resolve", denied)
    with pytest.raises(ValueError, match="Cannot access transcript folder.*access denied"):
        scan_transcripts(str(tmp_path))


def test_scan_does_not_return_partial_results_on_walk_error(tmp_path, monkeypatch):
    (tmp_path / "lecture.en.txt").write_text("Lecture", encoding="utf-8")

    def failing_walk(root, onerror):
        yield str(root), ["private"], ["lecture.en.txt"]
        onerror(PermissionError("cannot access private"))

    monkeypatch.setattr("slide_gen.transcript_scanner.os.walk", failing_walk)
    with pytest.raises(ValueError, match="Cannot scan.*private"):
        scan_transcripts(str(tmp_path))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_load_strict_utf8_with_optional_bom_preserves_whitespace(tmp_path, encoding):
    text = "  Hello café नमस्ते 🌍\n\n "
    (tmp_path / "lecture.en.txt").write_bytes(text.encode(encoding))
    entries = scan_transcripts(str(tmp_path))
    assert load_texts(entries) is entries
    assert entries[0]["text"] == text


@pytest.mark.parametrize("content", [b"", b" \t\r\n", b"\xef\xbb\xbf", b"\xffbad"])
def test_load_rejects_empty_or_invalid_files_without_setting_text(tmp_path, content):
    path = tmp_path / "module" / "bad.en.txt"
    path.parent.mkdir()
    path.write_bytes(content)
    entries = scan_transcripts(str(tmp_path))
    with pytest.raises(ValueError, match="module/bad.en.txt"):
        load_texts(entries)
    assert entries[0]["text"] is None


@pytest.mark.parametrize("error", [PermissionError("denied"), FileNotFoundError("gone")])
def test_load_reports_read_errors_and_can_retry(tmp_path, monkeypatch, error):
    path = tmp_path / "lecture.en.txt"
    path.write_text("Recovered", encoding="utf-8")
    entries = scan_transcripts(str(tmp_path))

    def failed_read(*args, **kwargs):
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", failed_read)
        with pytest.raises(ValueError, match="lecture.en.txt") as caught:
            load_texts(entries)
        assert caught.value.__cause__ is error
        assert entries[0]["text"] is None
    assert load_texts(entries)[0]["text"] == "Recovered"


def test_load_skips_loaded_entries_and_retains_successes(tmp_path, monkeypatch):
    (tmp_path / "01.en.txt").write_text("Good", encoding="utf-8")
    (tmp_path / "02.en.txt").write_bytes(b"\xff")
    entries = scan_transcripts(str(tmp_path))
    with pytest.raises(ValueError, match="02.en.txt"):
        load_texts(entries)
    assert entries[0]["text"] == "Good"
    assert entries[1]["text"] is None

    def unexpected_read(*args, **kwargs):
        pytest.fail("Already loaded text must not be read again")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    loaded = entries[:1]
    assert load_texts(loaded) is loaded


def test_grouping_and_summary_use_complete_folder_paths(transcript_tree):
    root, _ = transcript_tree
    entries = scan_transcripts(str(root))
    groups = group_by_folder(entries)
    assert list(groups) == ["01_module/02_section", "01_module", "02_module/01_section", ""]
    assert groups["01_module/02_section"] == entries[:1]
    assert groups["01_module/02_section"][0] is entries[0]
    assert summarise(entries) == (
        "Found **4** transcripts in **4** folders containing transcripts "
        "(maximum depth: **2** below selected root)."
    )
    assert summarise(entries[:1]) == (
        "Found **1** transcript in **1** folder containing transcripts "
        "(maximum depth: **2** below selected root)."
    )
    assert summarise(entries[-1:]) == (
        "Found **1** transcript in **1** folder containing transcripts "
        "(maximum depth: **0** below selected root)."
    )
    assert summarise([]) == "No .en.txt transcript files found in this folder."
    assert group_by_folder([]) == {}


def test_context_counts_headers_and_separators_at_exact_boundary():
    entries = [make_entry("One"), make_entry("Two", "other.en.txt", "S0002")]
    blocks = ["[S0001] lecture.en.txt\nOne", "[S0002] other.en.txt\nTwo"]
    combined = "\n\n".join(blocks)
    budget = len(combined.encode("utf-8"))
    assert build_llm_context(entries, budget) == [combined]
    assert build_llm_context(entries, budget - 1) == blocks


@pytest.mark.parametrize("text", [" abc\n\n" * 20, "é漢🌍 नमस्ते\n " * 20])
@pytest.mark.parametrize("capacity", [4, 5, 11, 29])
def test_context_splits_without_losing_unicode_or_whitespace(text, capacity):
    entry = make_entry(text, "模块/é.en.txt", "S0042")
    header = "[S0042] 模块/é.en.txt\n"
    budget = len(header.encode("utf-8")) + capacity
    txt_segs = build_llm_context([entry], budget)
    assert len(txt_segs) > 1
    assert all(txt_seg.startswith(header) for txt_seg in txt_segs)
    assert all(len(txt_seg.encode("utf-8")) <= budget for txt_seg in txt_segs)
    assert "".join(txt_seg[len(header):] for txt_seg in txt_segs) == text
    assert entry["text"] == text


def test_context_covers_multiple_sources_in_order():
    entries = [make_entry("a" * 80), make_entry("漢" * 40, "other.en.txt", "S0002")]
    txt_segs = build_llm_context(entries, 40)
    expected = "".join(entry["text"] for entry in entries)
    restored = "".join(txt_seg.split("\n", 1)[1] for txt_seg in txt_segs)
    assert restored == expected
    assert all(len(txt_seg.encode("utf-8")) <= 40 for txt_seg in txt_segs)
    assert txt_segs[0].startswith("[S0001]")
    assert txt_segs[-1].startswith("[S0002]")


def test_context_default_budget_is_6000_bytes():
    text = "🌍" * 4000
    txt_segs = build_llm_context([make_entry(text)])
    assert len(txt_segs) == 3
    assert all(len(txt_seg.encode("utf-8")) <= 6000 for txt_seg in txt_segs)
    assert "".join(txt_seg.split("\n", 1)[1] for txt_seg in txt_segs) == text


@pytest.mark.parametrize("budget", [0, -1, 1.5, True, "100"])
def test_context_rejects_invalid_budgets_even_without_entries(budget):
    with pytest.raises(ValueError, match="positive integer"):
        build_llm_context([], budget)


@pytest.mark.parametrize("extra", [-1, 0, 1, 3])
def test_context_rejects_budget_without_room_for_header_and_character(extra):
    header = "[S0001] lecture.en.txt\n"
    with pytest.raises(ValueError, match="too small.*lecture.en.txt"):
        build_llm_context([make_entry("🌍")], len(header.encode("utf-8")) + extra)


def test_context_accepts_minimum_ascii_capacity():
    header = "[S0001] lecture.en.txt\n"
    assert build_llm_context([make_entry("ab")], len(header) + 1) == [
        header + "a", header + "b"
    ]


def test_context_requires_loading_every_entry():
    entries = [make_entry("Ready"), make_entry(None, "unloaded.en.txt", "S0002")]
    with pytest.raises(ValueError, match=r"unloaded.en.txt.*load_texts\(\) first"):
        build_llm_context(entries)
    assert entries[1]["text"] is None


def test_context_empty_input():
    assert build_llm_context([]) == []
