import re
from pathlib import Path

import pytest

from slide_gen.transcript_scanner import (
    build_extraction_segments,
    build_llm_context,
    find_dataset_root,
    group_by_folder,
    load_texts,
    natural_sort_key,
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
    assert all(re.match(r"^S[0-9a-f]{12}$", entry["source_id"]) for entry in entries)
    assert len({entry["source_id"] for entry in entries}) == 4
    assert entries == scan_transcripts(str(root))
    for entry in entries:
        assert set(entry) == {
            "path", "rel", "rel_root", "folder_parts", "folder", "lecture", "source_id", "text"
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
    assert re.match(r"^S[0-9a-f]{12}$", entry["source_id"])
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
    assert re.match(r"^S[0-9a-f]{12}$", entry["source_id"])
    expected_prompt = [f"[{entry['source_id']}] {relative}\nSynthetic float lecture"]
    assert build_llm_context(entries) == expected_prompt
    assert build_extraction_segments(entries) == expected_prompt


def test_specialization_under_courses_preserves_id_and_extraction_segments_across_roots(tmp_path):
    folder_parts = [
        "courses",
        "candlestick-float-analysis-trading",
        "01_foundations-of-float-financial-planning",
        "01_understanding-float-basics",
    ]
    path = tmp_path.joinpath(*folder_parts, "01_float.en.txt")
    path.parent.mkdir(parents=True)
    path.write_text("Synthetic float lecture", encoding="utf-8")

    # When scanned from courses/ or from any sub-level, source_id and prompt header match
    expected_rel_root = "/".join(folder_parts[1:] + [path.name])
    for depth in range(1, len(folder_parts) + 1):
        selected = tmp_path.joinpath(*folder_parts[:depth])
        entries = load_texts(scan_transcripts(str(selected)))
        assert len(entries) == 1
        sid = entries[0]["source_id"]
        assert re.match(r"^S[0-9a-f]{12}$", sid)
        expected = [f"[{sid}] {expected_rel_root}\nSynthetic float lecture"]
        assert build_extraction_segments(entries) == expected
        assert build_llm_context(entries) == expected


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
    assert all(re.match(r"^S[0-9a-f]{12}$", entry["source_id"]) for entry in entries)
    assert len({entry["source_id"] for entry in entries}) == 6
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


def test_adding_unrelated_file_preserves_existing_ids(tmp_path):
    root = tmp_path / "courses" / "course_a"
    root.mkdir(parents=True)
    f1 = root / "01_intro.en.txt"
    f2 = root / "02_second.en.txt"
    f1.write_text("intro text", encoding="utf-8")
    f2.write_text("second text", encoding="utf-8")

    entries_before = scan_transcripts(str(root))
    id_map_before = {e["rel"]: e["source_id"] for e in entries_before}

    # Add unrelated file in a different directory
    other_course = tmp_path / "courses" / "course_b"
    other_course.mkdir(parents=True)
    (other_course / "01_other.en.txt").write_text("other text", encoding="utf-8")

    # Add unrelated file in same course
    (root / "01_a_inserted.en.txt").write_text("inserted text", encoding="utf-8")

    entries_after = scan_transcripts(str(root))
    id_map_after = {e["rel"]: e["source_id"] for e in entries_after}

    assert id_map_after["01_intro.en.txt"] == id_map_before["01_intro.en.txt"]
    assert id_map_after["02_second.en.txt"] == id_map_before["02_second.en.txt"]


def test_hard_collision_raises_clear_error_naming_both_files(tmp_path, monkeypatch):
    root = tmp_path / "courses" / "course_test"
    root.mkdir(parents=True)
    (root / "file_a.en.txt").write_text("text a", encoding="utf-8")
    (root / "file_b.en.txt").write_text("text b", encoding="utf-8")

    # Force identical sha256 prefix for testing collision error
    import hashlib
    class FakeSha:
        def hexdigest(self):
            return "0123456789abcdef"

    monkeypatch.setattr(hashlib, "sha256", lambda *args: FakeSha())

    with pytest.raises(ValueError, match="Source ID collision detected.*file_a.en.txt.*file_b.en.txt"):
        scan_transcripts(str(root))


def test_natural_sorting_per_component(tmp_path):
    root = tmp_path / "course"
    root.mkdir()
    files = ["10_lecture.en.txt", "2_lecture.en.txt", "1_lecture.en.txt", "20_lecture.en.txt"]
    for f in files:
        (root / f).write_text("content", encoding="utf-8")

    entries = scan_transcripts(str(root))
    assert [e["rel"] for e in entries] == [
        "1_lecture.en.txt", "2_lecture.en.txt", "10_lecture.en.txt", "20_lecture.en.txt"
    ]


def test_dataset_root_discovery(tmp_path):
    courses_dir = tmp_path / "my_stuff" / "courses"
    sub = courses_dir / "specialization" / "course_1" / "module_a"
    sub.mkdir(parents=True)

    assert find_dataset_root(sub) == courses_dir
    assert find_dataset_root(courses_dir) == courses_dir

    # Fallback to self when no ancestor named 'courses'
    isolated = tmp_path / "downloads" / "course_x"
    isolated.mkdir(parents=True)
    assert find_dataset_root(isolated) == isolated


def test_folder_scoped_extraction_splitting_and_reconstruction(tmp_path):
    root = tmp_path / "course"
    folder_a = root / "folder_a"
    folder_b = root / "folder_b"
    folder_a.mkdir(parents=True)
    folder_b.mkdir(parents=True)

    # Folder A fits within budget
    (folder_a / "01_short.en.txt").write_text("Short text in folder A", encoding="utf-8")

    # Folder B exceeds budget: has paragraphs and sentences
    para1 = "Paragraph 1 is here. It has multiple sentences."
    para2 = "Paragraph 2 is here. It also has content."
    full_text_b = para1 + "\n\n" + para2
    (folder_b / "01_long.en.txt").write_text(full_text_b, encoding="utf-8")

    entries = load_texts(scan_transcripts(str(root)))
    # Set budget small enough that folder B must split, but large enough for paragraph 1
    budget = 120
    segs = build_extraction_segments(entries, budget=budget)

    # Folder A must be 1 segment and must NOT contain any folder B text
    seg_a = [s for s in segs if "folder_a" in s]
    assert len(seg_a) == 1
    assert "folder_b" not in seg_a[0]

    # Folder B must NOT contain any folder A text
    seg_b = [s for s in segs if "folder_b" in s]
    assert len(seg_b) > 1
    for s in seg_b:
        assert "folder_a" not in s

    # When piecing folder B's body segments back together, original text is reconstructed
    b_entry = next(e for e in entries if e["rel"] == "folder_b/01_long.en.txt")
    header = f"[{b_entry['source_id']}] {b_entry['rel']}\n"
    reconstructed = "".join(s.replace(header, "") for s in seg_b)
    assert reconstructed == full_text_b


def test_natural_sort_key_nested_cases():
    paths = [
        "course_1/module_10/lecture_2.en.txt",
        "course_1/module_2/lecture_10.en.txt",
        "course_1/module_2/lecture_2.en.txt",
        "course_2/module_1/lecture_1.en.txt",
    ]
    sorted_paths = sorted(paths, key=natural_sort_key)
    assert sorted_paths == [
        "course_1/module_2/lecture_2.en.txt",
        "course_1/module_2/lecture_10.en.txt",
        "course_1/module_10/lecture_2.en.txt",
        "course_2/module_1/lecture_1.en.txt",
    ]


def test_find_dataset_root_nearest_courses_precedence(tmp_path):
    # If there are nested 'courses' folders, nearest parent named 'courses' wins
    nested = tmp_path / "courses" / "sub" / "courses" / "course_name"
    nested.mkdir(parents=True)
    assert find_dataset_root(nested) == tmp_path / "courses" / "sub" / "courses"


def test_split_text_deterministic_hierarchy_and_multibyte():
    from slide_gen.transcript_scanner import split_text_deterministic

    # 1. Fits within budget
    assert split_text_deterministic("hello world", 50) == ["hello world"]

    # 2. Paragraph split
    text_paras = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    chunks = split_text_deterministic(text_paras, 25)
    assert "".join(chunks) == text_paras
    assert all(len(c.encode("utf-8")) <= 25 for c in chunks)

    # 3. Sentence split inside large paragraph
    long_para = "Sentence one. Sentence two. Sentence three."
    chunks_sent = split_text_deterministic(long_para, 20)
    assert "".join(chunks_sent) == long_para
    assert all(len(c.encode("utf-8")) <= 20 for c in chunks_sent)

    # 4. Multibyte Unicode boundary split
    multibyte = "漢" * 20 + "😀" * 10
    chunks_mb = split_text_deterministic(multibyte, 15)
    assert "".join(chunks_mb) == multibyte
    assert all(len(c.encode("utf-8")) <= 15 for c in chunks_mb)
    # Ensure no multibyte char was corrupted
    for c in chunks_mb:
        c.encode("utf-8").decode("utf-8")


@pytest.mark.parametrize("invalid_budget", [0, -5, 1.5, True, False, "6000"])
def test_build_extraction_segments_rejects_invalid_budgets(invalid_budget):
    with pytest.raises(ValueError, match="budget must be a positive integer"):
        build_extraction_segments([], budget=invalid_budget)


def test_build_extraction_segments_multiple_folders_count(tmp_path):
    root = tmp_path / "course"
    for i in range(1, 4):
        folder = root / f"module_{i}"
        folder.mkdir(parents=True)
        (folder / "lec.en.txt").write_text(f"Lecture content {i}", encoding="utf-8")

    entries = load_texts(scan_transcripts(str(root)))
    # With large budget, each folder forms exactly 1 segment
    segs = build_extraction_segments(entries, budget=24000)
    assert len(segs) == 3
    for i in range(1, 4):
        assert any(f"module_{i}" in s for s in segs)


