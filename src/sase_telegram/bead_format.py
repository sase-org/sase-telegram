"""Convert ``sase bead show`` plain-text output into standard Markdown.

The result is intended to be passed through
:func:`sase_telegram.formatting.markdown_to_telegram_v2` for final
MarkdownV2 escaping before being sent to Telegram.
"""

from __future__ import annotations

import re

# Header line: "<icon> <id> · <title>   [STATUS]"
_HEADER_RE = re.compile(
    r"^(?P<icon>\S+)\s+(?P<id>\S+)\s+·\s+(?P<title>.*?)\s+\[(?P<status>[^\]]+)\]\s*$"
)

# "<icon> <id>: <title>   [STATUS]" or "<icon> <id>: <title>"
_CHILD_LINE_RE = re.compile(
    r"^(?P<icon>\S+)\s+(?P<id>\S+):\s+(?P<title>.*?)"
    r"(?:\s+\[(?P<status>[^\]]+)\])?\s*$"
)

# "↑ <id> · <title>   [STATUS]" or "↑ <id>"
_PARENT_LINE_RE = re.compile(
    r"^↑\s+(?P<id>\S+)(?:\s+·\s+(?P<title>.*?)(?:\s+\[(?P<status>[^\]]+)\])?)?\s*$"
)

# "→ <icon> <id>: <title>   [STATUS]" or "→ <id> (not found)"
_DEP_LINE_RE = re.compile(
    r"^(?P<arrow>[→←])\s+(?:(?P<icon>\S+)\s+(?P<id>\S+):\s+(?P<title>.*?)"
    r"(?:\s+\[(?P<status>[^\]]+)\])?|(?P<missing_id>\S+)\s*\(not found\))\s*$"
)

_KNOWN_SECTIONS = {
    "PARENT": "Parent",
    "CHILDREN": "Children",
    "DEPENDS ON": "Depends On",
    "BLOCKS": "Blocks",
    "DESCRIPTION": "Description",
    "NOTES": "Notes",
    "PLAN": "Plan",
}


def _is_section_header(line: str) -> str | None:
    """Return the canonical section name if *line* is a section header."""
    stripped = line.strip()
    if not stripped or stripped != stripped.upper():
        return None
    if stripped in _KNOWN_SECTIONS:
        return stripped
    # Tolerate unknown ALL-CAPS section headers (e.g. future additions).
    if re.fullmatch(r"[A-Z][A-Z _]*[A-Z]", stripped):
        return stripped
    return None


def _format_child_line(line: str) -> str:
    """Format a CHILDREN body line as a markdown bullet."""
    m = _CHILD_LINE_RE.match(line.strip())
    if not m:
        return f"- {line.strip()}"
    icon = m.group("icon")
    bid = m.group("id")
    title = m.group("title")
    status = m.group("status")
    suffix = f" _({status})_" if status else ""
    return f"- {icon} `{bid}` — {title}{suffix}"


def _format_parent_line(line: str) -> str:
    m = _PARENT_LINE_RE.match(line.strip())
    if not m:
        return f"- {line.strip()}"
    bid = m.group("id")
    title = m.group("title")
    status = m.group("status")
    if title is None:
        return f"- ↑ `{bid}`"
    suffix = f" _({status})_" if status else ""
    return f"- ↑ `{bid}` — {title}{suffix}"


def _format_dep_line(line: str) -> str:
    m = _DEP_LINE_RE.match(line.strip())
    if not m:
        return f"- {line.strip()}"
    arrow = m.group("arrow")
    if m.group("missing_id"):
        return f"- {arrow} `{m.group('missing_id')}` _(not found)_"
    icon = m.group("icon")
    bid = m.group("id")
    title = m.group("title")
    status = m.group("status")
    suffix = f" _({status})_" if status else ""
    return f"- {arrow} {icon} `{bid}` — {title}{suffix}"


def _section_title(section: str) -> str:
    if section in _KNOWN_SECTIONS:
        return _KNOWN_SECTIONS[section]
    # Unknown: title-case ("FOO BAR" -> "Foo Bar")
    return " ".join(part.capitalize() for part in section.split())


