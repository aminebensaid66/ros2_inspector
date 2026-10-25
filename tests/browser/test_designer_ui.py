"""Drive the package designer in headless Chromium against the real local server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
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


# ── canvas: layering, geometry, panning (#6 #7 #8 #9 #10 #12 #13 #14) ───────


def _screen_box(page: Page, selector: str) -> dict[str, float]:
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    return box


def _on_canvas(page: Page, selector: str) -> bool:
    box, cv = _screen_box(page, selector), _screen_box(page, "#cv")
    return (
        box["x"] >= cv["x"]
        and box["y"] >= cv["y"]
        and box["x"] + box["width"] <= cv["x"] + cv["width"]
        and box["y"] + box["height"] <= cv["y"] + cv["height"]
    )


def test_handles_sit_on_their_rows_above_the_cards(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    for side, row in (("pub", ".tr.pub"), ("sub", ".tr.sub")):
        handle = _screen_box(page, f"#hg circle[data-side='{side}'] >> nth=0")
        nid = page.locator(f"#hg circle[data-side='{side}']").first.get_attribute("data-nid")
        row_box = _screen_box(page, f"#nd{nid} {row} >> nth=0")
        assert (
            abs((handle["y"] + handle["height"] / 2) - (row_box["y"] + row_box["height"] / 2)) <= 1
        )
        # Both halves of the handle (inside and outside the card edge) are hittable.
        cx, cy = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
        for dx in (-3, 0, 3):
            tag = page.evaluate("([x,y])=>document.elementFromPoint(x,y).tagName", [cx + dx, cy])
            assert tag == "circle"


def test_wire_ends_on_the_handle_rim_with_a_matching_arrowhead(
    page: Page, designer: dict[str, Any]
) -> None:
    _open(page, designer)
    wire = page.locator("#eg path").first
    assert wire.get_attribute("marker-end") == "url(#arr)"
    end_x = float(wire.get_attribute("d").rsplit(" ", 1)[1].split(",")[0])  # type: ignore[union-attr]
    handle_x = float(page.locator("#hg circle[data-side='sub']").first.get_attribute("cx") or 0)
    assert end_x == handle_x - 6
    # A wire that no longer matches turns red, arrowhead included.
    page.locator(".node").nth(1).click()
    _set_field(page, "#ssubs .tc input.sinp", "/renamed")
    expect(page.locator("#eg path[stroke='#f87171']")).to_have_attribute(
        "marker-end", "url(#arr-bad)"
    )
    # Visible wires are painted above the cards; their click targets sit under the cards.
    wires = page.locator("#eg path").count()
    assert page.evaluate("()=>document.getElementById('eg').ownerSVGElement.id") == "svh"
    expect(page.locator("#wh path")).to_have_count(wires)


def test_long_names_never_hide_the_publishers_column(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    page.locator(".node").nth(1).click()
    _set_field(page, "#ssubs .tc input.sinp", "/robot/front_left/wheel/joint_states_filtered")
    card = _screen_box(page, ".node >> nth=1")
    publisher = _screen_box(page, ".node >> nth=1 >> .tr.pub")
    assert card["x"] < publisher["x"] and publisher["x"] + publisher["width"] <= card["x"] + 280
    expect(page.locator(".node >> nth=1 >> .tr.sub .tn")).to_have_attribute(
        "title", "/robot/front_left/wheel/joint_states_filtered"
    )


def test_new_nodes_appear_on_screen_and_the_canvas_pans(
    page: Page, designer: dict[str, Any]
) -> None:
    _open(page, designer)
    for _ in range(14):
        page.click("#an")
        assert _on_canvas(page, f"#nd{page.evaluate('()=>S.sel')}")
    page.keyboard.press("Escape")
    first = ".node >> nth=0"
    assert not _on_canvas(page, first)
    page.click("#fit")
    assert _on_canvas(page, first)
    # Dragging the background pans and does not close/open anything.
    vx, vy = page.evaluate("()=>[VIEW.x, VIEW.y]")
    # Press on empty background: Show all centres the design, so cards can sit anywhere.
    x, y = page.evaluate(
        """()=>{const c=CV.getBoundingClientRect();
        for(let y=c.bottom-30;y>c.top+150;y-=20) for(let x=c.right-30;x>c.left+250;x-=20)
          if(document.elementFromPoint(x,y)===CV) return [x,y];
        return null}"""
    )
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 200, y - 100, steps=6)
    page.mouse.up()
    assert page.evaluate("()=>[VIEW.x, VIEW.y]") == [vx - 200, vy - 100]
    expect(page.locator("#sb")).to_have_class("h")
    page.mouse.wheel(0, 300)
    assert page.evaluate("()=>VIEW.y") == vy - 100 - 300


def test_wiring_still_works_after_the_canvas_scrolled(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    page.evaluate(
        "()=>{const p=pkg();p.edges=[];p.nodes[0].x=960;p.nodes[1].x=1400;p.nodes[1].y=300;"
        "renderAll();}"
    )
    # Keyboard focus on a card that sticks out makes the browser scroll the canvas.
    page.locator(".node >> nth=1 >> .ndel").focus()
    page.wait_for_timeout(50)
    assert page.evaluate("()=>[CV.scrollLeft, CV.scrollTop]") == [0, 0]
    assert page.evaluate("()=>VIEW.x") < 0
    _drag(page, "#hg circle[data-side='pub'] >> nth=0", "#hg circle[data-side='sub'] >> nth=0")
    expect(page.locator("#eg path")).to_have_count(1)


def test_toolbar_fits_and_hint_is_readable_at_1024(browser: Any, designer: dict[str, Any]) -> None:
    context = browser.new_context(viewport={"width": 1024, "height": 700})
    page = context.new_page()
    _open(page, designer)
    toolbar = "()=>document.getElementById('tb').scrollWidth"
    assert page.evaluate(toolbar) <= 1024
    contrast = page.evaluate(
        """()=>{
          const lum=c=>{
            const v=c.match(/\\d+/g).slice(0,3).map(x=>x/255)
              .map(x=>x<=.03928?x/12.92:((x+.055)/1.055)**2.4);
            return .2126*v[0]+.7152*v[1]+.0722*v[2]};
          const fg=getComputedStyle(document.querySelector('.tb-hint')).color;
          const bg=getComputedStyle(document.getElementById('tb')).backgroundColor;
          const [a,b]=[lum(fg),lum(bg)].sort((x,y)=>y-x);return (a+.05)/(b+.05)}"""
    )
    assert contrast >= 4.5
    context.close()


def test_equivalent_type_spellings_can_be_wired(page: Page, designer: dict[str, Any]) -> None:
    _open(page, designer)
    page.locator(".node").nth(1).click()
    _set_field(page, "#ssubs .tc input.ssel", "sensor_msgs/msg/Image")
    expect(page.locator("#eg path[stroke='#f87171']")).to_have_count(0)


_WIRE_HITS_CARDS = """() => {
  const cards = [...document.querySelectorAll('.node')].map(el => el.getBoundingClientRect());
  return [...document.querySelectorAll('#eg path')].map(path => {
    const m = path.getScreenCTM(), length = path.getTotalLength(), hits = [];
    for (let i = 2; i < 98; i++) {
      const q = path.getPointAtLength(length * i / 100);
      const x = q.x * m.a + m.e, y = q.y * m.d + m.f;
      cards.forEach((r, k) => {
        if (x > r.left + 2 && x < r.right - 2 && y > r.top + 2 && y < r.bottom - 2) hits.push(k);
      });
    }
    return [...new Set(hits)];
  });
}"""


def test_wires_never_pass_through_cards(page: Page, designer: dict[str, Any], shot: Any) -> None:
    """Issue #6: a wire over a third card, or running backwards, must not look like another link."""
    _open(page, designer)
    page.evaluate(
        """()=>{
      const p=pkg(), ep=(id,name)=>({id,name,msgType:'std_msgs/String'});
      const mk=(id,label,x,y,pubs,subs)=>({id,label,x,y,publishers:pubs,subscribers:subs,
        services:[],clients:[],action_servers:[],action_clients:[]});
      p.nodes=[mk('a','lidar',40,60,[ep('a1','/scan')],[]),
               mk('b','camera',380,60,[ep('b1','/image')],[]),
               mk('c','detector',720,60,[],[ep('c1','/scan'),ep('c2','/image')]),
               mk('d','navigator',720,420,[ep('d1','/goal')],[]),
               mk('e','mission',120,420,[],[ep('e1','/goal')])];
      p.edges=[{id:'w1',fn:'a',ft:'a1',tn:'c',tt:'c1'},{id:'w2',fn:'b',ft:'b1',tn:'c',tt:'c2'},
               {id:'w3',fn:'d',ft:'d1',tn:'e',tt:'e1'}];
      renderAll();
    }"""
    )
    expect(page.locator("#eg path")).to_have_count(3)
    shot(page, "designer-wires-routed")
    assert page.evaluate(_WIRE_HITS_CARDS) == [[], [], []]
    # The straight-through wire and the backwards one are detours, the free one stays a curve.
    kinds = [
        " C" in (d or "")
        for d in page.locator("#eg path").evaluate_all("ps=>ps.map(p=>p.getAttribute('d'))")
    ]
    assert kinds == [False, True, False]
    # Visible wires are drawn above the cards; clicking one in open canvas removes it.
    assert page.evaluate("()=>document.getElementById('eg').ownerSVGElement.id") == "svh"
    page.locator("#wh path").nth(2).evaluate(
        "p=>p.dispatchEvent(new MouseEvent('click',{bubbles:true}))"
    )
    expect(page.locator("#eg path")).to_have_count(2)


