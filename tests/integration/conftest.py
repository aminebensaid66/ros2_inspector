from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURE_WS = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"


@pytest.fixture(scope="module")
def real_ws() -> Path:
    """Portable representative workspace committed with the repository."""
    return FIXTURE_WS


@pytest.fixture(scope="session")
def _ros_domain_pool() -> Iterator[int]:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    index = int(worker[2:]) if worker.startswith("gw") else 0
    workers = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1"))
    domains = [*range(10, 102), *range(215, 233), *range(1, 10)]
    return iter(domains[index::workers])


@pytest.fixture(autouse=True)
def ros_domain(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    _ros_domain_pool: Iterator[int],
) -> Iterator[int | None]:
    if request.node.get_closest_marker("requires_ros2") is None:
        yield None
        return
    domain = next(_ros_domain_pool, None)
    if domain is None:
        pytest.fail("No unused ROS test domain remains for this worker")
    monkeypatch.setenv("ROS_DOMAIN_ID", str(domain))
    monkeypatch.setenv("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")
    yield domain


@pytest.fixture
def ros_environment(ros_domain: int | None) -> dict[str, str]:
    assert ros_domain is not None, "ROS fixtures require the requires_ros2 marker"
    return dict(os.environ)


@pytest.fixture
def ros_process(
    ros_environment: dict[str, str],
) -> Iterator[Callable[..., subprocess.Popen[bytes]]]:
    processes: list[subprocess.Popen[bytes]] = []

    def start(command: list[str], **options: Any) -> subprocess.Popen[bytes]:
        env = ros_environment | (options.pop("env", None) or {})
        env["ROS_DOMAIN_ID"] = ros_environment["ROS_DOMAIN_ID"]
        env["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"
        process = subprocess.Popen(command, env=env, **{**options, "start_new_session": True})
        processes.append(process)
        return process

    def signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    try:
        yield start
    finally:
        for process in processes:
            signal_group(process, signal.SIGINT)
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                signal_group(process, signal.SIGKILL)
                process.wait(timeout=5)
            finally:
                # A leader may exit before a descendant that ignored the interrupt.
                signal_group(process, signal.SIGKILL)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
