from __future__ import annotations

import json
from pathlib import Path

import pytest

from ros2inspector.model.uam import UAM
from tests.unit.name_resolution_fixtures import make_workspace, write_launch


@pytest.mark.parametrize("suffix", [".py", ".xml", ".yaml"])
def test_duplicate_remaps_keep_order_and_json_pairs(tmp_path: Path, suffix: str) -> None:
    launch = make_workspace(tmp_path)
    launch = launch.with_suffix(suffix)
    if suffix == ".py":
        write_launch(
            launch,
            "Node(package='demo', executable='talker', "
            "remappings=[('chatter', '/first'), ('chatter', '/last')])",
        )
    elif suffix == ".xml":
        launch.write_text(
            '<launch><node pkg="demo" exec="talker">'
            '<remap from="chatter" to="/first"/>'
            '<remap from="chatter" to="/last"/></node></launch>'
        )
    else:
        launch.write_text(
            "launch:\n- node:\n    pkg: demo\n    exec: talker\n"
            "    remap:\n    - {from: chatter, to: /first}\n"
            "    - {from: chatter, to: /last}\n"
        )
    model = UAM.build(tmp_path, use_cache=False)
    assert [topic["name"] for topic in model.topics()] == ["/first"]
    payload = json.loads(json.dumps(model.to_dict()))
    expected = [["chatter", "/first"], ["chatter", "/last"]]
    assert payload["launch_remaps"]["demo"][0]["remaps"] == expected
    node = next(n for n in payload["graph"]["nodes"] if n["kind"] == "Node")
    assert node["deployments"][0]["remaps"] == expected
