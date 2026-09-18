"""Packaged SASE service-proc config for the Telegram receiver."""

from __future__ import annotations

import importlib.resources
from pathlib import Path
import tomllib

import yaml
from sase.config.core import ConfigLayer
from sase.service.config import compose_service_config


def test_package_declares_sase_config_entry_point() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert (
        pyproject["project"]["entry-points"]["sase_config"]["sase_telegram"]
        == "sase_telegram"
    )


def test_default_config_declares_disabled_service_receiver() -> None:
    ref = importlib.resources.files("sase_telegram").joinpath("default_config.yml")
    data = yaml.safe_load(ref.read_text(encoding="utf-8"))

    composition = compose_service_config(
        [
            ConfigLayer(
                name="plugin:sase_telegram",
                path=None,
                exists=True,
                list_strategy="concatenate",
                data=data,
            )
        ]
    )

    proc = composition.get("telegram_receiver")
    assert proc is not None
    assert proc.available, proc.unavailable_reasons
    assert proc.source == "plugin"
    assert proc.enabled is False
    assert proc.mode == "daemon"
    assert proc.restart == "on-failure"
    assert proc.success_exit_codes == (0,)
    assert proc.launcher is not None
    assert proc.launcher.argv == ("sase_job_tg_inbound", "--receiver")
