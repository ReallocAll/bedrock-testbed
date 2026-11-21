"""Testbed-only form sender with an independent server-side answer callback."""

from endstone.event import PlayerChatEvent, event_handler
from endstone.form import ActionForm, Button
from endstone.plugin import Plugin


class FormProbe(Plugin):
    api_version = "0.11"
    prefix = "FormProbe"

    def on_enable(self):
        self.register_events(self)
        self.logger.info("form_probe_enabled")

    @event_handler
    def on_chat(self, event: PlayerChatEvent):
        if event.player.name != "EndPilotTest" or event.message != "endpilot-form-probe":
            return
        event.player.send_form(ActionForm(
            title="EndPilot probe",
            content="Choose a server-side action",
            buttons=[Button(text="Cancel"), Button(text="Confirm")],
            on_submit=self.on_submit,
        ))
        self.logger.info("form_probe_sent=EndPilotTest")

    def on_submit(self, player, button_index: int):
        self.logger.info(f"form_probe_submit={player.name}:{button_index}")
