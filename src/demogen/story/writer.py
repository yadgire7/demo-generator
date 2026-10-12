"""Write the narration: one passage per step, sized to the time that step is on screen."""

from __future__ import annotations

from demogen.llm.base import LLMProvider
from demogen.llm.structured import Problems, complete_checked
from demogen.playbook.schema import Intent, Playbook
from demogen.story.budget import Budget
from demogen.story.schema import Facts, Story

SYSTEM = """\
You write the voice-over for a short product demo video. The video is already recorded. Your \
narration is spoken over it, one passage per step, and each passage plays while its step is \
on screen.

What makes the narration good:
- It tells the viewer why what they are seeing matters to them. They can already see what is \
being clicked, so do not read out the clicking.
- It is true to the footage. You cannot see the video; the notes on what each step shows are \
your eyes. Say nothing the notes and the author's brief do not support.
- It is written for the ear: plain spoken sentences, and nothing a voice cannot say (no \
symbols, lists, brackets or URLs).
- It flows as one piece. The passages are heard back to back, so do not repeat yourself or \
restart with each step.
- It serves the author's brief: their audience, tone and key messages. If there is a call to \
action, close on it in the final passage when it fits.

Length is a hard constraint, because each passage has to fit the seconds its step is on \
screen. Every step comes with a word count to aim for and a maximum. Aim for the first; never \
exceed the second. When the count is very small, a few well-chosen words are right: a short \
phrase is better than a clipped sentence. Where the author gives a hint for a passage, it is \
the point they want made there, in your words rather than theirs."""


def _brief(playbook: Playbook, intent: Intent, facts: Facts, budgets: dict[str, Budget]) -> str:
    lines = [f"Demo: {playbook.name}"]
    for label, value in (("Audience", intent.audience), ("Tone", intent.tone), ("Call to action", intent.cta)):
        if value:
            lines.append(f"{label}: {value}")
    if intent.key_messages:
        lines.append("Key messages:\n" + "\n".join(f"- {message}" for message in intent.key_messages))
    if intent.notes:
        lines.append(f"The author's notes:\n{intent.notes}")
    lines.append("\nThe steps, in order:")
    shown = {step.step_id: step for step in facts.steps}
    for number, step in enumerate(playbook.steps, start=1):
        budget = budgets[step.id]
        seen = shown[step.id]
        lines.append(f"\n{number}. id: {step.id}")
        lines.append(f"   The presenter: {step.do}")
        lines.append(f"   What the footage shows: {seen.happens}")
        if seen.on_screen:
            lines.append("   On screen: " + "; ".join(f'"{text}"' for text in seen.on_screen))
        if step.say_hint:
            lines.append(f"   The author's hint for this passage: {step.say_hint}")
        lines.append(f"   On screen for {budget.seconds:.1f} seconds: aim for about {budget.target} words, never more than {budget.limit}.")
    return "\n".join(lines)


def _checker(step_ids: list[str], budgets: dict[str, Budget], only: set[str] | None = None):
    """A check that the story covers every step in order and keeps each passage within its limit."""

    def check(story: Story) -> Problems:
        got = [step.step_id for step in story.steps]
        if got != step_ids:
            return [f"write exactly one passage per step, with these ids in this order: {step_ids} (got {got})"], []
        fatal = [f"the passage for {step.step_id} is empty" for step in story.steps if not step.text.strip()]
        long = [
            f"the passage for {step.step_id} has {len(step.text.split())} words; its maximum is {budgets[step.step_id].limit}"
            for step in story.steps
            if (only is None or step.step_id in only) and len(step.text.split()) > budgets[step.step_id].limit
        ]
        return fatal, long

    return check


def write_narration(llm: LLMProvider, playbook: Playbook, intent: Intent, facts: Facts, budgets: dict[str, Budget]) -> Story:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": _brief(playbook, intent, facts, budgets) + "\n\nWrite the narration."},
    ]
    return complete_checked(llm, messages, Story, _checker([step.id for step in playbook.steps], budgets))


def rewrite_passages(
    llm: LLMProvider,
    playbook: Playbook,
    intent: Intent,
    facts: Facts,
    budgets: dict[str, Budget],
    current: Story,
    spoken: dict[str, float],
) -> Story:
    """Shorten the passages that ran too long once voiced. Every other passage is kept word for word.

    `budgets` holds the tightened budget for each passage to rewrite; `spoken` how long each ran.
    """
    narration = "\n".join(f"- {step.step_id}: {step.text}" for step in current.steps)
    too_long = "\n".join(
        f"- {step_id}: took {seconds:.1f} seconds to say, but its step is on screen for "
        f"{budgets[step_id].seconds:.1f}. Rewrite it in at most {budgets[step_id].limit} words."
        for step_id, seconds in spoken.items()
    )
    request = (
        f"{_brief(playbook, intent, facts, budgets)}\n\nThe narration as it stands:\n{narration}\n\n"
        f"Once voiced, these passages ran too long for their steps:\n{too_long}\n\n"
        "Rewrite only those passages, keeping the point each one makes and the flow into its "
        "neighbours. Return the whole narration, with every other passage exactly as it is."
    )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": request}]
    step_ids = [step.step_id for step in current.steps]
    reply = complete_checked(llm, messages, Story, _checker(step_ids, budgets, only=set(spoken)))
    # Passages that were not flagged are taken from the current story, whatever the reply did to them.
    return Story(steps=[reply_step if reply_step.step_id in spoken else kept for kept, reply_step in zip(current.steps, reply.steps)])
