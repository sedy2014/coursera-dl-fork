# Repository Guidelines

## Project Structure & Module Organization

This fork extends `coursera-dl` with compatibility fixes, specialization workflows, and a Gradio interface. Preserve upstream attribution and license notices.

- `coursera-dl`: root CLI launcher; calls `coursera/coursera_dl.py`.
- `coursera/`: argument parsing, authentication, API access, extraction, and download workflows. `adapter.py` bridges UI inputs to the downloader; `gradio_ui.py` defines the interface.
- `gradio_app.py`: web UI launcher.
- `coursera/test/`: automated tests and fixtures, including JSON API responses.
- `assets/`: visual assets; `courses/`: ignored local download output.

## Build, Test, and Development Commands

Use a virtual environment and Python 3.11+ as the development starting point; verify compatibility before claiming support for other versions.

- `python -m pip install -r requirements.txt -r requirements-dev.txt`: install runtime and test dependencies.
- `uv run python .\coursera-dl -c .\cookies.txt --path .\courses --subtitle-language en <course-slug>`: download one course from the repository checkout.
- `uv run python gradio_app.py`: launch the Gradio interface.
- `python -m pytest coursera/test -v --cov=coursera --cov-report=html`: run the suite and generate coverage output.
- `python -m pytest coursera/test/test_adapter.py coursera/test/test_gradio_helpers.py -v`: check adapter and UI helpers.

The inherited Dockerfile installs the upstream PyPI package, not this checkout. The tox matrix targets historical Python versions; do not treat either as verified fork tooling.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase` classes, and uppercase constants. Match surrounding style and keep changes focused. Reuse shared authentication and download logic across CLI and UI. `CONTRIBUTING.md` recommends flake8 and pylint; no dedicated formatter configuration was found.

## Testing Guidelines

Use pytest with `test_*.py` files and `test_*` functions. Mock network interactions and use fixtures or temporary directories. Add regression coverage for behavior changes; no numeric coverage threshold is configured. Report baseline failures separately from new regressions.

CLI course downloads were manually tested by the maintainer. Gradio remains unverified. Do not infer certificate expansion, cross-platform support, or passing automated tests from that manual run.

## Commit & Pull Request Guidelines

History mixes descriptive subjects with `feat:`, `fix:`, and `docs+feat:` prefixes. Use a short, focused subject and explain why in the body. Describe behavior changes, related issues, test results, and limitations; include screenshots for UI changes.

## Security & Configuration

Never commit cookies, account credentials, authenticated logs, or downloaded course material. Sanitize fixtures and screenshots; use placeholders in examples.
