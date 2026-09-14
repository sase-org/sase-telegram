"""Tests for console-script executable resolution."""

from __future__ import annotations

import os
from pathlib import Path

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
    script = bin_dir / "sase_chop_tg_inbound"
    _write_executable(python)
    _write_executable(script)
    monkeypatch.setattr(executables.sys, "executable", str(python))
    monkeypatch.setattr(
        executables.shutil,
        "which",
        lambda _name: str(tmp_path / "path" / "sase_chop_tg_inbound"),
    )

    assert executables.resolve_console_script("sase_chop_tg_inbound") == str(script)


def test_resolve_console_script_falls_back_to_path_lookup(
    tmp_path: Path, monkeypatch
) -> None:
    bin_dir = tmp_path / "venv" / "bin"
    python = bin_dir / "python"
    path_script = tmp_path / "path" / "sase_chop_tg_inbound"
    _write_executable(python)
    _write_executable(path_script)
    monkeypatch.setattr(executables.sys, "executable", str(python))
    monkeypatch.setattr(
        executables.shutil,
        "which",
        lambda _name: str(path_script),
    )

    assert executables.resolve_console_script("sase_chop_tg_inbound") == str(
        path_script
    )


def test_resolve_console_script_keeps_bare_name_as_last_resort(
    tmp_path: Path, monkeypatch
) -> None:
    python = tmp_path / "venv" / "bin" / "python"
    _write_executable(python)
    monkeypatch.setattr(executables.sys, "executable", str(python))
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)

    assert executables.resolve_console_script("sase_chop_tg_inbound") == (
        "sase_chop_tg_inbound"
    )


def test_default_receiver_argv_uses_existing_executable() -> None:
    argv = receiver._receiver_argv()

    executable = Path(argv[0])
    assert executable.is_absolute()
    assert executable.is_file()
    assert os.access(executable, os.X_OK)
    assert argv[1:] == ["--receiver"]
