"""
coursera/adapter.py

-------------------
Thin adapter layer between the Gradio web UI and the existing coursera-dl
internals.  Nothing here modifies core download logic — it just provides
helper functions so Gradio can:

  • Extract a (slug, kind) pair from any Coursera URL
  • Build an authenticated requests.Session without going through argparse
  • Construct a SimpleNamespace that looks like a parsed argparse.Namespace
  • Compute the on-disk folder name that mirrors the CLI naming convention
  • Render the equivalent CLI command for reference / copy-paste
"""

import re
import os
import shlex
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace

# ---------------------------------------------------------------------------
# URL → slug extraction
# ---------------------------------------------------------------------------

# Each entry: (regex_pattern, kind)
#   kind is 'course' or 'specialization'
_URL_PATTERNS = [
    (r'coursera\.org/learn/([^/?#]+)',                     'course'),
    (r'coursera\.org/specializations/([^/?#]+)',           'specialization'),
    (r'coursera\.org/professional-certificates/([^/?#]+)', 'specialization'),
]


def extract_slug(url: str):
    """
    Parse a Coursera URL and return ``(slug, kind)``.

    Parameters
    ----------
    url : str
        Any Coursera course / specialization / professional-certificate URL,
        or a bare slug (returned unchanged with kind='course').

    Returns
    -------
    tuple[str, str]
        ``(slug, kind)`` where kind is ``'course'`` or ``'specialization'``.

    Raises
    ------
    ValueError
        If the URL is not a recognised Coursera URL and does not look like a
        plain slug.
    """
    url = url.strip()
    for pattern, kind in _URL_PATTERNS:
        m = re.search(pattern, url)
        if m:
            return m.group(1).rstrip('/'), kind

    # Bare slug — no slashes, no dots → treat as single course slug
    if re.fullmatch(r'[a-z0-9][a-z0-9-]*', url):
        return url, 'course'

    raise ValueError(
        f"Unrecognised URL or slug: {url!r}\n"
        "Expected a coursera.org/learn/…, coursera.org/specializations/…, "
        "or coursera.org/professional-certificates/… URL."
    )


# ---------------------------------------------------------------------------
# Session construction — delegate to the canonical implementation in
# coursera_dl so there is exactly one copy of the auth logic.
# ---------------------------------------------------------------------------

def build_session(cookies_file: str | None = None, cauth: str | None = None):
    """
    Create an authenticated ``requests.Session``.
    Thin wrapper around ``coursera_dl.build_session`` — see that function
    for full parameter documentation.
    """
    from .coursera_dl import build_session as _build_session
    return _build_session(cookies_file=cookies_file, cauth=cauth)


# ---------------------------------------------------------------------------
# Programmatic args construction (no argparse required)
# ---------------------------------------------------------------------------

def make_args(
    path: str,
    cookies_file: str | None,
    resolution: str = '540p',
    subtitle_language: str = 'en',
    file_formats: list | None = None,
    jobs: int = 1,
    class_names: list | None = None,
    overwrite: bool = False,
    resume: bool = True,
    download_quizzes: bool = False,
    download_notebooks: bool = False,
    cauth: str | None = None,
) -> SimpleNamespace:
    """
    Build a SimpleNamespace that looks exactly like a parsed argparse.Namespace
    so it can be passed straight into ``download_class()`` and friends.
    """
    if file_formats is None:
        file_formats = ['all']
    if class_names is None:
        class_names = []

    return SimpleNamespace(
        # Identity
        path=path,
        cookies_file=cookies_file,
        cookies_cauth=cauth,
        class_names=class_names,
        # Video / subtitles
        video_resolution=resolution,
        subtitle_language=subtitle_language,
        file_formats=file_formats,
        ignore_formats=None,
        # Parallelism
        jobs=jobs,
        download_delay=5,          # shorter than CLI default; GUI shows progress
        # Download behaviour
        overwrite=overwrite,
        resume=resume,
        reverse=False,
        preview=False,
        about=False,
        download_quizzes=download_quizzes,
        download_notebooks=download_notebooks,
        only_syllabus=False,
        # Filtering (disabled — user selects courses in the table)
        section_filter=None,
        lecture_filter=None,
        resource_filter=None,
        # Specialization flag always False here — expansion is done before
        # download_class() is ever called in the Gradio flow
        specialization=False,
        assume_yes=True,
        # File naming
        verbose_dirs=False,
        combined_section_lectures_nums=False,
        unrestricted_filenames=False,
        disable_url_skipping=False,
        # External downloaders (disabled)
        wget=None,
        curl=None,
        aria2=None,
        axel=None,
        downloader_arguments=[],
        # Misc
        skip_download=False,
        playlist=False,
        hooks=[],
        mathjax_cdn_url='https://cdn.mathjax.org/mathjax/latest/MathJax.js',
        cache_syllabus=False,
        quiet=False,
        debug=False,
    )


