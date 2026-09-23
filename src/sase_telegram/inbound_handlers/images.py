"""Photo and album staging and flushing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from pathlib import Path
from typing import Any
from sase_telegram import credentials, telegram_client
from sase_telegram.inbound import (
    IMAGES_DIR,
    build_image_prompt,
    build_photo_prompt,
    make_image_filename,
    normalize_launch_xprompt_at_refs,
    reconstruct_code_markers,
)
from sase_telegram.inbound_handlers.common import (
    _telegram_agent_launches_disabled,
    _context_chat_id,
    _message_id,
)
from sase_telegram.inbound_handlers.project_context import _record_project_context
from sase_telegram.inbound_handlers.agent_launch import _launch_agent

import logging

log = logging.getLogger(__name__)


_MEDIA_GROUPS_PATH = Path.home() / ".sase" / "telegram" / "media_groups.json"


_MEDIA_GROUP_QUIET_SECONDS = 2.0


@dataclass(frozen=True)
class _MediaGroupMessageContext:
    chat_id: str


def _media_group_id(message: Any) -> str | None:
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id is None:
        return None
    media_group_id = str(media_group_id)
    return media_group_id if media_group_id else None


def _media_group_key(chat_id: str, media_group_id: str) -> str:
    return f"{chat_id}:{media_group_id}"


def _load_media_groups() -> dict[str, Any]:
    try:
        data = json.loads(_MEDIA_GROUPS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log.warning("Failed to load Telegram media groups", exc_info=True)
        return {}

    if not isinstance(data, dict):
        return {}

    groups = data.get("groups")
    if isinstance(groups, dict):
        data = groups

    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _save_media_groups(groups: dict[str, Any]) -> None:
    try:
        if not groups:
            _MEDIA_GROUPS_PATH.unlink(missing_ok=True)
            return
        _MEDIA_GROUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MEDIA_GROUPS_PATH.write_text(
            json.dumps(groups, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        log.warning("Failed to save Telegram media groups", exc_info=True)


def _message_caption(message: Any) -> str | None:
    caption = getattr(message, "caption", None)
    if not caption:
        return None
    caption = reconstruct_code_markers(
        caption,
        getattr(message, "caption_entities", None),
    )
    caption = normalize_launch_xprompt_at_refs(caption)
    return caption if caption.strip() else None


def _safe_document_filename(file_name: str | None) -> str:
    candidate = Path(file_name or "image.jpg").name.strip()
    candidate = candidate.replace("\x00", "")
    candidate = re.sub(r"[^A-Za-z0-9._ -]+", "_", candidate).strip(" .")
    return candidate[:160] or "image.jpg"


def _make_document_image_filename(file_id: str, file_name: str | None) -> str:
    base = make_image_filename(file_id).removesuffix(".jpg")
    return f"{base}_{_safe_document_filename(file_name)}"


def _media_group_item_from_message(message: Any, kind: str) -> dict[str, Any] | None:
    if kind == "photo":
        photos = getattr(message, "photo", None) or []
        if not photos:
            return None
        file_id = getattr(photos[-1], "file_id", None)
        file_name = None
    elif kind == "document":
        document = getattr(message, "document", None)
        if document is None:
            return None
        file_id = getattr(document, "file_id", None)
        raw_name = getattr(document, "file_name", None)
        file_name = raw_name if isinstance(raw_name, str) else None
    else:
        return None

    if not isinstance(file_id, str) or not file_id:
        return None

    return {
        "message_id": _message_id(message),
        "kind": kind,
        "file_id": file_id,
        "file_name": file_name,
    }


def _media_group_items(group: dict[str, Any]) -> list[dict[str, Any]]:
    items = group.get("items")
    if not isinstance(items, list):
        return []

    valid_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        file_id = item.get("file_id")
        if kind not in {"photo", "document"}:
            continue
        if not isinstance(file_id, str) or not file_id:
            continue
        valid_items.append(item)

    return sorted(
        valid_items,
        key=lambda item: (
            item.get("message_id", 0) if isinstance(item.get("message_id"), int) else 0,
            str(item.get("file_id", "")),
        ),
    )


def _stage_media_group_image(message: Any, kind: str) -> bool:
    """Persist one photo/document image that belongs to a Telegram album."""
    if _telegram_agent_launches_disabled():
        log.info("Ignoring Telegram media group because agent launches are disabled")
        return False

    media_group_id = _media_group_id(message)
    chat_id = _context_chat_id(message)
    item = _media_group_item_from_message(message, kind)
    if media_group_id is None or chat_id is None or item is None:
        log.warning("Skipping malformed Telegram media group message")
        return False

    now = time.time()
    groups = _load_media_groups()
    key = _media_group_key(chat_id, media_group_id)
    group = groups.get(key)
    if not isinstance(group, dict):
        group = {
            "chat_id": chat_id,
            "media_group_id": media_group_id,
            "caption": None,
            "first_seen_at": now,
            "last_seen_at": now,
            "items": [],
        }

    group["chat_id"] = chat_id
    group["media_group_id"] = media_group_id
    group["last_seen_at"] = now
    caption = _message_caption(message)
    if caption and not group.get("caption"):
        group["caption"] = caption

    items = group.get("items")
    if not isinstance(items, list):
        items = []
    if not any(
        isinstance(existing, dict) and existing.get("file_id") == item["file_id"]
        for existing in items
    ):
        items.append(item)
    group["items"] = _media_group_items({"items": items})

    groups[key] = group
    _save_media_groups(groups)
    return True


def _download_media_group_item(item: dict[str, Any]) -> Path:
    file_id = str(item["file_id"])
    if item["kind"] == "document":
        file_name = item.get("file_name")
        filename = _make_document_image_filename(
            file_id,
            file_name if isinstance(file_name, str) else None,
        )
    else:
        filename = make_image_filename(file_id)

    dest = IMAGES_DIR / filename
    telegram_client.download_file(file_id, dest)
    return dest


def _launch_media_group(group: dict[str, Any]) -> None:
    items = _media_group_items(group)
    if not items:
        return

    raw_chat_id = group.get("chat_id")
    chat_id = str(raw_chat_id) if raw_chat_id is not None else credentials.get_chat_id()
    downloaded_paths: list[Path] = []
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        for item in items:
            dest = _download_media_group_item(item)
            downloaded_paths.append(dest)
            log.info("Downloaded media group image to %s", dest)
    except Exception as e:
        log.error("Failed to download Telegram media group: %s", e, exc_info=True)
        for path in downloaded_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("Failed to clean up %s", path, exc_info=True)
        try:
            telegram_client.send_message(
                chat_id,
                f"Failed to download image album: {e}",
            )
        except Exception:
            log.error("Failed to send media group error to Telegram", exc_info=True)
        return

    caption = group.get("caption")
    prompt = build_image_prompt(
        downloaded_paths,
        caption if isinstance(caption, str) else None,
    )
    _record_project_context(prompt, _MediaGroupMessageContext(chat_id=chat_id))
    _launch_agent(prompt)


def _flush_ready_media_groups() -> int:
    """Launch staged media groups whose quiet window has elapsed."""
    if _telegram_agent_launches_disabled():
        return 0

    groups = _load_media_groups()
    if not groups:
        return 0

    now = time.time()
    ready_keys: list[str] = []
    for key, group in groups.items():
        try:
            last_seen_at = float(group.get("last_seen_at", 0))
        except (TypeError, ValueError):
            last_seen_at = 0.0
        if now - last_seen_at >= _MEDIA_GROUP_QUIET_SECONDS:
            ready_keys.append(key)

    flushed = 0
    for key in ready_keys:
        group = groups.get(key)
        if isinstance(group, dict):
            _launch_media_group(group)
            flushed += 1
        groups.pop(key, None)
        _save_media_groups(groups)

    return flushed


def _handle_photo_message(message: Any) -> None:
    """Handle a photo message: download and launch agent."""
    if _telegram_agent_launches_disabled():
        log.info("Ignoring Telegram photo launch because agent launches are disabled")
        return

    photo = message.photo[-1]  # highest resolution
    file_id: str = photo.file_id
    filename = make_image_filename(file_id)
    dest = IMAGES_DIR / filename

    chat_id = credentials.get_chat_id()
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        telegram_client.download_file(file_id, dest)
        log.info("Downloaded photo to %s", dest)
    except Exception as e:
        log.error("Failed to download photo: %s", e, exc_info=True)
        telegram_client.send_message(chat_id, f"Failed to download photo: {e}")
        return

    caption = (
        reconstruct_code_markers(message.caption, message.caption_entities)
        if message.caption
        else message.caption
    )
    caption = normalize_launch_xprompt_at_refs(caption) if caption else caption
    prompt = build_photo_prompt(dest, caption)
    _record_project_context(prompt, message)
    _launch_agent(prompt)


def _handle_document_image(message: Any) -> None:
    """Handle an image sent as a document: download and launch agent."""
    if _telegram_agent_launches_disabled():
        log.info(
            "Ignoring Telegram document image launch because agent launches are disabled"
        )
        return

    doc = message.document
    file_id: str = doc.file_id
    original_name = doc.file_name or "image.jpg"
    ts = make_image_filename(file_id).split("_", 2)  # extract timestamp parts
    filename = f"{ts[0]}_{ts[1]}_{original_name}"
    dest = IMAGES_DIR / filename

    chat_id = credentials.get_chat_id()
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        telegram_client.download_file(file_id, dest)
    except Exception as e:
        telegram_client.send_message(chat_id, f"Failed to download image: {e}")
        return

    caption = (
        reconstruct_code_markers(message.caption, message.caption_entities)
        if message.caption
        else message.caption
    )
    caption = normalize_launch_xprompt_at_refs(caption) if caption else caption
    prompt = build_photo_prompt(dest, caption)
    _record_project_context(prompt, message)
    _launch_agent(prompt)
