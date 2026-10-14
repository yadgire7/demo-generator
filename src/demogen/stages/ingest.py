from __future__ import annotations

from typing import Any

from demogen.llm.base import llm_for
from demogen.playbook.draft import draft_playbook as draft
from demogen.playbook.loader import merge_intent, parse_playbook
from demogen.stages.base import StageContext, stage


@stage("draft_playbook", config_keys=("models.planner",))
def draft_playbook(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Write a playbook from the run's prompt, or revise the last draft from the author's feedback."""
    request = ctx.store.get_bytes(inputs["prompt_src"]["path"]).decode()
    previous = ctx.store.get_bytes(inputs["playbook_src"]["path"]).decode() if inputs.get("playbook_src") else None
    text = draft(llm_for(ctx.config, "planner"), request, inputs.get("target_url"), previous, inputs.get("playbook_feedback"))
    return {"playbook_src": ctx.store.put_bytes("playbook.yaml", text.encode())}


@stage("load_validate")
def load_validate(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    playbook = parse_playbook(ctx.store.get_bytes(inputs["playbook_src"]["path"]).decode())
    notes = ctx.store.get_bytes(inputs["intent_src"]["path"]).decode() if inputs.get("intent_src") else None
    return {
        "playbook": ctx.store.put_json("playbook.json", playbook.model_dump(mode="json")),
        "intent": ctx.store.put_json("intent.json", merge_intent(playbook, notes).model_dump(mode="json")),
    }

