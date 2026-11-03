"""
slide_gen/transcript_scanner.py
--------------------------------
Scan .en.txt transcripts recursively under any selected folder.

Preserve every directory level relative to the selected root. Folder names
are structural labels; course, module, and section roles are not inferred.
Results are sorted by relative path, including numeric filename prefixes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path


def find_dataset_root(folder: str | Path) -> Path:
    """
    Find dataset root: nearest parent folder named 'courses', or the folder itself.
    """
    root = Path(folder).resolve()
    if root.name == "courses":
        return root
    for parent in root.parents:
        if parent.name == "courses":
            return parent
    return root


def natural_sort_key(path: Path | str) -> tuple:
    """
    Per-path-component natural sort key so that numbers sort numerically
    (e.g., 2_lecture before 10_lecture).
    """
    parts = Path(path).parts
    key = []
    for part in parts:
        chunks = re.split(r"(\d+)", part)
        key.append(tuple((0, int(c)) if c.isdigit() else (1, c.lower()) for c in chunks if c))
    return tuple(key)


def scan_transcripts(folder: str, dataset_root: str | Path | None = None) -> list[dict]:
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
        source_id : str — stable 12-hex hash derived from dataset root relative path
        text    : str | None  — file text, loaded lazily by load_texts()

    Raises ValueError if the directory cannot be resolved or scanned,
    or if a source ID collision occurs.
    """
    try:
        root = Path(folder).resolve(strict=True)
        if not stat.S_ISDIR(root.stat().st_mode):
            raise ValueError(f"Transcript folder is not a directory: '{root}'")
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot access transcript folder '{folder}': {exc}") from exc

    d_root = Path(dataset_root).resolve(strict=True) if dataset_root is not None else find_dataset_root(root)

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
    seen_ids: dict[str, str] = {}

    for path in sorted(paths, key=lambda p: natural_sort_key(p.relative_to(root))):
        rel_parts = path.relative_to(root).parts
        folder_parts = list(rel_parts[:-1])
        lecture = path.stem
        if lecture.endswith(".en"):
            lecture = lecture[:-3]

        rel_to_d_root = path.relative_to(d_root).as_posix()
        source_id = f"S{hashlib.sha256(rel_to_d_root.encode('utf-8')).hexdigest()[:12]}"

        if source_id in seen_ids and seen_ids[source_id] != rel_to_d_root:
            raise ValueError(
                f"Source ID collision detected for ID '{source_id}' between "
                f"'{seen_ids[source_id]}' and '{rel_to_d_root}'"
            )
        seen_ids[source_id] = rel_to_d_root

        results.append({
            "path":    str(path),
            "rel":     path.relative_to(root).as_posix(),
            "rel_root": rel_to_d_root,
            "folder_parts": folder_parts,
            "folder":  "/".join(folder_parts),
            "lecture": lecture,
            "source_id": source_id,
            "text":    None,
        })

    # Save mapping ID -> relative path under cache directory
    if seen_ids:
        try:
            cache_dir = d_root / ".slide_gen_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            map_file = cache_dir / "source_ids.json"
            existing: dict[str, str] = {}
            if map_file.exists():
                try:
                    existing = json.loads(map_file.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            for sid, rel in seen_ids.items():
                if sid in existing and existing[sid] != rel:
                    raise ValueError(
                        f"Source ID collision detected for ID '{sid}' between "
                        f"'{existing[sid]}' and '{rel}'"
                    )
                existing[sid] = rel
            tmp_map = map_file.with_suffix(".tmp")
            tmp_map.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
            tmp_map.replace(map_file)
        except (OSError, RuntimeError):
            pass

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
        path_in_prompt = entry.get("rel_root", entry["rel"])
        header = f"[{entry['source_id']}] {Path(path_in_prompt).as_posix()}\n"
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


def split_text_deterministic(text: str, max_bytes: int) -> list[str]:
    """
    Deterministically split *text* into chunks each <= *max_bytes* UTF-8 bytes.
    Preserves all text so that "".join(chunks) == text.
    Hierarchy:
    1. Paragraph boundaries (\\n\\n)
    2. Sentence boundaries (. , ? , ! , \\n)
    3. Character / UTF-8 boundary as last resort.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    tokens = [t for t in re.findall(r".*?(?:\n\n|$)", text) if t]
    chunks: list[str] = []
    current = ""

    for token in tokens:
        if len(token.encode("utf-8")) > max_bytes:
            if current:
                chunks.append(current)
                current = ""
            sub_tokens = [st for st in re.findall(r".*?(?:\. |\? |\! |\n|$)", token) if st]
            for st in sub_tokens:
                if len(st.encode("utf-8")) > max_bytes:
                    if current:
                        chunks.append(current)
                        current = ""
                    st_bytes = st.encode("utf-8")
                    offset = 0
                    while offset < len(st_bytes):
                        end = min(offset + max_bytes, len(st_bytes))
                        while end < len(st_bytes) and (st_bytes[end] & 0xC0 == 0x80):
                            end -= 1
                        part = st_bytes[offset:end].decode("utf-8")
                        chunks.append(part)
                        offset = end
                else:
                    if len((current + st).encode("utf-8")) <= max_bytes:
                        current += st
                    else:
                        if current:
                            chunks.append(current)
                        current = st
        else:
            if len((current + token).encode("utf-8")) <= max_bytes:
                current += token
            else:
                if current:
                    chunks.append(current)
                current = token

    if current:
        chunks.append(current)

    return chunks


def build_extraction_segments(entries: list[dict], budget: int = 24000) -> list[str]:
    """
    Group transcripts by immediate folder and return extraction segments <= *budget* UTF-8 bytes.
    If an entire folder fits within *budget*, it is kept in a single segment.
    If it exceeds *budget*, it is deterministically split strictly within that folder
    (preserving paragraphs, then sentences, then characters). Sibling folders are never mixed.
    """
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError("budget must be a positive integer byte budget")
    for entry in entries:
        if entry["text"] is None:
            raise ValueError(
                f"Transcript '{entry['rel']}' is not loaded; call load_texts() first"
            )

    groups = group_by_folder(entries)
    extraction_segs: list[str] = []

    for _, folder_entries in groups.items():
        full_blocks = [
            f"[{e['source_id']}] {Path(e.get('rel_root', e['rel'])).as_posix()}\n{e['text']}"
            for e in folder_entries
        ]
        folder_text = "\n\n".join(full_blocks)
        if len(folder_text.encode("utf-8")) <= budget:
            extraction_segs.append(folder_text)
            continue

        current_parts: list[str] = []
        current_bytes = 0

        for entry in folder_entries:
            path_in_prompt = entry.get("rel_root", entry["rel"])
            header = f"[{entry['source_id']}] {Path(path_in_prompt).as_posix()}\n"
            header_bytes = len(header.encode("utf-8"))
            capacity = budget - header_bytes
            if capacity < 1:
                raise ValueError(
                    f"budget is too small for the header and transcript text of '{entry['rel']}'"
                )

            body_chunks = split_text_deterministic(entry["text"], capacity)
            for chunk in body_chunks:
                block = header + chunk
                block_bytes = header_bytes + len(chunk.encode("utf-8"))
                separator_bytes = 2 if current_parts else 0
                if current_bytes + separator_bytes + block_bytes > budget:
                    if current_parts:
                        extraction_segs.append("\n\n".join(current_parts))
                        current_parts = []
                        current_bytes = 0
                        separator_bytes = 0
                current_parts.append(block)
                current_bytes += separator_bytes + block_bytes

        if current_parts:
            extraction_segs.append("\n\n".join(current_parts))

    return extraction_segs

