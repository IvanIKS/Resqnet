

class AlarmService:
    def __init__(self, notifications_service):
        self.active_disasters = []
        self.notifications_service = notifications_service

    def is_alarm_active(self):
        return len(self.active_disasters) > 0

    def add_disaster(self, disaster : Disaster):
        self.active_disasters.append(disaster)
        self.notifications_service.notify_all(disaster)
