"""Temporary BDS probe for comparing server inventory and submitted hotbar item."""
import argparse
import asyncio
import sys

from endpilot import Bot


async def run(url):
    bot = Bot("EndPilotTest")
    try:
        if url.startswith("raknet://"):
            host, _, port = url.removeprefix("raknet://").rpartition(":")
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        b = bot.protocol.binding

        def inspect(raw):
            if raw.packet_id == b.InventorySlotPacket.ID:
                pkt = bot.protocol.decode(raw)
                item = pkt.item
                print(f"slot_packet inventory={int(pkt.inventory_id)} slot={int(pkt.slot)} item={int(item.id)} count={int(item.stack_size)}", flush=True)
            elif raw.packet_id == b.InventoryContentPacket.ID:
                pkt = bot.protocol.decode(raw)
                if int(pkt.inventory_id) == int(b.ContainerID.INVENTORY):
                    print(f"content_inventory id={int(pkt.inventory_id)} n={len(pkt.slots)} slots0and2={[(int(pkt.slots[i].id),int(pkt.slots[i].stack_size)) for i in (0,2)]}", flush=True)
            elif raw.packet_id == b.MobEquipmentPacket.ID:
                pkt = bot.protocol.decode(raw)
                print(f"server_equipment slot={int(pkt.selected_slot)} item={int(pkt.item.id)}", flush=True)

        unsubscribe = bot.packets.subscribe_raw(inspect)
        try:
            print("spawned EndPilotTest", flush=True)
            for slot in (0, 2):
                await asyncio.to_thread(sys.stdin.readline)
                descriptor = await bot._inventory.wait_for_slot(slot)
                print(f"client_slot_before_select={slot} item={int(descriptor.id)} count={int(descriptor.stack_size)} variant={descriptor.net_id_variant}", flush=True)
                await bot.select_hotbar(slot)
                print(f"selected_hotbar_slot={slot}", flush=True)
            await asyncio.to_thread(sys.stdin.readline)
        finally:
            unsubscribe()
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    args = parser.parse_args()
    asyncio.run(run(args.url))
