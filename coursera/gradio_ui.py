"""
coursera/gradio_ui.py
---------------------
Gradio 6.x web interface for coursera-dl.

Tabs:
  1. Setup         — cookies, output path, quality / format options
  2. Course Selection — URL input, slug review table, CLI preview
  3. Progress & Logs  — streaming log panel, cancel

Launch via:
    python gradio_app.py
"""

import os
import time
import queue
import logging
import threading

import gradio as gr

from .adapter import (
    compute_folder, slug_to_display, build_command, extract_slug,
    fetch_all_metadata, load_conf_file,
)

# ---------------------------------------------------------------------------
# Global log queue — all coursera-dl logging flows here for the UI to drain
# ---------------------------------------------------------------------------

_log_queue: queue.Queue = queue.Queue()


class _QueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_queue.put_nowait(self.format(record))
        except Exception:
            pass


def _attach_log_handler() -> None:
    root = logging.getLogger()
    if not any(isinstance(h, _QueueHandler) for h in root.handlers):
        h = _QueueHandler()
        h.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
        root.addHandler(h)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Cancel signal (set by Cancel button; cleared at download start)
# ---------------------------------------------------------------------------

_cancel_event = threading.Event()
_is_downloading = False          # guard against concurrent download runs

# ---------------------------------------------------------------------------
# Theme & CSS  (passed to launch() in Gradio 6.x)
# ---------------------------------------------------------------------------

THEME = gr.themes.Default(
    # Amber = golden in dark mode; CSS overrides swap to lime-green in light mode.
    primary_hue=gr.themes.colors.amber,
    secondary_hue=gr.themes.colors.yellow,
    neutral_hue=gr.themes.colors.zinc,
    font=[gr.themes.GoogleFont("Inter"), "ui-sans-serif", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "monospace"],
)

