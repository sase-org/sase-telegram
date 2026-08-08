"""Tests for sase_telegram.bead_format."""

from __future__ import annotations

import json
from textwrap import dedent

from sase_telegram.bead_format import (
    BeadListEntry,
    bead_show_to_markdown,
    parse_bead_list_json,
)


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


def _envelope(results: list[dict[str, object]]) -> str:
    return json.dumps(
        {"count": len(results), "total": len(results), "results": results}
    )


class TestParseBeadListJson:
    """Tests for parse_bead_list_json."""

    def test_typical_envelope(self) -> None:
        raw = _envelope(
            [
                {
                    "id": "sase-13",
                    "title": "DELTAS ChangeSpec Field",
                    "status": "open",
                    "parent_id": None,
                },
                {
                    "id": "sase-13.5",
                    "title": "Phase 5: Lifecycle Wiring",
                    "status": "in_progress",
                    "parent_id": "sase-13",
                },
                {
                    "id": "sase-13.1",
                    "title": "Phase 1: Data Model",
                    "status": "closed",
                    "parent_id": "sase-13",
                },
            ]
        )
        entries = parse_bead_list_json(raw)
        assert entries == [
            BeadListEntry(
                icon="○",
                bead_id="sase-13",
                title="DELTAS ChangeSpec Field",
                parent_id=None,
            ),
            BeadListEntry(
                icon="◐",
                bead_id="sase-13.5",
                title="Phase 5: Lifecycle Wiring",
                parent_id="sase-13",
            ),
            BeadListEntry(
                icon="✓",
                bead_id="sase-13.1",
                title="Phase 1: Data Model",
                parent_id="sase-13",
            ),
        ]

    def test_empty_results(self) -> None:
        assert parse_bead_list_json(_envelope([])) == []

    def test_malformed_non_dict_payload(self) -> None:
        assert parse_bead_list_json("[]") == []
        assert parse_bead_list_json("not json") == []
        assert parse_bead_list_json("") == []
        assert parse_bead_list_json(json.dumps({"no_results_key": True})) == []
        assert parse_bead_list_json(json.dumps({"results": "not-a-list"})) == []

    def test_record_missing_optional_keys(self) -> None:
        raw = _envelope([{"id": "sase-9", "status": "snoozed"}])
        entries = parse_bead_list_json(raw)
        assert len(entries) == 1
        assert entries[0].icon == "◈"
        assert entries[0].bead_id == "sase-9"
        assert entries[0].title == ""
        assert entries[0].parent_id is None

    def test_unknown_status_falls_back_to_default_icon(self) -> None:
        raw = _envelope([{"id": "sase-1", "title": "Odd", "status": "bogus-status"}])
        entries = parse_bead_list_json(raw)
        assert entries[0].icon == "•"

    def test_record_missing_id_skipped(self) -> None:
        raw = _envelope([{"title": "No id"}, {"id": "sase-2", "title": "Good"}])
        entries = parse_bead_list_json(raw)
        assert [e.bead_id for e in entries] == ["sase-2"]
