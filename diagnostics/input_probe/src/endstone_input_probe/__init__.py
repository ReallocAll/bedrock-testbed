"""Temporary read-only BDS input receiver probe for Draft PR diagnostics."""

from endstone.event import PacketReceiveEvent, event_handler
from endstone.plugin import Plugin


class InputProbe(Plugin):
    api_version = "0.11"

    def on_enable(self):
        self._received = 0
        self.register_events(self)
        self.logger.info("AUTH_INPUT_PROBE_READY")

    @event_handler
    def on_packet(self, event: PacketReceiveEvent):
        player = event.player
        if event.packet_id != 144 or player is None or player.name != "EndPilotTest":
            return
        self._received += 1
        if self._received <= 500:
            self.logger.info(
                f"AUTH_INPUT_RX seq={self._received} size={len(event.payload)} "
                f"flags={int(player.is_sneaking)}/{int(player.is_sprinting)} "
                f"payload={event.payload.hex()}"
            )
