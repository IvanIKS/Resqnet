"""Alarm service.

Declares the fact of an alarm for a disaster and kicks off client
notification via the notification service.
"""

import logging

from models import Disaster
from services.notification import NotificationService

logger = logging.getLogger("resqnet.alarm")


class AlarmService:
    """Declares alarms and delegates delivery to the notification service."""

    def __init__(self, notification: NotificationService) -> None:
        self._notification = notification

    def raise_alarm(self, disaster: Disaster) -> None:
        """Declare an alarm for the given disaster and trigger notifications."""
        logger.warning(
            "ALARM raised for disaster '%s' (id=%s)", disaster.name, disaster.id
        )
        self._notification.dispatch(disaster)
