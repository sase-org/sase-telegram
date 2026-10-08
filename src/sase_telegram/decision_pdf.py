"""Decision-aware PDF preprocessing for Telegram plan PDFs (sase-1hi.10.6).

Prepends a rendered Decisions table and renders callouts as labelled
blockquotes. Uses frozen review data for pending gates and stamped answers
for accepted documents. Strips decision bookkeeping from Properties.
Preserves original source bytes, relative-resource resolution, engine
fallbacks, and temporary-file cleanup.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

_CALLOUT_RE = re.compile(r"^>\s*\[!decision\]\s*(?P<rest>.*)$")
_CONTINUATION_RE = re.compile(r"^>\s?(?P<body>.*)$")


def _facade() -> Any | None:
    try:
        import importlib

        return importlib.import_module("sase.sdd.plan_decisions")
    except Exception:
        return None


def preprocess_plan_for_pdf(
    source: Path, gate_context: dict[str, Any] | None = None
) -> Path | None:
    """Return a temp preprocessed sibling, or None when no decisions.

    Uses the frozen bundle's ``payload.decisions`` when *source* is a
    pending review (pass/derive its gate context), and the facade's
    ``load_stamped_decisions`` sheet when accepted. Frontmatter parsing
    serves only non-decision Properties/bookkeeping removal.
    """
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    frontmatter, body, had = _split_frontmatter(text)
    if not had:
        return None
    # Frozen pending-review facts first; stamped accepted facts as recovery.
    definitions = _frozen_definitions_for_source(source, gate_context)
    stamped_values: dict[str, Any] | None = None
    if definitions is None:
        stamped = _stamped_for_source(source)
        if stamped is None:
            return None
        definitions, stamped_values = stamped
    if not definitions:
        return None
    answers = dict(stamped_values or {})
    # Fill answers from stamped plan when available; pending reviews show defaults.
    table = _decisions_table(definitions, answers)
    body = _label_callouts(body)
    # Strip bookkeeping so the shared Properties card omits it.
    for key in ("decisions", "decided_by", "decided_via"):
        frontmatter.pop(key, None)
    rebuilt = _rebuild(frontmatter, table, body)
    fd, tmp = tempfile.mkstemp(
        dir=source.parent, prefix=f".{source.stem}.decisions.", suffix=".md"
    )
    try:
        with open(fd, "w", encoding="utf-8") as stream:
            stream.write(rebuilt)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        return None
    return Path(tmp)


def _frozen_definitions_for_source(
    source: Path, gate_context: dict[str, Any] | None
) -> list[dict[str, Any]] | None:
    """Return frozen ``payload.decisions`` for a pending review, if any."""
    # Explicit gate context wins (bundle path or request payload).
    if isinstance(gate_context, dict):
        for key in ("decisions", "payload_decisions"):
            raw = gate_context.get(key)
            if isinstance(raw, list) and raw:
                return [dict(d) for d in raw if isinstance(d, dict)]
        bundle_raw = gate_context.get("bundle_path")
        if isinstance(bundle_raw, str) and bundle_raw:
            defs = _definitions_from_bundle(Path(bundle_raw))
            if defs is not None:
                return defs
    # Derive from a sibling bundle when source is bundle/plan.md.
    try:
        parent = source.parent
        request_file = parent / "request.json"
        if request_file.is_file():
            defs = _definitions_from_bundle(parent)
            if defs is not None:
                return defs
    except Exception:
        pass
    return None


def _definitions_from_bundle(bundle_path: Path) -> list[dict[str, Any]] | None:
    try:
        import json as _json

        request = _json.loads(
            (bundle_path / "request.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(request, dict):
        return None
    payload = request.get("payload")
    if not isinstance(payload, dict):
        return None
    raw = payload.get("decisions")
    if not isinstance(raw, list) or not raw:
        return None
    return [dict(d) for d in raw if isinstance(d, dict)]


def _stamped_for_source(
    source: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    mod = _facade()
    if mod is None or not hasattr(mod, "load_stamped_decisions"):
        return None
    try:
        stamped = mod.load_stamped_decisions(str(source))
    except Exception:
        return None
    if stamped is None:
        return None
    try:
        definitions = getattr(stamped, "definitions", None)
        values = dict(getattr(stamped, "values", {}) or {})
        if definitions is None:
            # Older handoff returns values only; definitions come from the
            # frozen sheet rows when available.
            sheet = getattr(stamped, "sheet", None)
            if isinstance(sheet, dict) and isinstance(sheet.get("rows"), list):
                definitions = sheet["rows"]
            else:
                return None
        defs = [dict(d) for d in definitions if isinstance(d, dict)]
    except Exception:
        return None
    return defs, values


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, False
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, text, False
    try:
        import yaml  # type: ignore[import-untyped]

        frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    except Exception:
        return {}, text, False
    if not isinstance(frontmatter, dict):
        return {}, text, False
    return dict(frontmatter), "\n".join(lines[end + 1 :]), True


def _display_value(value: Any, default: Any) -> str:
    if isinstance(default, bool) or isinstance(value, bool):
        word = "yes" if bool(value) else "no"
        star = "" if value == default else " ●"
        return f"{word}{star}"
    star = "" if value == default else " ●"
    return f"{value}{star}"


def _decisions_table(definitions: list[dict[str, Any]], answers: dict[str, Any]) -> str:
    lines = [
        "## Decisions",
        "",
        "| # | ID | Value | Default |",
        "| --- | --- | --- | --- |",
    ]
    for number, definition in enumerate(definitions, start=1):
        decision_id = str(definition.get("id", ""))
        default = definition.get("default")
        answer = answers.get(decision_id)
        value = answer if answer is not None else default
        if isinstance(default, bool):
            default_word = "yes ★" if default else "no ★"
        else:
            default_word = f"{default} ★"
        lines.append(
            f"| {number} | `{decision_id}` | {_display_value(value, default)} | {default_word} |"
        )
    return "\n".join(lines)


def _label_callouts(body: str) -> str:
    """Label every line of each validated decision callout block.

    The opening ``> [!decision]`` line and every continuation ``>`` line
    receive a Decision label, covering yes/no/choice branches and wrapped
    prose. Fenced code is left untouched.
    """
    out: list[str] = []
    in_callout = False
    in_fence = False
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            in_callout = False
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        match = _CALLOUT_RE.match(stripped)
        if match is not None:
            rest = match.group("rest").strip()
            out.append(f"> Decision {rest}" if rest else "> Decision")
            in_callout = True
            continue
        if in_callout:
            cont = _CONTINUATION_RE.match(stripped)
            if cont is not None:
                inner = cont.group("body").strip()
                if not inner:
                    out.append(line)
                    continue
                # Already labelled continuations keep their label.
                if inner.lower().startswith("decision"):
                    out.append(line)
                else:
                    out.append(f"> Decision {inner}")
                continue
            # Blank lines inside a blockquote keep the block open.
            if stripped == "" or stripped == ">":
                out.append(line)
                continue
            in_callout = False
            out.append(line)
            continue
        out.append(line)
    return "\n".join(out)


def _rebuild(frontmatter: dict[str, Any], table: str, body: str) -> str:
    try:
        import yaml  # type: ignore[import-untyped]

        dumped = yaml.safe_dump(frontmatter, sort_keys=False).rstrip("\n")
    except Exception:
        dumped = ""
    return f"---\n{dumped}\n---\n\n{table}\n\n{body.lstrip(chr(10))}"


__all__ = ["preprocess_plan_for_pdf"]
