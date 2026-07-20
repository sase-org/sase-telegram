"""Pure reference resolution and kinship models for Telegram ``/show``."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Literal


ShowKind = Literal["agent", "clan", "family", "tribe"]
_TRIBE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class InvalidShowReference(ValueError):
    """Raised when a forced ``@tribe`` reference has invalid grammar."""


@dataclass(frozen=True)
class ShowSuggestion:
    """One close reference offered by the not-found view."""

    kind: ShowKind
    name: str
    ref: str


@dataclass(frozen=True)
class ShowTarget:
    """A resolved ``/show`` reference and its kind-specific payload."""

    kind: ShowKind
    name: str
    entries: tuple[Any, ...] = ()
    entry: Any | None = None
    named_agent: Any | None = None
    clan: Any | None = None
    family: Any | None = None
    clan_tribe: str | None = None
    clan_summary: str | None = None
    also_tribe: bool = False


@dataclass(frozen=True)
class ShowNotFound:
    """A reference that did not resolve, plus mobile-friendly suggestions."""

    query: str
    suggestions: tuple[ShowSuggestion, ...] = ()


@dataclass(frozen=True)
class KinshipIndexItem:
    kind: Literal["clan", "family", "tribe"]
    name: str
    ref: str
    member_count: int
    done_count: int


@dataclass(frozen=True)
class KinshipIndex:
    clans: tuple[KinshipIndexItem, ...] = ()
    families: tuple[KinshipIndexItem, ...] = ()
    tribes: tuple[KinshipIndexItem, ...] = ()


@dataclass(frozen=True)
class ClanAttributes:
    tribe: str | None = None
    summary: str | None = None


AgentLookup = Callable[[str], Any | None]
MetaReader = Callable[[Path], Mapping[str, Any] | None]


def resolve_show_reference(
    ref: str,
    entries: Iterable[Any],
    *,
    find_agent: AgentLookup | None = None,
    find_clan: AgentLookup | None = None,
    find_family: AgentLookup | None = None,
    clan_attribute_resolver: Callable[[Any], ClanAttributes] | None = None,
) -> ShowTarget | ShowNotFound:
    """Resolve *ref* with agent > clan > family > bare-tribe precedence."""
    query = ref.strip()
    all_entries = tuple(entries)
    if not query:
        return ShowNotFound(query="")

    known_tribes = _known_tribes(all_entries)
    forced_tribe = _parse_tribe_reference(query)
    if forced_tribe is not None:
        canonical = known_tribes.get(forced_tribe.casefold())
        if canonical is None:
            return _not_found(query, all_entries)
        return ShowTarget(
            kind="tribe",
            name=canonical,
            entries=_tribe_entries(canonical, all_entries),
        )

    find_agent = find_agent or _default_find_agent
    find_clan = find_clan or _default_find_clan
    find_family = find_family or _default_find_family

    exact_entry = next(
        (entry for entry in all_entries if getattr(entry, "name", None) == query),
        None,
    )
    named_agent = find_agent(query)
    if named_agent is not None or exact_entry is not None:
        return ShowTarget(
            kind="agent",
            name=query,
            entries=(exact_entry,) if exact_entry is not None else (),
            entry=exact_entry,
            named_agent=named_agent,
            also_tribe=query.casefold() in known_tribes,
        )

    clan = find_clan(query)
    if clan is not None:
        resolver = clan_attribute_resolver or resolve_clan_attributes
        attributes = resolver(clan)
        clan_entries = _clan_entries(clan, all_entries)
        effective_entry_tribe = next(
            (
                tribe
                for entry in clan_entries
                if isinstance((tribe := getattr(entry, "tribe", None)), str) and tribe
            ),
            None,
        )
        return ShowTarget(
            kind="clan",
            name=query,
            entries=clan_entries,
            clan=clan,
            clan_tribe=attributes.tribe or effective_entry_tribe,
            clan_summary=attributes.summary,
            also_tribe=query.casefold() in known_tribes,
        )

    family = find_family(query)
    if family is not None:
        return ShowTarget(
            kind="family",
            name=query,
            entries=_family_entries(family, all_entries),
            family=family,
            also_tribe=query.casefold() in known_tribes,
        )

    canonical_tribe = known_tribes.get(query.casefold())
    if canonical_tribe is not None:
        return ShowTarget(
            kind="tribe",
            name=canonical_tribe,
            entries=_tribe_entries(canonical_tribe, all_entries),
        )
    return _not_found(query, all_entries)


def build_kinship_index(entries: Iterable[Any]) -> KinshipIndex:
    """Build the bare ``/show`` index from live and recent list entries."""
    all_entries = tuple(entries)
    return KinshipIndex(
        clans=_index_items("clan", all_entries, "agent_clan"),
        families=_index_items("family", all_entries, "agent_family"),
        tribes=_index_items("tribe", all_entries, "tribe"),
    )


def suggest_show_references(
    query: str, entries: Iterable[Any], *, limit: int = 6
) -> tuple[ShowSuggestion, ...]:
    """Return deterministic casefold/prefix/substring suggestions."""
    all_entries = tuple(entries)
    candidates: list[ShowSuggestion] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: ShowKind, name: str, ref: str) -> None:
        identity = kind, name.casefold()
        if identity in seen:
            return
        seen.add(identity)
        candidates.append(ShowSuggestion(kind=kind, name=name, ref=ref))

    for entry in all_entries:
        name = getattr(entry, "name", None)
        if isinstance(name, str) and name:
            add("agent", name, name)
        clan = getattr(entry, "agent_clan", None)
        if isinstance(clan, str) and clan:
            add("clan", clan, clan)
        family = getattr(entry, "agent_family", None)
        if isinstance(family, str) and family:
            add("family", family, family)
        tribe = getattr(entry, "tribe", None)
        if isinstance(tribe, str) and tribe:
            add("tribe", tribe, f"@{tribe}")

    folded = query.removeprefix("@").casefold()

    def score(item: ShowSuggestion) -> tuple[int, int, str, str]:
        candidate = item.name.casefold()
        if candidate == folded:
            rank = 0
        elif candidate.startswith(folded) or folded.startswith(candidate):
            rank = 1
        elif folded in candidate or candidate in folded:
            rank = 2
        else:
            rank = 3
        return rank, abs(len(candidate) - len(folded)), candidate, item.kind

    matching = [item for item in candidates if score(item)[0] < 3]
    return tuple(sorted(matching, key=score)[:limit])


def resolve_clan_attributes(
    clan: Any,
    *,
    meta_reader: MetaReader | None = None,
    resolve_tribe: Callable[[str, str | None, list[Any]], Any] | None = None,
    resolve_summary: Callable[[str, str | None, list[Any]], Any] | None = None,
    wire_factory: Callable[..., Any] | None = None,
) -> ClanAttributes:
    """Resolve one clan generation's effective declared tribe and summary."""
    meta_reader = meta_reader or _read_json_dict
    if resolve_tribe is None or resolve_summary is None or wire_factory is None:
        try:
            from sase.core.agent_clan_tribe import (
                ClanTribeMemberWire,
                resolve_clan_summary,
                resolve_clan_tribe,
            )
        except ImportError:
            return _fallback_clan_attributes(clan, meta_reader)
        resolve_tribe = resolve_tribe or resolve_clan_tribe
        resolve_summary = resolve_summary or resolve_clan_summary
        wire_factory = wire_factory or ClanTribeMemberWire

    wire_members: list[Any] = []
    has_tribe = False
    has_summary = False
    for member in tuple(getattr(clan, "members", ()) or ()):
        artifacts_dir = Path(getattr(member, "artifacts_dir", ""))
        meta = meta_reader(artifacts_dir / "agent_meta.json") or {}
        raw_tribe = meta.get("clan_tribe")
        tribe = raw_tribe if isinstance(raw_tribe, str) and raw_tribe else None
        raw_summary = meta.get("clan_summary")
        summary = raw_summary if isinstance(raw_summary, str) and raw_summary else None
        has_tribe = has_tribe or tribe is not None
        has_summary = has_summary or summary is not None
        wire_members.append(
            wire_factory(
                agent_clan=str(getattr(clan, "name", "")),
                agent_clan_generation=getattr(member, "generation", None),
                clan_tribe=tribe,
                clan_summary=summary,
                launch_timestamp=str(getattr(member, "timestamp", "")),
                identity=f"{artifacts_dir}:{getattr(member, 'name', '')}",
            )
        )

    clan_name = str(getattr(clan, "name", ""))
    generation = getattr(clan, "generation", None)
    tribe = (
        getattr(resolve_tribe(clan_name, generation, wire_members), "tribe", None)
        if has_tribe
        else None
    )
    summary = (
        getattr(resolve_summary(clan_name, generation, wire_members), "summary", None)
        if has_summary
        else None
    )
    return ClanAttributes(tribe=tribe, summary=summary)


