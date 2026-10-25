from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_ros_domains_are_unique_and_restored(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "integration/conftest.py"
    shutil.copyfile(source, tmp_path / "conftest.py")
    (tmp_path / "test_domains.py").write_text(
        "import os\nimport pytest\nseen = set()\n"
        "@pytest.mark.requires_ros2\n@pytest.mark.parametrize('index', range(3))\n"
        "def test_each_domain(index):\n"
        "    domain = int(os.environ['ROS_DOMAIN_ID'])\n"
        "    assert domain in {*range(102), *range(215, 233)}\n"
        "    assert domain not in seen\n"
        "    seen.add(domain)\n"
        "    assert os.environ['ROS_AUTOMATIC_DISCOVERY_RANGE'] == 'LOCALHOST'\n"
        "def test_original_environment():\n"
        "    assert os.environ['ROS_DOMAIN_ID'] == '105'\n"
    )
    env = dict(os.environ)
    env["ROS_DOMAIN_ID"] = "105"
    env["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "SUBNET"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTEST_ADDOPTS"] = ""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "markers=requires_ros2: ROS runtime"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ros_processes_are_reaped_after_a_failed_test(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "integration/conftest.py"
    shutil.copyfile(source, tmp_path / "conftest.py")
    (tmp_path / "test_processes.py").write_text(
        "import subprocess\nimport sys\nimport pytest\nprocesses = []\n"
        "@pytest.mark.requires_ros2\ndef test_failure(ros_process):\n"
        "    process = ros_process([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    processes.append(process)\n"
        "    raise RuntimeError('exercise fixture teardown')\n"
        "def test_process_was_reaped():\n"
        "    assert len(processes) == 1\n"
        "    assert processes[0].poll() is not None\n"
    )
    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTEST_ADDOPTS"] = ""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "markers=requires_ros2: ROS runtime"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed, 1 passed" in result.stdout, result.stdout + result.stderr
