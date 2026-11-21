"""Testbed-only passive observer for actor actions and mob damage."""
from endstone.event import ActorDamageEvent, PacketReceiveEvent, PlayerInteractActorEvent, event_handler
from endstone.plugin import Plugin


class CombatProbe(Plugin):
    api_version = "0.11"
    prefix = "CombatProbe"

    def on_enable(self):
        self._capture_swing_input = False
        self.register_events(self)
        self.logger.info("combat_probe_enabled")

    @event_handler
    def on_received(self, event: PacketReceiveEvent):
        if event.packet_id == 44:
            self.logger.info(f"swing_animate_received={len(event.payload)}")
            self._capture_swing_input = True
        elif event.packet_id == 144 and self._capture_swing_input:
            # Save one post-animation input for decoding with the pinned SDK codec.
            self._capture_swing_input = False
            self.logger.info(f"swing_input_payload={bytes(event.payload).hex()}")
        if event.packet_id == 30:
            self.logger.info(f"combat_packet_received={len(event.payload)}")

    @event_handler
    def on_damage(self, event: ActorDamageEvent):
        source = event.damage_source.actor
        self.logger.info(f"combat_damage={event.actor.name}:{event.damage} attacker={getattr(source, 'name', None)}")

    @event_handler
    def on_interact(self, event: PlayerInteractActorEvent):
        self.logger.info(f"combat_interact={event.actor.name} player={event.player.name}")
