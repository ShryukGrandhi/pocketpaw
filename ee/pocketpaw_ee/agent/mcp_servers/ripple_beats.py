# ee/agent/mcp_servers/ripple_beats.py — Ripple explainer diagrams inside a
# HyperFrames composition: the brief the agent writes them from, and the check
# that runs before a composition is dispatched.
#
# The agent only writes host elements (`data-ripple="iso-scene"` or
# `data-ripple="anatomy"` with a JSON parts spec, or `data-ripple="figure"`); it
# never writes Ripple itself. An anatomy host is a product reveal drawn with the
# Anatomy kit (as used by github.com/annnguyentt/anatomy-reveal-skill): convex
# parts on a paper card that tumble in, explode, pin their labels and snap back. The
# browser draws each host frame by frame from Ripple's shipped code when it
# renders (paw-enterprise core/studio/editor/hyperframes/ripple.ts), so stored
# compositions stay small enough to round-trip through the MOTION GRAPHICS
# prompt block. FIGURE_ANCHORS mirrors ripple's manifest entry
# for `figure` (lib/manifest/entries/figure.ts); a figure added there needs its
# row here. ANATOMY_KINDS mirrors the part kinds anatomy.ts can build.

from __future__ import annotations

import json
import re

FIGURE_ANCHORS: dict[str, tuple[str, ...]] = {
    "heart": (
        "right-atrium",
        "right-ventricle",
        "left-atrium",
        "left-ventricle",
        "tricuspid",
        "mitral",
        "pulmonary-valve",
        "aortic-valve",
        "aorta",
        "vena-cava",
        "pulmonary-artery",
        "pulmonary-veins",
        "lungs",
        "body",
    ),
    "bike-gears": (
        "chainring",
        "crank",
        "pedal",
        "chain",
        "cassette",
        "rear-derailleur",
        "front-derailleur",
        "jockey-wheels",
        "rear-hub",
        "wheel",
    ),
    "rocket-engine": (
        "oxidiser-tank",
        "fuel-tank",
        "turbopump",
        "turbine",
        "oxidiser-pump",
        "fuel-pump",
        "preburner",
        "injector",
        "combustion-chamber",
        "throat",
        "nozzle",
        "cooling-channels",
        "gimbal",
        "load-cell",
        "plume",
        "pressure-gauge",
    ),
    "ion-thruster": (
        "xenon-tank",
        "flow-controller",
        "power-unit",
        "discharge-chamber",
        "magnets",
        "cathode",
        "plasma",
        "screen-grid",
        "accel-grid",
        "neutraliser",
        "ion-beam",
        "thrust-dial",
    ),
    "tokamak": (
        "plasma",
        "central-solenoid",
        "tf-coil",
        "pf-coil",
        "vacuum-vessel",
        "blanket",
        "divertor",
        "heating-beam",
        "port",
    ),
    "fission-reactor": (
        "reactor-vessel",
        "fuel",
        "control-rods",
        "pressuriser",
        "steam-generator",
        "coolant-pump",
        "hot-leg",
        "cold-leg",
        "containment",
        "steam-line",
        "turbine",
        "generator",
        "condenser",
        "feedwater-pump",
        "cooling-tower",
        "transformer",
    ),
    "particle-collider": (
        "collision-point",
        "detector",
        "tracker",
        "ecal",
        "hcal",
        "solenoid",
        "muon-chambers",
        "beam-pipes",
        "dipole",
        "quadrupole",
        "final-focus",
        "separation-magnet",
        "rf-cavities",
        "magnet-coils",
        "field-gauge",
    ),
}

ANATOMY_KINDS = ("box", "cylinder", "ball")