@pytest.mark.parametrize("gap", [4, 12, 30])
def test_wires_between_cards_that_nearly_touch_stay_out_of_cards(
    page: Page, designer: dict[str, Any], gap: int
) -> None:
    """Issue #6: with only a few pixels between cards, a detour's turn must stay in the gap."""
    _open(page, designer)
    page.evaluate(
        """(gap)=>{
      const p=pkg(), ep=(id,name)=>({id,name,msgType:'std_msgs/String'});
      const mk=(id,label,x,y,pubs,subs)=>({id,label,x,y,publishers:pubs,subscribers:subs,
        services:[],clients:[],action_servers:[],action_clients:[]});
      const w=document.querySelector('.node')?.offsetWidth||NW;
      p.nodes=[mk('a','source',40,200,[ep('a1','/near'),ep('a2','/far')],[]),
               // directly right of the source, lower: the wire must turn in the gap
               mk('b','neighbour',40+NW+gap,330,[],[ep('b1','/near')]),
               // a card just right of the source and above it, in the way of a turn at +20
               mk('c','blocker',40+NW+gap,20,[],[]),
               mk('d','target',40+2*NW+200,40,[],[ep('d1','/far')])];
      p.edges=[{id:'w1',fn:'a',ft:'a1',tn:'b',tt:'b1'},{id:'w2',fn:'a',ft:'a2',tn:'d',tt:'d1'}];
      renderAll();
    }""",
        gap,
    )
    expect(page.locator("#eg path")).to_have_count(2)
    assert page.evaluate(_WIRE_HITS_CARDS) == [[], []]


