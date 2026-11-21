"""Passive server-side verification of the player's held sneaking state."""

from endstone.event import PlayerChatEvent, event_handler
from endstone.plugin import Plugin


class StanceProbe(Plugin):
    api_version = "0.11"
    prefix = "StanceProbe"

    def on_enable(self):
        self.register_events(self)
        self.logger.info("stance_probe_enabled")

    @event_handler
    def on_chat(self, event: PlayerChatEvent):
        if (event.player.name == "EndPilotTest"
                and event.message in ("endpilot-sneak-on", "endpilot-sneak-off")):
            self.logger.info(
                f"stance_probe={event.message}:{str(event.player.is_sneaking).lower()}"
            )
