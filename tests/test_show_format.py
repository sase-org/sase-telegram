"""Tests for Telegram ``/show`` HTML and keyboard specifications."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sase_telegram.agent_format import pack_html_blocks
from sase_telegram.show_entities import (
    KinshipIndex,
    KinshipIndexItem,
    ShowNotFound,
    ShowSuggestion,
    ShowTarget,
)
from sase_telegram.show_format import (
    format_agent_show,
    format_clan_show,
    format_family_show,
    format_show_index,
    format_show_not_found,
    format_tribe_show,
)


def _entry(
    name: str,
    *,
    status: str = "RUNNING",
    bucket: str = "Running",
    terminal: bool = False,
    clan: str | None = None,
    family: str | None = None,
    role: str | None = None,
    tribe: str | None = None,
    activity: str | None = None,
    prompt: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        project="sase",
        pid=123,
        model="opus",
        provider_badge="🎭",
        workspace_num=2,
        duration="14m",
        started_at=None,
        finished_at=None,
        prompt=prompt,
        status=status,
        status_bucket=bucket,
        status_glyph="✓" if terminal else "▶",
        reasoning_effort=None,
        vcs_provider_display="GitHub",
        tag=None,
        bead_id=None,
        changespec_name=None,
        workflow_name=None,
        agent_clan=clan,
        agent_clan_generation="generation-12345678" if clan else None,
        clan_tribe=tribe if clan else None,
        tribe=tribe,
        agent_family=family,
        agent_family_role=role,
        parent_agent_name="parent" if name == "<alpha>&" else None,
        wait=SimpleNamespace(has_wait=False),
        retry=SimpleNamespace(has_retry=False, retry_attempt=None),
        children=SimpleNamespace(
            count=2,
            status_counts=(("Running", 1), ("Done", 1)),
            badge="×2",
        ),
        activity=activity,
        output_variables={},
        artifact_count=0,
        commit_count=0,
        error=None,
        has_file_changes=False,
        auto_badge=None,
        auto_approve_plan_action=None,
        is_terminal=terminal,
    )


def _member(
    name: str, outcome: str | None, *, timestamp: str = "001"
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        outcome=outcome,
        timestamp=timestamp,
        generation="generation-12345678",
        artifacts_dir=Path(f"/tmp/{name}"),
    )


def test_agent_view_escapes_html_and_includes_kinship_rows_and_jumps() -> None:
    entry = _entry(
        "<alpha>&",
        clan="review",
        family="migrate",
        role="planner",
        tribe="perf",
        prompt="Do <work>&",
    )
    target = ShowTarget(
        kind="agent",
        name="<alpha>&",
        entry=entry,
        entries=(entry,),
        also_tribe=True,
    )

    view = format_agent_show(target)
    text = "\n\n".join(view.blocks)

    assert "&lt;alpha&gt;&amp;" in text
    assert "Clan" in text and "⛺ review · gen 12345678" in text
    assert "Tribe" in text and "@perf" in text
    assert "Family" in text and "migrate · planner" in text
    assert "Parent" in text and "Children" in text
    assert "Do &lt;work&gt;&amp;" in text
    assert "/show @&lt;alpha&gt;&amp;" in text
    assert [button.label for button in view.button_rows[0]] == [
        "⛺ Clan",
        "🧬 Family",
    ]


def test_clan_view_formats_summary_rollup_archived_member_and_complete_fork() -> None:
    live = _entry("review.a", clan="review")
    members = (_member("review.a", "completed"), _member("review.b", "completed"))
    clan = SimpleNamespace(
        name="review",
        generation="generation-12345678",
        members=members,
        is_complete=True,
    )
    target = ShowTarget(
        kind="clan",
        name="review",
        clan=clan,
        entries=(live,),
        clan_tribe="perf",
        clan_summary="Audit <everything>",
    )

    view = format_clan_show(target)
    text = "\n\n".join(view.blocks)

    assert "⛺ <b>review</b> — clan · @perf · gen 12345678 · ✓ complete" in text
    assert "<i>Audit &lt;everything&gt;</i>" in text
    assert "▶ 1 running" in text
    assert "✓ <b>review.b</b> · done" in text
    assert any(
        button.label == "🍴 Fork clan" and button.ref == "#fork:review "
        for row in view.button_rows
        for button in row
    )


def test_incomplete_large_clan_omits_fork_and_chunks_with_explicit_truncation() -> None:
    members = tuple(
        _member(f"review.{index}", None, timestamp=str(index)) for index in range(20)
    )
    entries = tuple(
        _entry(
            f"review.{index}",
            clan="review",
            activity="x" * 300,
            prompt="y" * 300,
        )
        for index in range(20)
    )
    clan = SimpleNamespace(
        name="review", generation="g", members=members, is_complete=False
    )
    view = format_clan_show(
        ShowTarget(kind="clan", name="review", clan=clan, entries=entries)
    )

    assert not any(
        button.action == "copy" for row in view.button_rows for button in row
    )
    assert "…and 8 more members" in view.blocks[-1]
    assert len(pack_html_blocks(list(view.blocks))) > 1


def test_family_view_marks_active_member_and_shows_activity_prompt_and_outcome() -> (
    None
):
    members = (
        _member("migrate--planner", "completed", timestamp="001"),
        _member("migrate--coder", None, timestamp="002"),
        _member("migrate--reviewer", "failed", timestamp="003"),
    )
    active = _entry(
        "migrate--coder",
        family="migrate",
        role="coder",
        activity="writing tests",
        prompt="Implement the migration",
    )
    family = SimpleNamespace(base_name="migrate", members=members)
    view = format_family_show(
        ShowTarget(
            kind="family",
            name="migrate",
            family=family,
            entries=(active,),
        )
    )
    text = "\n\n".join(view.blocks)

    assert "1. ✓ <b>migrate--planner</b> · done" in text
    assert "2. ▶ <b>migrate--coder</b> · coder · running · 14m" in text
    assert "3. ✗ <b>migrate--reviewer</b> · failed" in text
    assert "<i>writing tests</i>" in text
    assert "<blockquote>Implement the migration</blockquote>" in text


def test_tribe_view_groups_clans_families_and_standalone_agents() -> None:
    entries = (
        _entry("review.a", clan="review", tribe="perf", terminal=True),
        _entry("review.b", clan="review", tribe="perf"),
        _entry("migrate--one", family="migrate", tribe="perf"),
        _entry("solo", tribe="perf"),
    )
    view = format_tribe_show(ShowTarget(kind="tribe", name="perf", entries=entries))
    text = "\n\n".join(view.blocks)

    assert "🏷️ <b>@perf</b> — tribe · 3 entities · 4 agents" in text
    assert "⛺ <b>review</b> · 1/2 done" in text
    assert "🧬 <b>migrate</b> · phase 1/1" in text
    assert "🎭 <b>solo</b> · opus · ▶ 14m" in text
    assert view.button_rows[0][0].action == "refresh"


def test_index_and_not_found_views_produce_mobile_open_specs() -> None:
    index = KinshipIndex(
        clans=(KinshipIndexItem("clan", "review", "review", 2, 1),),
        families=(KinshipIndexItem("family", "migrate", "migrate", 3, 3),),
        tribes=(KinshipIndexItem("tribe", "perf", "@perf", 5, 2),),
    )
    index_view = format_show_index(index)
    text = "\n\n".join(index_view.blocks)
    assert "🧭 <b>Agents by kinship</b>" in text
    assert "🧬 <b>migrate</b> · 3 members · ✓ complete" in text
    assert [button.ref for row in index_view.button_rows for button in row] == [
        "review",
        "migrate",
        "@perf",
    ]

    missing = format_show_not_found(
        ShowNotFound(
            query="<rev>&",
            suggestions=(ShowSuggestion("clan", "review", "review"),),
        )
    )
    assert "<code>&lt;rev&gt;&amp;</code>" in missing.blocks[0]
    assert missing.button_rows[0][0].ref == "review"


def test_index_caps_buttons_and_states_the_truncation() -> None:
    clans = tuple(
        KinshipIndexItem("clan", f"clan-{index}", f"clan-{index}", 1, 0)
        for index in range(27)
    )

    view = format_show_index(KinshipIndex(clans=clans))

    assert sum(len(row) for row in view.button_rows) == 24
    assert any("…and 3 more groups" in block for block in view.blocks)
    assert any("buttons capped" in block for block in view.blocks)
