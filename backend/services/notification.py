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


# Registered delivery channels. Add SMS/push/etc. notifiers here.
NOTIFIERS: list[Notifier] = [LogNotifier()]


def dispatch(disaster: Disaster) -> None:
    """Broadcast the alarm through every registered notifier."""
    for notifier in NOTIFIERS:
        notifier.send(disaster)
