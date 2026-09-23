"""Telegram inbound update handlers.

Split from ``scripts/sase_tg_inbound.py``, which remains the entry point.
Modules in this package are in layer order: a module may import only from
modules listed above it. Tests patch inbound modules through
``tests/inbound_namespace.py``.
"""
