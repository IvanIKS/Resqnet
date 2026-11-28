"""Notification service.

Sends clients an alert about a declared alarm. The actual delivery channels
are defined by *notifiers* — each notifier implements one channel. The set of
registered notifiers decides by which means the alarm is broadcast.
"""

import logging
from typing import Protocol

from models import Disaster

logger = logging.getLogger("resqnet.notification")


class Notifier(Protocol):
    def send(self, disaster: Disaster) -> None: ...


class LogNotifier:
    """Baseline notifier: writes the alert to the application log."""

    def send(self, disaster: Disaster) -> None:
        logger.warning(
            "NOTIFY: alarm for disaster '%s' (id=%s)", disaster.name, disaster.id
        )


class NotificationService:
    """Broadcasts an alarm through every registered notifier."""

    def __init__(self, notifiers: list[Notifier] | None = None) -> None:
        # Default to the log notifier. Add SMS/push/etc. notifiers here.
        self.notifiers: list[Notifier] = (
            notifiers if notifiers is not None else [LogNotifier()]
        )

    def dispatch(self, disaster: Disaster) -> None:
        """Broadcast the alarm through every registered notifier."""
        for notifier in self.notifiers:
            notifier.send(disaster)
