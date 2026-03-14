"""CLI entry point wrappers for sase-telegram scripts."""

from sase_chop_telegram.scripts.sase_chop_tg_inbound import main as inbound_main
from sase_chop_telegram.scripts.sase_chop_tg_outbound import main as outbound_main

__all__ = ["inbound_main", "outbound_main"]
