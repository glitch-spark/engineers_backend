"""Slack client for the callers' workspace (a second bot token)."""

import logging
from typing import Any, Optional

import httpx

from app import slack_bot
from app.config import get_settings

log = logging.getLogger(__name__)
_TIMEOUT = 15.0


def callers_configured() -> bool:
    return bool((get_settings().SLACK_CALLERS_BOT_TOKEN or "").strip())


async def api(method: str, body: dict[str, Any]) -> dict[str, Any]:
    return await slack_bot._slack_api(method, body, workspace="callers")


async def upload_files(channel: str, thread_ts: str, files: list[tuple[str, bytes]], comment: str) -> None:
    """Upload files into a thread: getUploadURLExternal (form) + raw PUT per file, then one completeUploadExternal."""
    token = (get_settings().SLACK_CALLERS_BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("SLACK_CALLERS_BOT_TOKEN is not configured")
    done: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for filename, data in files:
            resp = await client.post(
                f"{slack_bot._SLACK_API}/files.getUploadURLExternal",
                headers={"Authorization": f"Bearer {token}"},
                data={"filename": filename, "length": len(data)},
            )
            payload = resp.json() if resp.content else {}
            if not payload.get("ok"):
                raise RuntimeError(payload.get("error") or f"files.getUploadURLExternal HTTP {resp.status_code}")
            up = await client.put(payload["upload_url"], content=data)
            if up.status_code >= 300:
                raise RuntimeError(f"file upload HTTP {up.status_code}")
            done.append({"id": payload["file_id"], "title": filename})
    await api("files.completeUploadExternal", {
        "channel_id": channel, "thread_ts": thread_ts, "initial_comment": comment, "files": done,
    })


async def workspace_name() -> Optional[str]:
    if not callers_configured():
        return None
    try:
        return (await api("auth.test", {})).get("team") or None
    except Exception as exc:
        log.warning("callers workspace auth.test failed: %s", exc)
        return None
