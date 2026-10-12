import pytest

from demogen.config import Config, ModelConfig
from demogen.store.artifacts import LocalArtifactStore
from demogen.store.cache import StageCache, cache_key
from demogen.story.budget import speech_seconds, word_budget


def test_artifact_round_trip_and_verify(tmp_path):
    store = LocalArtifactStore(tmp_path)
    ref = store.put_json("a/b.json", {"x": 1})
    assert store.get_json(ref) == {"x": 1}
    assert store.verify(ref)
    store.put_json("a/b.json", {"x": 2})
    assert not store.verify(ref)


def test_artifact_paths_cannot_escape_the_run(tmp_path):
    with pytest.raises(ValueError):
        LocalArtifactStore(tmp_path / "run").put_bytes("../outside.txt", b"x")


def test_cache_key_tracks_stage_version_config_and_inputs():
    base = cache_key("s", 1, {"wps": 2.4}, {"a": 1})
    assert base == cache_key("s", 1, {"wps": 2.4}, {"a": 1})
    assert len({base, cache_key("t", 1, {"wps": 2.4}, {"a": 1}), cache_key("s", 2, {"wps": 2.4}, {"a": 1}),
                cache_key("s", 1, {"wps": 3.0}, {"a": 1}), cache_key("s", 1, {"wps": 2.4}, {"a": 2})}) == 5


def test_cache_hit_requires_untouched_artifacts(tmp_path):
    store = LocalArtifactStore(tmp_path)
    cache = StageCache(store)
    assert cache.get("s", "k") is None
    outputs = {"story": store.put_json("story.json", {"v": 1}), "nested": {"n": [1, 2]}}
    cache.put("s", "k", outputs)
    assert cache.get("s", "k") == outputs
    store.put_json("story.json", {"v": 2})  # someone edited the artifact
    assert cache.get("s", "k") is None


def test_cache_invalidate_is_per_stage(tmp_path):
    cache = StageCache(LocalArtifactStore(tmp_path))
    cache.put("a", "k", {"v": 1})
    cache.put("b", "k", {"v": 2})
    cache.invalidate("a")
    assert cache.get("a", "k") is None
    assert cache.get("b", "k") == {"v": 2}


def test_config_fingerprint_follows_dotted_keys():
    config = Config(models={"writer": ModelConfig(model="x/y")})
    fingerprint = config.fingerprint(("target_wps", "models.writer", "models.vision"))
    assert fingerprint["target_wps"] == 3.0
    assert fingerprint["models.writer"]["model"] == "x/y"
    assert fingerprint["models.vision"] is None


def test_word_budget_and_speech_length():
    assert word_budget(3.0, 2.4) == 7
    assert word_budget(0.1, 2.4) == 1
    assert speech_seconds("one two three four five six", 2.4) == 2.5