def test_toolbar_fits_at_800_and_show_all_centres_a_small_design(
    browser: Any, designer: dict[str, Any]
) -> None:
    context = browser.new_context(viewport={"width": 800, "height": 700})
    page = context.new_page()
    _open(page, designer)
    assert page.evaluate("()=>document.getElementById('tb').scrollWidth") <= 800
    fit = _screen_box(page, "#fit")
    assert fit["x"] + fit["width"] <= 800
    page.click("#fit")
    centre = page.evaluate(
        """()=>{const r=[...document.querySelectorAll('.node')].map(n=>n.getBoundingClientRect());
        const c=CV.getBoundingClientRect();
        return [(Math.min(...r.map(x=>x.left))+Math.max(...r.map(x=>x.right)))/2-c.left,
                (Math.min(...r.map(x=>x.top))+Math.max(...r.map(x=>x.bottom)))/2-c.top,
                canvasWidth(), CV.clientHeight]}"""
    )
    assert abs(centre[0] - centre[2] / 2) <= 2 and abs(centre[1] - centre[3] / 2) <= 2
    # The small labels that were left at about 2.4:1 contrast.
    colours = page.evaluate(
        "()=>['.ndel','.tb-lbl'].map(s=>getComputedStyle(document.querySelector(s)).color)"
    )
    assert colours == ["rgb(124, 138, 160)"] * 2
    context.close()
