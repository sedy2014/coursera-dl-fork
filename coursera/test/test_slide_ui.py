import os

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

from coursera.gradio_ui import create_app


def test_slide_generator_shares_existing_tab_container():
    app = create_app()
    components = {entry["id"]: entry for entry in app.config["components"]}
    tabs = []

    def visit(node, parent=None):
        component = components.get(node["id"], {})
        if component.get("type") == "tabitem":
            tabs.append((component["props"]["label"], parent))
        for child in node.get("children", []):
            visit(child, node["id"])

    visit(app.config["layout"])
    assert [label for label, parent in tabs] == [
        "Setup", "Course Selection", "Progress & Logs", "Slide Generator",
    ]
    assert len({parent for label, parent in tabs}) == 1
