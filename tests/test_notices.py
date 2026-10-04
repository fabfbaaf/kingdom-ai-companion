"""Event notices use synthetic frames only and have no control/model authority."""

import asyncio
from datetime import UTC, datetime, timedelta

from test_companion import state_data

from companion.contracts import GameState
from companion.controller import ControlLease
from companion.notices import NoticeService


class Manager:
    def __init__(self):
        self.data = state_data()
        self.data["world"] = {"nearby_enemies": [], "is_night": False, "is_paused": False}
        self.data["players"][1]["can_sprint"] = True
        self.generation = 1
        self.lease = ControlLease("test", self.data["session_id"], self.data["control_epoch"])
        self.running = True
        self.coins = 4

    def status(self):
        return {"running": self.running, "coin_budget_remaining": self.coins}

    async def _state(self):
        return GameState.model_validate(self.data)

    def advance(self):
        self.data["observation_seq"] += 1
        self.data["captured_at"] = datetime.now(UTC).isoformat()


class Dialogue:
    def __init__(self):
        self.messages = []
        self.busy = False

    def public(self):
        return {"busy": self.busy, "queued": 0}

    async def notify(self, text):
        self.messages.append(text)


def test_notices_are_opt_in_and_repeat_limited():
    async def scenario():
        manager, dialogue = Manager(), Dialogue()
        now = [0]
        service = NoticeService(manager, dialogue, clock=lambda: now[0])
        await service.tick()
        assert not dialogue.messages
        await service.configure(True)
        await service.tick()
        manager.advance()
        manager.data["world"]["nearby_enemies"] = [{"x": 2}, {"x": 3}]
        await service.tick()
        assert len(dialogue.messages) == 1 and service.last_event == "enemy"
        for moment in (20, 61):
            manager.advance()
            manager.data["world"]["nearby_enemies"] = []
            await service.tick()
            now[0] = moment
            manager.advance()
            manager.data["world"]["nearby_enemies"] = [{"x": 2}, {"x": 3}]
            await service.tick()
        assert len(dialogue.messages) == 2
        await service.close()
        manager.advance()
        manager.data["world"]["is_night"] = True
        await service.tick()
        assert len(dialogue.messages) == 2

    asyncio.run(scenario())


def test_busy_dialogue_and_stale_or_foreign_frame_cannot_trigger_notice():
    async def scenario():
        manager, dialogue = Manager(), Dialogue()
        service = NoticeService(manager, dialogue)
        await service.configure(True)
        await service.tick()
        dialogue.busy = True
        manager.advance()
        manager.data["world"]["nearby_enemies"] = 1
        await service.tick()
        dialogue.busy = False
        manager.advance()
        manager.data["players"][1]["can_sprint"] = False
        manager.data["captured_at"] = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        await service.tick()
        manager.advance()
        manager.data["session_id"] = "foreign"
        await service.tick()
        assert not dialogue.messages
        await service.close()

    asyncio.run(scenario())


def test_quiet_period_global_cooldown_and_budget_event():
    async def scenario():
        manager, dialogue = Manager(), Dialogue()
        now = [0]
        service = NoticeService(manager, dialogue, clock=lambda: now[0])
        await service.configure(True)
        await service.tick()
        service.dialogue_activity()
        manager.advance()
        manager.data["players"][1]["can_sprint"] = False
        await service.tick()
        assert not dialogue.messages
        now[0] = 10
        manager.advance()
        manager.data["world"]["is_night"] = True
        await service.tick()
        assert service.last_event == "night"
        now[0] = 12
        manager.advance()
        manager.coins = 0
        await service.tick()
        assert len(dialogue.messages) == 1
        now[0] = 30
        manager.advance()
        manager.coins = 1
        await service.tick()
        manager.advance()
        manager.coins = 0
        await service.tick()
        assert service.last_event == "budget" and len(dialogue.messages) == 2
        await service.close()

    asyncio.run(scenario())
