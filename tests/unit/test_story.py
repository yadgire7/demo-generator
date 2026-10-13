"""Keyframe selection, word budgets, and what the vision and writer models are asked and held to."""

import pytest

from demogen.llm.base import LLMError, LLMResult, assistant_message
from demogen.llm.structured import complete_checked
from demogen.playbook.loader import parse_playbook
from demogen.playbook.schema import Intent
from demogen.story.budget import Budget, word_budget
from demogen.story.keyframes import plan_keyframes
from demogen.story.schema import Facts, Story
from demogen.story.vision import MAX_IMAGES_PER_CALL, _batches
from demogen.story.writer import rewrite_passages, write_narration
from tests.conftest import ScriptedLLM

FPS = 30
TICK = 1 / FPS


def _action(t, step_id, action="click", label="button"):
    return {"t": t, "type": "action", "step_id": step_id, "action": action, "label": label}


def test_keyframes_show_the_settled_screen_after_each_action():
    events = [_action(1.0, "a", "fill", 'textbox "Title"'), _action(3.0, "a", "click", 'button "Add"'), _action(6.0, "b")]
    windows = {"b": {"start": 5.0, "end": 8.0}, "a": {"start": 0.5, "end": 5.0}}
    (first, before, frames), (second, before_b, frames_b) = plan_keyframes(events, windows, [], FPS)
    assert (first, second) == ("a", "b")  # take order, whatever order the windows arrive in
    assert before.t == 0.5 and before_b.t == 5.0
    # After the fill: the last frame before the click. After the click: the last frame of the step.
    assert [f.t for f in frames] == pytest.approx([3.0 - TICK, 5.0 - TICK])
    assert [f.caption for f in frames] == ['after: fill textbox "Title"', 'after: click button "Add"']
    assert [f.t for f in frames_b] == pytest.approx([8.0 - TICK])


def test_a_step_without_actions_is_shown_by_its_last_frame():
    ((_, _, frames),) = plan_keyframes([], {"look": {"start": 2.0, "end": 4.0}}, [], FPS)
    assert [(round(f.t, 3), f.caption) for f in frames] == [(round(4.0 - TICK, 3), "end of the step")]


def test_a_state_that_comes_and_goes_gets_its_own_frame():
    events = [_action(1.0, "a")]
    windows = {"a": {"start": 0.5, "end": 5.0}}
    # The click shows a toast at 1.1s that disappears at 2.0s: no settled frame would show it.
    ((_, _, frames),) = plan_keyframes(events, windows, [1.1, 2.0], FPS)
    assert [(round(f.t, 2), f.caption) for f in frames] == [(1.55, "briefly on screen during the step"), (round(5.0 - TICK, 2), "after: click button")]


def test_animation_and_single_changes_add_no_frames():
    events = [_action(1.0, "a")]
    windows = {"a": {"start": 0.5, "end": 5.0}}
    animation = [1.1 + i * TICK for i in range(12)]  # a transition: a change on every frame
    for changes in ([1.1], animation):
        ((_, _, frames),) = plan_keyframes(events, windows, changes, FPS)
        assert len(frames) == 1


def test_passing_frames_are_capped_to_the_longest_held():
    events = [_action(1.0, "a")]
    changes = [1.5, 1.8, 2.8, 3.1, 4.5]  # states held for 0.3, 1.0, 0.3 and 1.4 seconds
    ((_, _, frames),) = plan_keyframes(events, {"a": {"start": 0.5, "end": 6.0}}, changes, FPS)
    passing = [round(f.t, 2) for f in frames if f.caption.startswith("briefly")]
    assert passing == [2.3, 3.8]


def test_budget_targets_the_window_and_caps_at_what_a_held_frame_can_cover():
    budget = Budget.for_window(5.0, 2.4)
    assert (budget.target, budget.limit) == (word_budget(5.0, 2.4), 17) == (12, 17)  # 5.0 x 1.6 x 0.9 x 2.4 = 17.28
    assert Budget.for_window(0.1, 2.4) == Budget(0.1, 1, 1)


def test_tightened_budget_uses_the_pace_the_passage_was_really_spoken_at():
    budget = Budget.for_window(2.0, 2.4)  # limit 6
    # Twelve words took 6.0 s (2 words a second): 2.0 x 1.6 x 0.9 = 2.88 s of room is 5 words.
    assert budget.tightened(12, 6.0) == Budget(2.0, 4, 5)
    # However generous the arithmetic, a rewrite must come out shorter than what it replaces.
    assert budget.tightened(3, 3.1).limit == 2


PLAYBOOK = parse_playbook("""
version: 1
name: Tasks walkthrough
target: {base_url: "http://localhost"}
steps:
  - {id: add, do: Add a task called Ship it, say_hint: Adding takes a second}
  - {id: finish, do: Mark it done}
""")
INTENT = Intent(audience="busy team leads", tone="warm", key_messages=["Nothing gets lost"], cta="Try it free", notes="Mention speed.")
FACTS = Facts.model_validate({"steps": [
    {"step_id": "add", "happens": "A task called Ship it appears in the list.", "on_screen": ["Ship it", "1 left"]},
    {"step_id": "finish", "happens": "The task is struck through.", "on_screen": ["0 left"]},
]})  # fmt: skip
BUDGETS = {"add": Budget.for_window(4.0, 2.4), "finish": Budget.for_window(2.0, 2.4)}  # limits 13 and 6


def _story(add, finish):
    return {"steps": [{"step_id": "add", "text": add}, {"step_id": "finish", "text": finish}]}


