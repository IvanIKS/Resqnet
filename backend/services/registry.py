"""Service registry.

Holds the application's long-lived service singletons and wires their
dependencies together. Services are constructed once at startup via
``init_services`` and resolved thereafter through typed accessors — including
the FastAPI dependency providers used by the routes.
"""

from typing import Type, TypeVar

from fastapi import HTTPException

from services.alarm import AlarmService
from services.notification import NotificationService

T = TypeVar("T")


class ServiceRegistry:
    """A simple type-keyed container of initialized service instances."""

    def __init__(self) -> None:
        self._services: dict[type, object] = {}

    def register(self, key: Type[T], service: T) -> None:
        self._services[key] = service

    def get(self, key: Type[T]) -> T:
        service = self._services.get(key)
        if service is None:
            raise RuntimeError(
                f"{key.__name__} is not registered; call init_services() at startup"
            )
        return service  # type: ignore[return-value]


# The process-wide registry, populated by init_services() during app startup.
registry = ServiceRegistry()


def init_services() -> None:
    """Construct the service singletons and register them.

    Dependencies are wired here: the notification service is built first, then
    injected into the alarm service that depends on it.
    """
    notification = NotificationService()
    alarm = AlarmService(notification)

    registry.register(NotificationService, notification)
    registry.register(AlarmService, alarm)


def get_alarm_service() -> AlarmService:
    """FastAPI dependency that resolves the initialized AlarmService."""
    try:
        return registry.get(AlarmService)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
