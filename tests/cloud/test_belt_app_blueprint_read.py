# tests/cloud/test_belt_app_blueprint_read.py — the blueprint readers and cache.
#
# ``read_blueprint`` over fake git and gh runners serving the B2 fixtures as a
# creative-apps clone: Halo's c4-planned.json, layer-keys and live-keys wave files,
# the layer-keys RECORD on its lead branch. Pins the cache (a second read within
# the TTL makes no calls; a moved lead head re-reads only its RECORD), the stale
# fallback on a rate limit, and the guards (a bad id or an oversized file is
# refused before it is read).

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import app_blueprint_read as abr  # noqa: E402
from pocketpaw_ee.cloud.belt.service import MAX_MODEL_BYTES, BeltConsoleError  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "app_blueprint"
BP = "docs/blueprints/halo"
FILES = {
    f"{BP}/c4-planned.json": (FX / "halo-c4-planned.json").read_text(),
    f"{BP}/features/layer-keys.wave.json": (FX / "halo-layer-keys.wave.json").read_text(),
    f"{BP}/features/live-keys.wave.json": (FX / "flare-live-keys.wave.json").read_text(),
    f"{BP}/features/Bad_Id.wave.json": "{}",
    f"{BP}/features/huge.wave.json": "{}",
}
SIZES = {f"{BP}/features/huge.wave.json": MAX_MODEL_BYTES + 1}
RECORD = (FX / "layer-keys-RECORD.md").read_text()


def sha(p: str) -> str:
    return hashlib.sha1(p.encode()).hexdigest()


class Fake:
    def __init__(self, repo: Path):
        self.repo, self.calls, self.limited = repo, [], False
        self.heads = {"layer-keys": "a1", "easing": "b1", "Bad.Ref": "c1"}
        self.records = {"layer-keys": RECORD, "easing": "Stage: 1 plan\n"}
        tree = [*FILES, *(FX / "dev-tree.txt").read_text().split()]
        self.ls = "".join(f"100644 blob {sha(p)} {SIZES.get(p, 10)}\t{p}\n" for p in tree)

    async def git(self, argv):
        self.calls.append(argv)
        out = {"rev-parse": "62a8577\n", "ls-tree": self.ls, "fetch": ""}.get(argv[0])
        if argv[0] == "grep":
            out = "origin/dev:UITests/HaloJourneyTests+Drag.swift:  /// H-J5 drag\n"
        if argv[0] == "cat-file":
            out = next(t for p, t in FILES.items() if sha(p) == argv[2])
        return 0, out, ""

    async def gh(self, argv):
        self.calls.append(argv)
        if argv[0] == "git":
            return 0, "https://github.com/qbtrix/creative-apps.git\n", ""
        if self.limited:
            return 1, "", "HTTP 403: API rate limit exceeded"
        rows: object = []
        if argv[1:3] == ["issue", "list"] and argv[argv.index("-R") + 1] == "qbtrix/creative-apps":
            rows = [{"number": 1036, "state": "OPEN", "labels": [{"name": "feature-lead"}]}]
        elif argv[1:3] == ["issue", "list"]:
            rows = [{"number": 171, "state": "CLOSED"}]
        elif argv[1:3] == ["run", "list"]:
            rows = [{"headSha": "62a8577", "conclusion": "success", "url": "u"}]
        elif argv[1] == "api" and "matching-refs" in argv[2]:
            rows = [
                {"ref": f"refs/heads/lead/{f}", "object": {"sha": s}} for f, s in self.heads.items()
            ]
        elif argv[1] == "api":
            f = argv[2].split("ref=lead/")[1]
            text = self.records[f].encode()
            rows = {"size": len(text), "content": base64.b64encode(text).decode()}
        return 0, json.dumps(rows), ""

    async def runs(self, ws):
        lead = {"feature": "layer-keys", "app": "halo", "prs": [], "stalls": []}
        return [{"action_id": "act-1", "status": "running", "repo": str(self.repo), "lead": lead}]

    async def touched(self, ws, action_id):
        return ["UITests/HaloJourneyTests+Drag.swift"]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    for name in ("_CACHE", "_LOCKS", "_BLOBS"):
        monkeypatch.setattr(abr, name, {})
    monkeypatch.setattr(abr, "_re_resolve_repo", lambda r: (tmp_path.resolve(), None))
    return Fake(tmp_path.resolve())