CSS = """
/* ═══════════════════════════════════════════════════════════════════════════
   DARK (default) — golden / amber accents on near-black
   LIGHT           — lime-yellow accents on white  (Horse Browser style)
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Base tokens (dark-first) ────────────────────────────────────────────── */
:root {
    /* Header */
    --header-bg:      #0f0f12;
    --header-text:    #f4f4f5;
    --header-sub:     #a1a1aa;
    --badge-bg:       #292524;
    --badge-text:     #fbbf24;
    --badge-border:   #78350f;

    /* Cookie step cards */
    --step-border:    #3f3f46;
    --step-num-bg:    #fbbf24;
    --step-num-text:  #1c1917;

    /* Accent override for primary buttons in dark mode (golden) */
    --cdl-accent:     #fbbf24;
    --cdl-accent-fg:  #1c1917;
    --cdl-accent-hover: #f59e0b;
}

/* ── Light mode token swap ───────────────────────────────────────────────── */
/* Matches both OS-level light preference and Gradio's manual light toggle   */
@media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
        --header-bg:      #ffffff;
        --header-text:    #0a0a0a;
        --header-sub:     #52525b;
        --badge-bg:       #c5e232;
        --badge-text:     #1a1a00;
        --badge-border:   #a3ba1a;

        --step-border:    #e4e4e7;
        --step-num-bg:    #c5e232;
        --step-num-text:  #1a1a00;

        --cdl-accent:     #c5e232;
        --cdl-accent-fg:  #1a1a00;
        --cdl-accent-hover: #b5d020;
    }
}
:root[data-theme="light"] {
    --header-bg:      #ffffff;
    --header-text:    #0a0a0a;
    --header-sub:     #52525b;
    --badge-bg:       #c5e232;
    --badge-text:     #1a1a00;
    --badge-border:   #a3ba1a;

    --step-border:    #e4e4e7;
    --step-num-bg:    #c5e232;
    --step-num-text:  #1a1a00;

    --cdl-accent:     #c5e232;
    --cdl-accent-fg:  #1a1a00;
    --cdl-accent-hover: #b5d020;
}

/* ── Light mode: override Gradio's amber buttons → lime-yellow ───────────── */
@media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) .gr-button-primary,
    :root:not([data-theme="dark"]) button[class*="primary"] {
        background: var(--cdl-accent) !important;
        color:      var(--cdl-accent-fg) !important;
        border-color: var(--cdl-accent) !important;
    }
    :root:not([data-theme="dark"]) .gr-button-primary:hover,
    :root:not([data-theme="dark"]) button[class*="primary"]:hover {
        background: var(--cdl-accent-hover) !important;
        border-color: var(--cdl-accent-hover) !important;
    }
}
:root[data-theme="light"] .gr-button-primary,
:root[data-theme="light"] button[class*="primary"] {
    background: var(--cdl-accent) !important;
    color:      var(--cdl-accent-fg) !important;
    border-color: var(--cdl-accent) !important;
}
:root[data-theme="light"] .gr-button-primary:hover,
:root[data-theme="light"] button[class*="primary"]:hover {
    background: var(--cdl-accent-hover) !important;
    border-color: var(--cdl-accent-hover) !important;
}

/* ── Header ──────────────────────────────────────────────────────────────── */
.cdl-header {
    background: var(--header-bg);
    color: var(--header-text);
    padding: 22px 28px 20px;
    border-radius: 12px;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    gap: 16px;
    border: 1px solid var(--badge-border);
}
.cdl-header-wordmark {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: var(--badge-bg);
    color: var(--badge-text);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    padding: 4px 10px;
    border-radius: 6px;
    margin-bottom: 8px;
    border: 1px solid var(--badge-border);
}
.cdl-header-body h1 {
    margin: 0;
    font-size: 1.45rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--header-text);
}
.cdl-header-body p {
    margin: 3px 0 0;
    font-size: 0.85rem;
    color: var(--header-sub);
}

/* ── Cookie step guide ───────────────────────────────────────────────────── */
.cookie-steps ol {
    padding-left: 0;
    list-style: none;
    margin: 8px 0 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.cookie-steps li {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 10px 14px;
    border: 1px solid var(--step-border);
    border-radius: 8px;
    font-size: 0.88rem;
    line-height: 1.5;
}
.cookie-steps li .step-num {
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: var(--step-num-bg);
    color: var(--step-num-text);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.75rem;
    font-weight: 700;
    margin-top: 1px;
}

/* ── Compact file upload ─────────────────────────────────────────────────── */
.small-upload .wrap {
    min-height: 72px  !important;
    padding: 10px 16px !important;
}
.small-upload .icon-wrap svg { width: 20px; height: 20px; }
.small-upload .file-name { font-size: 0.8rem; }

/* ── Log panel ───────────────────────────────────────────────────────────── */
.log-panel textarea {
    font-family: 'JetBrains Mono', 'Fira Code', monospace !important;
    font-size: 12px !important;
    background: #0f0f12 !important;
    color: #e4e4e7   !important;
    border-radius: 8px !important;
}
"""

HEADER_HTML = """
<div class="cdl-header">
  <div class="cdl-header-body">
    <div class="cdl-header-wordmark">&#128218;&nbsp; Coursera-Omni-Scribe</div>
    <h1>Download any Coursera course.</h1>
    <p>Paste a URL, confirm the courses, and go &mdash; no CLI required.</p>
  </div>
</div>
"""

