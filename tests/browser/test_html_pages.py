"""Open the generated HTML pages (docs and viz) in headless Chromium."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from typer.testing import CliRunner

from ros2inspector.cli.app import app

runner = CliRunner()


def _docs_html(workspace: Path) -> Path:
    result = runner.invoke(app, ["-q", "docs", str(workspace), "-f", "html", "--no-cache"])
    assert result.exit_code == 0, result.output
    return workspace / "ros2inspector_docs"


def test_docs_index_renders_and_every_link_resolves(page: Page, workspace: Path, shot: Any) -> None:
    out = _docs_html(workspace)
    page.goto((out / "index.html").as_uri())
    expect(page.locator("h1")).to_have_text("workspace_a")
    expect(page.locator("table").first).to_be_visible()
    links = page.locator("a[href^='packages/']")
    expect(links).to_have_count(3)
    shot(page, "docs-01-index")
    for href in links.evaluate_all("els => els.map(e => e.getAttribute('href'))"):
        assert (out / href).is_file(), href
    assert page.errors.messages == []  # type: ignore[attr-defined]


def test_docs_package_page_lists_endpoints_and_links_back(
    page: Page, workspace: Path, shot: Any
) -> None:
    out = _docs_html(workspace)
    page.goto((out / "index.html").as_uri())
    page.click("a[href='packages/pkg_a.html']")
    expect(page.locator("h1")).to_have_text("pkg_a")
    body = page.locator("body").inner_text()
    assert "/chatter" in body and "std_msgs/String" in body
    assert "msg_type=" not in body and "evidence=" not in body
    assert str(workspace) not in body
    shot(page, "docs-02-package")
    page.click("text=← Back to index")
    expect(page.locator("h1")).to_have_text("workspace_a")
    assert page.errors.messages == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("width", [375, 768])
def test_docs_pages_fit_small_screens(page: Page, workspace: Path, width: int, shot: Any) -> None:
    out = _docs_html(workspace)
    page.set_viewport_size({"width": width, "height": 800})
    page.goto((out / "packages" / "pkg_a.html").as_uri())
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    shot(page, f"docs-03-package-{width}px")
    # Long endpoint names may wrap inside tables, but the page itself must not scroll sideways
    # by more than a few pixels of table border.
    assert overflow <= 24, overflow


def test_docs_page_cannot_run_script_from_package_xml(page: Page, workspace: Path) -> None:
    pkg = workspace / "src" / "evil"
    pkg.mkdir()
    (pkg / "package.xml").write_text(
        '<?xml version="1.0"?><package format="3"><name>evil_pkg</name>'
        "<version>1.0.0</version>"
        "<description>&lt;img src=x onerror=&quot;document.title='pwned'&quot;&gt;</description>"
        '<maintainer email="a@b.c">&lt;script&gt;document.title="pwned"&lt;/script&gt;</maintainer>'
        "<license>MIT</license></package>",
        encoding="utf-8",
    )
    out = _docs_html(workspace)
    page.goto((out / "packages" / "evil_pkg.html").as_uri())
    page.wait_for_timeout(200)
    assert page.title() != "pwned"
    assert "<img src=x" in page.locator("body").inner_text()


def test_viz_graph_renders_offline(page: Page, workspace: Path, shot: Any) -> None:
    target = workspace / "graph.html"
    result = runner.invoke(
        app, ["-q", "viz", "-C", str(workspace), "-o", str(target), "--no-open", "--no-cache"]
    )
    assert result.exit_code == 0, result.output
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(target.as_uri())
    page.wait_for_function(
        "() => typeof cy !== 'undefined' && cy && cy.nodes().length > 0", timeout=10000
    )
    count = page.evaluate("cy.nodes().length")
    assert count >= 3
    assert all(url.startswith(("file:", "data:", "blob:")) for url in requests), requests
    shot(page, "viz-01-graph")
    # Cytoscape logs deprecation warnings for the existing viz styles; only errors fail here.
    errors = [m for m in page.errors.messages if not m.startswith("console.warning")]  # type: ignore[attr-defined]
    assert errors == []