# ---------------------------------------------------------------------------
# Folder name computation (mirrors CLI _to_slug logic in coursera_dl.py)
# ---------------------------------------------------------------------------

def compute_folder(api_slug: str, display_name: str) -> str:
    """
    Derive the on-disk folder name for a course.

    Rules (same as the interactive CLI prompt):
    - If display_name is blank or equals the api_slug → use api_slug as-is
    - Otherwise → ``{display_slug}_{api_slug}``
      where display_slug is the slugified form of display_name
    """
    display_name = display_name.strip()
    if not display_name:
        return api_slug

    slug = display_name.lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug.strip())
    slug = re.sub(r'-+', '-', slug).strip('-')

    if not slug or slug == api_slug:
        return api_slug
    return f"{slug}_{api_slug}"


def slug_to_display(api_slug: str) -> str:
    """
    Convert an API slug to a human-readable display name as a starting
    suggestion (hyphens → spaces, title-cased).

    e.g. ``"intro-tensorflow"`` → ``"Intro Tensorflow"``
    """
    return api_slug.replace('-', ' ').title()


# ---------------------------------------------------------------------------
# CLI command builder (for the reference panel in the Gradio UI)
# ---------------------------------------------------------------------------

def build_command(
    cookies_file: str | None,
    path: str,
    subtitle_language: str,
    resolution: str,
    file_formats: list,
    jobs: int,
    slugs: list,                  # final list of API slugs to pass
    is_specialization: bool,      # True only when a single spec slug, no corrections
    overwrite: bool = False,
    download_quizzes: bool = False,
    download_notebooks: bool = False,
) -> str:
    """
    Render the equivalent ``uv run python coursera-dl …`` command string so
    the user can copy-paste it into a terminal.
    """
    parts = ['uv run python .\\coursera-dl']

    if cookies_file:
        parts.append(f'-c {shlex.quote(cookies_file)}')

    if is_specialization and len(slugs) == 1:
        parts.append('--specialization')

    if path:
        parts.append(f'--path {shlex.quote(path)}')

    if subtitle_language and subtitle_language != 'all':
        parts.append(f'--subtitle-language {shlex.quote(subtitle_language)}')

    if resolution and resolution != '540p':
        parts.append(f'--video-resolution {resolution}')

    if file_formats and file_formats != ['all']:
        parts.append(f'-f {shlex.quote(" ".join(file_formats))}')

    if jobs > 1:
        parts.append(f'--jobs {jobs}')

    if overwrite:
        parts.append('--overwrite')

    if download_quizzes:
        parts.append('--download-quizzes')

    if download_notebooks:
        parts.append('--download-notebooks')

    parts.extend(slugs)

    # Wrap long commands for readability (one option per line on Windows)
    return ' ^\n  '.join(parts)


# ---------------------------------------------------------------------------
# Course metadata fetch (uses the authenticated session)
# ---------------------------------------------------------------------------

_COURSES_V1_URL = (
    "https://api.coursera.org/api/onDemandCourses.v1"
    "?q=slug&slug={slug}&fields=name,description,learningObjectives"
)

_log = logging.getLogger(__name__)


