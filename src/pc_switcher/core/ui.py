from pc_switcher.core.events import EventBus
from pc_switcher.config import LogLevel


class TerminalUI:
    def __init__(self, event_bus: EventBus, log_level: str | LogLevel):
        self.event_bus = event_bus
        self.log_level = log_level

    def start(self):
        pass

    def stop(self):
        pass

    async def run(self):
        pass