def test_writer_is_given_the_brief_the_footage_and_the_budgets():
    llm = ScriptedLLM(replies=[_story("Capture a task the moment it comes up.", "One click and it is done.")])
    story = write_narration(llm, PLAYBOOK, INTENT, FACTS, BUDGETS)
    assert story.text_of("finish") == "One click and it is done."
    brief = llm.transcript[1]
    for expected in (
        "Demo: Tasks walkthrough", "Audience: busy team leads", "Tone: warm", "- Nothing gets lost", "Call to action: Try it free", "Mention speed.",
        "What the footage shows: A task called Ship it appears in the list.", 'On screen: "Ship it"; "1 left"',
        "The author's hint for this passage: Adding takes a second",
        "On screen for 4.0 seconds: aim for about 9 words, never more than 13.",
        "On screen for 2.0 seconds: aim for about 4 words, never more than 6.",
    ):  # fmt: skip
        assert expected in brief
    assert brief.count("The author's hint") == 1  # the second step has none


def test_writer_is_sent_back_when_a_passage_is_over_its_limit():
    wordy = "With a single satisfying click the task is crossed off for good."  # 12 words; the limit is 6
    llm = ScriptedLLM(replies=[_story("Capture it.", wordy), _story("Capture it.", "One click, done.")])
    story = write_narration(llm, PLAYBOOK, INTENT, FACTS, BUDGETS)
    assert story.text_of("finish") == "One click, done."
    assert "the passage for finish has 12 words; its maximum is 6" in llm.transcript[-1]
    assert len(llm.requests) == 2 and len(llm.requests[1]) == len(llm.requests[0]) + 2  # same conversation, continued


def test_writer_that_stays_slightly_long_is_accepted_but_one_that_drops_steps_is_not():
    wordy = _story("Capture it.", "With a single satisfying click the task is crossed off.")
    assert write_narration(ScriptedLLM(replies=[wordy] * 3), PLAYBOOK, INTENT, FACTS, BUDGETS).text_of("add") == "Capture it."
    missing = {"steps": [{"step_id": "add", "text": "Capture it."}]}
    with pytest.raises(LLMError, match="one passage per step"):
        write_narration(ScriptedLLM(replies=[missing] * 3), PLAYBOOK, INTENT, FACTS, BUDGETS)
    with pytest.raises(LLMError, match="the passage for finish is empty"):
        write_narration(ScriptedLLM(replies=[_story("Capture it.", " ")] * 3), PLAYBOOK, INTENT, FACTS, BUDGETS)


def test_rewrite_changes_only_the_passages_that_ran_long():
    current = Story.model_validate(_story("Capture a task the moment it comes up.", "With one click the whole thing is marked as done."))
    budgets = {**BUDGETS, "finish": BUDGETS["finish"].tightened(10, 5.0)}  # 2.88 s of room at 2 words a second
    # The model shortens the flagged passage, and also meddles with one it was told to leave alone.
    llm = ScriptedLLM(replies=[_story("Capture tasks instantly!", "One click, done.")])
    story = rewrite_passages(llm, PLAYBOOK, INTENT, FACTS, budgets, current, {"finish": 5.0})
    assert story.text_of("finish") == "One click, done."
    assert story.text_of("add") == "Capture a task the moment it comes up."
    request = llm.transcript[1]
    assert "- finish: With one click the whole thing is marked as done." in request
    assert "- finish: took 5.0 seconds to say, but its step is on screen for 2.0. Rewrite it in at most 5 words." in request


def test_rewrite_holds_only_the_flagged_passages_to_their_limits():
    # `add` is over its limit in the current story (an author's edit, say) but was not flagged.
    long_add = " ".join(["word"] * 20)
    current = Story.model_validate(_story(long_add, "With one click the whole thing is marked as done."))
    llm = ScriptedLLM(replies=[_story(long_add, "One click, done.")])
    story = rewrite_passages(llm, PLAYBOOK, INTENT, FACTS, BUDGETS, current, {"finish": 4.8})
    assert len(llm.requests) == 1 and story.text_of("add") == long_add


def _entry(step_id, frames):
    return {"step_id": step_id, "before": {}, "frames": [{}] * frames}


def test_vision_batches_keep_each_call_within_the_image_limit():
    entries = [_entry(f"s{i}", 6) for i in range(7)]
    batches = _batches(entries)
    assert [[e["step_id"] for e in batch] for batch in batches] == [["s0", "s1", "s2"], ["s3", "s4", "s5"], ["s6"]]
    assert all(1 + sum(len(e["frames"]) for e in batch) <= MAX_IMAGES_PER_CALL for batch in batches)
    # A step with more frames than the limit still gets a call of its own.
    assert len(_batches([_entry("big", 30), _entry("next", 1)])) == 2


class _Counter:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def complete(self, messages, *, tools=None, response_schema=None):
        self.calls += 1
        reply = self.replies.pop(0)
        parsed = response_schema.model_validate(reply) if reply is not None else None
        return LLMResult(parsed=parsed, message=assistant_message("...", []))


def test_checked_reply_stops_at_the_first_clean_answer():
    clean = _story("a", "b")
    llm = _Counter(clean)
    assert complete_checked(llm, [], Story, lambda story: ([], [])).steps[0].text == "a"
    assert llm.calls == 1


def test_checked_reply_without_structure_is_an_error():
    with pytest.raises(LLMError, match="structured reply"):
        complete_checked(_Counter(None), [], Story, lambda story: ([], []))


def test_writer_is_sent_back_for_marks_a_voice_cannot_say():
    llm = ScriptedLLM(replies=[_story("Capture it.]", "One click, done."), _story("Capture it.", "One click, done.")])
    assert write_narration(llm, PLAYBOOK, INTENT, FACTS, BUDGETS).text_of("add") == "Capture it."
    assert "the passage for add contains characters a voice cannot say: ]" in llm.transcript[-1]