def _parse_tribe_reference(value: str) -> str | None:
    if not value.startswith("@"):
        return None
    try:
        from sase.core.agent_tribe import parse_tribe_reference
    except ImportError:
        tribe = value[1:]
        if not tribe or _TRIBE_NAME_RE.fullmatch(tribe) is None:
            raise InvalidShowReference(
                "tribe names use letters, digits, underscores, dots, and dashes"
            ) from None
        return tribe
    try:
        return parse_tribe_reference(value)
    except ValueError as exc:
        raise InvalidShowReference(str(exc)) from exc


def _default_find_agent(name: str) -> Any | None:
    from sase.agent.names import find_named_agent

    return find_named_agent(name)


def _default_find_clan(name: str) -> Any | None:
    try:
        from sase.agent.names import find_agent_clan
    except ImportError:
        return None
    return find_agent_clan(name)


def _default_find_family(name: str) -> Any | None:
    try:
        from sase.agent.names import find_agent_family
    except ImportError:
        return None
    return find_agent_family(name)


def _read_json_dict(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _fallback_clan_attributes(clan: Any, meta_reader: MetaReader) -> ClanAttributes:
    """Best-effort compatibility for an older installed SASE package."""
    declarations: list[tuple[str, str | None, str | None]] = []
    for member in tuple(getattr(clan, "members", ()) or ()):
        meta = meta_reader(
            Path(getattr(member, "artifacts_dir", "")) / "agent_meta.json"
        )
        meta = meta or {}
        tribe = meta.get("clan_tribe")
        summary = meta.get("clan_summary")
        declarations.append(
            (
                str(getattr(member, "timestamp", "")),
                tribe if isinstance(tribe, str) and tribe else None,
                summary if isinstance(summary, str) and summary else None,
            )
        )
    declarations.sort(key=lambda item: item[0])
    return ClanAttributes(
        tribe=next((item[1] for item in reversed(declarations) if item[1]), None),
        summary=next((item[2] for item in reversed(declarations) if item[2]), None),
    )


def _not_found(query: str, entries: tuple[Any, ...]) -> ShowNotFound:
    return ShowNotFound(
        query=query,
        suggestions=suggest_show_references(query, entries),
    )


def _known_tribes(entries: tuple[Any, ...]) -> dict[str, str]:
    known: dict[str, str] = {}
    for entry in entries:
        tribe = getattr(entry, "tribe", None)
        if isinstance(tribe, str) and tribe:
            known.setdefault(tribe.casefold(), tribe)
    return known


def _tribe_entries(tribe: str, entries: tuple[Any, ...]) -> tuple[Any, ...]:
    folded = tribe.casefold()
    return tuple(
        entry
        for entry in entries
        if isinstance(getattr(entry, "tribe", None), str)
        and entry.tribe.casefold() == folded
    )


def _clan_entries(clan: Any, entries: tuple[Any, ...]) -> tuple[Any, ...]:
    member_names = {
        getattr(member, "name", None)
        for member in tuple(getattr(clan, "members", ()) or ())
    }
    clan_name = getattr(clan, "name", None)
    generation = getattr(clan, "generation", None)
    return tuple(
        entry
        for entry in entries
        if getattr(entry, "name", None) in member_names
        or (
            getattr(entry, "agent_clan", None) == clan_name
            and (
                generation is None
                or getattr(entry, "agent_clan_generation", None) in {None, generation}
            )
        )
    )


def _family_entries(family: Any, entries: tuple[Any, ...]) -> tuple[Any, ...]:
    member_names = {
        getattr(member, "name", None)
        for member in tuple(getattr(family, "members", ()) or ())
    }
    base_name = getattr(family, "base_name", None)
    return tuple(
        entry
        for entry in entries
        if getattr(entry, "name", None) in member_names
        or getattr(entry, "agent_family", None) == base_name
    )


def _index_items(
    kind: Literal["clan", "family", "tribe"],
    entries: tuple[Any, ...],
    attribute: str,
) -> tuple[KinshipIndexItem, ...]:
    grouped: dict[str, list[Any]] = {}
    canonical_names: dict[str, str] = {}
    for entry in entries:
        value = getattr(entry, attribute, None)
        if not isinstance(value, str) or not value:
            continue
        key = value.casefold() if kind == "tribe" else value
        canonical_names.setdefault(key, value)
        grouped.setdefault(key, []).append(entry)

    items: list[KinshipIndexItem] = []
    for key, members in grouped.items():
        unique_members = _unique_entries(members)
        name = canonical_names[key]
        items.append(
            KinshipIndexItem(
                kind=kind,
                name=name,
                ref=f"@{name}" if kind == "tribe" else name,
                member_count=len(unique_members),
                done_count=sum(_entry_is_done(entry) for entry in unique_members),
            )
        )
    return tuple(sorted(items, key=lambda item: item.name.casefold()))


def _unique_entries(entries: Iterable[Any]) -> tuple[Any, ...]:
    by_name: dict[str, Any] = {}
    unnamed: list[Any] = []
    for entry in entries:
        name = getattr(entry, "name", None)
        if isinstance(name, str) and name:
            by_name[name] = entry
        else:
            unnamed.append(entry)
    return tuple(by_name.values()) + tuple(unnamed)


def _entry_is_done(entry: Any) -> bool:
    return bool(getattr(entry, "is_terminal", False)) or getattr(
        entry, "status_bucket", None
    ) in {"Done", "Failed"}
