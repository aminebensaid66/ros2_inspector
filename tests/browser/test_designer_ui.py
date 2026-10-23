"""Drive the package designer in headless Chromium against the real local server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, expect

from ros2inspector.design.codegen import _GENERATED_MARKER
from ros2inspector.design.server import STATE_FILE
from ros2inspector.design.ui import build_html


def _open(page: Page, designer: dict[str, Any]) -> None:
    page.goto(designer["url"])
    expect(page.locator(".node")).to_have_count(2)


def _center(page: Page, selector: str) -> tuple[float, float]:
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _drag(page: Page, source: str, target: str) -> None:
    sx, sy = _center(page, source)
    tx, ty = _center(page, target)
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move((sx + tx) / 2, (sy + ty) / 2, steps=5)
    page.mouse.move(tx, ty, steps=5)
    page.mouse.up()


def _toast(page: Page, kind: str) -> str:
    toast = page.locator(f"#toast.show.{kind}")
    expect(toast).to_be_visible()
    return toast.inner_text()


def _set_field(page: Page, selector: str, value: str) -> None:
    field = page.locator(selector)
    field.fill(value)
    field.dispatch_event("change")


def test_fresh_designer_renders_seed_without_errors(
    page: Page, designer: dict[str, Any], shot: Any
) -> None:
    _open(page, designer)
    expect(page.locator("#eg path")).to_have_count(1)
    expect(page.locator("#gb")).to_be_enabled()
    expect(page.locator(".ptab")).to_have_count(1)
    expect(page.locator("#pn")).to_have_value("my_package")
    expect(page.locator("#es")).to_be_hidden()
    shot(page, "designer-01-fresh")
    assert page.errors.messages == []  # type: ignore[attr-defined]


def test_add_node_configure_and_generate(page: Page, designer: dict[str, Any], shot: Any) -> None:
    _open(page, designer)
    page.click("#an")
    sidebar = page.locator("#sb")
    expect(sidebar).not_to_have_class("h")
    page.locator("#snm").fill("cmd_mux")
    expect(page.locator(".node .nlbl", has_text="cmd_mux")).to_have_count(1)

    page.locator("#sb .addb").nth(0).click()  # + publisher
    _set_field(page, "#spubs .sinp", "/cmd_vel")
    _set_field(page, "#spubs .ssel", "geometry_msgs/Twist")
    page.locator("#sb .addb").nth(2).click()  # + service provider
    page.locator("#sb .addb").nth(4).click()  # + action server
    shot(page, "designer-02-sidebar")

    page.click("#gb")
    text = _toast(page, "ok")
    assert "file(s) written" in text
    shot(page, "designer-03-generated")

    out: Path = designer["out"]
    package = out / "my_package"
    assert (package / _GENERATED_MARKER).is_file()
    source = (package / "my_package" / "cmd_mux.py").read_text()
    assert "from geometry_msgs.msg import Twist" in source
    assert "'/cmd_vel'" in source
    assert "ActionServer(" in source and "create_service(" in source
    for node in ("camera_node", "detector_node"):
        assert (package / "my_package" / f"{node}.py").is_file()
    state = json.loads((out / STATE_FILE).read_text())
    assert state["packages"][0]["_generated_name"] == "my_package"
    assert page.errors.messages == []  # type: ignore[attr-defined]


def test_validation_errors_use_readable_paths(
    page: Page, designer: dict[str, Any], shot: Any
) -> None:
    _open(page, designer)
    page.locator(".node").first.click()
    page.locator("#snm").fill("Bad Name")
    page.click("#gb")
    text = _toast(page, "err")
    assert "my_package › Bad Name › label" in text
    shot(page, "designer-04-validation-error")
    assert not (designer["out"] / "my_package").exists()


def test_wire_type_mismatch_is_refused(page: Page, designer: dict[str, Any], shot: Any) -> None:
    _open(page, designer)
    # detector_node publishes /detections (String); camera_node gets a subscriber of Image.
    page.locator(".node").nth(0).click()
    page.locator("#sb .addb").nth(1).click()  # + subscriber on camera_node
    _set_field(page, "#ssubs .ssel", "sensor_msgs/Image")
    edges_before = page.locator("#eg path").count()
    _drag(page, "#hg circle[data-side='pub'] >> nth=1", "#hg circle[data-side='sub'] >> nth=0")
    text = _toast(page, "err")
    assert "Type mismatch" in text
    expect(page.locator("#eg path")).to_have_count(edges_before)
    shot(page, "designer-05-wire-mismatch")


def test_matching_wire_renames_subscriber_to_publisher_topic(
    page: Page, designer: dict[str, Any]
) -> None:
    _open(page, designer)
    page.locator(".node").nth(0).click()
    page.locator("#sb .addb").nth(1).click()
    _set_field(page, "#ssubs .ssel", "std_msgs/String")
    _drag(page, "#hg circle[data-side='pub'] >> nth=1", "#hg circle[data-side='sub'] >> nth=0")
    assert "renamed" in _toast(page, "ok")
    expect(page.locator("#eg path")).to_have_count(2)
    expect(page.locator("#ssubs .sinp")).to_have_value("/detections")
    expect(page.locator("#eg path[stroke='#f87171']")).to_have_count(0)


def test_publisher_rename_follows_the_wire(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    page.locator(".node").nth(0).click()  # camera_node publishes /image_raw
    _set_field(page, "#spubs .sinp", "/camera/image")
    expect(page.locator(".node").nth(1).locator(".tr.sub .tn")).to_have_text("/camera/image")
    expect(page.locator("#eg path[stroke='#f87171']")).to_have_count(0)


def test_broken_wire_blocks_generate(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    page.locator(".node").nth(1).click()  # detector_node: change the subscriber side
    _set_field(page, "#ssubs .sinp", "/other")
    expect(page.locator("#eg path[stroke='#f87171']")).to_have_count(1)
    page.click("#gb")
    assert "no longer match" in _toast(page, "err")
    assert not (designer["out"] / "my_package").exists()


def test_save_then_reload_restores_the_design(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    page.click("#an")
    page.locator("#snm").fill("planner")
    page.keyboard.press("Control+s")
    _toast(page, "ok")
    page.reload()
    expect(page.locator(".node")).to_have_count(3)
    expect(page.locator(".node .nlbl", has_text="planner")).to_have_count(1)


def test_packages_tabs_and_delete_confirmation(
    page: Page, designer: dict[str, Any], shot: Any
) -> None:
    _open(page, designer)
    page.click(".ptab-add")
    expect(page.locator(".ptab")).to_have_count(2)
    expect(page.locator("#pn")).to_have_value("package_2")
    page.click("#an")
    page.locator("#pn").fill("second_pkg")
    expect(page.locator(".ptab.active")).to_contain_text("second_pkg")
    shot(page, "designer-06-two-packages")

    dialogs: list[str] = []

    def dismiss(dialog: Any) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.once("dialog", dismiss)
    page.locator(".ptab").nth(0).locator(".ptab-del").click()
    expect(page.locator(".ptab")).to_have_count(2)
    assert dialogs and "my_package" in dialogs[0]

    page.once("dialog", lambda dialog: dialog.accept())
    page.locator(".ptab").nth(0).locator(".ptab-del").click()
    expect(page.locator(".ptab")).to_have_count(1)
    expect(page.locator("#pn")).to_have_value("second_pkg")

    page.locator(".ptab-del").click()
    assert "last package" in _toast(page, "err")


def test_unsaved_changes_prompt_before_leaving(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    kinds: list[str] = []

    def accept(dialog: Any) -> None:
        kinds.append(dialog.type)
        dialog.accept()

    page.on("dialog", accept)
    page.click("#an")
    page.close(run_before_unload=True)
    page.wait_for_event("close")
    assert kinds == ["beforeunload"]


def test_no_prompt_after_saving(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    kinds: list[str] = []
    page.on("dialog", lambda dialog: (kinds.append(dialog.type), dialog.accept()))
    page.click("#an")
    page.click("#sv")
    _toast(page, "ok")
    page.close(run_before_unload=True)
    assert kinds == []


def test_node_drag_is_clamped_to_canvas(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    header = page.locator(".node .nh").first
    x, y = _center(page, ".node .nh >> nth=0")
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(-500, -500, steps=5)
    page.mouse.up()
    node = page.locator(".node").first
    assert node.evaluate("el => [el.style.left, el.style.top]") == ["0px", "0px"]
    assert header.is_visible()


def test_escape_closes_sidebar_and_delete_node(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    page.locator(".node").first.click()
    expect(page.locator("#sb")).not_to_have_class("h")
    page.keyboard.press("Escape")
    expect(page.locator("#sb")).to_have_class("h")
    page.locator(".node .ndel").first.click()
    expect(page.locator(".node")).to_have_count(1)
    expect(page.locator("#eg path")).to_have_count(0)


def test_custom_interface_types_are_accepted(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    page.locator(".node").first.click()
    _set_field(page, "#spubs .ssel", "my_interfaces/msg/RobotStatus")
    page.locator(".node").nth(1).click()
    _set_field(page, "#ssubs .ssel", "my_interfaces/msg/RobotStatus")
    page.click("#gb")
    _toast(page, "ok")
    source = (designer["out"] / "my_package" / "my_package" / "camera_node.py").read_text()
    assert "from my_interfaces.msg import RobotStatus" in source


def test_regenerate_reports_kept_edits(page: Page, designer: dict[str, Any], shot: Any) -> None:
    _open(page, designer)
    page.click("#gb")
    _toast(page, "ok")
    edited = designer["out"] / "my_package" / "my_package" / "camera_node.py"
    edited.write_text(edited.read_text() + "\n# hand edit\n")
    page.wait_for_timeout(300)
    page.click("#gb")
    expect(page.locator("#toast.show.ok")).to_contain_text("Kept your edits")
    assert "camera_node.py" in page.locator("#toast").inner_text()
    assert edited.read_text().endswith("# hand edit\n")
    shot(page, "designer-07-kept-edits")


def test_hostile_saved_state_is_rendered_as_text(page: Page) -> None:
    """Even a state that bypassed validation cannot run script in the page."""
    evil = "<img src=x onerror=alert(1)>"
    state = {
        "version": 1,
        "nid": 3,
        "tid": 3,
        "pid": 2,
        "activePkg": "p1",
        "packages": [
            {
                "id": "p1",
                "pkg_name": evil,
                "edges": [],
                "nodes": [
                    {
                        "id": "1');alert(2);('",
                        "label": "</script><script>alert(3)</script>",
                        "x": 10,
                        "y": 10,
                        "publishers": [{"id": "t'1", "name": evil, "msgType": evil}],
                        "subscribers": [],
                    }
                ],
            }
        ],
    }
    dialogs: list[str] = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    page.set_content(build_html(state, session_token="t"))
    page.locator(".node").first.click()
    page.locator("#sb .trem").first.click()
    page.wait_for_timeout(200)
    assert dialogs == []
    expect(page.locator(".ptab").first).to_contain_text(evil)
    expect(page.locator(".nlbl")).to_have_text("</script><script>alert(3)</script>")


def test_first_click_after_editing_a_field_selects_the_other_node(
    page: Page, designer: dict[str, Any]
) -> None:
    """The blur-time change event re-renders cards; it must not swallow the click."""
    _open(page, designer)
    page.locator(".node").first.click()
    field = page.locator("#spubs .ssel")
    field.fill("my_interfaces/msg/RobotStatus")  # no explicit change: blur commits it
    page.locator(".node").nth(1).click()
    expect(page.locator("#snm")).to_have_value("detector_node")
    expect(page.locator(".node").nth(1).locator(".tr.sub .tt")).to_have_text(
        "my_interfaces/msg/RobotStatus"
    )


def test_dragging_does_not_rebuild_cards(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    page.evaluate("window.__marker = document.querySelector('.node .nlbl')")
    x, y = _center(page, ".node .nh >> nth=0")
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 120, y + 80, steps=10)
    page.mouse.up()
    assert page.evaluate("window.__marker === document.querySelector('.node .nlbl')")


def test_new_nodes_never_overlap_existing_cards(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    for _ in range(5):
        page.click("#an")
    boxes = [
        page.locator(".node").nth(i).bounding_box() for i in range(page.locator(".node").count())
    ]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert a and b
            overlap_x = a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
            overlap_y = a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]
            assert not (overlap_x and overlap_y), (a, b)
