"""CLI entry point wrappers for sase-telegram scripts."""

from typing import Any


def inbound_main(*args: Any, **kwargs: Any) -> int:
    from sase_telegram.scripts.sase_tg_inbound import main

    return main(*args, **kwargs)


def outbound_main(*args: Any, **kwargs: Any) -> int:
    from sase_telegram.scripts.sase_tg_outbound import main

    return main(*args, **kwargs)


__all__ = ["inbound_main", "outbound_main"]
