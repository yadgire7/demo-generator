import pytest
from pydantic import ValidationError

from demogen.playbook.loader import merge_intent, parse_playbook
from demogen.playbook.schema import parse_duration
from demogen.playbook.secrets import REDACTED, MissingSecret, Redactor, resolve_secrets

MINIMAL = """
version: 1
name: demo
target: {base_url: "http://localhost:3000"}
steps:
  - {id: one, do: "Open the page", dwell: 500ms}
  - {id: two, do: "Click save"}
"""


def test_example_playbook_parses(playbook):
    parsed = parse_playbook(playbook.read_text())
    assert [s.id for s in parsed.steps] == ["add-first", "add-second", "complete-first", "filter-active"]
    assert parsed.steps[0].dwell == 1.5
    pinned = parsed.steps[3].action[0]
    assert (pinned.type, pinned.target[0].role, pinned.target[0].name) == ("click", "link", "Active")


def test_defaults_and_durations():
    parsed = parse_playbook(MINIMAL)
    assert parsed.steps[0].dwell == 0.5
    assert parsed.target.viewport.width == 1440


@pytest.mark.parametrize("value,seconds", [("1.5s", 1.5), ("500ms", 0.5), ("2m", 120), (3, 3.0), ("4", 4.0)])
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["soon", "1.5h", None, True])
def test_parse_duration_rejects(value):
    with pytest.raises(ValueError):
        parse_duration(value)


@pytest.mark.parametrize(
    "broken",
    [
        MINIMAL.replace("id: two", "id: one"),  # duplicate id
        MINIMAL.replace("do: \"Click save\"", "doo: \"Click save\""),  # unknown key
        MINIMAL.replace("version: 1", "version: 2"),
        MINIMAL.replace("target: {base_url", "target: {kind: desktop, base_url"),  # phase 2
        MINIMAL.split("steps:")[0] + "steps: []",
    ],
)
def test_invalid_playbooks_are_rejected(broken):
    with pytest.raises(ValidationError):
        parse_playbook(broken)


def test_intent_notes_ride_along(playbook):
    parsed = parse_playbook(playbook.read_text())
    merged = merge_intent(parsed, "  Emphasise speed.\n")
    assert merged.notes == "Emphasise speed."
    assert merged.audience == parsed.intent.audience
    assert merge_intent(parsed, None).notes is None


def test_secrets_resolve_and_redact():
    env = {"DEMO_USER": "ada", "DEMO_PASS": "ada-s3cret"}
    text = "Log in as ${secret:DEMO_USER} with ${secret:DEMO_PASS}"
    assert resolve_secrets(text, env) == "Log in as ada with ada-s3cret"
    with pytest.raises(MissingSecret):
        resolve_secrets(text, {"DEMO_USER": "ada"})
    # The longer secret contains the shorter one; neither may survive, whole or in part.
    assert Redactor.for_text(text, env).redact("typed ada-s3cret for ada") == f"typed {REDACTED} for {REDACTED}"
