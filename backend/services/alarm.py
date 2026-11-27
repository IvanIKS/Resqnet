"""Alarm service.

Declares the fact of an alarm for a disaster and kicks off client
notification via the notification service.
"""

import logging

from models import Disaster
from services import notification

logger = logging.getLogger("resqnet.alarm")


def raise_alarm(disaster: Disaster) -> None:
    """Declare an alarm for the given disaster and trigger notifications."""
    logger.warning(
        "ALARM raised for disaster '%s' (id=%s)", disaster.name, disaster.id
    )
    notification.dispatch(disaster)
