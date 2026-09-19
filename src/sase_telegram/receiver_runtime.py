"""Fingerprint the installed runtime the Telegram receiver is bound to.

The persistent ``getUpdates`` loop can outlive in-place SASE, plugin, and
native-extension updates. This helper is intentionally independent of
Telegram credentials and on-disk bot state: it only looks at the canonical
receiver executable and the resolved installed code roots for ``sase``,
``sase_telegram``, and ``sase_core_rs``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sase_telegram.executables import resolve_console_script

_RECEIVER_SCRIPT = "sase_job_tg_inbound"
_MONITORED_PACKAGES = ("sase", "sase_telegram", "sase_core_rs")
_CACHE_DIR_NAMES = frozenset({"__pycache__"})
_CACHE_DIR_SUFFIXES = (".egg-info", ".dist-info")
_CACHE_FILE_SUFFIXES = (".pyc", ".pyo")
SETTLE_INTERVAL_SECONDS = 0.25


class RuntimeScanError(Exception):
    """The on-disk runtime could not be scanned reliably."""


@dataclass(frozen=True)
class RuntimeGeneration:
    """Deterministic identity of one observed installed runtime."""

    digest: str
    executable: str
    roots: tuple[tuple[str, str], ...]


def observe_runtime_generation(
    *,
    executable: str | None = None,
    package_roots: Mapping[str, Path] | None = None,
) -> RuntimeGeneration:
    """Return the current runtime generation, or raise ``RuntimeScanError``.

    ``executable`` and ``package_roots`` are injection points for tests. The
    production defaults resolve the canonical ``sase_job_tg_inbound`` console
    script and the installed ``sase`` / ``sase_telegram`` / ``sase_core_rs``
    code roots.
    """
    try:
        executable_path = (
            executable
            if executable is not None
            else resolve_console_script(_RECEIVER_SCRIPT)
        )
        roots = (
            dict(package_roots)
            if package_roots is not None
            else _default_package_roots()
        )
        executable_fact = _file_fact(Path(executable_path), label="receiver executable")
        package_facts: list[tuple[str, str, int, int]] = []
        resolved_roots: list[tuple[str, str]] = []
        for name in _MONITORED_PACKAGES:
            if name not in roots:
                raise RuntimeScanError(f"runtime root missing for {name}")
            root = Path(roots[name])
            resolved = _resolve_existing_path(root, label=f"{name} runtime root")
            resolved_roots.append((name, str(resolved)))
            for relative, size, mtime_ns in _iter_runtime_files(resolved):
                package_facts.append((name, relative, size, mtime_ns))
    except RuntimeScanError:
        raise
    except OSError as exc:
        raise RuntimeScanError(f"runtime scan failed: {exc}") from exc

    package_facts.sort()
    resolved_roots_tuple = tuple(resolved_roots)
    digest = _digest_facts(executable_fact, resolved_roots_tuple, package_facts)
    return RuntimeGeneration(
        digest=digest,
        executable=executable_fact[0],
        roots=resolved_roots_tuple,
    )


def wait_for_settled_generation(
    *,
    scan: Callable[[], RuntimeGeneration] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    interval: float | None = None,
) -> RuntimeGeneration:
    """Scan until two consecutive observations agree.

    Transient scan errors (a torn install, a replaced file mid-walk) reset
    the consecutive-match counter so the caller never re-execs into an
    environment that is still changing.
    """
    scanner = scan or observe_runtime_generation
    delay = SETTLE_INTERVAL_SECONDS if interval is None else interval
    previous: RuntimeGeneration | None = None
    while True:
        try:
            current = scanner()
        except RuntimeScanError:
            previous = None
            sleep(delay)
            continue
        if previous is not None and current.digest == previous.digest:
            return current
        previous = current
        sleep(delay)


def _default_package_roots() -> dict[str, Path]:
    return {name: _resolve_installed_root(name) for name in _MONITORED_PACKAGES}


def _resolve_installed_root(module_name: str) -> Path:
    module = sys.modules.get(module_name)
    if module is not None:
        origin = getattr(module, "__file__", None)
        if origin:
            return _root_from_origin(Path(origin), module_name)
        paths = getattr(module, "__path__", None)
        if paths:
            return _resolve_existing_path(
                Path(next(iter(paths))),
                label=f"{module_name} runtime root",
            )
    spec = importlib.util.find_spec(module_name)
    if spec is None:
        raise RuntimeScanError(f"runtime module is not importable: {module_name}")
    origin = spec.origin
    if origin and origin not in {"built-in", "frozen"}:
        return _root_from_origin(Path(origin), module_name)
    locations = spec.submodule_search_locations
    if locations:
        return _resolve_existing_path(
            Path(next(iter(locations))),
            label=f"{module_name} runtime root",
        )
    raise RuntimeScanError(f"runtime module has no on-disk origin: {module_name}")


def _root_from_origin(origin: Path, module_name: str) -> Path:
    if origin.name == "__init__.py":
        return _resolve_existing_path(
            origin.parent, label=f"{module_name} runtime root"
        )
    return _resolve_existing_path(origin, label=f"{module_name} runtime root")


def _resolve_existing_path(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise RuntimeScanError(f"{label} is unreadable: {path}: {exc}") from exc
    if not resolved.exists():
        raise RuntimeScanError(f"{label} is missing: {resolved}")
    return resolved


def _file_fact(path: Path, *, label: str) -> tuple[str, int, int]:
    resolved = _resolve_existing_path(path, label=label)
    try:
        st = resolved.stat()
    except OSError as exc:
        raise RuntimeScanError(f"{label} is unreadable: {resolved}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeScanError(f"{label} is not a regular file: {resolved}")
    return (str(resolved), st.st_size, st.st_mtime_ns)


def _iter_runtime_files(root: Path) -> list[tuple[str, int, int]]:
    if root.is_file():
        st = _stat_regular_file(root)
        if st is None:
            raise RuntimeScanError(f"runtime root is not a regular file: {root}")
        return [(root.name, st.st_size, st.st_mtime_ns)]
    if not root.is_dir():
        raise RuntimeScanError(f"runtime root is missing: {root}")

    facts: list[tuple[str, int, int]] = []

    def _onerror(error: OSError) -> None:
        raise RuntimeScanError(f"runtime scan failed: {error}") from error

    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=_onerror
    ):
        dirnames[:] = sorted(name for name in dirnames if not _is_ignored_dir(name))
        for name in sorted(filenames):
            if _is_ignored_file(name):
                continue
            path = Path(dirpath) / name
            try:
                st = _stat_regular_file(path)
            except RuntimeScanError:
                raise
            except OSError as exc:
                raise RuntimeScanError(f"runtime scan failed: {exc}") from exc
            if st is None:
                continue
            relative = path.relative_to(root).as_posix()
            facts.append((relative, st.st_size, st.st_mtime_ns))
    facts.sort()
    return facts


def _stat_regular_file(path: Path) -> os.stat_result | None:
    try:
        st = path.stat()
    except OSError as exc:
        raise RuntimeScanError(f"runtime scan failed: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        return None
    return st


def _is_ignored_dir(name: str) -> bool:
    if name in _CACHE_DIR_NAMES or name.startswith("."):
        return True
    return name.endswith(_CACHE_DIR_SUFFIXES)


def _is_ignored_file(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(_CACHE_FILE_SUFFIXES)


def _digest_facts(
    executable: tuple[str, int, int],
    roots: tuple[tuple[str, str], ...],
    package_facts: list[tuple[str, str, int, int]],
) -> str:
    hasher = hashlib.sha256()
    path, size, mtime_ns = executable
    hasher.update(f"executable\t{path}\t{size}\t{mtime_ns}\n".encode())
    for name, root in roots:
        hasher.update(f"root\t{name}\t{root}\n".encode())
    for package, relative, size, mtime_ns in package_facts:
        hasher.update(f"{package}\t{relative}\t{size}\t{mtime_ns}\n".encode())
    return hasher.hexdigest()


__all__ = [
    "SETTLE_INTERVAL_SECONDS",
    "RuntimeGeneration",
    "RuntimeScanError",
    "observe_runtime_generation",
    "wait_for_settled_generation",
]
