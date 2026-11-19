"""Temporary BDS-side packet ingress probe for new EndPilot movement."""
from endstone.event import PacketReceiveEvent, PlayerMoveEvent, event_handler
from endstone.plugin import Plugin


class InputTrace(Plugin):
    api_version = "0.11"

    def on_enable(self):
        self.frames = 0
        self.register_events(self)
        self.logger.info("InputTrace enabled")

    @event_handler
    def on_receive(self, event: PacketReceiveEvent):
        if event.packet_id == 144:
            self.frames += 1
            self.logger.info(f"PlayerAuthInput incoming frame={self.frames} payload_length={len(event.payload)}")

    @event_handler
    def on_move(self, event: PlayerMoveEvent):
        self.logger.info(f"PlayerMoveEvent {event.player.name}")
