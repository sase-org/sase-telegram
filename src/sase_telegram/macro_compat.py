"""New-first compatibility imports for the renamed ``sase.macro`` APIs.

SASE renamed its reusable-prompt concept from "xprompt" to "macro". Every
name below is imported from its new ``sase.macro.*`` home first, falling back
to the legacy ``sase.xprompt.*`` spelling so this repo keeps working against
both sase master and its ``sase>=`` floor.

All sase imports that name the concept go through this module; callers import
from here instead of from ``sase`` directly. Tests therefore patch
``sase_telegram.macro_compat.<name>`` rather than ``sase.xprompt.*``.
"""

from __future__ import annotations

import importlib
from typing import Any


def _resolve(candidates: tuple[tuple[str, str], ...]) -> Any:
    """Return the first importable ``(module, attribute)`` candidate."""
    errors: list[str] = []
    for module_name, attr in candidates:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            errors.append(f"{module_name}: {exc}")
            continue
        value = getattr(module, attr, None)
        if value is not None:
            return value
        errors.append(f"{module_name} has no attribute {attr}")
    raise ImportError(
        "sase_telegram.macro_compat could not resolve any of "
        + ", ".join(f"{m}.{a}" for m, a in candidates)
        + " ("
        + "; ".join(errors)
        + ")"
    )


InputType = _resolve(
    (
        ("sase.macro.models", "InputType"),
        ("sase.xprompt.models", "InputType"),
    )
)
"""Supported input argument types for macro files."""

MacroValidationError = _resolve(
    (
        ("sase.macro.models", "MacroValidationError"),
        ("sase.xprompt.models", "XPromptValidationError"),
    )
)
"""Raised when typed input validation fails."""

UNSET = _resolve(
    (
        ("sase.macro.models", "UNSET"),
        ("sase.xprompt.models", "UNSET"),
    )
)
"""Sentinel for 'no default specified' (required input)."""

replace_ref_in_vcs_tag = _resolve(
    (
        ("sase.macro", "replace_ref_in_vcs_tag"),
        ("sase.xprompt", "replace_ref_in_vcs_tag"),
    )
)
"""Rewrite the ref inside a VCS workflow tag."""

extract_macro_calls = _resolve(
    (
        ("sase.macro.workflow_validator_extract", "extract_macro_calls"),
        ("sase.xprompt.workflow_validator_extract", "extract_xprompt_calls"),
    )
)
"""Extract ``#name`` macro calls from a prompt."""

extract_prompt_directives = _resolve(
    (
        ("sase.macro.directives", "extract_prompt_directives"),
        ("sase.xprompt.directives", "extract_prompt_directives"),
    )
)
"""Split a prompt into text and launch directives."""

plan_prompt_fanout_variants = _resolve(
    (
        ("sase.macro.directives", "plan_prompt_fanout_variants"),
        ("sase.xprompt.directives", "plan_prompt_fanout_variants"),
    )
)
"""Plan per-slot prompts for a multi-model fan-out launch."""

process_macro_references = _resolve(
    (
        ("sase.macro", "process_macro_references"),
        ("sase.macro.processor", "process_macro_references"),
        ("sase.xprompt", "process_xprompt_references"),
    )
)
"""Expand ``#name`` macro references in a prompt."""

list_patch_macro_tags = _resolve(
    (
        ("sase.integrations.patch_tags", "list_patch_macro_tags"),
        ("sase.integrations.patch_tags", "list_patch_xprompt_tags"),
    )
)
"""List active Patches and their copyable VCS macro tags."""

NoMacrosFound = _resolve(
    (
        ("sase.macro.catalog", "NoMacrosFound"),
        ("sase.xprompt.catalog", "NoXpromptsFound"),
    )
)
"""Raised when no macro definitions exist to build a catalog from."""

PdfEngineUnavailable = _resolve(
    (
        ("sase.macro.catalog", "PdfEngineUnavailable"),
        ("sase.xprompt.catalog", "PdfEngineUnavailable"),
    )
)
"""Raised when no PDF engine is installed on the bot host."""

build_macros_catalog = _resolve(
    (
        ("sase.macro.catalog", "build_macros_catalog"),
        ("sase.xprompt.catalog", "build_xprompts_catalog"),
    )
)
"""Build the macros PDF catalog."""

CatalogArtifact = _resolve(
    (
        ("sase.macro.catalog", "CatalogArtifact"),
        ("sase.xprompt.catalog", "CatalogArtifact"),
    )
)
"""One built macros catalog (PDF path plus stats)."""

CatalogStats = _resolve(
    (
        ("sase.macro.catalog", "CatalogStats"),
        ("sase.xprompt.catalog", "CatalogStats"),
    )
)
"""Summary counts for a built macros catalog."""


def legacy_xprompt_alias_enabled() -> bool:
    """Return True while the retired ``/xprompts`` alias stays accepted.

    Follows sase's ``legacy_xprompt_syntax`` sunset flag. A missing flag (an
    older sase that predates the rename) counts as enabled.
    """
    try:
        from sase.legacy_xprompt_syntax import legacy_xprompt_syntax_enabled
    except ImportError:
        return True
    try:
        return bool(legacy_xprompt_syntax_enabled())
    except Exception:
        return True
