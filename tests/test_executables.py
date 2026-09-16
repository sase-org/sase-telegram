"""Tests for console-script executable resolution."""

from __future__ import annotations

from pathlib import Path
import tomllib

from sase_telegram import executables, receiver


def _write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_resolve_console_script_prefers_interpreter_sibling(
    tmp_path: Path, monkeypatch
) -> None:
    bin_dir = tmp_path / "venv" / "bin"
    python = bin_dir / "python"
    script = bin_dir / "sase_job_tg_inbound"
    _write_executable(python)
    _write_executable(script)
    monkeypatch.setattr(executables.sys, "executable", str(python))
    monkeypatch.setattr(
        executables.shutil,
        "which",
        lambda _name: str(tmp_path / "path" / "sase_job_tg_inbound"),
    )

    assert executables.resolve_console_script("sase_job_tg_inbound") == str(script)


def test_resolve_console_script_falls_back_to_path_lookup(
    tmp_path: Path, monkeypatch
) -> None:
    bin_dir = tmp_path / "venv" / "bin"
    python = bin_dir / "python"
    path_script = tmp_path / "path" / "sase_job_tg_inbound"
    _write_executable(python)
    _write_executable(path_script)
    monkeypatch.setattr(executables.sys, "executable", str(python))
    monkeypatch.setattr(
        executables.shutil,
        "which",
        lambda _name: str(path_script),
    )

    assert executables.resolve_console_script("sase_job_tg_inbound") == str(path_script)


def test_resolve_console_script_keeps_bare_name_as_last_resort(
    tmp_path: Path, monkeypatch
) -> None:
    python = tmp_path / "venv" / "bin" / "python"
    _write_executable(python)
    monkeypatch.setattr(executables.sys, "executable", str(python))
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)

    assert executables.resolve_console_script("sase_job_tg_inbound") == (
        "sase_job_tg_inbound"
    )


def test_default_receiver_argv_uses_canonical_executable(monkeypatch) -> None:
    monkeypatch.setattr(
        receiver,
        "resolve_console_script",
        lambda name: f"/venv/bin/{name}",
    )

    argv = receiver._receiver_argv()

    assert argv == ["/venv/bin/sase_job_tg_inbound", "--receiver"]


def test_project_registers_canonical_and_legacy_console_scripts() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]

    assert scripts["sase_job_tg_inbound"] == scripts["sase_chop_tg_inbound"]
    assert scripts["sase_job_tg_outbound"] == scripts["sase_chop_tg_outbound"]