def fetch_course_metadata(session, api_slug: str) -> dict:
    """
    Fetch name, description, learning objectives, and skills for a single
    course slug.

    Returns a dict with keys:
      ``name``        — course title
      ``description`` — short blurb (fallback)
      ``objectives``  — list of "What you'll learn" bullet strings
      ``skills``      — list of skill strings (shown in Outcomes section)

    The onDemandCourses.v1 endpoint is public; ``session`` is used when
    provided (for auth), but a plain requests.get is tried as fallback so
    metadata still loads even before the user connects cookies.
    Falls back gracefully — never raises.
    """
    import requests as _requests
    try:
        url = _COURSES_V1_URL.format(slug=api_slug)
        try:
            r = session.get(url, timeout=8) if session else _requests.get(url, timeout=8)
        except Exception:
            r = _requests.get(url, timeout=8)

        _log.debug("fetch_course_metadata %s → HTTP %s", api_slug, r.status_code)
        r.raise_for_status()

        data = r.json()
        _log.debug("fetch_course_metadata %s raw keys: %s",
                   api_slug, list(data.get("elements", [{}])[0].keys()) if data.get("elements") else "no elements")

        elements = data.get("elements", [])
        if elements:
            el = elements[0]

            # Short marketing blurb — fallback when objectives absent
            desc = (el.get("description", "") or "").strip()
            if len(desc) > 220:
                desc = desc[:220].rsplit(" ", 1)[0] + "…"

            # "What you'll learn" bullets (outcomes / learningObjectives section)
            raw_obj = el.get("learningObjectives") or []
            objectives = [o.strip() for o in raw_obj if o and o.strip()]

            _log.info("fetch_course_metadata %s: name=%r  objectives=%d",
                      api_slug, el.get("name"), len(objectives))

            return {
                "name": el.get("name", api_slug),
                "description": desc,
                "objectives": objectives,
                "skills": [],
            }
        else:
            _log.warning("fetch_course_metadata %s: empty elements list", api_slug)
    except Exception as exc:
        _log.warning("fetch_course_metadata %s failed: %s", api_slug, exc)
    return {"name": api_slug, "description": "", "objectives": [], "skills": []}


def fetch_all_metadata(session, api_slugs: list, max_workers: int = 6) -> dict:
    """
    Fetch metadata for multiple slugs in parallel.

    ``session`` may be None — each worker falls back to an unauthenticated
    request so metadata loads even before cookies are validated.

    Returns a dict mapping ``api_slug -> {name, description, objectives, skills}``.
    """
    results = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(api_slugs) or 1)) as pool:
        future_to_slug = {
            pool.submit(fetch_course_metadata, session, slug): slug
            for slug in api_slugs
        }
        for future in as_completed(future_to_slug):
            slug = future_to_slug[future]
            try:
                results[slug] = future.result()
            except Exception:
                results[slug] = {"name": slug, "description": "", "objectives": [], "skills": []}
    return results


# ---------------------------------------------------------------------------
# Persistent settings loader (reads coursera-dl.conf if present)
# ---------------------------------------------------------------------------

# Maps conf-file key → (UI field name, type)
_CONF_FIELDS = {
    "path":             "path",
    "subtitle-language": "subtitle_language",
    "video-resolution": "resolution",
    "subtitle_language": "subtitle_language",  # underscore alias
    "video_resolution":  "resolution",
}

_VALID_RESOLUTIONS = {"360p", "540p", "720p"}


def load_conf_file(conf_path: str = "coursera-dl.conf") -> dict:
    """
    Parse ``coursera-dl.conf`` and return a dict of recognised UI field values.

    Keys in the returned dict:
      ``path``, ``subtitle_language``, ``resolution``
    """
    if not os.path.isfile(conf_path):
        return {}

    raw: dict[str, str] = {}
    with open(conf_path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                raw[key.strip().lower()] = val.strip()

    settings: dict[str, str] = {}
    for conf_key, field in _CONF_FIELDS.items():
        if conf_key in raw:
            val = raw[conf_key]
            if field == "resolution" and val not in _VALID_RESOLUTIONS:
                continue   # ignore unrecognised resolution values
            settings[field] = val

    return settings
