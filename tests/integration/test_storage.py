"""S3 artifacts and Postgres checkpoints: the same behaviour as the local folder and SQLite file."""

from __future__ import annotations

import pytest

from demogen.exec.local import LocalExecutor
from demogen.graph.runtime import RunError, Runtime
from demogen.store.cache import StageCache
from demogen.store.s3 import S3ArtifactStore
from demogen.store.stores import S3Stores
from demogen.config import Config
from tests.conftest import all_fakes


@pytest.fixture
def store(minio_server, bucket):
    return S3ArtifactStore(minio_server["client"], bucket, "runs/r1")


def test_s3_artifacts_round_trip_and_verify(store, tmp_path):
    ref = store.put_json("a/b.json", {"x": 1})
    assert store.get_json(ref) == {"x": 1} and store.verify(ref) and store.sha256("a/b.json") == ref["sha256"]
    assert store.put_bytes("a/b.json", b'{"x": 2}')["sha256"] != ref["sha256"]
    assert not store.verify(ref)  # the object changed under the old ref
    assert not store.verify({"path": "missing.json", "sha256": "0"}) and not store.exists("missing.json")
    with pytest.raises(FileNotFoundError):
        store.get_bytes("missing.json")


def test_s3_files_are_fetched_to_a_local_path_by_content(store, tmp_path):
    source = tmp_path / "raw.mp4"
    source.write_bytes(b"first take")
    first = store.put_file("raw.mp4", source)
    assert store.local_path(first).read_bytes() == b"first take"
    source.write_bytes(b"second take")
    second = store.put_file("raw.mp4", source)
    # The same artifact path, new content: never the stale download.
    assert store.local_path(second).read_bytes() == b"second take" and store.local_path(first) != store.local_path(second)


def test_s3_paths_cannot_leave_the_run_and_runs_do_not_see_each_other(minio_server, bucket, store):
    with pytest.raises(ValueError):
        store.put_bytes("../r2/secret.txt", b"x")
    store.put_bytes("note.txt", b"mine")
    other = S3ArtifactStore(minio_server["client"], bucket, "runs/r2")
    assert not other.exists("note.txt")


def test_both_stores_list_what_is_under_a_folder(store, tmp_path):
    from demogen.store.artifacts import LocalArtifactStore

    for each in (store, LocalArtifactStore(tmp_path / "r1")):
        for path in ("failures/b.json", "failures/a.png", "failures-old/c.json", "story.json"):
            each.put_bytes(path, b"x")
        assert each.list_prefix("failures") == ["failures/a.png", "failures/b.json"]
        assert each.list_prefix("nothing") == []
        assert len(each.list_prefix("")) == 4


def test_stage_cache_works_over_s3_and_invalidates_by_stage(store):
    cache = StageCache(store)
    assert cache.get("write_story", "k") is None
    outputs = {"story": store.put_json("story.json", {"v": 1})}
    cache.put("write_story", "k", outputs)
    cache.put("rehearse", "k", {"v": 2})
    assert cache.get("write_story", "k") == outputs
    cache.invalidate("write_story")
    assert cache.get("write_story", "k") is None and cache.get("rehearse", "k") == {"v": 2}
    store.put_json("story.json", {"v": 2})
    cache.put("write_story", "k", outputs)
    assert cache.get("write_story", "k") is None  # the artifact it points at has since changed


def test_run_survives_restarts_on_postgres_and_s3(minio_server, bucket, database_url, playbook, tmp_path):
    def runtime():
        return Runtime(S3Stores(minio_server["client"], bucket), database_url, LocalExecutor(all_fakes()))

    first = runtime()
    status = first.start(playbook, None, Config(), "r1")
    assert status.phase == "awaiting_approval" and status.awaiting["kind"] == "plan"
    first.close()

    second = runtime()  # a different process, as far as the run can tell
    assert second.status("r1").awaiting["kind"] == "plan"
    assert second.approve("r1").awaiting["kind"] == "story"
    assert second.approve("r1").phase == "done"
    with pytest.raises(RunError, match="already exists"):
        second.start(playbook, None, Config(), "r1")
    with pytest.raises(RunError, match="no run named 'nope' in s3://"):
        second.status("nope")

    brief = tmp_path / "brief.md"
    brief.write_text("Lead with speed.")
    assert second.rerun("r1", "write_story", brief).awaiting["kind"] == "story"
    assert second.context("r1").store.get_json("intent.json")["notes"] == "Lead with speed."

    second.forget("r1")
    assert second.status("r1").phase == "incomplete" and second.status("r1").state == {}  # history gone
    assert second.context("r1").store.exists("story.json")  # artifacts kept
    second.close()
