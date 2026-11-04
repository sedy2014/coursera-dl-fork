"""
slide_gen/cache.py
------------------
Persistent, validated, atomic-write extraction and merge cache.

Supports Merkle-style cache key generation, strict read validation,
atomic writes, and deterministic merge planning.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

CACHE_SCHEMA_VERSION = 1
KIND_EXTRACTION = "extraction"
KIND_MERGE = "merge"

SOURCE_PATTERN = re.compile(r"\[(S[0-9a-fA-F]{4,})\]")


def source_ids(text: str) -> set[str]:
    return set(SOURCE_PATTERN.findall(text))


def validate_references(text: str, known: list[str] | set[str], required: tuple | list | set = ()) -> None:
    references = source_ids(text)
    if not references:
        raise ValueError("Generated content must cite source IDs such as [S0001].")
    unknown = references - set(known)
    missing = set(required) - references
    if unknown:
        raise ValueError("Unknown source references: " + ", ".join(sorted(unknown)))
    if missing:
        raise ValueError("Missing source references: " + ", ".join(sorted(missing)))


def parse_topics(outline: str, known: list[str] | set[str]) -> list[tuple[str, str]]:
    headings = list(re.finditer(r"(?m)^## (.+)$", outline))
    if not headings:
        raise ValueError("The outline needs ## topic headings and source references.")
    topics = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(outline)
        topic = outline[heading.start():end].strip()
        validate_references(topic, known)
        topics.append((heading.group(1).strip(), topic))
    validate_references(outline, known)
    return topics


def compute_extraction_cache_key(model: str, system_prompt: str, segment_text: str) -> str:
    """
    Deterministic SHA-256 fingerprint for an extraction request.
    Changes if the model, prompt instructions, or segment text change.
    """
    hasher = hashlib.sha256()
    hasher.update(b"extraction\x00")
    hasher.update(model.strip().encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(system_prompt.strip().encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(segment_text.encode("utf-8"))
    return hasher.hexdigest()


def compute_merge_cache_key(model: str, system_prompt: str, child_keys: list[str]) -> str:
    """
    Deterministic SHA-256 fingerprint for a merge request.
    Includes ordered child cache keys so ancestor merges invalidate when any child changes.
    """
    hasher = hashlib.sha256()
    hasher.update(b"merge\x00")
    hasher.update(model.strip().encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(system_prompt.strip().encode("utf-8"))
    hasher.update(b"\x00")
    for key in child_keys:
        hasher.update(key.strip().encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def create_cache_record(
    kind: str,
    cache_key: str,
    text: str,
    manifest: list[str] | set[str],
    max_bytes: int = 16000,
) -> dict[str, Any]:
    """Create a structured cache dictionary with text hash and manifest."""
    text_bytes = text.encode("utf-8")
    return {
        "version": CACHE_SCHEMA_VERSION,
        "kind": kind,
        "cache_key": cache_key,
        "text": text,
        "text_hash": hashlib.sha256(text_bytes).hexdigest(),
        "manifest": sorted(list(set(manifest))),
        "max_bytes": max_bytes,
    }


def save_cache_entry(cache_dir: Path | str, record: dict[str, Any]) -> Path:
    """
    Atomically write a cache record to disk as JSON.
    Writes to a temporary file in the cache directory first, then replaces target.
    """
    dir_path = Path(cache_dir).resolve()
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / f"{record['cache_key']}.json"
    temp_path: Path | None = None
    try:
        content = json.dumps(record, indent=2, ensure_ascii=False)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".tmp",
            dir=dir_path,
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(content)
        os.replace(temp_path, target)
        return target
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def load_and_validate_cache_entry(
    cache_dir: Path | str,
    cache_key: str,
    expected_kind: str,
    expected_manifest: list[str] | set[str],
    max_bytes: int = 16000,
) -> dict[str, Any] | None:
    """
    Read and strictly validate a cache entry.
    Re-runs identical checks to live API output. Returns None on any failure (cache miss).
    """
    target = Path(cache_dir).resolve() / f"{cache_key}.json"
    if not target.is_file():
        return None

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    if data.get("version") != CACHE_SCHEMA_VERSION:
        return None
    if data.get("cache_key") != cache_key:
        return None
    if data.get("kind") != expected_kind:
        return None

    text = data.get("text")
    if not isinstance(text, str):
        return None

    text_bytes = text.encode("utf-8")
    if data.get("text_hash") != hashlib.sha256(text_bytes).hexdigest():
        return None

    record_max = data.get("max_bytes", max_bytes)
    if not isinstance(record_max, int) or len(text_bytes) > record_max or len(text_bytes) > max_bytes:
        return None

    manifest = data.get("manifest")
    if not isinstance(manifest, list):
        return None
    if set(manifest) != set(expected_manifest):
        return None

    # Semantic validation of note text
    try:
        required = manifest if expected_kind == KIND_EXTRACTION else ()
        validate_references(text, manifest, required=required)
        parse_topics(text, manifest)
    except Exception:
        return None

    return data


def calculate_merge_call_count(num_notes: int, fanout: int = 6) -> int:
    """
    Deterministically calculate the exact number of merge API calls required
    to combine *num_notes* into a single outline note using fixed *fanout*.
    """
    if not isinstance(num_notes, int) or num_notes < 0:
        raise ValueError("num_notes must be a non-negative integer")
    if not isinstance(fanout, int) or fanout < 2:
        raise ValueError("fanout must be an integer >= 2")
    if num_notes <= 1:
        return 0

    total = 0
    count = num_notes
    while count > 1:
        full_batches = count // fanout
        rem = count % fanout
        batches = full_batches + (1 if rem > 1 else 0)
        total += batches
        count = full_batches + (1 if rem > 0 else 0)
    return total
