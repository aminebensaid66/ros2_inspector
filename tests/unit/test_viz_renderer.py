from __future__ import annotations

import json
import re
from pathlib import Path

from ros2inspector.model.uam import UAM
from ros2inspector.viz.renderer import generate_html


def test_generate_html_preserves_data_matching_a_template_placeholder(tmp_path: Path) -> None:
    model = UAM()
    model.graph.add_node(
        "node:demo/publisher",
        kind="Node",
        name="__SUMMARY_JSON__",
        package="demo",
    )

    html = generate_html(model, tmp_path)
    match = re.search(r"const GRAPHS\s*=\s*(\{.*?\});\nconst DIAGNOSTICS", html)

    assert match is not None
    graphs = json.loads(match.group(1))
    assert any(node["data"].get("label") == "__SUMMARY_JSON__" for node in graphs["comms"]["nodes"])
