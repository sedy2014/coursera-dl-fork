"""
gradio_app.py
-------------
Entry point for the coursera-dl Gradio web interface.

Usage:
    uv run python gradio_app.py
    uv run python gradio_app.py --port 7861 --share

The app opens at http://localhost:7860 by default.
"""

import argparse
import sys
import os

# Make sure the package root is on the path when run as a script
sys.path.insert(0, os.path.dirname(__file__))

from coursera.gradio_ui import create_app, THEME, CSS


def main():
    parser = argparse.ArgumentParser(description="coursera-dl Gradio web UI")
    parser.add_argument("--port", type=int, default=7860, help="Port to listen on (default: 7860)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--share", action="store_true", help="Create a public Gradio share link")
    parser.add_argument("--debug", action="store_true", help="Enable Gradio debug mode")
    args = parser.parse_args()

    app = create_app()
    app.launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        debug=args.debug,
        show_error=True,
        theme=THEME,
        css=CSS,
    )


if __name__ == "__main__":
    main()