COOKIE_STEPS_HTML = """
<div class="cookie-steps">
<ol>
  <li>
    <span class="step-num">1</span>
    <span>Go to <strong><a href="https://www.coursera.org" target="_blank">coursera.org</a></strong>
    and sign in to your account.</span>
  </li>
  <li>
    <span class="step-num">2</span>
    <span>Install the <strong><a href="https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc" target="_blank">Get cookies.txt LOCALLY</a></strong>
    extension for Chrome or Edge.</span>
  </li>
  <li>
    <span class="step-num">3</span>
    <span>While on <strong>any coursera.org page</strong>, click the extension icon in the toolbar.</span>
  </li>
  <li>
    <span class="step-num">4</span>
    <span>Click <strong>Export</strong> — a <code>cookies.txt</code> file will download.</span>
  </li>
  <li>
    <span class="step-num">5</span>
    <span>Upload that file using the button below, then click <strong>Validate cookies &amp; connect</strong>.</span>
  </li>
</ol>
</div>
"""

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _make_table_rows(api_slugs: list, output_path_val: str) -> list:
    rows = []
    for i, api_slug in enumerate(api_slugs, 1):
        display = slug_to_display(api_slug)
        folder  = compute_folder(api_slug, display)
        already = output_path_val and os.path.exists(
            os.path.join(output_path_val, folder)
        )
        badge = ("✅ " + str(i)) if already else str(i)
        rows.append([badge, api_slug, display, folder, True])
    return rows


def _rows_to_cli(
    rows, cookies_file_obj, output_path_val,
    subtitle_lang, resolution_val, file_formats_val,
    jobs_val, overwrite_val, quiz_val, nb_val,
) -> str:
    import pandas as pd
    if rows is None or (isinstance(rows, pd.DataFrame) and rows.empty):
        return "# Resolve some courses above to see the command"
    if isinstance(rows, pd.DataFrame):
        rows = rows.values.tolist()
    if not rows:
        return "# Resolve some courses above to see the command"
    included = [r for r in rows if len(r) >= 5 and r[4]]
    if not included:
        return "# No courses selected (check the Include? column)"
    return build_command(
        cookies_file=cookies_file_obj.name if cookies_file_obj else "<cookies.txt>",
        path=output_path_val or ".",
        subtitle_language=subtitle_lang or "en",
        resolution=resolution_val or "540p",
        file_formats=file_formats_val if file_formats_val else ["all"],
        jobs=int(jobs_val or 1),
        slugs=[r[1] for r in included],
        is_specialization=False,
        overwrite=bool(overwrite_val),
        download_quizzes=bool(quiz_val),
        download_notebooks=bool(nb_val),
    )


def _drain_queue(log_lines: list, max_lines: int = 500) -> bool:
    """Pull all pending messages from _log_queue into log_lines. Returns True if any added."""
    added = False
    while True:
        try:
            log_lines.append(_log_queue.get_nowait())
            added = True
        except queue.Empty:
            break
    # Keep a rolling window to avoid unbounded growth in the UI
    if len(log_lines) > max_lines:
        log_lines[:] = log_lines[-max_lines:]
    return added


# ---------------------------------------------------------------------------
# Download generator  (streams progress + logs to the UI)
# ---------------------------------------------------------------------------

