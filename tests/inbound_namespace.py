"""Patchable namespace over all inbound modules.

Exposes ``INBOUND``, a proxy over the inbound entry-point script plus every
submodule of ``sase_telegram.inbound_handlers``. It reproduces the old
single-namespace patch semantics: patching ``telegram_client``,
``_launch_agent``, or even ``time`` reaches every function that could see it
before the split.
"""

from __future__ import annotations

import importlib
import pkgutil
from functools import cache
from types import ModuleType

_PACKAGE = "sase_telegram.inbound_handlers"
_SCRIPT = "sase_telegram.scripts.sase_tg_inbound"


@cache
def inbound_modules() -> tuple[ModuleType, ...]:
    """Import and return the script plus every inbound_handlers submodule."""
    package = importlib.import_module(_PACKAGE)
    names = [
        _SCRIPT,
        _PACKAGE,
        *(f"{_PACKAGE}.{info.name}" for info in pkgutil.iter_modules(package.__path__)),
    ]
    return tuple(importlib.import_module(name) for name in names)


class _InboundNamespace:
    """Proxy that fans patching out to every inbound module binding a name."""

    __slots__ = ()

    def _owners(self, name: str) -> list[ModuleType]:
        return [m for m in inbound_modules() if name in vars(m)]

    def _lookup(self, name: str) -> object:
        if name == "log" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        owners = self._owners(name)
        if not owners:
            raise AttributeError(name)
        first = vars(owners[0])[name]
        for module in owners[1:]:
            if vars(module)[name] is not first:
                raise AttributeError(name)
        return first

    @property
    def __dict__(self) -> dict[str, object]:  # type: ignore[override]
        outer = self

        class _NamespaceDict(dict[str, object]):
            def __getitem__(self, key: str) -> object:
                try:
                    return outer._lookup(key)
                except AttributeError:
                    raise KeyError(key) from None

            def __contains__(self, key: object) -> bool:
                if not isinstance(key, str):
                    return False
                try:
                    outer._lookup(key)
                except AttributeError:
                    return False
                return True

        result: dict[str, object] = _NamespaceDict()
        seen: set[str] = set()
        for module in inbound_modules():
            for key in vars(module):
                if key in seen:
                    continue
                try:
                    value = self._lookup(key)
                except AttributeError:
                    continue
                seen.add(key)
                dict.__setitem__(result, key, value)
        return result

    def __getattr__(self, name: str) -> object:
        return self._lookup(name)

    def __setattr__(self, name: str, value: object) -> None:
        self._lookup(name)
        for module in self._owners(name):
            setattr(module, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError(name)


INBOUND = _InboundNamespace()
