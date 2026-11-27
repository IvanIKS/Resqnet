# Notifier is an object that can send a notification. This implements observer pattern.
class Notifier:
    def notify_about(self, disaster: Disaster):
        raise NotImplementedError


class NotificationService:
    def __init__(self):
        self.notifiers = []

    def register_notifier(self, notifier):
        self.notifiers.append(notifier)

    def notify_all(self, disaster: Disaster):
        for notifier in self.notifiers:
            notifier.notify_about(disaster)
