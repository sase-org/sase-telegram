"""Guard tests for the inbound split namespace."""

from __future__ import annotations

import importlib
from pathlib import Path

from inbound_namespace import inbound_modules


def test_every_handler_file_is_in_inbound_modules() -> None:
    package = importlib.import_module("sase_telegram.inbound_handlers")
    package_dir = Path(package.__file__).parent
    expected = {p.stem for p in package_dir.glob("*.py")}
    actual = {m.__name__.split(".")[-1] for m in inbound_modules()}
    # The package itself imports as ``inbound_handlers``; __init__ stem differs.
    expected.discard("__init__")
    actual.discard("inbound_handlers")
    # The entry-point script has no file under the package dir.
    actual.discard("sase_tg_inbound")
    assert expected <= actual
    assert {m.__name__ for m in inbound_modules()} >= {
        "sase_telegram.scripts.sase_tg_inbound",
        "sase_telegram.inbound_handlers",
    }


def test_no_name_is_bound_to_different_objects() -> None:
    seen: dict[str, object] = {}
    conflicts: dict[str, list[str]] = {}
    for module in inbound_modules():
        for name, value in vars(module).items():
            if name == "log" or (name.startswith("__") and name.endswith("__")):
                continue
            if name not in seen:
                seen[name] = value
            elif seen[name] is not value:
                conflicts.setdefault(name, []).append(module.__name__)
    assert not conflicts, f"names bound to different objects: {sorted(conflicts)}"
