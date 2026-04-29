"""Tests for sase_telegram.bead_format."""

from __future__ import annotations

from textwrap import dedent

from sase_telegram.bead_format import bead_show_to_markdown


def test_plan_bead_with_children_and_plan() -> None:
    raw = dedent(
        """\
        ○ sase-13 · DELTAS ChangeSpec Field   [OPEN]
        Type: plan · Owner: bryanbugyi34@gmail.com

        CHILDREN
          ✓ sase-13.1: Phase 1: Data Model, Parsing, Serialization
          ◐ sase-13.3: Phase 3: VCS Computation

        PLAN
          ../sase/plans/202604/deltas_field.md
        """
    )
    md = bead_show_to_markdown(raw)
    assert md.startswith("# ○ sase-13 — DELTAS ChangeSpec Field")
    assert "**Status:** OPEN" in md
    assert "**Type:** plan  •  **Owner:** bryanbugyi34@gmail.com" in md
    assert "## Children" in md
    assert "- ✓ `sase-13.1` — Phase 1: Data Model, Parsing, Serialization" in md
    assert "- ◐ `sase-13.3` — Phase 3: VCS Computation" in md
    assert "## Plan" in md
    assert "`../sase/plans/202604/deltas_field.md`" in md


def test_phase_bead_with_parent_blocks_description_notes() -> None:
    raw = dedent(
        """\
        ✓ sase-13.1 · Phase 1: Data Model, Parsing, Serialization   [CLOSED]
        Type: phase · Owner: bryanbugyi34@gmail.com
        Assignee: sase-13.1

        PARENT
          ↑ sase-13 · DELTAS ChangeSpec Field   [OPEN]

        BLOCKS
          ← ✓ sase-13.2: Phase 2: Atomic Update Helper   [CLOSED]

        DESCRIPTION
          Round-trip a ChangeSpec with a DELTAS section through the parser.

        NOTES
          COMMIT: 616a50ea
        """
    )
    md = bead_show_to_markdown(raw)
    assert "# ✓ sase-13.1 — Phase 1: Data Model, Parsing, Serialization" in md
    assert "**Status:** CLOSED" in md
    assert "**Assignee:** sase-13.1" in md
    assert "## Parent" in md
    assert "- ↑ `sase-13` — DELTAS ChangeSpec Field _(OPEN)_" in md
    assert "## Blocks" in md
    assert "- ← ✓ `sase-13.2` — Phase 2: Atomic Update Helper _(CLOSED)_" in md
    assert "## Description" in md
    assert "Round-trip a ChangeSpec with a DELTAS section through the parser." in md
    assert "## Notes" in md
    # Notes section is fenced as a code block.
    assert "```\nCOMMIT: 616a50ea\n```" in md


def test_depends_on_section() -> None:
    raw = dedent(
        """\
        ○ sase-7 · Some Title   [OPEN]
        Type: phase · Owner: someone@example.com

        DEPENDS ON
          → ✓ sase-6: Predecessor   [CLOSED]
          → bogus-id (not found)
        """
    )
    md = bead_show_to_markdown(raw)
    assert "## Depends On" in md
    assert "- → ✓ `sase-6` — Predecessor _(CLOSED)_" in md
    assert "- → `bogus-id` _(not found)_" in md


def test_minimal_bead() -> None:
    raw = dedent(
        """\
        ○ sase-99 · Tiny   [OPEN]
        Type: phase · Owner: (none)
        """
    )
    md = bead_show_to_markdown(raw)
    assert "# ○ sase-99 — Tiny" in md
    assert "**Status:** OPEN" in md
    assert "**Type:** phase  •  **Owner:** (none)" in md
    # No section headers should be present.
    assert "##" not in md


def test_unknown_section_passes_through() -> None:
    raw = dedent(
        """\
        ○ sase-1 · Something   [OPEN]
        Type: phase · Owner: x@y

        FUTURE THING
          some body line
        """
    )
    md = bead_show_to_markdown(raw)
    assert "## Future Thing" in md
    assert "some body line" in md


def test_unicode_status_icons_preserved() -> None:
    raw = dedent(
        """\
        ⊘ sase-2 · Cancelled   [CLOSED]
        Type: phase · Owner: x@y
        """
    )
    md = bead_show_to_markdown(raw)
    assert md.startswith("# ⊘ sase-2 — Cancelled")


def test_parent_without_title() -> None:
    raw = dedent(
        """\
        ○ sase-3 · Child   [OPEN]
        Type: phase · Owner: x@y

        PARENT
          ↑ unknown-parent-id
        """
    )
    md = bead_show_to_markdown(raw)
    assert "- ↑ `unknown-parent-id`" in md


def test_description_multiline_reflow() -> None:
    raw = dedent(
        """\
        ○ sase-4 · Multi   [OPEN]
        Type: phase · Owner: x@y

        DESCRIPTION
          first line
          second line
        """
    )
    md = bead_show_to_markdown(raw)
    assert "first line\nsecond line" in md
