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


def _open_viz(page: Page, workspace: Path) -> None:
    target = workspace / "graph.html"
    result = runner.invoke(
        app, ["-q", "viz", "-C", str(workspace), "-o", str(target), "--no-open", "--no-cache"]
    )
    assert result.exit_code == 0, result.output
    page.goto(target.as_uri())
    page.wait_for_function("() => typeof cy !== 'undefined' && cy.nodes().length > 0")


def _settle(page: Page) -> None:
    page.wait_for_timeout(1300)  # longest layout animation is 500ms, cose needs a moment


_OVERLAPS = """() => {
  const ns = cy.nodes(':childless:visible'); let c = 0;
  for (let i = 0; i < ns.length; i++) for (let j = i + 1; j < ns.length; j++) {
    const a = ns[i].boundingBox(), b = ns[j].boundingBox();
    if (a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2) c++;
  }
  return c; }"""
_OUTSIDE = """() => { const e = cy.extent();
  return cy.nodes(':visible').filter(n => { const b = n.boundingBox();
    return b.x2 < e.x1 || b.x1 > e.x2 || b.y2 < e.y1 || b.y1 > e.y2; }).length; }"""


@pytest.mark.parametrize("layout", ["cose", "breadthfirst", "circle", "concentric", "grid"])
def test_viz_free_layouts_are_flat_overlap_free_and_in_view(
    page: Page, workspace: Path, layout: str
) -> None:
    _open_viz(page, workspace)
    page.click('.tab[data-g="full"]')
    _settle(page)
    assert page.evaluate("cy.nodes(':parent').length") > 0  # Grouped keeps the packages
    page.select_option("#lay-sel", layout)
    _settle(page)
    assert page.evaluate("cy.nodes(':parent').length") == 0
    assert page.evaluate(_OVERLAPS) == 0
    assert page.evaluate(_OUTSIDE) == 0
    undrawable = "cy.edges(':visible').filter(e => e._private.rscratch.badLine).length"
    assert page.evaluate(undrawable) == 0
    page.select_option("#lay-sel", "compound")
    _settle(page)
    assert page.evaluate("cy.nodes(':parent').length") > 0


def test_viz_stats_follow_filters_and_search_survives_tab_switch(
    page: Page, workspace: Path
) -> None:
    _open_viz(page, workspace)
    page.click('.tab[data-g="comms"]')
    _settle(page)
    page.locator('input[data-kind="Topic"]').uncheck()
    assert "Topic" not in page.locator("#stats").inner_text()
    page.locator('input[data-kind="Topic"]').check()
    assert "Topic" in page.locator("#stats").inner_text()
    page.fill("#search", "chatter")
    faded = page.evaluate("cy.elements('.faded').length")
    assert faded > 0
    page.click('.tab[data-g="full"]')
    _settle(page)
    assert page.evaluate("cy.elements('.faded').length") > 0
    highlighted = (
        "cy.nodes().filter(n => (n.data('name') || '').includes('chatter')"
        " && !n.hasClass('faded')).length"
    )
    assert page.evaluate(highlighted) > 0


def test_viz_labels_fit_their_shapes_without_style_warnings(page: Page, workspace: Path) -> None:
    _open_viz(page, workspace)
    page.click('.tab[data-g="comms"]')
    _settle(page)
    # A diamond only fits text in its middle: it must be well over the text width.
    ratio = page.evaluate(
        "(() => { const n = cy.nodes('[kind=\"Service\"]').first();"
        " return n.width() / textWidth(n); })()"
    )
    assert ratio >= 1.9
    warnings = [m for m in page.errors.messages if "deprecated" in m]  # type: ignore[attr-defined]
    assert warnings == []
