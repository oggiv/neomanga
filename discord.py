"""Discord notifications (webhook-based, isolated for later configuration).

The webhook URL is never hard-coded.  It is resolved, in order, from:

1. the ``DISCORD_WEBHOOK_URL`` environment variable, or
2. the ``discord_webhook_url`` key in ``manga.json``.

If neither is set, notifications are silently skipped (logged at debug).
A failed notification never deletes or otherwise affects downloaded files.
"""

import logging
import os

import requests

log = logging.getLogger("discord")


class DiscordNotifier:
    def __init__(self, webhook_url=None):
        self.webhook_url = webhook_url or os.environ.get(
            "DISCORD_WEBHOOK_URL", ""
        ).strip()

    @property
    def enabled(self):
        return bool(self.webhook_url)

    def notify_chapter_downloaded(self, title_name, chapter_number):
        """Send "Chapter #N - Title has been downloaded and is available."

        Returns True on success.  Never raises.
        """
        if not self.enabled:
            log.debug("Discord not configured; skipping notification")
            return False
        text = (
            "Chapter #%s - %s has been downloaded and is available."
            % (chapter_number, title_name)
        )
        try:
            response = requests.post(
                self.webhook_url, json={"content": text}, timeout=15
            )
        except requests.RequestException as exc:
            log.warning("Discord notification failed: %s", exc)
            return False
        if not response.ok:
            log.warning(
                "Discord notification HTTP %s", response.status_code
            )
            return False
        log.info("Discord notification sent: %s", text)
        return True
