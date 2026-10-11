"""
slide_gen/transcript_scanner.py
--------------------------------
Scan .en.txt transcripts recursively under any selected folder.

Preserve every directory level relative to the selected root. Folder names
are structural labels; course, module, and section roles are not inferred.
Results are sorted by relative path, including numeric filename prefixes.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def scan_transcripts(folder: str) -> list[dict]:
    """
    Walk *folder* recursively and collect all ``*.en.txt`` files.

    Returns
    -------
    list of dicts, each containing:
        path    : str  — absolute path to the file
        rel     : str  — path relative to *folder*
        folder_parts : list[str] — all relative parent directories, in order
        folder  : str  — relative containing directory, or "" for root files
        lecture : str  — filename stem without the ``.en`` suffix
        source_id : str — stable path-order identifier, starting at S0001
        text    : str | None  — file text, loaded lazily by load_texts()

    Raises ValueError if the directory cannot be resolved or scanned.
    """
    try:
        root = Path(folder).resolve(strict=True)
        if not stat.S_ISDIR(root.stat().st_mode):
            raise ValueError(f"Transcript folder is not a directory: '{root}'")
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot access transcript folder '{folder}': {exc}") from exc

    def raise_scan_error(error: OSError) -> None:
        raise error

    paths: list[Path] = []
    try:
        for directory, _, filenames in os.walk(root, onerror=raise_scan_error):
            for filename in filenames:
                path = Path(directory) / filename
                if filename.endswith(".en.txt") and path.is_file():
                    paths.append(path)
    except OSError as exc:
        raise ValueError(f"Cannot scan transcript folder '{root}': {exc}") from exc

    results: list[dict] = []

    for source_number, path in enumerate(
        sorted(paths, key=lambda path: path.relative_to(root).as_posix()), start=1
    ):
        rel_parts = path.relative_to(root).parts
        folder_parts = list(rel_parts[:-1])
        lecture = path.stem
        if lecture.endswith(".en"):
            lecture = lecture[:-3]

        results.append({
            "path":    str(path),
            "rel":     path.relative_to(root).as_posix(),
            "folder_parts": folder_parts,
            "folder":  "/".join(folder_parts),
            "lecture": lecture,
            "source_id": f"S{source_number:04d}",
            "text":    None,
        })

    return results


def load_texts(entries: list[dict]) -> list[dict]:
    """
    Read text from disk for each entry in *entries* (in-place, returns list).
    Skips entries that already have text loaded.
    Accepts UTF-8 with an optional BOM. Failed entries retain text=None and
    raise ValueError naming the relative file; earlier successes remain loaded.
    """
    for entry in entries:
        if entry["text"] is None:
            try:
                text = Path(entry["path"]).read_text(encoding="utf-8-sig", errors="strict")
            except (OSError, UnicodeError) as exc:
                raise ValueError(
                    f"Cannot read transcript '{entry['rel']}' as UTF-8: {exc}"
                ) from exc
            if not text.strip():
                raise ValueError(f"Transcript '{entry['rel']}' is empty or whitespace-only")
            entry["text"] = text
    return entries


def group_by_folder(entries: list[dict]) -> dict[str, list[dict]]:
    """
    Group by complete relative containing directory, preserving input order.
    Root-level transcripts use the empty key. Identically named directories
    beneath different parents remain separate groups.
    """
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        groups.setdefault(entry["folder"], []).append(entry)
    return groups


def summarise(entries: list[dict]) -> str:
    """
    Count transcript-containing folders and maximum depth below the root.
    The selected root counts as one folder when it contains transcripts.
    """
    count = len(entries)
    if count == 0:
        return "No .en.txt transcript files found in this folder."
    folders = len(group_by_folder(entries))
    depth = max(len(entry["folder_parts"]) for entry in entries)
    return (
        f"Found **{count}** transcript{'s' if count != 1 else ''} "
        f"in **{folders}** folder{'s' if folders != 1 else ''} "
        f"containing transcripts (maximum depth: **{depth}** below selected root)."
    )


def build_llm_context(entries: list[dict], token_budget: int = 6000) -> list[str]:
    """
    Return text segments no larger than *token_budget* UTF-8 bytes, using byte size
    as a conservative token upper bound. Headers and separators count too.
    Each fragment has a ``[source_id] relative/path`` header followed by a
    newline. Blocks within a text segment are separated by two newlines. Oversized
    text is split only at Unicode character boundaries, with nothing stripped.
    Call load_texts() first. Invalid budgets or unloaded entries raise ValueError.
    """
    if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget <= 0:
        raise ValueError("token_budget must be a positive integer byte budget")
    for entry in entries:
        if entry["text"] is None:
            raise ValueError(
                f"Transcript '{entry['rel']}' is not loaded; call load_texts() first"
            )

    txt_segs: list[str] = []
    current_parts: list[str] = []
    current_bytes = 0

    for entry in entries:
        header = f"[{entry['source_id']}] {Path(entry['rel']).as_posix()}\n"
        header_bytes = len(header.encode("utf-8"))
        capacity = token_budget - header_bytes
        body = entry["text"].encode("utf-8")
        if capacity < 1 or (
            capacity < 4 and any(len(character.encode("utf-8")) > capacity
                                 for character in entry["text"])
        ):
            raise ValueError(
                f"token_budget is too small for the header and transcript text "
                f"of '{entry['rel']}'"
            )

        offset = 0
        while offset < len(body) or not body:
            end = min(offset + capacity, len(body))
            while end < len(body) and body[end] & 0xC0 == 0x80:
                end -= 1
            block = header + body[offset:end].decode("utf-8")
            block_bytes = header_bytes + end - offset
            separator_bytes = 2 if current_parts else 0
            if current_bytes + separator_bytes + block_bytes > token_budget:
                txt_segs.append("\n\n".join(current_parts))
                current_parts = []
                current_bytes = 0
                separator_bytes = 0
            current_parts.append(block)
            current_bytes += separator_bytes + block_bytes
            offset = end
            if not body:
                break

    if current_parts:
        txt_segs.append("\n\n".join(current_parts))

    return txt_segs