RIPPLE_BEATS_BRIEF = (
    "Explainer diagram (include one whenever the post involves a physical thing, product, "
    "building part, machine or process, which most do): a live, moving Ripple drawing can "
    "be one beat of the composition. Write only a host element below; the render draws it "
    "frame by frame, so never write Ripple markup or scripts yourself. An iso-scene builds "
    "itself: its parts drop into place one by one in the order you list them (about four "
    "seconds in all), then its pins reveal. Each pin is drawn with its own label next to "
    "it, revealed one at a time, so the diagram explains itself: "
    "keep labels to one to four words and never repeat them as separate text. Size and place "
    "the host with CSS (width about 1000px, centred; height follows) and fade it in and "
    "out with the timeline like any element. Give it at least eight seconds.\n"
    "- Anything, drawn from parts (the usual choice): "
    '<div data-ripple="iso-scene" data-ripple-at="<second it appears>">'
    '<script type="application/json">{"parts":[...],"motions":[...],"labels":[...]}'
    "</script></div>. parts: 8 to 30, listed in build order (base and ground first, then "
    "what sits on it, the moving and key parts last), each {id, kind "
    "(slab|box|cylinder|disc|gear|tube|sphere|cone|rod|arrow|plate|chain), at [x,y,z] "
    "(z is up, keep within about 300 across), w/d/h for slab, box and plate, r/h/axis for "
    "cylinder, disc, gear and cone, from/to for rod and arrow, path [[x,y,z],...] for tube, "
    "around [gearId, gearId] for chain, color neutral|accent|red|blue|green|amber|metal, "
    "ghost: true for a see-through housing, on: parent id}. motions: {part, kind "
    "spin|slide|swing|pulse|flow, axis, speed (turns or cycles per second)}; give at least "
    "one so it moves. labels: 3 to 6 of {part, label}, in the order the story is told.\n"
    "- A product reveal, when the post is about one physical product (a device, gadget, "
    "tool, appliance, piece of hardware): "
    '<div data-ripple="anatomy" data-ripple-at="<second>" data-ripple-for="<seconds on '
    'screen, 8 to 14>"><script type="application/json">{"parts":[...],"assemblies":[...],'
    '"labels":[...]}</script></div>. It is drawn as a crafted exploded-view figure on its own '
    "paper card: it tumbles in, comes apart into its assemblies, reveals its pins while "
    "apart, then snaps back together for a hero shot at the end. parts: 8 to 40 solids "
    "in real millimetres (x right, y back, z up, standing on z=0), each {id, kind "
    "box|cylinder|ball, at [x,y,z] (bottom centre of a box, start of a cylinder's axis, "
    "centre of a ball), w/d/h and round (corner radius) for box, r/h/axis x|y|z for "
    "cylinder (r2 for the far end makes a cone), r for ball, material (optional) "
    "steel|chrome|gunmetal|rubber|brass|copper|gold|glass|pcb|porcelain, group: the "
    "assembly it comes apart with}. Build the real thing from its real parts: a shell of "
    "panels, then what sits inside (board, battery, motor, lens). assemblies: 2 to 6 of "
    "{id, dir up|down|left|right|front|back or [x,y,z], travel in mm, delay 0 to 1 (outer "
    "parts lead), parent (optional assembly id)}; parts with no group stay put. labels: 3 "
    "to 6 of {part, label}.\n"
    "- A curated figure, when the post is about one of these exactly: "
    '<div data-ripple="figure" data-figure="<id>" data-params=\'{"bpm":72}\' '
    'data-labels=\'[{"anchor":"<anchor>","label":"<1-4 words>"}]\' data-ripple-at="<second>">'
    "</div>. Ids and "
    "their anchors: "
    + "; ".join(f"{fid}: {', '.join(anchors)}" for fid, anchors in FIGURE_ANCHORS.items())
    + ".\n"
    '- Optional on any: data-theme="light" or "dark" (figure and iso-scene default dark, '
    "anatomy defaults to light paper), data-reveal='[0.5,2,3.5]' "
    "(seconds after data-ripple-at when pin 1, 2, 3 show; default 0.5, 1.3, 2.1, ...)."
)

_HOST_RE = re.compile(r"<[a-zA-Z][^>]*\bdata-ripple\s*=\s*([\"'])([^\"']*)\1[^>]*>", re.IGNORECASE)
_FIGURE_RE = re.compile(r"\bdata-figure\s*=\s*([\"'])([^\"']*)\1", re.IGNORECASE)
_SPEC_RE = re.compile(
    r"<script\b[^>]*application/json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)


def validate_ripple_beats(html: str) -> str | None:
    """Check each Ripple host names a known figure or carries a parseable spec."""
    for host in _HOST_RE.finditer(html):
        kind = host.group(2).strip()
        if kind == "figure":
            figure = _FIGURE_RE.search(host.group(0))
            fid = figure.group(2).strip() if figure else ""
            if fid not in FIGURE_ANCHORS:
                return (
                    f"data-figure {fid!r} is not a Ripple figure. Use one of "
                    f"{', '.join(FIGURE_ANCHORS)}, or draw it as an iso-scene."
                )
        elif kind == "iso-scene":
            spec = _SPEC_RE.search(html, host.end())
            try:
                parsed = json.loads(spec.group(1)) if spec else None
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict) or not isinstance(parsed.get("parts"), list):
                return (
                    'A data-ripple="iso-scene" host needs a <script type="application/json"> '
                    'child holding {"parts": [...]} as valid JSON.'
                )
        elif kind == "anatomy":
            spec = _SPEC_RE.search(html, host.end())
            try:
                parsed = json.loads(spec.group(1)) if spec else None
            except ValueError:
                parsed = None
            parts = parsed.get("parts") if isinstance(parsed, dict) else None
            if not isinstance(parts, list) or not any(
                isinstance(p, dict) and p.get("kind") in ANATOMY_KINDS for p in parts
            ):
                return (
                    'A data-ripple="anatomy" host needs a <script type="application/json"> '
                    'child holding {"parts": [...]} as valid JSON, with parts of kind '
                    f"{', '.join(ANATOMY_KINDS)}."
                )
        else:
            return f'data-ripple={kind!r} is unknown. Use "figure", "iso-scene" or "anatomy".'
    return None


__all__ = ["ANATOMY_KINDS", "FIGURE_ANCHORS", "RIPPLE_BEATS_BRIEF", "validate_ripple_beats"]
