# ee/pocketpaw_ee/cloud/mandates/lead_planner.py — the feature-lead shift plan.
#
# A mandate whose charter says ``run_kind: "feature-lead"`` plans without the
# foreman: ONE ``feature-lead`` run per free feature issue, every shift. The
# foreman may split, merge or skip issues; a lead line needs exactly one run per
# issue, so its plan is deterministic. Pure: no I/O, no clock.
#
# ``plan`` takes the free sightings (the caller has already dropped the ones in
# flight, blocked by an open dependency or closed: ``service._backlog``), keeps
# those naming a GitHub issue, orders them p1 before p2 before p3 (issue labels,
# unlabelled last), then oldest first, and plans one task per sighting up to the
# shift's ``budget``: title = the issue title, why = a one-line pointer to the
# issue (``#<n>``, which the cloud station reads the lead's issue from), recipe
# = ``feature-lead``, evidence = the sighting. A title naming another issue
# (``Follow-up to #12``) is prefixed with its own ``#<n>``, since the station
# takes the FIRST ``#N`` in title + why. No free issue stands the shift down.
#
# NEVER TWICE, UNTIL RE-ARMED: an issue labelled ``needs-human`` is skipped,
# read off the LIVE labels (``labels``) the caller passes, else the ones filed
# with the sighting. The station's ways to end a lead short of done (a release,
# a stale-base refusal) label the issue ``needs-human``, then reject the run; a
# human removes the label to retry. A rejected run never counts as a failure.
#
# CIRCUIT BREAKER: a lead that failed (``failed`` or ``develop failed``) is
# tried again until ``BREAKER`` failures. ``failed`` is the issue's failed lead
# run ids across ALL its sightings (the caller unions them and keeps them on
# the sighting), so re-filing the issue can't reset the count. ``trips`` names
# each issue at the limit with a one-line reason; the caller labels it
# ``needs-human`` and records the count as ``breaker_at``. A human taking the
# label off re-arms it for ``BREAKER`` more failures.
#
# The plan needs no human: each feature issue comes from an approved blueprint
# and packet, so the shift trigger approves it as ``APPROVER``.

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud.mandates.domain import LEAD_RUN_KIND
from pocketpaw_ee.cloud.mandates.foreman import PlannedTask, PlanProposal

APPROVER = "system:lead-planner"

# ``github:<owner>/<repo>#<n>``, the issues patrol's (and a hand-filed issue's) source.
_SOURCE = re.compile(r"^github:([\w.-]+/[\w.-]+)#(\d+)$")
# The station's issue reference (``cloud_station._ISSUE``).
_ISSUE_REF = re.compile(r"(?<![\w/&])#(\d+)\b")
_NEEDS_HUMAN = "needs-human"
# A lead task in one of these failed outside the station's release path. A
# ``rejected`` one (released, or rejected by hand) defers to the label.
# ponytail: if the release's label write fails, a rejected task with no label
# frees the issue next shift; reading the label's add time off the issue's events
# (after the run started) would close that.
FAILED = frozenset({"failed", "develop failed"})
BREAKER = 3
_PRIORITY = {"p1": 1, "p2": 2, "p3": 3}
_UNRANKED = len(_PRIORITY) + 1


def priority(labels: list[str]) -> int:
    """1 for a ``p1`` issue, 2 for ``p2``, 3 for ``p3`` (the highest wins), 4 unlabelled."""
    ranks = [_PRIORITY.get(str(lb).strip().lower(), _UNRANKED) for lb in labels or []]
    return min(ranks, default=_UNRANKED)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _labels(sighting: dict[str, Any]) -> list[str]:
    """The issue's live labels when the caller read them, else the filed ones."""
    live = sighting.get("labels")
    return list(live if live is not None else (sighting.get("evidence") or {}).get("labels") or [])


def _flagged(sighting: dict[str, Any]) -> bool:
    return _NEEDS_HUMAN in {str(lb).strip().lower() for lb in _labels(sighting)}


def _tripped(sighting: dict[str, Any]) -> bool:
    """``BREAKER`` failed leads since the breaker last tripped (a human re-armed it)."""
    failed = len(set(sighting.get("failed") or []))
    return failed - int(sighting.get("breaker_at") or 0) >= BREAKER


def _held(sighting: dict[str, Any]) -> bool:
    """Labelled ``needs-human``, or the breaker tripped."""
    return _flagged(sighting) or _tripped(sighting)


def trips(sightings: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Each issue sighting whose breaker trips now (not labelled yet), with the
    one-line reason the caller comments when it labels it ``needs-human``."""
    out = []
    for s in sightings:
        if _SOURCE.match(str((s.get("evidence") or {}).get("source") or "")) and (
            _tripped(s) and not _flagged(s)
        ):
            runs = sorted(set(s.get("failed") or []))
            out.append(
                (
                    s,
                    f"Belt: {len(runs)} feature lead attempts failed ({', '.join(runs)}); "
                    "held until a human removes needs-human.",
                )
            )
    return out


def _task(sighting: dict[str, Any], number: str) -> PlannedTask:
    ev = sighting.get("evidence") or {}
    title = str(ev.get("title") or "").strip() or f"Feature issue #{number}"
    first = _ISSUE_REF.search(title)
    if first and first[1] != number:
        title = f"#{number} {title}"
    url = str(ev.get("url") or "").strip()
    return PlannedTask(
        title=title,
        why=f"Feature issue #{number}" + (f": {url}" if url else ""),
        evidence_refs=[str(sighting["id"])],
        # The planner reads no packet, so the outcome names both done rules.
        expected_outcome=(
            f"#{number} shipped by its feature lead, slice by slice when its wave.json lists "
            "slices: every slice done on a green ci.yml run that holds its last PR "
            "(builders never wait on CI or critics, findings queue, at most 2 fix rounds per "
            "slice), then a contact sheet on the issue. A feature without slices: a merged "
            "feature-done PR, dev CI green and a contact sheet"
        ),
        recipe=LEAD_RUN_KIND,
    )


def plan(shift_no: int, sightings: list[dict[str, Any]], budget: int) -> PlanProposal:
    """One ``feature-lead`` task per free feature issue in ``sightings`` (each
    ``{id, evidence, ts}``, optionally the live ``labels``, the issue's
    ``failed`` lead run ids and ``breaker_at``), p1 first, then oldest, at most
    ``budget``."""
    issues = []
    for s in sightings:
        found = _SOURCE.match(str((s.get("evidence") or {}).get("source") or ""))
        if found and not _held(s):
            issues.append((s, found[2]))
    issues.sort(
        key=lambda pair: (
            priority(_labels(pair[0])),
            _aware(pair[0]["ts"]),
        )
    )
    tasks = [_task(s, number) for s, number in issues[: max(0, budget)]]
    if not tasks:
        return PlanProposal(
            shift_no=shift_no, no_action=True, no_action_reason="no free feature issue"
        )
    return PlanProposal(shift_no=shift_no, tasks=tasks)


__all__ = ["APPROVER", "BREAKER", "FAILED", "plan", "priority", "trips"]