def _flush_text_section(out: list[str], buf: list[str], section: str) -> None:
    """Render an accumulated free-text section (DESCRIPTION/NOTES/PLAN)."""
    if not buf:
        return
    # Strip the leading two-space indentation produced by the CLI.
    body_lines = [ln[2:] if ln.startswith("  ") else ln for ln in buf]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    if not body_lines:
        return
    if section == "NOTES":
        out.append("```")
        out.extend(body_lines)
        out.append("```")
    elif section == "PLAN":
        # PLAN body is a single path; render as inline code.
        out.append(f"`{body_lines[0].strip()}`")
        for extra in body_lines[1:]:
            out.append(extra)
    else:  # DESCRIPTION or unknown free-text
        out.append("\n".join(body_lines))


def bead_show_to_markdown(raw: str) -> str:
    """Render the output of ``sase bead show <id>`` as standard Markdown.

    The transformation is forgiving: unknown sections pass through with a
    title-cased header and body preserved verbatim.
    """
    lines = raw.splitlines()
    # Drop trailing empties so we don't generate a stray blank line.
    while lines and not lines[-1].strip():
        lines.pop()

    out: list[str] = []
    i = 0

    # --- Header (first non-blank line) ---
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines):
        m = _HEADER_RE.match(lines[i])
        if m:
            out.append(f"# {m.group('icon')} {m.group('id')} — {m.group('title')}")
            out.append("")
            out.append(f"**Status:** {m.group('status')}")
            i += 1
        else:
            # Unrecognised header — pass through.
            out.append(lines[i])
            i += 1

    # --- Top-level metadata: Type/Owner, Assignee ---
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            break  # blank line ends metadata block
        type_owner = re.match(
            r"^Type:\s+(?P<type>\S+)\s+·\s+Owner:\s+(?P<owner>.+)$", stripped
        )
        if type_owner:
            out.append(
                f"**Type:** {type_owner.group('type')}  •  "
                f"**Owner:** {type_owner.group('owner')}"
            )
            i += 1
            continue
        assignee = re.match(r"^Assignee:\s+(?P<a>.+)$", stripped)
        if assignee:
            out.append(f"**Assignee:** {assignee.group('a')}")
            i += 1
            continue
        # Unknown metadata line — bail out and let section parsing take over.
        break

    # --- Sections ---
    current_section: str | None = None
    text_buf: list[str] = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        section_name = _is_section_header(line) if stripped else None
        if section_name:
            if current_section in {"DESCRIPTION", "NOTES", "PLAN"} or (
                current_section is not None
                and current_section not in _KNOWN_SECTIONS
                and text_buf
            ):
                _flush_text_section(out, text_buf, current_section or "")
                text_buf = []
            current_section = section_name
            out.append("")
            out.append(f"## {_section_title(section_name)}")
            i += 1
            continue

        if current_section is None:
            # Skip stray blank lines / unknown content before any section.
            i += 1
            continue

        if not stripped:
            if current_section in {"DESCRIPTION", "NOTES", "PLAN"}:
                text_buf.append("")
            i += 1
            continue

        if current_section == "PARENT":
            out.append(_format_parent_line(line))
        elif current_section == "CHILDREN":
            out.append(_format_child_line(line))
        elif current_section in {"DEPENDS ON", "BLOCKS"}:
            out.append(_format_dep_line(line))
        elif current_section in {"DESCRIPTION", "NOTES", "PLAN"}:
            text_buf.append(line)
        else:
            # Unknown section — preserve body verbatim.
            text_buf.append(line)
        i += 1

    if current_section in {"DESCRIPTION", "NOTES", "PLAN"} or text_buf:
        _flush_text_section(out, text_buf, current_section or "")

    # Collapse any runs of >2 blank lines.
    rendered: list[str] = []
    blank_run = 0
    for line in out:
        if not line.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        rendered.append(line)
    return "\n".join(rendered).rstrip() + "\n"