def _run_download(
    courses, session, output_path_val, cookies_file_obj,
    subtitle_lang, resolution_val, file_formats_val,
    jobs_val, overwrite_val, quiz_val, nb_val,
    progress=gr.Progress(),
):
    """
    Generator that runs the download and yields UI state updates after each
    batch of log messages or after each course finishes.

    Yields 7-tuples:
        (start_btn, start_btn2, cancel_btn, reset_btn, current_course_md, log_panel, summary_md)
    """
    # Shorthand for common update patterns
    def _state(starting=False, running=False, done=False, course_msg="", summary="", log=""):
        enabled = done or (not running and not starting)
        return (
            gr.update(interactive=enabled),   # start_btn
            gr.update(interactive=enabled),   # start_btn2
            gr.update(interactive=running),   # cancel_btn
            gr.update(interactive=enabled),   # reset_btn
            course_msg,
            log,
            summary,
        )

    global _is_downloading
    log_lines: list[str] = []

    # Validation ─────────────────────────────────────────────────────────────
    if _is_downloading:
        yield _state(done=True,
                     course_msg="⚠️ A download is already in progress. Please wait or Cancel first.")
        return

    if session is None:
        yield _state(done=True,
                     course_msg="❌ Not authenticated — validate cookies in the Setup tab first.")
        return

    included = [r for r in (courses or []) if len(r) >= 5 and r[4]]
    if not included:
        yield _state(done=True,
                     course_msg="❌ No courses selected — resolve some URLs in Course Selection first.")
        return

    # Setup ───────────────────────────────────────────────────────────────────
    _is_downloading = True
    _cancel_event.clear()
    _drain_queue(log_lines)   # clear any stale messages from previous runs

    from .adapter import make_args
    from .coursera_dl import download_class

    args = make_args(
        path=output_path_val or ".",
        cookies_file=cookies_file_obj.name if cookies_file_obj else None,
        resolution=resolution_val or "540p",
        subtitle_language=subtitle_lang or "en",
        file_formats=file_formats_val if file_formats_val else ["all"],
        jobs=int(jobs_val or 1),
        overwrite=bool(overwrite_val),
        download_quizzes=bool(quiz_val),
        download_notebooks=bool(nb_val),
    )

    n = len(included)
    log_lines.append(f"INFO     Starting download of {n} course(s)...")
    yield _state(running=True,
                 course_msg=f"⬇️  Starting {n} course(s)…",
                 log="\n".join(log_lines))

    completed_ok, completed_err = [], []

    for idx, row in enumerate(included):
        if _cancel_event.is_set():
            log_lines.append("INFO     ⏹  Download cancelled by user.")
            break

        api_slug    = str(row[1])
        folder_name = str(row[3])
        label       = f"({idx + 1}/{n}) {folder_name}"

        progress((idx, n), desc=f"Course {idx+1}/{n}: {folder_name}")
        log_lines.append(f"INFO     ── Downloading {label}")
        yield _state(running=True,
                     course_msg=f"⬇️  {label}",
                     log="\n".join(log_lines))

        # Run blocking download in a daemon thread so we can stream logs ─────
        result_box: list = [None]
        exc_box:    list = [None]

        def _do(slug=api_slug, fname=folder_name):
            try:
                result_box[0] = download_class(session, args, slug, folder_name=fname)
            except Exception as e:
                exc_box[0] = e

        t = threading.Thread(target=_do, daemon=True)
        t.start()

        # Poll while thread is alive: drain log queue and yield updates ───────
        while t.is_alive():
            time.sleep(0.4)
            if _drain_queue(log_lines):
                yield _state(running=True,
                             course_msg=f"⬇️  {label}",
                             log="\n".join(log_lines))
            if _cancel_event.is_set():
                # Can't stop mid-file; wait up to 60 s for current file then move on
                t.join(timeout=60)
                break

        t.join(timeout=60)   # safety net: never block forever
        _drain_queue(log_lines)  # final drain for this course

        if exc_box[0] is not None:
            completed_err.append(folder_name)
            log_lines.append(f"ERROR    {label}: {exc_box[0]}")
        else:
            err_flag, done_flag = result_box[0] or (False, False)
            if err_flag:
                completed_err.append(folder_name)
                log_lines.append(f"WARNING  {label}: finished with parse errors")
            else:
                completed_ok.append(folder_name)
                log_lines.append(f"INFO     ✅ {label}: complete")

        progress((idx + 1, n), desc=f"Done {idx+1}/{n}")
        yield _state(running=True,
                     course_msg=f"✅ Done {idx+1}/{n}: {folder_name}",
                     log="\n".join(log_lines))

        # Delay between courses (respect args.download_delay = 5 s)
        if idx < n - 1 and not _cancel_event.is_set():
            for _ in range(10):   # 5 s in 0.5-s ticks
                if _cancel_event.is_set():
                    break
                time.sleep(0.5)
                _drain_queue(log_lines)

    # Final summary ───────────────────────────────────────────────────────────
    _drain_queue(log_lines)
    cancelled = _cancel_event.is_set()

    parts = []
    if completed_ok:
        parts.append(f"✅ **{len(completed_ok)} completed:** " + ", ".join(completed_ok))
    if completed_err:
        parts.append(f"⚠️ **{len(completed_err)} with errors:** " + ", ".join(completed_err))
    if cancelled:
        remaining = n - len(completed_ok) - len(completed_err)
        parts.append(
            f"⏹ **Cancelled** — {len(completed_ok) + len(completed_err)} of {n} courses processed, "
            f"{remaining} skipped.\n\n"
            "Already-downloaded files are preserved. "
            "Click **Start Download** again to resume — coursera-dl will skip files that already exist."
        )

    summary = "\n\n".join(parts) if parts else "No courses were downloaded."
    log_lines.append("INFO     Download session ended.")

    _is_downloading = False   # clear before final yield so buttons re-enable cleanly
    yield _state(done=True,
                 course_msg="✅ Download finished." if not cancelled else "⏹ Download cancelled.",
                 log="\n".join(log_lines),
                 summary=summary)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> gr.Blocks:
    _attach_log_handler()

    with gr.Blocks(title="Coursera-Omni-Scribe") as app:

        gr.HTML(HEADER_HTML)

        session_state = gr.State(None)
        courses_state = gr.State([])

        with gr.Tabs() as tabs:

            # ================================================================
            # TAB 1 — Setup
            # ================================================================
            with gr.Tab("Setup"):
                gr.Markdown("### Authentication & download options")

                with gr.Row():
                    with gr.Column(scale=2):
                        # Step-by-step cookie guide (collapsible)
                        with gr.Accordion("How to export your Coursera cookies", open=False):
                            gr.HTML(COOKIE_STEPS_HTML)

                        cookies_file = gr.File(
                            label="cookies.txt",
                            file_types=[".txt"],
                            file_count="single",
                            elem_classes=["small-upload"],
                        )
                        output_path = gr.Textbox(
                            label="Download path",
                            placeholder=r"e.g.  .\courses\IBM Data Science",
                            info="Folder where course sub-directories will be created.",
                        )
                    with gr.Column(scale=1):
                        resolution = gr.Radio(
                            choices=["360p", "540p", "720p"],
                            value="540p",
                            label="Video resolution",
                        )
                        subtitle_language = gr.Textbox(
                            label="Subtitle language(s)",
                            value="en",
                            info='Use "all" for every language, or "en,zh-CN" for multiple.',
                        )
                        jobs = gr.Slider(
                            minimum=1, maximum=8, step=1, value=1,
                            label="Parallel download jobs",
                        )

                with gr.Row():
                    file_formats = gr.CheckboxGroup(
                        choices=["mp4", "pdf", "srt", "txt", "zip"],
                        value=["mp4", "pdf"],
                        label="File formats to download",
                        info="Uncheck all to download every available format.",
                    )
                    with gr.Column():
                        download_quizzes   = gr.Checkbox(label="Download quiz questions",    value=False)
                        download_notebooks = gr.Checkbox(label="Download Jupyter notebooks", value=False)
                        overwrite          = gr.Checkbox(label="Overwrite existing files",   value=False)

                connect_btn  = gr.Button("Validate cookies & connect", variant="primary")
                setup_status = gr.Markdown("")

                def on_connect(cookies_file_obj, _path):
                    if cookies_file_obj is None:
                        return None, "⚠️ Please upload a cookies.txt file first."
                    try:
                        from .adapter import build_session as _bs
                        sess = _bs(cookies_file=cookies_file_obj.name)
                        return sess, "✅ Session ready — cookies loaded successfully."
                    except Exception as exc:
                        return None, f"❌ Failed to load cookies: {exc}"

                connect_btn.click(
                    fn=on_connect,
                    inputs=[cookies_file, output_path],
                    outputs=[session_state, setup_status],
                )

            # ================================================================
            # TAB 2 — Course Selection
            # ================================================================
            with gr.Tab("Course Selection"):
                gr.Markdown("### Paste a Coursera URL")
                gr.Markdown(
                    "Works for individual courses, specializations, and "
                    "professional certificates. One URL (or bare slug) per line."
                )

                url_input = gr.Textbox(
                    label="Coursera URL(s)",
                    placeholder=(
                        "https://www.coursera.org/specializations/deep-learning\n"
                        "https://www.coursera.org/learn/neural-networks-deep-learning\n"
                        "ibm-data-science"
                    ),
                    lines=3,
                    info="Specializations are auto-expanded into all their component courses.",
                )

                resolve_btn    = gr.Button("Resolve courses", variant="primary")
                resolve_status = gr.Markdown("")
                meta_card      = gr.Markdown("", visible=False)

                course_table = gr.Dataframe(
                    headers=["#", "API Slug", "Display Name", "Folder Name", "Include?"],
                    datatype=["str", "str", "str", "str", "bool"],
                    interactive=True,
                    label=(
                        "Resolved courses — edit Display Name to rename the folder; "
                        "uncheck Include? to skip"
                    ),
                    row_count=(0, "dynamic"),
                    column_count=(5, "fixed"),
                    wrap=True,
                )

                gr.Markdown(
                    "_Tip: **API Slug** and **Folder Name** are managed automatically. "
                    "Only edit **Display Name** and **Include?**._"
                )

                cli_preview = gr.Code(
                    label="Equivalent CLI command (copy-pasteable reference)",
                    language="shell",
                    interactive=False,
                    value="# Resolve some courses above to see the command",
                )

                start_btn2 = gr.Button("Start Download", variant="primary", interactive=False)

                # ── Resolve ─────────────────────────────────────────────────
                def on_resolve(
                    url_text, session, output_path_val,
                    cookies_file_obj, subtitle_lang, resolution_val,
                    file_formats_val, jobs_val, overwrite_val, quiz_val, nb_val,
                ):
                    if not url_text or not url_text.strip():
                        return ([], [], "Please enter at least one URL or slug.",
                                gr.update(value="", visible=False),
                                "# Resolve some courses above to see the command",
                                gr.update(interactive=False))

                    all_slugs, errors = [], []
                    for url in [u.strip() for u in url_text.strip().splitlines() if u.strip()]:
                        try:
                            slug, kind = extract_slug(url)
                        except ValueError as exc:
                            errors.append(str(exc)); continue

                        if kind == "specialization":
                            if session is None:
                                errors.append(
                                    f"Cannot expand '{slug}': please validate your "
                                    "cookies in the Setup tab first."
                                )
                                continue
                            try:
                                from .api import expand_specializations
                                all_slugs.extend(expand_specializations(session, [slug]))
                            except Exception as exc:
                                errors.append(f"Failed to expand '{slug}': {exc}")
                        else:
                            all_slugs.append(slug)

                    rows = _make_table_rows(all_slugs, output_path_val or "")
                    cli  = _rows_to_cli(rows, cookies_file_obj, output_path_val,
                                        subtitle_lang, resolution_val, file_formats_val,
                                        jobs_val, overwrite_val, quiz_val, nb_val)

                    status = f"Resolved **{len(rows)}** course(s)."
                    if errors:
                        status += "\n\n**Errors:**\n" + "\n".join(f"- {e}" for e in errors)

                    # -- Metadata preview card --------------------------------
                    meta_md = ""
                    if all_slugs:
                        try:
                            # session may be None — fetch_all_metadata falls back
                            # to unauthenticated requests for the public API
                            meta = fetch_all_metadata(session, all_slugs)
                            blocks = ["### 📋 Course preview\n"]
                            for i, slug in enumerate(all_slugs, 1):
                                info = meta.get(slug, {})
                                name = info.get("name") or slug
                                desc = info.get("description") or ""
                                objectives = info.get("objectives") or []
                                already = output_path_val and os.path.exists(
                                    os.path.join(output_path_val, compute_folder(slug, slug_to_display(slug)))
                                )
                                badge = " ✅" if already else ""
                                course_lines = [f"**{i}. {name}**{badge}"]
                                # Learning objectives (What you'll learn)
                                if objectives:
                                    course_lines.append("\n**What you'll learn:**")
                                    for obj in objectives:
                                        course_lines.append(f"- {obj}")
                                elif desc:
                                    course_lines.append(f"\n> {desc}")
                                blocks.append("\n".join(course_lines))
                            meta_md = "\n\n---\n\n".join(blocks)
                        except Exception:
                            pass  # metadata is nice-to-have, never fatal

                    meta_update = gr.update(value=meta_md, visible=bool(meta_md))
                    return (rows, rows, status, meta_update, cli,
                            gr.update(interactive=len(rows) > 0))

                resolve_btn.click(
                    fn=on_resolve,
                    inputs=[url_input, session_state, output_path, cookies_file,
                            subtitle_language, resolution, file_formats, jobs,
                            overwrite, download_quizzes, download_notebooks],
                    outputs=[courses_state, course_table, resolve_status,
                             meta_card, cli_preview, start_btn2],
                )

                # ── Table edit: recompute Folder Name ────────────────────────
                def on_table_change(
                    table_data, cookies_file_obj, output_path_val,
                    subtitle_lang, resolution_val, file_formats_val,
                    jobs_val, overwrite_val, quiz_val, nb_val,
                ):
                    import pandas as pd
                    if table_data is None or (isinstance(table_data, pd.DataFrame) and table_data.empty):
                        return table_data, table_data, "# Resolve some courses above to see the command"
                    updated = []
                    _rows = table_data.values.tolist() if isinstance(table_data, pd.DataFrame) else table_data
                    for row in _rows:
                        if len(row) < 5:
                            updated.append(row); continue
                        badge, api_slug, display_name, _, include = row[0], row[1], row[2], row[3], row[4]
                        folder = compute_folder(str(api_slug), str(display_name))
                        updated.append([badge, api_slug, display_name, folder, include])
                    cli = _rows_to_cli(updated, cookies_file_obj, output_path_val,
                                       subtitle_lang, resolution_val, file_formats_val,
                                       jobs_val, overwrite_val, quiz_val, nb_val)
                    return updated, updated, cli

                course_table.change(
                    fn=on_table_change,
                    inputs=[course_table, cookies_file, output_path, subtitle_language,
                            resolution, file_formats, jobs, overwrite,
                            download_quizzes, download_notebooks],
                    outputs=[course_table, courses_state, cli_preview],
                )

                # ── Setup changes: refresh CLI preview ───────────────────────
                _setup_inputs = [course_table, cookies_file, output_path, subtitle_language,
                                 resolution, file_formats, jobs, overwrite,
                                 download_quizzes, download_notebooks]

                def refresh_cli(table_data, cookies_file_obj, output_path_val,
                                subtitle_lang, resolution_val, file_formats_val,
                                jobs_val, overwrite_val, quiz_val, nb_val):
                    import pandas as pd
                    _td = [] if (table_data is None or (isinstance(table_data, pd.DataFrame) and table_data.empty)) else table_data
                    return _rows_to_cli(_td, cookies_file_obj, output_path_val,
                                        subtitle_lang, resolution_val, file_formats_val,
                                        jobs_val, overwrite_val, quiz_val, nb_val)

                for _trigger in [subtitle_language, resolution, output_path, jobs,
                                  overwrite, download_quizzes, download_notebooks, file_formats]:
                    _trigger.change(fn=refresh_cli, inputs=_setup_inputs, outputs=[cli_preview])

            # ================================================================
            # TAB 3 — Progress & Logs
            # ================================================================
            with gr.Tab("Progress & Logs"):
                gr.Markdown("### Download progress")

                with gr.Row():
                    start_btn  = gr.Button("Start Download", variant="primary",   interactive=False)
                    cancel_btn = gr.Button("Cancel",         variant="stop",       interactive=False)
                    reset_btn  = gr.Button("↩ Reset",        variant="secondary",  interactive=False)

                current_course_md = gr.Markdown("*No download running.*")
                log_panel = gr.Textbox(
                    label="Download log",
                    lines=24,
                    max_lines=500,
                    interactive=False,
                    elem_classes=["log-panel"],
                )
                summary_md = gr.Markdown("")

                def on_cancel():
                    global _is_downloading
                    _cancel_event.set()
                    _is_downloading = False   # unblock guard so Start Download re-enables after timeout
                    return gr.update(interactive=False), "⏹ Cancellation requested — finishing current file…"

                cancel_btn.click(
                    fn=on_cancel,
                    inputs=[],
                    outputs=[cancel_btn, current_course_md],
                )

                def on_reset():
                    """Clear log/status and jump back to Course Selection tab."""
                    return (
                        "*No download running.*",   # current_course_md
                        "",                          # log_panel
                        "",                          # summary_md
                        gr.update(interactive=False),  # reset_btn — disable until next run
                        gr.update(selected=1),          # tabs → Course Selection
                    )

                reset_btn.click(
                    fn=on_reset,
                    inputs=[],
                    outputs=[current_course_md, log_panel, summary_md, reset_btn, tabs],
                )

        # ====================================================================
        # Cross-tab wiring (registered after all components exist)
        # ====================================================================

        # Enable start_btn (Tab 3) when courses are resolved
        courses_state.change(
            fn=lambda courses: gr.update(interactive=bool(courses)),
            inputs=[courses_state],
            outputs=[start_btn],
        )

        # Shared download inputs / outputs lists
        _dl_inputs = [
            courses_state, session_state, output_path, cookies_file,
            subtitle_language, resolution, file_formats, jobs,
            overwrite, download_quizzes, download_notebooks,
        ]
        _dl_outputs = [start_btn, start_btn2, cancel_btn, reset_btn, current_course_md, log_panel, summary_md]

        start_btn.click(
            fn=_run_download,
            inputs=_dl_inputs,
            outputs=_dl_outputs,
        )

        start_btn2.click(
            fn=_run_download,
            inputs=_dl_inputs,
            outputs=_dl_outputs,
        )

        # ====================================================================
        # ✅ Badge refresh: re-check already-downloaded folders when path changes
        # ====================================================================
        def refresh_badges(table_data, new_path):
            import pandas as pd
            if table_data is None or (isinstance(table_data, pd.DataFrame) and table_data.empty):
                return table_data
            updated = []
            _rows = table_data.values.tolist() if isinstance(table_data, pd.DataFrame) else table_data
            for row in _rows:
                if len(row) < 5:
                    updated.append(row); continue
                badge_raw, api_slug, display_name, folder, include = (
                    row[0], row[1], row[2], row[3], row[4]
                )
                # Extract bare index number (strip existing ✅ prefix)
                idx = badge_raw.replace("✅ ", "").strip()
                already = new_path and os.path.exists(os.path.join(new_path, str(folder)))
                badge = ("✅ " + idx) if already else idx
                updated.append([badge, api_slug, display_name, folder, include])
            return updated

        output_path.change(
            fn=refresh_badges,
            inputs=[course_table, output_path],
            outputs=[course_table],
        )

        # ====================================================================
        # Persistent settings: pre-fill from coursera-dl.conf on app load
        # ====================================================================
        def _load_persistent_settings():
            conf = load_conf_file()
            return (
                conf.get("path", ""),
                conf.get("subtitle_language", "en"),
                conf.get("resolution", "540p"),
            )

        app.load(
            fn=_load_persistent_settings,
            inputs=[],
            outputs=[output_path, subtitle_language, resolution],
        )

    return app
