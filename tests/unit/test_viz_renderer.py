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


_HOSTILE = "ws<!--<script>name</script>& x"


def test_json_for_script_leaves_no_markup_characters() -> None:
    from ros2inspector.utils.html import json_for_script

    encoded = json_for_script({"path": _HOSTILE})
    assert not any(char in encoded for char in "<>&  ")
    assert json.loads(encoded) == {"path": _HOSTILE}


def test_hostile_names_and_paths_stay_data_in_the_viz_page(tmp_path: Path) -> None:
    model = UAM()
    model.graph.add_node("node:demo/n", kind="Node", name=_HOSTILE, package="demo")
    root = tmp_path / "ws<!--<script>name"
    html = generate_html(model, root)
    data = re.search(
        r"const GRAPHS  = (.*?);\nconst DIAGNOSTICS = (.*?);\nconst SUMMARY = (.*?);\n", html
    )
    assert data is not None
    for block in data.groups():
        assert not any(char in block for char in "<>&")
    graphs = json.loads(data.group(1))
    assert any(node["data"].get("label") == _HOSTILE for node in graphs["comms"]["nodes"])
    # Outside the data, the path only appears HTML-escaped.
    assert "<!--" not in html.split("<script>", 2)[0]
    assert "ws&lt;!--&lt;script&gt;name" in html


def test_designer_page_uses_the_same_script_escaping() -> None:
    from ros2inspector.design.ui import build_html

    html = build_html({"packages": [{"pkg_name": _HOSTILE}]}, session_token=_HOSTILE)
    assert "<!--<script>" not in html
    assert "\\u003c!--\\u003cscript\\u003e" in html
