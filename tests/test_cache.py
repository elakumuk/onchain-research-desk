import json

import pytest

from desk.data import cache as C


def test_live_fetch_is_cached_then_served_offline(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(C, "http_get_json", lambda url, params=None: calls.append(url) or {"x": 1})
    live = C.Cache("live", raw_dir=tmp_path / "raw", snapshot_dir=tmp_path / "snap")
    assert live.get("src", "k", "http://example/api") == {"x": 1}
    assert live.get("src", "k", "http://example/api") == {"x": 1}   # fresh cache hit, no 2nd call
    assert len(calls) == 1
    files = list((tmp_path / "raw" / "src" / "k").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["payload"] == {"x": 1}

    off = C.Cache("offline", raw_dir=tmp_path / "raw", snapshot_dir=tmp_path / "snap")
    monkeypatch.setattr(C, "http_get_json", lambda *a, **k: pytest.fail("offline mode hit the network"))
    assert off.get("src", "k", "http://example/api") == {"x": 1}


def test_network_failure_falls_back_to_snapshot(tmp_path, monkeypatch):
    live = C.Cache("live", raw_dir=tmp_path / "raw", snapshot_dir=tmp_path / "snap")
    monkeypatch.setattr(C, "http_get_json", lambda *a, **k: {"y": 2})
    live.get("src", "k", "u")
    assert live.save_snapshot() == 1

    def boom(*a, **k):
        raise C.DataUnavailable("down")
    monkeypatch.setattr(C, "http_get_json", boom)
    fresh = C.Cache("live", ttl_hours=0, raw_dir=tmp_path / "empty", snapshot_dir=tmp_path / "snap")
    assert fresh.get("src", "k", "u") == {"y": 2}
    assert fresh.served[-1]["origin"] == "snapshot"


def test_snapshot_mode_missing_raises(tmp_path):
    snap = C.Cache("snapshot", raw_dir=tmp_path / "raw", snapshot_dir=tmp_path / "snap")
    with pytest.raises(C.DataUnavailable):
        snap.get("src", "nope", "u")


def test_http_4xx_is_cached_as_negative_result(tmp_path, monkeypatch):
    import requests

    class R:
        status_code = 400

    def bad(*a, **k):
        raise requests.HTTPError("400", response=R())
    monkeypatch.setattr(C, "http_get_json", bad)
    live = C.Cache("live", raw_dir=tmp_path / "raw", snapshot_dir=tmp_path / "snap")
    with pytest.raises(C.DataUnavailable):
        live.get("src", "k", "u")
    live.save_snapshot()
    snap = C.Cache("snapshot", raw_dir=tmp_path / "raw", snapshot_dir=tmp_path / "snap")
    with pytest.raises(C.DataUnavailable, match="cached HTTP 400"):
        snap.get("src", "k", "u")
    assert snap.served[-1]["origin"] == "snapshot http-400"
