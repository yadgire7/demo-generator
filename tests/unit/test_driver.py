import math
import random

import pytest
from pydantic import ValidationError

from demogen.driver.playwright.humanize import MAX_SECONDS, MIN_SECONDS, SAMPLE_HZ, mouse_path
from demogen.driver.playwright.session import snapshot_elements
from demogen.playbook.actions import Action

SNAPSHOT = """\
- generic [ref=e1]:
  - heading "todos" [level=1] [ref=e7]
  - textbox "What needs to be done?" [active] [ref=e8]
  - 'button "Save: now" [ref=e9] [cursor=pointer]'
  - link "Say \\"hi\\"" [ref=e10] [cursor=pointer]:
    - /url: "#/hi"
  - paragraph: no ref on this line
  - list [ref=e12]:
    - listitem [ref=e13]: one
"""


def test_snapshot_elements_reads_role_and_name_per_ref():
    elements = snapshot_elements(SNAPSHOT)
    assert elements["e8"] == ("textbox", "What needs to be done?")
    assert elements["e9"] == ("button", "Save: now")
    assert elements["e10"] == ("link", 'Say "hi"')
    assert elements["e12"] == ("list", None) and elements["e1"] == ("generic", None)
    assert len(elements) == 7


@pytest.mark.parametrize(
    "shorthand,expected",
    [
        ({"click": {"role": "button", "name": "Save"}}, ("click", "role", "Save", None)),
        ({"click": {"css": "#save"}}, ("click", "css", None, None)),
        ({"fill": {"role": "textbox", "name": "Email", "text": "a@b.co"}}, ("fill", "role", "Email", "a@b.co")),
        ({"select": {"css": "select#plan", "value": "Pro"}}, ("select", "css", None, "Pro")),
        ({"hover": {"role": "link", "name": "Docs"}}, ("hover", "role", "Docs", None)),
    ],
)
def test_playbook_shorthand_for_element_actions(shorthand, expected):
    action = Action.model_validate(shorthand)
    assert (action.type, action.target[0].by, action.target[0].name, action.text) == expected


def test_playbook_shorthand_for_page_actions():
    assert Action.model_validate({"press": "Enter"}).key == "Enter"
    assert Action.model_validate({"goto": "/settings"}).url == "/settings"


@pytest.mark.parametrize(
    "broken",
    [
        {"click": {}},  # no element
        {"fill": {"role": "textbox", "name": "Email"}},  # nothing to type
        {"tap": {"role": "button", "name": "Save"}},  # unknown action
        {"click": {"role": "button", "name": "Save", "colour": "red"}},  # unknown key
        {"type": "press"},  # no key
    ],
)
def test_incomplete_actions_are_rejected(broken):
    with pytest.raises(ValidationError):
        Action.model_validate(broken)


def test_action_round_trips_through_the_lockfile_form():
    action = Action.model_validate({"fill": {"css": "#q", "text": "${secret:TOKEN}"}})
    assert Action.model_validate(action.model_dump(mode="json")) == action


def test_mouse_path_ends_on_target_and_is_repeatable():
    start, end = (100.0, 100.0), (900.0, 500.0)
    path = mouse_path(start, end, random.Random(7))
    assert path[-1] == end and path[0] != start
    assert MIN_SECONDS * SAMPLE_HZ <= len(path) <= MAX_SECONDS * SAMPLE_HZ
    assert path == mouse_path(start, end, random.Random(7))
    # Eased: the pointer covers less ground in its first and last frames than mid-flight.
    steps = [math.dist(a, b) for a, b in zip([start, *path], path)]
    assert steps[0] < max(steps) / 3 and steps[-1] < max(steps) / 3
    # A gentle arc, never a detour: no sample strays far from the straight line.
    assert sum(steps) < 1.1 * math.dist(start, end)


def test_mouse_path_for_a_pointer_already_there():
    assert mouse_path((5.0, 5.0), (5.0, 5.0), random.Random(0)) == [(5.0, 5.0)]
