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
        source, target = event.from_location, event.to_location
        self.logger.info(
            f"PlayerMoveEvent {event.player.name} "
            f"from=({source.x:.3f},{source.y:.3f},{source.z:.3f}) "
            f"to=({target.x:.3f},{target.y:.3f},{target.z:.3f}) "
            f"cancelled={event.is_cancelled}"
        )
