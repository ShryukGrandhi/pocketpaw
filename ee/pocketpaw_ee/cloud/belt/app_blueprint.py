# ee/pocketpaw_ee/cloud/belt/app_blueprint.py — the Belt blueprint's pure derivation.
#
# Turns one app's planning files plus the GitHub and Belt evidence the caller
# already read into the feature rows of ``GET /belt/blueprints``. No I/O, no
# clock, no network: the readers (git, gh, the run store) live elsewhere and pass
# text and rows in. Size caps and id validation belong to those readers.
#
#   parsers  ``parse_wave`` (a feature's ``.wave.json``), ``parse_record`` (a
#            lead's RECORD.md: ``Stage:``/``Next:`` and the task table read by
#            header name). The app's ``c4-planned.json`` goes through
#            ``orient.load_model``. Unreadable input gives ``None`` fields, never a
#            guessed value.
#   layers   a container maps to tests, shell, model, app or other by id, first
#            match wins; an external system's container is engine. A file under
#            ``Tests/`` or ``UITests/`` is tests before any container lookup. A
#            task sits on the layer most of its non-test files map to, else tests.
#            A feature is placed by its ``owned`` paths, else its RECORD tasks,
#            else "unknown".
#   status   the first rule that holds wins (design doc, section 1): done, built,
#            gap, stuck, building, filed, planned. The wave.json ``status`` is
#            only the lead's claim; a built or done claim with no merged
#            feature-done PR is the ``claimed_ahead`` gap.

from __future__ import annotations

import json
import posixpath
import re
from collections import Counter
from typing import Any

from pocketpaw_ee.cloud.belt.orient import (
    PathIndex,
    _dicts,
    _owned_system,
    component_for,
    path_index,
)

LAYERS = ("tests", "app", "shell", "model", "engine", "other")
_LAYER_RULES = (
    ("tests", re.compile(r".*-(tests|journeys|snapshots)|app-audit")),
    ("shell", re.compile(r"suiteui.*|suite-shell|afterglow-.*")),
    ("model", re.compile(r".*-model|suite-logic")),
    ("app", re.compile(r".*-app|apps")),
)
_FIELD = re.compile(r"^(Stage|Next):[ \t]*(\S.*?)[ \t]*$", re.M)
_SETTLED = {"landed", "rejected", "failed"}


def parse_wave(text: str) -> dict[str, Any] | None:
    """A feature's ``.wave.json`` as plan fields, or ``None`` when it is not a
    JSON object. ``issue`` is the ``feature_issue`` number only when it reads
    ``#N`` exactly ("file when ready" and "later" are not filed)."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    issue = re.fullmatch(r"#(\d+)", str(raw.get("feature_issue") or "").strip())
    waits = str(raw.get("waits_on") or "").split(";")
    owned = raw.get("owned")
    return {
        "title": raw.get("title"),
        "wave": str(raw.get("wave") or "").removeprefix("W"),
        "order": raw.get("order"),
        "claimed": raw.get("status"),
        "issue": int(issue.group(1)) if issue else None,
        "depends": [d for d in raw.get("depends") or [] if isinstance(d, str)],
        # ponytail: refs are plain numbers; B3 resolves which repo each lives in
        "waits_on": [
            int(n) for c in waits if "not blocking" not in c for n in re.findall(r"#(\d+)", c)
        ],
        "owned": [p for p in owned if isinstance(p, str)] if isinstance(owned, list) else [],
    }


def parse_record(text: str | None) -> dict[str, Any]:
    """A lead's RECORD.md: ``stage`` and ``next`` from their header lines (``None``
    when absent), and the rows of the table whose header has ``id`` and
    ``files``. A ``same as <id>`` files cell takes that task's files."""
    if not isinstance(text, str) or "\x00" in text:
        return {"stage": None, "next": None, "tasks": []}
    fields = dict(_FIELD.findall(text))
    tasks: list[dict[str, Any]] = []
    files_of: dict[str, list[str]] = {}
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = (
                [c.lower() for c in cells] if {"id", "files"} <= {c.lower() for c in cells} else []
            )
            continue
        if not header or set(cells[0]) <= set("-: "):
            continue
        row = dict(zip(header, cells, strict=False))
        same = re.match(r"same as (\S+)", row.get("files", ""))
        files = (
            files_of.get(same.group(1), [])
            if same
            else [f.strip() for f in row.get("files", "").split(",") if f.strip()]
        )
        pr = re.fullmatch(r"#(\d+)", row.get("pr", ""))
        files_of[row.get("id", "")] = files
        tasks.append(
            {
                "id": row.get("id"),
                "wave": row.get("wave") or None,
                "files": files,
                "pr": int(pr.group(1)) if pr else None,
                "state": row.get("state") or None,
            }
        )
    return {"stage": fields.get("Stage"), "next": fields.get("Next"), "tasks": tasks}


