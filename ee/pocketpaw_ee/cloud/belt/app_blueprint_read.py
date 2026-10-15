# ee/pocketpaw_ee/cloud/belt/app_blueprint_read.py — the readers and cache behind
# ``GET /belt/blueprints``: one repo's graph, derived by ``app_blueprint``.
#
# Plan files come from git on the bound clone (``_READ_GIT`` through executor
# ``_run``; ``_re_resolve_repo`` keeps the repo inside the allowlist): one fetch of
# dev, ``ls-tree -r -l`` for the tree and sizes, ``cat-file`` per blueprint file
# (cached by blob sha), and a ``git grep`` of UITests for existing journeys. GitHub
# comes through the patrols gh runner: issues, merged feature-done PRs, dev CI, all
# lead heads in one call, a RECORD.md only for a lead whose head moved, and one
# issue list per dependency repo a ``waits_on`` names. Lead runs come from
# ``service.list_runs``. The graph is cached per (workspace, repo) for ``TTL``
# behind a single-flight lock; a failed read (git error, any gh failure on a list,
# a rate limit) serves the last graph with ``stale: True``, a 503 with none yet.
# Guards: app and feature ids match ``_ID`` before any path or ref is built, and a
# file over ``MAX_MODEL_BYTES`` is refused before it is read. Files are data.

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from . import app_blueprint as ab
from .executor import _re_resolve_repo, _run
from .orient import _dicts, _owned_system, component_for, load_model, path_index
from .service import _READ_GIT, MAX_MODEL_BYTES, BeltConsoleError

Run = Callable[[list[str]], Awaitable[tuple[int, str, str]]]
TTL = 60.0
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_FILE = re.compile(r"docs/blueprints/([^/]+)/(?:features/([^/]+)\.wave\.json|c4-planned\.json)")
_JID = re.compile(r"[A-Z]+-[JP][0-9]+")
_STATUSES = ("done", "built", "building", "stuck", "filed", "planned", "gap")
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
# ponytail: grows by one entry per blueprint file version read; LRU if it matters
_BLOBS: dict[str, str] = {}


class ReadFailed(Exception):
    """A git or gh read failed (ref lock, rate limit): serve the last graph."""


async def read_blueprint(
    workspace_id: str,
    repo: str,
    *,
    git: Run | None = None,
    gh: Run | None = None,
    runs: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    touched: Callable[[str, str], Awaitable[list[str]]] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The repo's blueprint graph (the design doc's section 5 shape). 404 for a
    repo outside the allowlist; 503 when the first read fails."""
    from pocketpaw_ee.cloud.mandates.patrols import _default_gh

    path, _err = _re_resolve_repo(repo)
    if path is None:
        raise BeltConsoleError(404, "Repo not found.")
    key = (workspace_id, str(path))
    async with _LOCKS.setdefault(key, asyncio.Lock()):
        hit = _CACHE.get(key)
        if hit and now() - hit["at"] < TTL:
            return hit["graph"]
        io = (git or _git(path), gh or _default_gh, runs or _lead_runs, touched or _touched)
        try:
            entry = await _read(workspace_id, path, *io, hit or {})
        except ReadFailed:
            if hit is None:
                raise BeltConsoleError(503, "Blueprint not readable yet.") from None
            entry = {**hit, "graph": {**hit["graph"], "stale": True}}
        _CACHE[key] = {**entry, "at": now()}
        return entry["graph"]


def _git(path: Path) -> Run:
    async def run(argv: list[str]) -> tuple[int, str, str]:
        try:
            return await _run([*_READ_GIT, *argv], cwd=path)
        except (OSError, RuntimeError) as exc:  # git missing, or the timeout
            raise ReadFailed(str(exc)) from exc

    return run


async def _lead_runs(workspace_id: str) -> list[dict[str, Any]]:
    from .service import list_runs

    return (await list_runs(workspace_id))["runs"]


async def _touched(workspace_id: str, action_id: str) -> list[str]:
    from .service import _owned_run, _touched_files

    _action, blob = await _owned_run(workspace_id, action_id)
    return await _touched_files(workspace_id, action_id, blob)


async def _ok(run: Run, argv: list[str]) -> str:
    code, out, err = await run(argv)
    if code != 0:
        raise ReadFailed(err.strip()[:200] or argv[1])
    return out


async def _rows(gh: Run, argv: list[str]) -> list[dict[str, Any]]:
    try:
        rows = json.loads(await _ok(gh, argv))
    except ValueError as exc:
        raise ReadFailed("gh printed no JSON") from exc
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def _record(gh: Run, slug: str, feature: str) -> str | None:
    """A lead's RECORD.md off its branch; ``None`` when absent or oversized."""
    argv = ["gh", "api", f"repos/{slug}/contents/docs/leads/{feature}/RECORD.md?ref=lead/{feature}"]
    code, out, err = await gh(argv)
    if code != 0 and "rate limit" in err.lower():
        raise ReadFailed(err.strip()[:200])
    try:
        body = json.loads(out) if code == 0 else {}
        if int(body.get("size") or 0) > MAX_MODEL_BYTES:
            return None
        return base64.b64decode(body.get("content") or "").decode("utf-8", "replace") or None
    except (ValueError, AttributeError, TypeError):
        return None


