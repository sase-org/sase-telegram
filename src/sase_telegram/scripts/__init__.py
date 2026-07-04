"""CLI entry point wrappers for sase-telegram scripts."""

import sys
from collections.abc import Callable
from typing import Any

from sase_telegram.credentials import TelegramCredentialError


def inbound_main(*args: Any, **kwargs: Any) -> int:
    from sase_telegram.enabled import is_telegram_enabled

    if not is_telegram_enabled():
        return 0
    from sase_telegram.scripts.sase_tg_inbound import main

    return _run_cleanly(main, *args, **kwargs)


def outbound_main(*args: Any, **kwargs: Any) -> int:
    from sase_telegram.enabled import is_telegram_enabled

    if not is_telegram_enabled():
        return 0
    from sase_telegram.scripts.sase_tg_outbound import main

    return _run_cleanly(main, *args, **kwargs)


def _run_cleanly(main: Callable[..., int], *args: Any, **kwargs: Any) -> int:
    try:
        return main(*args, **kwargs)
    except TelegramCredentialError as exc:
        print(str(exc), file=sys.stderr)
        return 1


__all__ = ["inbound_main", "outbound_main"]
