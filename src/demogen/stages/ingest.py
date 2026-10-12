from __future__ import annotations

from typing import Any

from demogen.playbook.loader import merge_intent, parse_playbook
from demogen.stages.base import StageContext, stage


@stage("load_validate")
def load_validate(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    playbook = parse_playbook(ctx.store.get_bytes(inputs["playbook_src"]["path"]).decode())
    notes = ctx.store.get_bytes(inputs["intent_src"]["path"]).decode() if inputs.get("intent_src") else None
    return {
        "playbook": ctx.store.put_json("playbook.json", playbook.model_dump(mode="json")),
        "intent": ctx.store.put_json("intent.json", merge_intent(playbook, notes).model_dump(mode="json")),
    }

