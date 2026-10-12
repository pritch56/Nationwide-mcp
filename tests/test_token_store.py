import json
import os
import subprocess
import sys
import threading

import pytest

from nationwide_mcp.errors import ConfigError, TokenStoreError
from nationwide_mcp.token_store import EnvTokenStore, FileTokenStore, MemoryTokenStore, build_token_store


def test_file_store_round_trip_and_rotation(tmp_path):
    path = tmp_path / "nested" / "tokens.json"
    store = FileTokenStore(path)
    assert store.load() == {}
    store.put("enablebanking", {"session_id": "s1"})
    store.put("other", {"refresh": "r1"})
    store.put("other", {"refresh": "r2"})  # a rotated token replaces the old one
    reloaded = FileTokenStore(path).load()
    assert reloaded == {"enablebanking": {"session_id": "s1"}, "other": {"refresh": "r2"}}
    assert [p.name for p in path.parent.iterdir()] == ["tokens.json"]  # no temp files left behind


def test_file_store_corrupt_file_is_a_token_store_error(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TokenStoreError) as info:
        FileTokenStore(path).load()
    assert str(info.value).startswith("[token_store_error] ")


def test_env_store_reads_and_keeps_writes_in_memory():
    store = EnvTokenStore({"NATIONWIDE_TOKEN_JSON": json.dumps({"enablebanking": {"session_id": "s9"}})})
    assert store.get("enablebanking")["session_id"] == "s9"
    store.put("enablebanking", {"session_id": "s10"})
    assert store.get("enablebanking")["session_id"] == "s10"
    with pytest.raises(ConfigError):
        EnvTokenStore({"NATIONWIDE_TOKEN_JSON": "nope"})


def test_factory(tmp_path):
    assert isinstance(build_token_store({"NATIONWIDE_TOKEN_STORE": "memory"}), MemoryTokenStore)
    file_store = build_token_store({"NATIONWIDE_TOKEN_FILE": str(tmp_path / "t.json")})
    assert isinstance(file_store, FileTokenStore)
    custom = build_token_store({"NATIONWIDE_TOKEN_STORE": "nationwide_mcp.token_store:MemoryTokenStore"})
    assert isinstance(custom, MemoryTokenStore)
    with pytest.raises(ConfigError):
        build_token_store({"NATIONWIDE_TOKEN_STORE": "redis"})
    with pytest.raises(ConfigError):
        build_token_store({"NATIONWIDE_TOKEN_STORE": "no.such.module:Thing"})


def test_file_store_is_readable_by_the_owner_only(tmp_path):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).put("truelayer", {"refresh_token": "r1"})
    if sys.platform == "win32":
        listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
        entries = [line.replace(str(path), "").strip() for line in listing.splitlines() if ":(" in line]
        assert len(entries) == 1, listing  # inheritance removed: exactly one explicit entry ...
        assert os.environ["USERNAME"].lower() in entries[0].lower() and "(F)" in entries[0]  # ... the current user
    else:
        assert (path.stat().st_mode & 0o777) == 0o600


def test_acl_failure_is_logged_not_fatal(tmp_path, monkeypatch, caplog):
    from nationwide_mcp import token_store

    def boom(*args, **kwargs):
        raise OSError("icacls / chmod unavailable")

    monkeypatch.setattr(token_store.subprocess, "run", boom)
    monkeypatch.setattr(token_store.os, "chmod", boom)
    path = tmp_path / "tokens.json"
    with caplog.at_level("WARNING", logger="nationwide_mcp.token_store"):
        FileTokenStore(path).put("truelayer", {"refresh_token": "r1"})
    assert FileTokenStore(path).get("truelayer") == {"refresh_token": "r1"}
    assert any("permissions" in r.message.lower() or "acl" in r.message.lower() for r in caplog.records)


def test_file_store_fsyncs_before_the_rename(tmp_path, monkeypatch):
    from nationwide_mcp import token_store

    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(token_store.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(token_store.os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    FileTokenStore(tmp_path / "tokens.json").save({"a": {"b": 1}})
    assert events == ["fsync", "replace"]


def test_file_store_wraps_os_errors(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    store = FileTokenStore(blocker / "tokens.json")  # the parent is a file: mkdir / open must fail
    with pytest.raises(TokenStoreError) as info:
        store.save({"a": {}})
    assert str(info.value).startswith("[token_store_error] ")
    directory = tmp_path / "a-directory.json"
    directory.mkdir()
    with pytest.raises(TokenStoreError):
        FileTokenStore(directory).load()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".tokens-")] == []


def test_put_is_serialised_so_concurrent_sections_are_not_lost(tmp_path):
    class Slow(MemoryTokenStore):
        def load(self):
            data = super().load()
            threading.Event().wait(0.01)  # widen the read-modify-write window
            return data

    for target in (FileTokenStore(tmp_path / "tokens.json"), Slow()):
        threads = [threading.Thread(target=target.put, args=(f"s{i}", {"n": i})) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert target.load() == {f"s{i}": {"n": i} for i in range(12)}


def test_clear_forgets_everything(tmp_path):
    path = tmp_path / "tokens.json"
    store = FileTokenStore(path)
    store.put("truelayer", {"refresh_token": "r1"})
    store.clear()
    assert not path.exists() and store.load() == {}
    store.clear()  # idempotent
    memory = MemoryTokenStore({"truelayer": {"refresh_token": "r1"}})
    memory.clear()
    assert memory.load() == {}