def resolve_files(tokens: list[str], tree: list[str]) -> list[str]:
    """RECORD file tokens (``Live/HaloKeyRules``, ``HaloKeying.swift``) as repo
    paths: every tree path whose extensionless form is the token's, or ends in
    ``/`` plus it. A token that names nothing in the tree is dropped."""
    out: list[str] = []
    for tok in tokens:
        stem = posixpath.splitext(tok)[0]
        out += [
            p for p in tree if (s := posixpath.splitext(p)[0]) == stem or s.endswith("/" + stem)
        ]
    return list(dict.fromkeys(out))


def container_layer(container: str, external: bool = False) -> str:
    """The line-view layer of a C4 container id; first matching rule wins."""
    if external:
        return "engine"
    return next((name for name, rx in _LAYER_RULES if rx.fullmatch(container)), "other")


def component_layers(model: dict[str, Any] | None) -> dict[str, str]:
    """Every component id in a C4 model mapped to its container's layer."""
    systems = _dicts(((model or {}).get("model") or {}).get("systems"))
    owned = _owned_system(model) if model else None
    return {
        str(k["id"]): container_layer(str(c.get("id") or ""), external=s is not owned)
        for s in systems
        for c in _dicts(s.get("containers"))
        for k in _dicts(c.get("components"))
        if k.get("id")
    }


def file_layer(path: str, index: PathIndex, layers: dict[str, str]) -> str:
    if path.startswith(("Tests/", "UITests/")):
        return "tests"
    return layers.get(component_for(path, index) or "", "other")


def task_layer(files: list[str], index: PathIndex, layers: dict[str, str]) -> str:
    """The layer most of a task's non-test files sit on (ties: the first file's),
    ``tests`` when all are test files, ``unknown`` with none."""
    placed = [file_layer(p, index, layers) for p in files]
    rest = Counter(p for p in placed if p != "tests")
    return rest.most_common(1)[0][0] if rest else ("tests" if placed else "unknown")


def feature_status(
    claimed: Any,
    issue_number: int | None,
    issue: dict[str, Any] | None,
    run: dict[str, Any] | None,
    prs: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """``(status, gap kinds)``. ``issue`` is ``{state, labels}``; ``run`` the newest
    lead run row (console ``status``, ``lead`` with ``prs`` and ``stalls``);
    ``prs`` the merged feature-done PRs that name the feature, as ``{number,
    state, done}`` rows like ``lead.prs``."""
    issue, run = issue or {}, run or {}
    lead = run.get("lead") or {}
    closed = str(issue.get("state") or "").lower() == "closed"
    done_pr = any(
        p.get("done") and p.get("state") == "MERGED" for p in [*prs, *(lead.get("prs") or [])]
    )
    ahead = claimed in ("built", "done") and not done_pr
    if run.get("status") == "landed" or (closed and done_pr):
        status = "done"
    elif done_pr:
        status = "built"
    elif ahead or closed:
        status = "gap"
    elif (
        "needs-human" in (issue.get("labels") or [])
        or len(lead.get("stalls") or []) >= 2
        or run.get("status") in _SETTLED
    ):
        status = "stuck"
    elif run:
        status = "building"
    else:
        status = "filed" if issue_number else "planned"
    gaps = ["claimed_ahead"] if ahead else []
    return status, gaps + (["issue_closed_not_done"] if closed and status != "done" else [])


def derive_feature(
    app: str,
    feature: str,
    wave_text: str,
    record_text: str | None,
    *,
    model: dict[str, Any] | None,
    tree: list[str],
    issue: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    prs: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """One feature row of the blueprint graph (``None``: unreadable wave.json).
    ``model`` is the app's loaded c4-planned.json, ``tree`` dev's file paths."""
    wave = parse_wave(wave_text)
    if wave is None:
        return None
    rec = parse_record(record_text)
    index, layers = path_index(model), component_layers(model)
    owned = wave["owned"]
    tasks, task_files = [], []
    for t in rec["tasks"]:
        files = resolve_files(t["files"], [*tree, *owned])
        task_files += files
        tasks.append(
            {
                "id": t["id"],
                "wave": t["wave"],
                "layer": task_layer(files, index, layers),
                "pr": t["pr"],
                "state": t["state"],
            }
        )
    if owned:
        placed = {file_layer(p, index, layers) for p in owned}
    else:
        placed = {t["layer"] for t in tasks} or {"unknown"}
    comps = (component_for(p, index) for p in owned or task_files)
    status, gaps = feature_status(wave["claimed"], wave["issue"], issue, run, prs or [])
    return {
        "id": f"{app}/{feature}",
        "app": app,
        **{k: wave[k] for k in ("title", "wave", "order", "claimed", "depends", "waits_on")},
        "status": status,
        "issue": {**(issue or {}), "number": wave["issue"]} if wave["issue"] else None,
        "run": {"action_id": run.get("action_id"), "stage": rec["stage"], "next": rec["next"]}
        if run
        else None,
        "tasks": tasks,
        "layers": [x for x in (*LAYERS, "unknown") if x in placed],
        "components": list(dict.fromkeys(f"{app}/{c}" for c in comps if c)),
        "gaps": gaps + ([] if owned else ["feature_no_owned"]),
    }
