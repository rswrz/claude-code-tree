"""The parse cache: it must be exact when it hits, and silent when it cannot work."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cct import cache as cache_mod
from cct import paths, transcript
from cct.cache import CACHE_V, Cache
from cct.model import Session
from cct.store import Scope, scan_once

SID = "aaaaaaaa-0000-0000-0000-000000000001"
ENCODED = "-tmp-project"


def a_session() -> Session:
    return Session(
        sid=SID,
        path="/tmp/x.jsonl",
        project_dir="/tmp",
        cwd="/home/user/thing",
        mtime=1787719953.5,
        uuids=["u1", "u2"],
        count=7,
        users=3,
        first="do the thing",
        custom="The Thing",
        ai="guessed thing",
        branch="feat/thing",
        worktree="wt-thing",
        prs={353, 142},
        frames=2,
        ctx=459000,
        ts_min=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
        ts_max=datetime(2026, 1, 1, 17, 30, tzinfo=timezone.utc),
        stub=False,
        hay="some words that were said",
    )


def test_a_session_survives_the_round_trip(fake_home):
    original = a_session()
    restored = cache_mod.from_json(SID, original.path, cache_mod.to_json(original))
    assert restored == original


def test_sets_and_timestamps_serialise_to_json_scalars(fake_home):
    data = cache_mod.to_json(a_session())
    json.dumps(data)
    assert data["prs"] == [142, 353]
    assert data["ts_min"] == "2026-01-01T09:00:00+00:00"


def test_a_hit_needs_the_same_version_size_and_mtime(fake_home, tmp_path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    stat = os.stat(path)

    cache = Cache(ENCODED)
    cache.put(SID, stat, transcript.parse_session(str(path)))
    cache.save()

    assert Cache(ENCODED).get(SID, str(path), stat) is not None

    stale = os.stat_result(tuple(stat))
    assert Cache(ENCODED).get(SID, str(path), _bump(stat, mtime=1.0)) is None
    assert Cache(ENCODED).get(SID, str(path), _bump(stat, size=1)) is None
    assert Cache(ENCODED).get("other-sid", str(path), stale) is None


def _bump(stat: os.stat_result, mtime: float = 0.0, size: int = 0) -> os.stat_result:
    fields = list(stat)
    fields[6] += size          # st_size
    fields[8] += int(mtime)    # st_mtime
    return os.stat_result(tuple(fields))


def test_a_bumped_version_invalidates_every_entry(fake_home, tmp_path, monkeypatch):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n")
    stat = os.stat(path)
    cache = Cache(ENCODED)
    cache.put(SID, stat, transcript.parse_session(str(path)))
    cache.save()

    monkeypatch.setattr(cache_mod, "CACHE_V", CACHE_V + 1)
    assert Cache(ENCODED).get(SID, str(path), stat) is None


def test_the_file_holds_only_what_the_last_scan_saw(fake_home, tmp_path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n")
    stat = os.stat(path)
    session = transcript.parse_session(str(path))

    first = Cache(ENCODED)
    first.put(SID, stat, session)
    first.put("gone-sid", stat, session)
    first.save()

    second = Cache(ENCODED)
    assert second.get(SID, str(path), stat) is not None
    second.save()

    with open(cache_mod.cache_path(ENCODED)) as fh:
        assert set(json.load(fh)) == {SID}


def test_a_corrupt_cache_file_is_ignored(fake_home, tmp_path):
    os.makedirs(paths.CACHE_DIR, exist_ok=True)
    Path(cache_mod.cache_path(ENCODED)).write_text("]not json[")
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n")
    cache = Cache(ENCODED)
    assert cache.get(SID, str(path), os.stat(path)) is None
    assert cache.hits == 0


def test_a_truncated_entry_is_ignored(fake_home, tmp_path):
    os.makedirs(paths.CACHE_DIR, exist_ok=True)
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n")
    stat = os.stat(path)
    Path(cache_mod.cache_path(ENCODED)).write_text(json.dumps(
        {SID: {"v": CACHE_V, "mtime": stat.st_mtime, "size": stat.st_size}}))
    assert Cache(ENCODED).get(SID, str(path), stat) is None


def test_an_unwritable_cache_directory_never_raises(fake_home, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CACHE_DIR", "/proc/definitely/not/writable")
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n")
    cache = Cache(ENCODED)
    cache.put(SID, os.stat(path), transcript.parse_session(str(path)))
    cache.save()
    assert not Path(cache_mod.cache_path(ENCODED)).exists()


def test_a_disabled_cache_reads_and_writes_nothing(fake_home, tmp_path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n")
    stat = os.stat(path)
    warm = Cache(ENCODED)
    warm.put(SID, stat, transcript.parse_session(str(path)))
    warm.save()

    cold = Cache(ENCODED, enabled=False)
    assert cold.get(SID, str(path), stat) is None
    cold.put(SID, stat, transcript.parse_session(str(path)))
    cold.save()
    with open(cache_mod.cache_path(ENCODED)) as fh:
        assert set(json.load(fh)) == {SID}


def test_a_cached_scan_matches_an_uncached_one(fake_home, fake_project, make_transcript,
                                               stub_claude, tmp_path):
    work = tmp_path / "work"
    project_dir = fake_project(cwd=str(work))
    for i in range(4):
        make_transcript(project_dir, f"{i}{i}{i}{i}{i}{i}{i}{i}-0000-0000-0000-00000000000{i}",
                        turns=i + 1)
    scope = Scope.project(paths.resolve_dir(str(work)))

    cold = scan_once(scope, use_cache=False)
    warm = scan_once(scope, use_cache=True)   # writes the cache
    hot = scan_once(scope, use_cache=True)    # reads it back

    assert cold.sessions == warm.sessions == hot.sessions
    assert cold.roots == hot.roots
    assert Path(cache_mod.cache_path(project_dir.name)).exists()


def test_a_changed_transcript_is_reparsed(fake_home, fake_project, make_transcript,
                                          stub_claude, tmp_path):
    work = tmp_path / "work"
    project_dir = fake_project(cwd=str(work))
    path = make_transcript(project_dir, SID, turns=1)
    scope = Scope.project(paths.resolve_dir(str(work)))
    assert scan_once(scope).sessions[SID].users == 1

    with open(path, "a") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "extra",
                             "message": {"content": "one more"}}) + "\n")
    os.utime(path, (0, 0))   # a size change alone would already do it; prove mtime counts too
    assert scan_once(scope).sessions[SID].users == 2
