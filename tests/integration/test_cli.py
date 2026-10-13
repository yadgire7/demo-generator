import json

import yaml
from typer.testing import CliRunner

from demogen.cli import app
from tests.conftest import SCRIPTED

runner = CliRunner()


def test_run_approve_approve(pinned_playbook, scripted_llm, tmp_path):
    """Every real stage, start to finish, from the command line: browser, speech engines, renderer.

    Only the story models are scripted.
    """
    scripted_llm.replies = [
        {"steps": [{"step_id": s, "happens": "It changes.", "on_screen": []} for s in ("add-task", "finish-task")]},
        {"steps": [{"step_id": "add-task", "text": "Add a task."}, {"step_id": "finish-task", "text": "Done."}]},
    ]
    config = tmp_path / "demogen.yaml"
    config.write_text(yaml.safe_dump(SCRIPTED))
    env = {"DEMOGEN_RUNS_DIR": str(tmp_path / "runs")}
    started = runner.invoke(app, ["run", str(pinned_playbook), "--run-id", "demo", "--config", str(config)], env=env)
    assert started.exit_code == 0 and "approve the plan" in started.output

    assert "approve the story" in runner.invoke(app, ["approve", "demo"], env=env).output
    finished = runner.invoke(app, ["approve", "demo"], env=env)
    assert finished.exit_code == 0 and "done" in finished.output
    run = tmp_path / "runs" / "demo"
    assert (run / "report.json").is_file() and (run / "raw.mp4").stat().st_size > 0
    assert (run / "keyframes" / "add-task" / "00.png").is_file()
    assert "Add a task." in (run / "story.json").read_text()
    # The finished video: voiced, captioned, and checked against its own plan.
    report = json.loads((run / "report.json").read_text())
    assert report["final_check"]["problems"] == [] and report["final_check"]["narration_wer"] == 0
    assert report["final_check"]["size"] == [800, 600] and report["audio_warnings"] == []
    assert (run / "final.mp4").stat().st_size > 0
    assert (run / "final.srt").read_text().startswith("1\n00:00:0")
    assert "Add a task." in (run / "final.srt").read_text()

    again = runner.invoke(app, ["approve", "demo"], env=env)
    assert again.exit_code == 1 and "not waiting for approval" in again.output


def test_invalid_playbook_fails_before_a_run_exists(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nname: x\ntarget: {base_url: http://localhost}\nsteps: []\n")
    result = runner.invoke(app, ["run", str(bad), "--run-id", "demo", "--runs-dir", str(tmp_path / "runs")])
    assert result.exit_code == 1 and "steps" in result.output
    assert not (tmp_path / "runs" / "demo").exists()
