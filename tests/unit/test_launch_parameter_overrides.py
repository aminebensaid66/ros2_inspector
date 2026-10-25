from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ros2inspector.cli.app import app
from ros2inspector.model.uam import UAM
from tests.unit.name_resolution_fixtures import make_workspace, write_launch


@pytest.mark.parametrize(
    "parameters, expected",
    [
        ("[{'output_topic': '/configured'}]", "/configured"),
        ("[{'output_topic': '/first'}, {'output_topic': '/last'}]", "/last"),
    ],
)
def test_launch_parameters_replace_topic_default(
    tmp_path: Path, parameters: str, expected: str
) -> None:
    launch = make_workspace(tmp_path, parameters=True)
    write_launch(launch, f"Node(package='demo', executable='talker', parameters={parameters})")
    model = UAM.build(tmp_path, use_cache=False)
    assert model.topics()[0]["name"] == expected
    edge = next(d for _, _, d in model.graph.edges(data=True) if d.get("rel") == "publishes")
    assert edge["evidence"] == "launch_parameter_override"


def test_unread_launch_parameters_do_not_keep_defaults(tmp_path: Path) -> None:
    launch = make_workspace(tmp_path, parameters=True)
    write_launch(launch, "Node(package='demo', executable='talker', parameters=['params.yaml'])")
    model = UAM.build(tmp_path, use_cache=False)
    assert "parameters" in model.deployments()[0]["unresolved_fields"]
    assert not any(t["name"] == "/fallback" for t in model.topics())
    result = CliRunner().invoke(app, ["audit", str(tmp_path), "-f", "json"])
    assert not json.loads(result.stdout)["findings"]
