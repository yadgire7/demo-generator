from __future__ import annotations

import yaml

from demogen.playbook.schema import Intent, Playbook


def parse_playbook(text: str) -> Playbook:
    return Playbook.model_validate(yaml.safe_load(text))


def merge_intent(playbook: Playbook, notes: str | None) -> Intent:
    """The author's free-text brief (intent.md) rides along with the playbook's structured intent."""
    notes = notes.strip() if notes else None
    return playbook.intent.model_copy(update={"notes": notes or playbook.intent.notes})
