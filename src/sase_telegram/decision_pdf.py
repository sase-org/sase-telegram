"""Decision-aware PDF preprocessing for Telegram plan PDFs (sase-1hi.7).

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


def preprocess_plan_for_pdf(source: Path) -> Path | None:
    """Return a temp preprocessed sibling, or None when no decisions."""
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    frontmatter, body, had = _split_frontmatter(text)
    if not had:
        return None
    decisions = frontmatter.get("decisions")
    if not isinstance(decisions, dict) or not decisions:
        return None
    answers = {key: _answer_of(value) for key, value in decisions.items()}
    table = _decisions_table(decisions, answers)
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


def _answer_of(value: Any) -> Any | None:
    if isinstance(value, dict) and "answer" in value:
        return value.get("answer")
    return None


def _display_value(value: Any, default: Any) -> str:
    if isinstance(default, bool) or isinstance(value, bool):
        word = "yes" if bool(value) else "no"
        star = "" if value == default else " ●"
        return f"{word}{star}"
    star = "" if value == default else " ●"
    return f"{value}{star}"


def _decisions_table(decisions: dict[str, Any], answers: dict[str, Any]) -> str:
    lines = [
        "## Decisions",
        "",
        "| # | ID | Value | Default |",
        "| --- | --- | --- | --- |",
    ]
    for number, (decision_id, spec) in enumerate(decisions.items(), start=1):
        default = spec.get("default") if isinstance(spec, dict) else None
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
    out: list[str] = []
    for line in body.split("\n"):
        match = _CALLOUT_RE.match(line.strip())
        if match is None:
            out.append(line)
            continue
        rest = match.group("rest").strip()
        out.append(f"> Decision {rest}" if rest else "> Decision")
    return "\n".join(out)


def _rebuild(frontmatter: dict[str, Any], table: str, body: str) -> str:
    try:
        import yaml  # type: ignore[import-untyped]

        dumped = yaml.safe_dump(frontmatter, sort_keys=False).rstrip("\n")
    except Exception:
        dumped = ""
    return f"---\n{dumped}\n---\n\n{table}\n\n{body.lstrip(chr(10))}"


__all__ = ["preprocess_plan_for_pdf"]
