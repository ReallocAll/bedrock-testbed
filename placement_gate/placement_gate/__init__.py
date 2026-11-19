"""Testbed-only Endstone listener: reject one player placement, allow the next."""

from endstone.event import BlockPlaceEvent, event_handler
from endstone.plugin import Plugin


class PlacementGate(Plugin):
    api_version = "0.11"
    prefix = "PlacementGate"

    def on_enable(self):
        self._count = 0
        self.register_events(self)
        self.logger.info("placement_gate_enabled")

    @event_handler
    def on_placement(self, event: BlockPlaceEvent):
        self._count += 1
        refused = self._count == 1
        if refused:
            event.cancel()
        self.logger.info(f"placement_gate_event={self._count} cancelled={int(refused)}")
