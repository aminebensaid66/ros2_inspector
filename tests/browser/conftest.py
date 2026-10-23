"""Headless-browser fixtures. Skipped unless Playwright and a Chromium build are available.

Run with ``pytest -m browser``. Set ``ROS2INSPECTOR_SCREENSHOT_DIR`` to keep screenshots.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Browser, Error, Page, sync_playwright  # noqa: E402

from ros2inspector.design.server import create_design_server  # noqa: E402

FIXTURE_WS = Path(__file__).parent.parent / "fixtures" / "workspaces" / "workspace_a"
_FALLBACK_CHROMIUM = ("/opt/pw-browsers/chromium",)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/browser" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(pytest.mark.browser)


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        candidates: list[str | None] = [executable] if executable else [None]
        candidates += [path for path in _FALLBACK_CHROMIUM if Path(path).exists()]
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                instance = playwright.chromium.launch(executable_path=candidate)
                break
            except Error as exc:
                last_error = exc
        else:
            pytest.skip(f"no usable Chromium for Playwright: {last_error}")
        yield instance
        instance.close()


class PageErrors:
    """Console errors, uncaught exceptions and failed requests seen by a page."""

    def __init__(self, page: Page) -> None:
        self.messages: list[str] = []
        page.on("pageerror", lambda exc: self.messages.append(f"pageerror: {exc}"))
        page.on(
            "console",
            lambda msg: (
                self.messages.append(f"console.{msg.type}: {msg.text}")
                if msg.type in {"error", "warning"}
                else None
            ),
        )
        page.on(
            "requestfailed",
            lambda req: self.messages.append(f"requestfailed: {req.url} {req.failure}"),
        )


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    instance = context.new_page()
    instance.errors = PageErrors(instance)  # type: ignore[attr-defined]
    yield instance
    context.close()


@pytest.fixture
def shot(request: pytest.FixtureRequest) -> Any:
    """Save a full-page screenshot when ROS2INSPECTOR_SCREENSHOT_DIR is set."""
    target = os.environ.get("ROS2INSPECTOR_SCREENSHOT_DIR")

    def take(page: Page, name: str) -> None:
        if not target:
            return
        folder = Path(target)
        folder.mkdir(parents=True, exist_ok=True)
        page.wait_for_timeout(400)  # let the sidebar and toast transitions finish
        page.screenshot(path=str(folder / f"{name}.png"), full_page=True)

    return take


@pytest.fixture
def designer(tmp_path: Path) -> Iterator[dict[str, Any]]:
    out = tmp_path / "ws" / "src"
    out.mkdir(parents=True)
    server = create_design_server(0, out, Console(quiet=True))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    yield {"url": f"http://127.0.0.1:{server.server_address[1]}/", "out": out}
    server.shutdown()
    server.server_close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace_a"
    shutil.copytree(FIXTURE_WS, root)
    return root
