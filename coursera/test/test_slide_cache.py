"""Tests for slide_gen/cache.py and Phase 2 resumable fixed-fanout caching."""

import json
from pathlib import Path

import pytest

from slide_gen import cache, pipeline


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


def install_client(monkeypatch, responses):
    client = FakeClient(responses)
    monkeypatch.setattr(pipeline, "make_client", lambda *args: client)
    return client


@pytest.mark.parametrize(
    "num_notes, fanout, expected_calls",
    [
        (0, 6, 0),
        (1, 6, 0),
        (2, 6, 1),
        (5, 6, 1),
        (6, 6, 1),
        (7, 6, 2),
        (12, 6, 3),
        (13, 6, 3),
        (36, 6, 7),
        (37, 6, 8),
        (50, 6, 12),
        (4, 2, 3),
        (7, 2, 6),
    ],
)
def test_merge_call_count_formula_matches_hand_calculated_expectations(num_notes, fanout, expected_calls):
    assert cache.calculate_merge_call_count(num_notes, fanout) == expected_calls


@pytest.mark.parametrize("bad_notes, bad_fanout", [(-1, 6), ("3", 6), (5, 1), (5, 0)])
def test_calculate_merge_call_count_rejects_invalid_inputs(bad_notes, bad_fanout):
    with pytest.raises(ValueError):
        cache.calculate_merge_call_count(bad_notes, bad_fanout)


def test_cache_entry_atomic_save_and_load(tmp_path):
    cache_dir = tmp_path / "notes"
    text = "## Topic A\nKey explanation [S0001]\n## Topic B\nAnother note [S0002]"
    manifest = ["S0001", "S0002"]
    key = cache.compute_extraction_cache_key("test-model", "test-prompt", "segment-text")

    record = cache.create_cache_record(cache.KIND_EXTRACTION, key, text, manifest, max_bytes=16000)
    saved_path = cache.save_cache_entry(cache_dir, record)

    assert saved_path.is_file()
    assert saved_path.name == f"{key}.json"

    loaded = cache.load_and_validate_cache_entry(
        cache_dir, key, cache.KIND_EXTRACTION, manifest, max_bytes=16000
    )
    assert loaded is not None
    assert loaded["cache_key"] == key
    assert loaded["text"] == text
    assert loaded["manifest"] == manifest
    assert loaded["version"] == cache.CACHE_SCHEMA_VERSION


def test_cache_entry_validation_failures(tmp_path):
    cache_dir = tmp_path / "notes"
    cache_dir.mkdir()
    valid_text = "## Topic A\nValid note [S0001]"
    manifest = ["S0001"]
    key = "test_key_123"

    # 1. Missing file -> None
    assert cache.load_and_validate_cache_entry(cache_dir, "missing_key", cache.KIND_EXTRACTION, manifest) is None

    # 2. Corrupted JSON -> None
    bad_json_file = cache_dir / "bad_json.json"
    bad_json_file.write_text("{not valid json", encoding="utf-8")
    assert cache.load_and_validate_cache_entry(cache_dir, "bad_json", cache.KIND_EXTRACTION, manifest) is None

    # Helper to write record and test load
    def check_corrupt_record(corrupt_fn):
        rec = cache.create_cache_record(cache.KIND_EXTRACTION, key, valid_text, manifest)
        corrupt_fn(rec)
        target = cache_dir / f"{key}.json"
        target.write_text(json.dumps(rec), encoding="utf-8")
        return cache.load_and_validate_cache_entry(cache_dir, key, cache.KIND_EXTRACTION, manifest)

    # 3. Wrong schema version -> None
    assert check_corrupt_record(lambda r: r.update({"version": 999})) is None

    # 4. Cache key mismatch -> None
    assert check_corrupt_record(lambda r: r.update({"cache_key": "other_key"})) is None

    # 5. Kind mismatch -> None
    assert check_corrupt_record(lambda r: r.update({"kind": cache.KIND_MERGE})) is None

    # 6. Text hash mismatch -> None
    assert check_corrupt_record(lambda r: r.update({"text_hash": "wronghash"})) is None

    # 7. Byte size cap exceeded -> None
    assert check_corrupt_record(lambda r: r.update({"max_bytes": 5})) is None

    # 8. Manifest mismatch -> None
    assert check_corrupt_record(lambda r: r.update({"manifest": ["S0001", "S0002"]})) is None

    # 9. Fabricated citation in note (ID not in manifest) -> None
    bad_cite_rec = cache.create_cache_record(
        cache.KIND_EXTRACTION, key, "## Topic A\nHallucinated citation [S9999]", manifest
    )
    (cache_dir / f"{key}.json").write_text(json.dumps(bad_cite_rec), encoding="utf-8")
    assert cache.load_and_validate_cache_entry(cache_dir, key, cache.KIND_EXTRACTION, manifest) is None

    # 10. Malformed markdown (no ## headings) -> None
    no_headings_rec = cache.create_cache_record(
        cache.KIND_EXTRACTION, key, "No headings but valid citation [S0001]", manifest
    )
    (cache_dir / f"{key}.json").write_text(json.dumps(no_headings_rec), encoding="utf-8")
    assert cache.load_and_validate_cache_entry(cache_dir, key, cache.KIND_EXTRACTION, manifest) is None


