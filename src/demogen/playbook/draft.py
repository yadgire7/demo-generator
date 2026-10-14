"""Draft a playbook from a plain request, and redraft it from an author's feedback."""

from __future__ import annotations

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from demogen.llm.base import LLMProvider
from demogen.llm.structured import Problems, complete_checked
from demogen.playbook.schema import Playbook

SYSTEM = """\
You turn a request for a product demo video into a playbook: the ordered steps the demo \
will show on screen.

A later stage performs each step in a real browser and works out for itself what to click, \
so describe a step by what the presenter does, the way you would tell a person: "Search for \
running shoes", "Open the first result", "Choose the colour Navy". Never mention selectors, \
element names you have not been told about, or coordinates. You have not seen the site, so \
do not invent details of its layout; keep to what the request says and what any such site \
would plainly have.

For each step:
- id: a short lowercase slug with hyphens, unique in the playbook.
- do: one action, or one tight group of actions such as filling a form and submitting it. \
Put the concrete values the request gives (search terms, names, options) into the text.
- expect: what should be visible once the step has worked, so that it can be checked.
- say_hint: the point the narration should make while this step is on screen, in a few words. \
Say why it matters to the viewer, not what is being clicked.
- dwell_seconds: how long to rest on the result before moving on. Narration has to fit in the \
time a step is on screen, so allow 2 to 3 seconds where there is something to say or read.

Keep the demo short: three to eight steps is typical. Show only what the request asks for. \
The recording begins with base_url already open, so do not write a step for opening it. \
Leave out signing in: sign-in is set up separately and never shown.

base_url is where the demo starts: the address the request gives, or the home page of the \
site it names. audience, tone, key_messages and cta describe who the video is for and what \
it should leave them with; take them from the request where it says, and otherwise choose \
what fits. Use an empty string for cta if the request suggests no call to action."""


class DraftStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    do: str
    expect: str
    say_hint: str
    dwell_seconds: float


class Draft(BaseModel):
    """What the model is asked to write: the playbook's substance, without its optional machinery."""

    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    audience: str
    tone: str
    key_messages: list[str]
    cta: str
    steps: list[DraftStep]

    def to_playbook(self) -> dict:
        return {
            "version": 1,
            "name": self.name,
            "target": {"base_url": self.base_url},
            "intent": {"audience": self.audience, "tone": self.tone, "key_messages": self.key_messages, "cta": self.cta or None},
            "steps": [
                {"id": step.id, "do": step.do, "expect": step.expect, "say_hint": step.say_hint, "dwell": f"{max(step.dwell_seconds, 0):g}s"}
                for step in self.steps
            ],
        }


def _check(draft: Draft) -> Problems:
    try:
        Playbook.model_validate(draft.to_playbook())
    except ValidationError as error:
        return [f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in error.errors()], []
    if not draft.base_url.startswith(("http://", "https://")):
        return ["base_url must be a full address starting with http:// or https://"], []
    return [], []


def draft_playbook(llm: LLMProvider, request: str, url: str | None = None, previous: str | None = None, feedback: str | None = None) -> str:
    """The playbook for a request, as YAML. With `previous` and `feedback`, a revision of that draft."""
    parts = [f"The request:\n{request.strip()}"]
    if url:
        parts.append(f"The demo starts at: {url}")
    if previous and feedback:
        parts.append(f"Your current draft:\n{previous.strip()}")
        parts.append(f"The author read it and asks for these changes:\n{feedback.strip()}\n\nRevise the playbook. Change what they ask and keep the rest as it is.")
    else:
        parts.append("Write the playbook.")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]
    draft = complete_checked(llm, messages, Draft, _check)
    # Field order as an author would write it, so the draft reads well when they review or edit it.
    return yaml.safe_dump(draft.to_playbook(), sort_keys=False, allow_unicode=True, width=100)
