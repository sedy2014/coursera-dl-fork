"""Gradio controls for source-backed tutorial and slide generation."""

from pathlib import Path

import gradio as gr

from .llm_client import ANTHROPIC_MODELS, OPENAI_MODELS
from .pipeline import slides, synthesise, tutorial
from .transcript_scanner import scan_transcripts, summarise


def _scan_folder(folder):
    try:
        entries = scan_transcripts(folder)
        return summarise(entries), str(Path(folder).expanduser().resolve() / "slides")
    except (OSError, ValueError) as error:
        return f"Cannot scan transcripts: {error}", ""


def _file_update(path):
    return gr.update(value=path, visible=bool(path))


def build_slide_tab():
    """Build controls inside the existing application's fourth tab."""
    snapshot = gr.State({})
    with gr.Accordion("Setup", open=True):
        gr.Markdown(
            "Selected transcript text is sent to your chosen API provider. "
            "Review the outline and supplemental explanations before using the output."
        )
        with gr.Row():
            folder = gr.Textbox(label="Transcript folder", placeholder=r".\courses\course-slug")
            output_folder = gr.Textbox(label="Output folder")
            base_filename = gr.Textbox(label="Base filename (no extension)", value="course_notes")
        summary = gr.Markdown(
            "Choose any folder containing .en.txt transcripts. All subfolders are scanned; "
            "their complete paths are preserved without inferring course/module/section roles."
        )
        with gr.Row():
            provider = gr.Dropdown(choices=["Anthropic", "OpenAI"], value="OpenAI", label="API provider")
            model = gr.Dropdown(choices=OPENAI_MODELS, value=OPENAI_MODELS[0],
                                allow_custom_value=True, label="Model")
            api_key = gr.Textbox(label="API key", type="password")

    with gr.Accordion("Step 1 — Curriculum Outline", open=True):
        gr.Markdown(
            "Synthesise source-referenced topics, then edit the outline. "
            "Keep ## topic headings and [S0001]-style source references."
        )
        outline_button = gr.Button("Synthesise outline", variant="primary")
        outline_status = gr.Markdown("")
        outline = gr.Textbox(label="Curriculum outline (editable)", lines=18, max_lines=60)

    with gr.Accordion("Step 2 — Generate Tutorial", open=True):
        depth = gr.Slider(minimum=1, maximum=5, step=1, value=3, label="Explanation depth")
        tutorial_button = gr.Button("Generate Tutorial", variant="primary")
        tutorial_status = gr.Markdown("")
        tutorial_preview = gr.Markdown("", height=500)
        tutorial_file = gr.File(label="Download tutorial", visible=False)

    with gr.Accordion("Step 2 — Generate Slides (optional)", open=False):
        gr.Markdown(
            "Generates validated slide content and renders it with maintained layouts. "
            "Dense content is split across continuation slides."
        )
        with gr.Row():
            palette = gr.Dropdown(choices=["IBM New", "GCP", "IBM Original"],
                                  value="IBM New", label="Palette")
            style = gr.Textbox(label="Teaching style guidance",
                               placeholder="For example: concise explanations with practical examples.")
        slides_button = gr.Button("Generate Slides", variant="primary")
        slides_log = gr.Textbox(label="Generation log", lines=10, interactive=False)
        slides_file = gr.File(label="Download slides", visible=False)
        table = gr.Dataframe(headers=["Filename", "Size", "Full path"],
                             datatype=["str", "str", "str"], interactive=False,
                             row_count=(0, "dynamic"), label="Decks in output folder")

    def change_provider(value):
        choices = ANTHROPIC_MODELS if value == "Anthropic" else OPENAI_MODELS
        return gr.update(choices=choices, value=choices[0])

    def change_folder(value):
        message, destination = _scan_folder(value)
        return message, destination, "", {}, _file_update(None), _file_update(None)

    def generate_tutorial(*args):
        for text, status, path in tutorial(*args):
            yield text, status, _file_update(path)

    def generate_slides(*args):
        for status, path, rows in slides(*args):
            yield status, _file_update(path), rows

    provider.change(fn=change_provider, inputs=[provider], outputs=[model])
    folder.change(fn=change_folder, inputs=[folder],
                  outputs=[summary, output_folder, outline, snapshot, tutorial_file, slides_file],
                  concurrency_id="slide_generation", concurrency_limit=1)
    outline_button.click(fn=synthesise, inputs=[folder, provider, model, api_key],
                         outputs=[outline, outline_status, snapshot],
                         concurrency_id="slide_generation", concurrency_limit=1)
    tutorial_button.click(
        fn=generate_tutorial,
        inputs=[outline, depth, provider, model, api_key, output_folder, base_filename, snapshot, folder],
        outputs=[tutorial_preview, tutorial_status, tutorial_file],
        concurrency_id="slide_generation", concurrency_limit=1,
    )
    slides_button.click(
        fn=generate_slides,
        inputs=[outline, style, provider, model, api_key, output_folder, base_filename, snapshot, folder, palette],
        outputs=[slides_log, slides_file, table],
        concurrency_id="slide_generation", concurrency_limit=1,
    )