def test_atomic_write_failure_cleanup(tmp_path, monkeypatch):
    cache_dir = tmp_path / "notes"
    record = cache.create_cache_record(cache.KIND_EXTRACTION, "k1", "## T\n[S0001]", ["S0001"])

    def fail_replace(*args):
        raise OSError("simulated disk error")

    monkeypatch.setattr(cache.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated disk error"):
        cache.save_cache_entry(cache_dir, record)

    # Ensure no lingering .tmp files remained
    tmp_files = list(cache_dir.glob("*.tmp"))
    assert not tmp_files
    assert not (cache_dir / "k1.json").exists()


def test_merkle_cache_keys_deterministic_and_unique():
    k1 = cache.compute_extraction_cache_key("m1", "prompt", "text A")
    k2 = cache.compute_extraction_cache_key("m1", "prompt", "text A")
    assert k1 == k2

    # Different text -> different key
    assert cache.compute_extraction_cache_key("m1", "prompt", "text B") != k1
    # Different model -> different key
    assert cache.compute_extraction_cache_key("m2", "prompt", "text A") != k1

    # Merge keys include ordered child keys
    mk1 = cache.compute_merge_cache_key("m1", "mprompt", ["k1", "k2"])
    mk2 = cache.compute_merge_cache_key("m1", "mprompt", ["k1", "k2"])
    assert mk1 == mk2

    # Different order of children -> different key
    assert cache.compute_merge_cache_key("m1", "mprompt", ["k2", "k1"]) != mk1
    # Different child key -> different key
    assert cache.compute_merge_cache_key("m1", "mprompt", ["k1", "k3"]) != mk1


def test_manifest_validation_allows_subset_citations(tmp_path):
    cache_dir = tmp_path / "notes"
    # Merge note covering 5 lectures, citing only 2 in prose
    manifest = ["S0001", "S0002", "S0003", "S0004", "S0005"]
    text = "## High Level Summary\nSummary citing [S0001] and [S0003]"
    key = "merge_subset_key"

    record = cache.create_cache_record(cache.KIND_MERGE, key, text, manifest)
    cache.save_cache_entry(cache_dir, record)

    loaded = cache.load_and_validate_cache_entry(cache_dir, key, cache.KIND_MERGE, manifest)
    assert loaded is not None
    assert loaded["text"] == text


def test_resumable_synthesis_after_interruption(monkeypatch, tmp_path):
    m1 = tmp_path / "mod_1"
    m1.mkdir()
    (m1 / "first.en.txt").write_text("first lecture text", encoding="utf-8")
    m2 = tmp_path / "mod_2"
    m2.mkdir()
    (m2 / "second.en.txt").write_text("second lecture text", encoding="utf-8")
    m3 = tmp_path / "mod_3"
    m3.mkdir()
    (m3 / "third.en.txt").write_text("third lecture text", encoding="utf-8")

    entries = pipeline.scan_transcripts(str(tmp_path))
    s1, s2, s3 = entries[0]["source_id"], entries[1]["source_id"], entries[2]["source_id"]

    # Run 1: Fails on third extraction call
    client_1 = install_client(
        monkeypatch,
        [
            f"## Topic 1\nFact [{s1}]",
            f"## Topic 2\nFact [{s2}]",
            RuntimeError("Network failure on extraction 3"),
        ],
    )
    updates_1 = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy"))
    assert "Network failure" in updates_1[-1][1]
    assert len(client_1.calls) == 3

    # Check that extractions for mod_1 and mod_2 are cached on disk
    cache_dir = tmp_path / ".slide_gen_cache" / "notes"
    cached_records = list(cache_dir.glob("*.json"))
    assert len(cached_records) == 2

    # Run 2: Resume. Only 1 extraction call (for mod_3) and 1 merge call should happen!
    client_2 = install_client(
        monkeypatch,
        [
            f"## Topic 3\nFact [{s3}]",
            f"## Consolidated\nFact [{s1}]\nFact [{s2}]\nFact [{s3}]",
        ],
    )
    updates_2 = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy"))
    assert updates_2[-1][2]["complete"]
    assert len(client_2.calls) == 2
    # Verify cached notes were reused in progress updates
    status_messages = [u[1] for u in updates_2]
    assert any("Reusing cached source notes 1/3" in msg for msg in status_messages)
    assert any("Reusing cached source notes 2/3" in msg for msg in status_messages)


def test_selective_cache_invalidation_on_file_edit(monkeypatch, tmp_path):
    m1 = tmp_path / "mod_1"
    m1.mkdir()
    f1 = m1 / "first.en.txt"
    f1.write_text("first lecture text", encoding="utf-8")
    m2 = tmp_path / "mod_2"
    m2.mkdir()
    (m2 / "second.en.txt").write_text("second lecture text", encoding="utf-8")
    m3 = tmp_path / "mod_3"
    m3.mkdir()
    (m3 / "third.en.txt").write_text("third lecture text", encoding="utf-8")

    entries = pipeline.scan_transcripts(str(tmp_path))
    s1, s2, s3 = entries[0]["source_id"], entries[1]["source_id"], entries[2]["source_id"]

    # Initial full run: 3 extractions + 1 merge = 4 calls
    client_init = install_client(
        monkeypatch,
        [
            f"## Topic 1\nFact [{s1}]",
            f"## Topic 2\nFact [{s2}]",
            f"## Topic 3\nFact [{s3}]",
            f"## Merged\nTopic 1 [{s1}]\nTopic 2 [{s2}]\nTopic 3 [{s3}]",
        ],
    )
    updates_init = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy"))
    assert updates_init[-1][2]["complete"]
    assert len(client_init.calls) == 4

    # Run without changes: 0 calls (all 4 hit cache)
    client_cached = install_client(monkeypatch, [])
    updates_cached = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy"))
    assert updates_cached[-1][2]["complete"]
    assert len(client_cached.calls) == 0

    # Modify mod_1 transcript
    f1.write_text("updated first lecture text with new content", encoding="utf-8")

    # Run again: Only mod_1 extraction and merge should run (2 calls). mod_2 and mod_3 hit cache!
    client_updated = install_client(
        monkeypatch,
        [
            f"## Topic 1 Updated\nFact [{s1}]",
            f"## Merged Updated\nTopic 1 Updated [{s1}]\nTopic 2 [{s2}]\nTopic 3 [{s3}]",
        ],
    )
    updates_updated = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy"))
    assert updates_updated[-1][2]["complete"]
    assert len(client_updated.calls) == 2
    # Verify mod_2 and mod_3 were reused from cache
    status_messages = [u[1] for u in updates_updated]
    assert any("Reusing cached source notes 2/3" in msg for msg in status_messages)
    assert any("Reusing cached source notes 3/3" in msg for msg in status_messages)


def test_synthesis_multi_round_fanout_execution(monkeypatch, tmp_path):
    # 7 folders with fanout 6 -> Round 1: 1 batch of 6 + 1 tail of 1 -> 2 notes
    # Round 2: 1 batch of 2 -> 1 final note
    # Total calls: 7 extraction + 2 merge = 9 calls
    source_ids = []
    extraction_responses = []
    for i in range(1, 8):
        m = tmp_path / f"mod_{i:02d}"
        m.mkdir()
        (m / f"lecture_{i}.en.txt").write_text(f"lecture {i} content", encoding="utf-8")

    entries = pipeline.scan_transcripts(str(tmp_path))
    for i, entry in enumerate(entries, 1):
        sid = entry["source_id"]
        source_ids.append(sid)
        extraction_responses.append(f"## Topic {i}\nFact from {i} [{sid}]")

    round_1_merge = "## Consolidated Batch 1\n" + "\n".join(f"Point [{s}]" for s in source_ids[:6])
    round_2_merge = "## Final Outline\n" + "\n".join(f"Final [{s}]" for s in source_ids)

    all_responses = extraction_responses + [round_1_merge, round_2_merge]
    client = install_client(monkeypatch, all_responses)

    updates = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy", fanout=6))
    assert updates[-1][2]["complete"]
    assert len(client.calls) == 9

    # Check Round 1 merge prompt contained 6 outlines
    merge_call_1_prompt = client.calls[7][1]
    for i in range(1, 7):
        assert f"Outline {i}:" in merge_call_1_prompt
    assert "Outline 7:" not in merge_call_1_prompt

    # Check Round 2 merge prompt contained 2 outlines
    merge_call_2_prompt = client.calls[8][1]
    assert "Outline 1:" in merge_call_2_prompt
    assert "Outline 2:" in merge_call_2_prompt
    assert "Outline 3:" not in merge_call_2_prompt


def test_extraction_cache_entry_rejects_missing_required_citations(tmp_path):
    cache_dir = tmp_path / "notes"
    manifest = ["S0001", "S0002"]
    # Only cites S0001, missing S0002
    incomplete_text = "## Topic A\nNote only citing [S0001]"
    key = "incomplete_extraction_key"

    record = cache.create_cache_record(cache.KIND_EXTRACTION, key, incomplete_text, manifest)
    cache.save_cache_entry(cache_dir, record)

    # Extraction must reject incomplete citation coverage
    assert cache.load_and_validate_cache_entry(
        cache_dir, key, cache.KIND_EXTRACTION, manifest
    ) is None

    # Merge allows subset citations
    record_merge = cache.create_cache_record(cache.KIND_MERGE, key, incomplete_text, manifest)
    cache.save_cache_entry(cache_dir, record_merge)
    assert cache.load_and_validate_cache_entry(
        cache_dir, key, cache.KIND_MERGE, manifest
    ) is not None


def test_resumable_synthesis_after_merge_interruption(monkeypatch, tmp_path):
    # 7 modules, fanout 6 -> Round 1: Batch 1 (6 notes) -> merged note. Batch 2 (1 note) -> carried over.
    # Round 2: 1 merge call (Batch 1 merged note + Note 7).
    source_ids = []
    extraction_responses = []
    for i in range(1, 8):
        m = tmp_path / f"mod_{i:02d}"
        m.mkdir()
        (m / f"lecture_{i}.en.txt").write_text(f"lecture {i} content", encoding="utf-8")

    entries = pipeline.scan_transcripts(str(tmp_path))
    for i, entry in enumerate(entries, 1):
        sid = entry["source_id"]
        source_ids.append(sid)
        extraction_responses.append(f"## Topic {i}\nFact from {i} [{sid}]")

    round_1_merge = "## Consolidated Batch 1\n" + "\n".join(f"Point [{s}]" for s in source_ids[:6])
    round_2_merge = "## Final Outline\n" + "\n".join(f"Final [{s}]" for s in source_ids)

    # Run 1: 7 extractions succeed, Round 1 merge succeeds, Round 2 merge fails
    client_1 = install_client(
        monkeypatch,
        extraction_responses + [round_1_merge, RuntimeError("Network fail on round 2 merge")],
    )
    updates_1 = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy", fanout=6))
    assert "Network fail on round 2 merge" in updates_1[-1][1]
    assert len(client_1.calls) == 9

    # Run 2: Resume. All 7 extractions and Round 1 merge hit cache. ONLY Round 2 merge executes!
    client_2 = install_client(monkeypatch, [round_2_merge])
    updates_2 = list(pipeline.synthesise(str(tmp_path), "OpenAI", "test", "dummy", fanout=6))
    assert updates_2[-1][2]["complete"]
    assert len(client_2.calls) == 1
    # Check status indicated reuse of cached consolidation
    status_messages = [u[1] for u in updates_2]
    assert any("Reusing cached outline consolidation (round 1)" in msg for msg in status_messages)
