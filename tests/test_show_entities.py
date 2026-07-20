"""Tests for pure ``/show`` reference resolution and index modeling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sase_telegram.show_entities import (
    ClanAttributes,
    InvalidShowReference,
    ShowNotFound,
    build_kinship_index,
    resolve_clan_attributes,
    resolve_show_reference,
)


def _entry(
    name: str,
    *,
    tribe: str | None = None,
    clan: str | None = None,
    family: str | None = None,
    terminal: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        tribe=tribe,
        agent_clan=clan,
        agent_clan_generation="gen-1" if clan else None,
        agent_family=family,
        is_terminal=terminal,
        status_bucket="Done" if terminal else "Running",
    )


def _lookups() -> tuple[MagicMock, MagicMock, MagicMock]:
    return (
        MagicMock(return_value=None),
        MagicMock(return_value=None),
        MagicMock(return_value=None),
    )


def test_forced_tribe_is_casefolded_and_bypasses_other_lookups() -> None:
    find_agent, find_clan, find_family = _lookups()
    entries = [_entry("alpha", tribe="Perf")]

    target = resolve_show_reference(
        "@perf",
        entries,
        find_agent=find_agent,
        find_clan=find_clan,
        find_family=find_family,
    )

    assert target.kind == "tribe"
    assert target.name == "Perf"
    assert target.entries == tuple(entries)
    find_agent.assert_not_called()
    find_clan.assert_not_called()
    find_family.assert_not_called()


def test_invalid_forced_tribe_raises_friendly_domain_error() -> None:
    with pytest.raises(InvalidShowReference):
        resolve_show_reference("@bad tribe!", [])


def test_exact_agent_wins_and_detects_also_a_tribe() -> None:
    named = SimpleNamespace(name="review")
    entry = _entry("review", tribe="review")

    target = resolve_show_reference(
        "review",
        [entry],
        find_agent=lambda _name: named,
        find_clan=lambda _name: SimpleNamespace(name="review"),
        find_family=lambda _name: SimpleNamespace(base_name="review"),
    )

    assert target.kind == "agent"
    assert target.entry is entry
    assert target.named_agent is named
    assert target.also_tribe is True


def test_exact_entry_fallback_resolves_clan_and_family_members_as_agents() -> None:
    for name in ("review.worker", "migrate--planner"):
        entry = _entry(name)
        target = resolve_show_reference(
            name,
            [entry],
            find_agent=lambda _name: None,
            find_clan=lambda _name: None,
            find_family=lambda _name: None,
        )
        assert target.kind == "agent"
        assert target.entry is entry


def test_clan_then_family_precedence() -> None:
    clan = SimpleNamespace(name="review", generation="g", members=())
    family_lookup = MagicMock(return_value=SimpleNamespace(base_name="review"))
    target = resolve_show_reference(
        "review",
        [],
        find_agent=lambda _name: None,
        find_clan=lambda _name: clan,
        find_family=family_lookup,
        clan_attribute_resolver=lambda _clan: ClanAttributes(
            tribe="perf", summary="Audit the hot path"
        ),
    )

    assert target.kind == "clan"
    assert target.clan is clan
    assert target.clan_tribe == "perf"
    assert target.clan_summary == "Audit the hot path"
    family_lookup.assert_not_called()

    family = SimpleNamespace(base_name="migrate", members=())
    target = resolve_show_reference(
        "migrate",
        [],
        find_agent=lambda _name: None,
        find_clan=lambda _name: None,
        find_family=lambda _name: family,
    )
    assert target.kind == "family"
    assert target.family is family


def test_bare_known_tribe_resolves_after_group_lookups() -> None:
    entry = _entry("alpha", tribe="Perf")
    target = resolve_show_reference(
        "perf",
        [entry],
        find_agent=lambda _name: None,
        find_clan=lambda _name: None,
        find_family=lambda _name: None,
    )
    assert target.kind == "tribe"
    assert target.name == "Perf"


def test_not_found_suggestions_cover_each_kind_and_are_limited() -> None:
    entries = [
        _entry("review.worker", clan="review", tribe="reviewers"),
        _entry("review--planner", family="review-flow"),
    ]
    result = resolve_show_reference(
        "rev",
        entries,
        find_agent=lambda _name: None,
        find_clan=lambda _name: None,
        find_family=lambda _name: None,
    )

    assert isinstance(result, ShowNotFound)
    assert {suggestion.kind for suggestion in result.suggestions} == {
        "agent",
        "clan",
        "family",
        "tribe",
    }
    assert len(result.suggestions) <= 6


def test_kinship_index_counts_unique_members_and_progress() -> None:
    entries = [
        _entry("review.a", clan="review", tribe="perf", terminal=True),
        _entry("review.b", clan="review", tribe="perf"),
        _entry("migrate--one", family="migrate", tribe="perf", terminal=True),
    ]

    index = build_kinship_index(entries)

    assert [
        (item.name, item.member_count, item.done_count) for item in index.clans
    ] == [("review", 2, 1)]
    assert [item.name for item in index.families] == ["migrate"]
    assert [
        (item.name, item.member_count, item.done_count) for item in index.tribes
    ] == [("perf", 3, 2)]
    assert build_kinship_index([]).clans == ()


def test_clan_attribute_wiring_uses_member_metadata() -> None:
    members = (
        SimpleNamespace(
            name="review.a",
            artifacts_dir=Path("/tmp/review-a"),
            timestamp="001",
            generation="g1",
        ),
        SimpleNamespace(
            name="review.b",
            artifacts_dir=Path("/tmp/review-b"),
            timestamp="002",
            generation="g1",
        ),
    )
    clan = SimpleNamespace(name="review", generation="g1", members=members)
    metadata = {
        "/tmp/review-a/agent_meta.json": {"clan_tribe": "perf"},
        "/tmp/review-b/agent_meta.json": {"clan_summary": "Find regressions"},
    }

    result = resolve_clan_attributes(
        clan,
        meta_reader=lambda path: metadata.get(str(path)),
        wire_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        resolve_tribe=lambda _name, _generation, _members: SimpleNamespace(
            tribe="perf"
        ),
        resolve_summary=lambda _name, _generation, _members: SimpleNamespace(
            summary="Find regressions"
        ),
    )

    assert result == ClanAttributes(tribe="perf", summary="Find regressions")