async def read(fake, t=0.0):
    io = {"git": fake.git, "gh": fake.gh, "runs": fake.runs, "touched": fake.touched}
    return await abr.read_blueprint("ws1", "creative-apps", **io, now=lambda: t)


def feature(graph, fid):
    return next(f for f in graph["features"] if f["id"] == fid)


async def test_first_read_derives_the_graph(fake):
    g = await read(fake)
    lk = feature(g, "halo/layer-keys")
    assert (g["slug"], g["sha"], g["stale"], g["dev_ci"]["state"]) == (
        "qbtrix/creative-apps",
        "62a8577",
        False,
        "green",
    )
    assert lk["status"] == "building" and lk["run"]["stage"].startswith("5 finish")
    assert {"ref": "qbtrix/creative-apps#1036", "state": "open"} not in lk["waits_on"]
    assert {"ref": "qbtrix/creative-apps#777", "state": "unknown"} in lk["waits_on"]
    live = feature(g, "halo/live-keys")
    assert live["waits_on"][0] == {"ref": "qbtrix/creative-core#171", "state": "closed"}
    assert (
        live["status"] == "gap"
        and {"kind": "claimed_ahead", "ref": "feature:halo/live-keys"} in g["gaps"]
    )
    assert g["in_flight"] == [
        {"action_id": "act-1", "kind": "lead", "feature": "halo/layer-keys", "layers": ["tests"]}
    ]
    assert {c["status"] for c in g["components"]} <= {"built", "partial", "planned"} and g[
        "components"
    ]
    assert g["apps"][0]["id"] == "halo" and g["apps"][0]["counts"]["total"] == 2


async def test_second_read_within_ttl_makes_no_calls(fake):
    first = await read(fake)
    n = len(fake.calls)
    assert await read(fake, t=59.0) is first and len(fake.calls) == n
    await read(fake, t=61.0)
    assert len(fake.calls) > n


async def test_moved_lead_head_rereads_only_that_record(fake):
    await read(fake)
    fake.heads["layer-keys"] = "a2"
    fake.records["layer-keys"] = "Stage: 6 landed\n"
    n = len(fake.calls)
    g = await read(fake, t=61.0)
    contents = [c for c in fake.calls[n:] if c[1] == "api" and "contents/" in c[2]]
    assert [c[2].split("ref=")[1] for c in contents] == ["lead/layer-keys"]
    assert feature(g, "halo/layer-keys")["run"]["stage"] == "6 landed"


async def test_rate_limit_serves_the_last_graph_stale(fake):
    with pytest.raises(BeltConsoleError):  # nothing read yet: 503
        fake.limited = True
        await read(fake)
    fake.limited = False
    first = await read(fake)
    fake.limited = True
    g = await read(fake, t=61.0)
    assert g["stale"] is True and g["features"] == first["features"]


async def test_bad_id_and_oversized_files_are_refused_not_parsed(fake):
    g = await read(fake)
    read_shas = {c[2] for c in fake.calls if c[0] == "cat-file"}
    assert sha(f"{BP}/features/Bad_Id.wave.json") not in read_shas
    assert sha(f"{BP}/features/huge.wave.json") not in read_shas
    assert {f["id"] for f in g["features"]} == {"halo/layer-keys", "halo/live-keys"}
    assert not any("Bad.Ref" in c[2] for c in fake.calls if c[1:2] == ["api"])
    fake.records["layer-keys"] = "x" * 10
    fake.heads["layer-keys"] = "a3"
    big = {"size": MAX_MODEL_BYTES + 1, "content": base64.b64encode(RECORD.encode()).decode()}
    gh = fake.gh

    async def oversized(argv):
        if argv[1:2] == ["api"] and "contents/" in argv[2]:
            return 0, json.dumps(big), ""
        return await gh(argv)

    fake.gh = oversized
    g = await read(fake, t=61.0)
    assert feature(g, "halo/layer-keys")["run"]["stage"] is None


async def test_repo_outside_the_allowlist_is_404(fake, monkeypatch):
    monkeypatch.setattr(abr, "_re_resolve_repo", lambda r: (None, "not allowed"))
    with pytest.raises(BeltConsoleError) as err:
        await read(fake)
    assert err.value.status_code == 404 and fake.calls == []
