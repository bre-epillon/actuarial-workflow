"""Teams notifications via a Workflows ("Post to a channel when a webhook
request is received") webhook, using an Adaptive Card.

Payload shape follows Microsoft's documented webhook format at the time of
writing; Microsoft has been changing Teams webhooks, so verify if it fails.
"""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)


def build_payload(title: str, text: str, facts: dict[str, str]) -> dict:
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "FactSet", "facts": [{"title": k, "value": str(v)} for k, v in facts.items()]},
            {"type": "TextBlock", "text": text or "(no summary)", "wrap": True},
        ],
    }
    return {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}],
    }


def notify(webhook_url: str | None, title: str, text: str, facts: dict[str, str]) -> None:
    log.info("NOTIFY %s | %s | %s", title, facts, text.replace("\n", " ")[:300])
    if not webhook_url:
        return
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(build_payload(title, text, facts)).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 300:
                log.warning("Teams webhook returned HTTP %s", resp.status)
    except Exception:
        log.exception("Teams notification failed (job is unaffected)")