def _waits(raw: Any, slug: str, external: set[str]) -> list[str]:
    """``waits_on`` refs as ``owner/repo#n``: a chunk led by an external system's
    id in the app's model (``creative-core #171``) is that sibling repo's."""
    out: list[str] = []
    for chunk in str(raw or "").split(";"):
        if "not blocking" in chunk:
            continue
        head = re.match(r"\s*([a-z0-9][a-z0-9-]*) #", chunk)
        repo = f"{slug.split('/')[0]}/{head[1]}" if head and head[1] in external else slug
        out += [f"{repo}#{n}" for n in re.findall(r"#(\d+)", chunk)]
    return out


def _present(path: str, tree: list[str]) -> bool:
    # ponytail: a scan of the tree per path; index by prefix if repos get big
    return any(t == path or t.startswith(path.rstrip("/") + "/") or fnmatch(t, path) for t in tree)


async def _read(
    ws: str, path: Path, git: Run, gh: Run, runs: Any, touched: Any, prev: dict[str, Any]
) -> dict[str, Any]:
    from pocketpaw_ee.cloud.mandates.patrols import _github_slug

    await _ok(git, ["fetch", "--no-tags", "origin", "dev"])
    sha = (await _ok(git, ["rev-parse", "origin/dev"])).strip()
    tree, shas, sizes = [], {}, {}
    for line in (await _ok(git, ["ls-tree", "-r", "-l", "origin/dev"])).splitlines():
        meta, _, p = line.partition("\t")
        parts = meta.split()
        if len(parts) == 4 and parts[1] == "blob":
            tree.append(p)
            shas[p], sizes[p] = parts[2], int(parts[3]) if parts[3].isdigit() else 0
    code, grep, err = await git(
        ["grep", "-E", "/// [A-Z]+-[JP][0-9]+", "origin/dev", "--", "UITests/"]
    )
    if code > 1:
        raise ReadFailed(err.strip()[:200])
    existing = {j: ln.split(":")[1] for ln in grep.splitlines() for j in _JID.findall(ln)}

    async def blob(p: str | None) -> str | None:
        if p not in shas or sizes[p] > MAX_MODEL_BYTES:
            return None
        if shas[p] not in _BLOBS:
            _BLOBS[shas[p]] = await _ok(git, ["cat-file", "blob", shas[p]])
        return _BLOBS[shas[p]]

    apps: dict[str, dict[str, str]] = {}
    for p in tree:
        m = _FILE.fullmatch(p)
        if m and _ID.fullmatch(m[1]) and (m[2] is None or _ID.fullmatch(m[2])):
            apps.setdefault(m[1], {})[m[2] or ""] = p

    slug = prev["slug"] if "slug" in prev else await _github_slug(str(path), gh)
    issues: dict[int, dict[str, Any]] = {}
    done_prs: dict[str, list[dict[str, Any]]] = {}
    ci, heads, records = None, {}, {}
    if slug:
        r = ["-R", slug, "--limit", "500", "--json"]
        for row in await _rows(
            gh, ["gh", "issue", "list", "--state", "all", *r, "number,state,labels"]
        ):
            issues[row.get("number")] = row
        merged = ["gh", "pr", "list", "--label", "feature-done", "--state", "merged"]
        for row in await _rows(gh, [*merged, *r, "number,body"]):
            for f in re.findall(
                r"^Feature:\s*(?:[\w-]+/)?([\w-]+)", str(row.get("body") or ""), re.M
            ):
                done_prs.setdefault(f, []).append(
                    {"number": row.get("number"), "state": "MERGED", "done": True}
                )
        ci = next(
            iter(
                await _rows(
                    gh,
                    [
                        "gh",
                        "run",
                        "list",
                        "--branch",
                        "dev",
                        *r[:2],
                        "--limit",
                        "5",
                        "--json",
                        "headSha,conclusion,url",
                    ],
                )
            ),
            None,
        )
        for row in await _rows(gh, ["gh", "api", f"repos/{slug}/git/matching-refs/heads/lead/"]):
            f = str(row.get("ref") or "").removeprefix("refs/heads/lead/")
            if _ID.fullmatch(f):
                heads[f] = (row.get("object") or {}).get("sha")
        for f, head in heads.items():
            if head == prev.get("heads", {}).get(f):
                records[f] = prev.get("records", {}).get(f)
            else:
                records[f] = await _record(gh, slug, f)
    dev_ci = ci and {
        "sha": ci.get("headSha"),
        "state": {"success": "green", "failure": "red"}.get(ci.get("conclusion"), "pending"),
        "url": ci.get("url"),
    }

    lead_rows: dict[tuple[str, str], dict[str, Any]] = {}
    in_flight, models = [], []
    for row in await runs(ws):
        if not row.get("repo") or Path(str(row["repo"])).expanduser().resolve() != path:
            continue
        lead = row.get("lead") or {}
        if lead.get("feature"):
            lead_rows.setdefault((str(lead.get("app")), str(lead["feature"])), row)
        if row.get("status") not in ab._SETTLED:
            fid = f"{lead.get('app')}/{lead['feature']}" if lead.get("feature") else None
            in_flight.append(
                {
                    "action_id": row.get("action_id"),
                    "kind": "lead" if fid else "task",
                    "feature": fid,
                    "files": await touched(ws, str(row.get("action_id"))),
                }
            )

    features, components, journeys, gaps, app_rows = [], [], [], [], []
    for app, files in sorted(apps.items()):
        model = load_model(await blob(files.get("")) or "")
        owned = _owned_system(model) if model else None
        external = (
            {str(s.get("id")) for s in _dicts(model["model"]["systems"])}
            - {str((owned or {}).get("id"))}
            if model
            else set()
        )
        models.append((path_index(model), ab.component_layers(model)))
        mine, named = [], {}
        for f, p in sorted(files.items()):
            text = await blob(p) if f else None
            wave = ab.parse_wave(text) if text else None
            if wave is None:
                continue
            raw = json.loads(text or "{}")
            row = issues.get(wave["issue"]) or {}
            issue = row and {
                "state": str(row.get("state")).lower(),
                "labels": [str(x.get("name")) for x in _dicts(row.get("labels"))],
            }
            feat = ab.derive_feature(
                app,
                f,
                text or "",
                records.get(f),
                model=model,
                tree=tree,
                issue=issue or None,
                run=lead_rows.get((app, f)),
                prs=done_prs.get(f, []),
            )
            feat["waits_on"] = _waits(raw.get("waits_on"), slug or "", external)
            feat["journeys"] = [
                f"{app}/{j}" for j in _JID.findall(json.dumps(raw.get("journeys") or []))
            ]
            for j in feat["journeys"]:
                named.setdefault(j, []).append(feat)
            mine.append(feat)
        for c in _dicts(owned.get("containers")) if owned else []:
            for k in _dicts(c.get("components")):
                paths = [x for x in k.get("paths") or [] if isinstance(x, str)]
                present = sum(_present(x, tree) for x in paths)
                status = (
                    "built"
                    if paths and present == len(paths)
                    else "partial"
                    if present
                    else "planned"
                )
                cid = f"{app}/{k.get('id')}"
                owners = [x for x in mine if cid in x["components"]]
                if status != "built" and any(x["status"] in ("built", "done") for x in owners):
                    gaps.append({"kind": "component_missing", "ref": f"component:{cid}"})
                components.append(
                    {
                        "id": cid,
                        "container": c.get("id"),
                        "layer": ab.container_layer(str(c.get("id"))),
                        "status": status,
                        "present": present,
                        "total": len(paths),
                        "features": [x["id"] for x in owners],
                    }
                )
        green = bool(dev_ci) and dev_ci["state"] == "green"
        for j, owners in named.items():
            built = any(x["status"] in ("built", "done") for x in owners)
            state = (
                "passing"
                if j.split("/")[1] in existing and built and green
                else "existing"
                if j.split("/")[1] in existing
                else "named"
            )
            if state == "named" and any(x["status"] == "done" for x in owners):
                gaps.append({"kind": "journey_no_test", "ref": f"journey:{j}"})
            journeys.append(
                {
                    "id": j,
                    "file": existing.get(j.split("/")[1]),
                    "state": state,
                    "features": [x["id"] for x in owners],
                }
            )
        mine_j = [x for x in journeys if x["id"].startswith(app + "/")]
        app_rows.append(
            {
                "id": app,
                "counts": {
                    "total": len(mine),
                    **{s: sum(x["status"] == s for x in mine) for s in _STATUSES},
                },
                "journeys": {
                    "named": len(mine_j),
                    "existing": sum(x["state"] != "named" for x in mine_j),
                    "passing": sum(x["state"] == "passing" for x in mine_j),
                },
            }
        )
        features += mine

    states: dict[str, dict[int, str]] = {
        slug or "": {n: str(r.get("state")).lower() for n, r in issues.items()}
    }
    for dep in {ref.split("#")[0] for f in features for ref in f["waits_on"]} - states.keys():
        rows = await _rows(
            gh,
            [
                "gh",
                "issue",
                "list",
                "-R",
                dep,
                "--state",
                "all",
                "--limit",
                "500",
                "--json",
                "number,state",
            ],
        )
        states[dep] = {r.get("number"): str(r.get("state")).lower() for r in rows}
    for f in features:
        gaps += [{"kind": g, "ref": f"feature:{f['id']}"} for g in f["gaps"]]
        f["waits_on"] = [
            {
                "ref": ref,
                "state": states.get(ref.split("#")[0], {}).get(int(ref.split("#")[1]), "unknown"),
            }
            for ref in f["waits_on"]
        ]
    for run in in_flight:
        placed = {
            ab.file_layer(p, i, lay)
            for p in run.pop("files")
            for i, lay in models
            if p.startswith(("Tests/", "UITests/")) or component_for(p, i)
        }
        run["layers"] = [x for x in ab.LAYERS if x in placed]
    graph = {
        "repo": str(path),
        "slug": slug,
        "sha": sha,
        "stale": False,
        "read_at": datetime.now(UTC).isoformat(),
        "dev_ci": dev_ci,
        "layers": list(ab.LAYERS),
        "apps": app_rows,
        "features": features,
        "components": components,
        "journeys": journeys,
        "in_flight": in_flight,
        "gaps": gaps,
    }
    return {"graph": graph, "slug": slug, "heads": heads, "records": records}
