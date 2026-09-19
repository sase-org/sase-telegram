"""Tests for Telegram receiver runtime generation fingerprinting."""

from __future__ import annotations

from pathlib import Path

import pytest

from sase_telegram.receiver_runtime import (
    RuntimeGeneration,
    RuntimeScanError,
    observe_runtime_generation,
    wait_for_settled_generation,
)


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> None:
    _write(path, content)
    path.chmod(0o755)


def _runtime_tree(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    executable = tmp_path / "bin" / "sase_job_tg_inbound"
    _write_executable(executable)
    roots = {
        "sase": tmp_path / "host" / "sase",
        "sase_telegram": tmp_path / "plugin" / "sase_telegram",
        "sase_core_rs": tmp_path / "native" / "sase_core_rs.so",
    }
    _write(roots["sase"] / "__init__.py", "host = 1\n")
    _write(roots["sase"] / "api.py", "def run() -> None:\n    return None\n")
    _write(roots["sase_telegram"] / "__init__.py", "plugin = 1\n")
    _write(roots["sase_telegram"] / "inbound.py", "VALUE = 1\n")
    _write(roots["sase_core_rs"], "native-extension")
    return executable, roots


def _observe(executable: Path, roots: dict[str, Path]) -> RuntimeGeneration:
    return observe_runtime_generation(executable=str(executable), package_roots=roots)


def test_unchanged_tree_keeps_the_same_generation(tmp_path: Path) -> None:
    executable, roots = _runtime_tree(tmp_path)

    first = _observe(executable, roots)
    second = _observe(executable, roots)

    assert first.digest == second.digest
    assert first.executable == str(executable.resolve())
    assert dict(first.roots)["sase"] == str(roots["sase"].resolve())


def test_host_plugin_and_native_changes_each_new_generation(tmp_path: Path) -> None:
    executable, roots = _runtime_tree(tmp_path)
    baseline = _observe(executable, roots)

    _write(roots["sase"] / "api.py", "def run() -> None:\n    return None\n# changed\n")
    host_changed = _observe(executable, roots)
    assert host_changed.digest != baseline.digest

    _write(roots["sase_telegram"] / "inbound.py", "VALUE = 2\n")
    plugin_changed = _observe(executable, roots)
    assert plugin_changed.digest != host_changed.digest

    _write(roots["sase_core_rs"], "native-extension-v2")
    native_changed = _observe(executable, roots)
    assert native_changed.digest != plugin_changed.digest


def test_added_and_removed_runtime_files_change_generation(tmp_path: Path) -> None:
    executable, roots = _runtime_tree(tmp_path)
    baseline = _observe(executable, roots)

    extra = roots["sase"] / "new_module.py"
    _write(extra, "NEW = 1\n")
    added = _observe(executable, roots)
    assert added.digest != baseline.digest

    extra.unlink()
    restored = _observe(executable, roots)
    assert restored.digest == baseline.digest


def test_executable_replacement_changes_generation(tmp_path: Path) -> None:
    executable, roots = _runtime_tree(tmp_path)
    baseline = _observe(executable, roots)

    _write_executable(executable, "#!/bin/sh\nexit 1\n")
    replaced = _observe(executable, roots)
    assert replaced.digest != baseline.digest

    other = tmp_path / "bin" / "other_inbound"
    _write_executable(other)
    moved = _observe(other, roots)
    assert moved.digest != replaced.digest
    assert moved.executable == str(other.resolve())


def test_cache_churn_does_not_change_generation(tmp_path: Path) -> None:
    executable, roots = _runtime_tree(tmp_path)
    baseline = _observe(executable, roots)

    pycache = roots["sase"] / "__pycache__"
    _write(pycache / "api.cpython-312.pyc", "bytecode")
    _write(roots["sase"] / "api.pyc", "stale-pyc")
    _write(roots["sase_telegram"] / "inbound.pyo", "optimized")
    _write(roots["sase"] / ".mypy_cache" / "3.12" / "api.data.json", "{}")
    _write(
        roots["sase_telegram"] / "sase_telegram.egg-info" / "PKG-INFO",
        "Name: sase-telegram\n",
    )

    after_cache = _observe(executable, roots)
    assert after_cache.digest == baseline.digest


def test_wait_for_settled_skips_transient_scan_errors() -> None:
    stable = RuntimeGeneration(
        digest="abc",
        executable="/venv/bin/sase_job_tg_inbound",
        roots=(("sase", "/sase"),),
    )
    calls: list[str] = []
    sleeps: list[float] = []

    def scan() -> RuntimeGeneration:
        calls.append("scan")
        if len(calls) == 1:
            raise RuntimeScanError("torn install")
        return stable

    result = wait_for_settled_generation(scan=scan, sleep=sleeps.append, interval=0.01)

    assert result is stable
    assert calls == ["scan", "scan", "scan"]
    assert sleeps == [0.01, 0.01]


def test_wait_for_settled_requires_two_matching_observations() -> None:
    first = RuntimeGeneration(
        digest="aaa",
        executable="/venv/bin/sase_job_tg_inbound",
        roots=(("sase", "/sase"),),
    )
    second = RuntimeGeneration(
        digest="bbb",
        executable="/venv/bin/sase_job_tg_inbound",
        roots=(("sase", "/sase"),),
    )
    queue = [first, second, second]

    def scan() -> RuntimeGeneration:
        return queue.pop(0)

    result = wait_for_settled_generation(
        scan=scan, sleep=lambda _delay: None, interval=0
    )

    assert result is second
    assert queue == []


def test_missing_runtime_file_is_a_generation_change(tmp_path: Path) -> None:
    executable, roots = _runtime_tree(tmp_path)
    _observe(executable, roots)
    roots["sase_core_rs"].unlink()

    with pytest.raises(RuntimeScanError, match="missing"):
        _observe(executable, roots)


def test_missing_package_root_fails_the_scan(tmp_path: Path) -> None:
    executable, roots = _runtime_tree(tmp_path)
    roots.pop("sase_telegram")

    with pytest.raises(RuntimeScanError, match="runtime root missing"):
        _observe(executable, roots)
