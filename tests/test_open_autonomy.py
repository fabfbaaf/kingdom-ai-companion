"""Autonomous coin decisions and automatic feedback recovery, using local fakes."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from test_autonomous_play import SequenceModel, continuous, move
from test_companion import FakeBridge, manager_for, state_data, store_for

from companion.bridge import BridgeError
from companion.contracts import Decision, GameState, Receipt, StartRequest
from companion.model import ModelClient


def test_wallet_can_spend_more_than_old_total_limit_without_new_authorization(tmp_path):
    async def scenario():
        pay = Decision(operation="pay", target_id="wall-1", max_coins=3)
        model = SequenceModel([pay] * 9 + [move()])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        bridge.data["players"][1]["coins"] = 30
        try:
            await manager.start(continuous(spending_mode="wallet", play_style="independent"))
            await asyncio.wait_for(model.exhausted.wait(), 3)
            assert len(bridge.commands) == 10
            assert bridge.data["players"][1]["coins"] == 3
            assert manager.coin_budget is None and all(value is None for value in model.budgets)
            assert manager.status()["spending_mode"] == "wallet"
            assert manager.gameplay_context()["spending_mode"] == "wallet"
            await manager.change_goal("巡视右边")
            assert manager.spending_mode == "wallet" and manager.coin_budget is None
            assert not manager.status()["needs_review"]
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_wallet_still_uses_real_balance_and_follow_has_no_spending(tmp_path):
    async def scenario():
        pay = Decision(operation="pay", target_id="wall-1", max_coins=3)
        model = SequenceModel([pay, move()])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        bridge.data["players"][1]["coins"] = 2
        try:
            await manager.start(continuous(spending_mode="wallet"))
            await asyncio.wait_for(model.exhausted.wait(), 1)
            assert [body["operation"] for body in bridge.commands] == ["move"]
            assert manager.status()["running"]
            await manager.stop()
            await manager.start(StartRequest(mode="follow", spending_mode="wallet"))
            assert manager.spending_mode == "budgeted" and manager.coin_budget == 0
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_no_motion_and_wallet_change_do_not_require_manual_unlock(tmp_path):
    async def scenario():
        model = SequenceModel([move(), Decision(operation="move", direction="left", duration_ms=300)])
        manager, bridge, _ = manager_for(tmp_path, model=model)
        bridge.effect = False
        try:
            await manager.start(continuous(spending_mode="wallet"))
            await asyncio.wait_for(model.exhausted.wait(), 1)
            assert len(bridge.commands) == 2 and manager.status()["running"]
            assert manager.last_result["status"] == "completed"
            assert manager.last_result["before"] == manager.last_result["after"]
            assert not manager.status()["needs_review"] and manager.journal.pending is None
        finally:
            await manager.stop()
    asyncio.run(scenario())


class RecoverBridge(FakeBridge):
    def __init__(self, result="failed"):
        super().__init__()
        self.result = result
        self.last_receipt = None

    async def command(self, body):
        if not self.commands:
            self.commands.append(body)
            if self.result == "unknown":
                raise BridgeError("synthetic response lost")
            self.data["control_epoch"] += 1
            self.last_receipt = Receipt(action_id=body["action_id"], session_id=body["session_id"],
                                        status=self.result, message="synthetic native result")
            return self.last_receipt
        return await super().command(body)

    async def receipt(self, _action_id):
        return self.last_receipt


@pytest.mark.parametrize("result", ["failed", "unknown"])
def test_failed_or_lost_action_recovers_and_replans_without_changing_authority(tmp_path, result):
    async def scenario():
        bridge = RecoverBridge(result)
        model = SequenceModel([move(), Decision(operation="move", direction="left", duration_ms=300)])
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        try:
            await manager.start(continuous(spending_mode="wallet", play_style="independent"))
            await asyncio.wait_for(model.exhausted.wait(), 1.5)
            assert manager.status()["running"] and len(bridge.commands) == 2
            assert bridge.commands[0]["control_id"] != bridge.commands[1]["control_id"]
            assert manager.play_style == "independent" and manager.spending_mode == "wallet"
            assert manager.coin_budget is None and manager.error is None
            assert manager.recent_actions[0]["status"] == result
            assert manager.journal.pending is None and not manager.status()["needs_review"]
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_native_cancel_or_f8_is_never_automatically_overridden(tmp_path):
    async def scenario():
        bridge = RecoverBridge("cancelled")
        model = SequenceModel([move(), move()])
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(continuous(spending_mode="wallet"))
        await asyncio.wait_for(manager.task, 1)
        assert manager.mode == "idle" and len(bridge.commands) == 1 and model.calls == 1
        assert not manager.status()["needs_review"] and manager.journal.pending is None
    asyncio.run(scenario())


def test_failed_native_payment_can_recover_after_old_heartbeat_is_rejected(tmp_path):
    class DelayedFailureBridge(RecoverBridge):
        async def command(self, body):
            first = not self.commands
            receipt = await super().command(body)
            if first:
                await asyncio.sleep(0.6)
            return receipt

    async def scenario():
        bridge = DelayedFailureBridge()
        model = SequenceModel([Decision(operation="pay", target_id="wall-1", max_coins=3), move()])
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        try:
            await manager.start(continuous(spending_mode="wallet"))
            await asyncio.wait_for(model.exhausted.wait(), 2)
            assert manager.status()["running"] and manager.error is None
            assert len(bridge.commands) == 2 and bridge.heartbeats >= 3
            assert not manager.status()["needs_review"]
        finally:
            await manager.stop()
    asyncio.run(scenario())


def test_emergency_stop_during_recovery_prevents_a_new_control_grant(tmp_path):
    class DelayedRecoveryBridge(RecoverBridge):
        def __init__(self):
            super().__init__()
            self.recovering = asyncio.Event()

        async def stop(self):
            if self.stops == 0:
                self.stops += 1
                self.recovering.set()
                await asyncio.Event().wait()
            await super().stop()

    async def scenario():
        bridge = DelayedRecoveryBridge()
        model = SequenceModel([move(), move()])
        manager, _, _ = manager_for(tmp_path, bridge=bridge, model=model)
        await manager.start(continuous(spending_mode="wallet"))
        await asyncio.wait_for(bridge.recovering.wait(), 1)
        await manager.stop()
        assert manager.mode == "idle" and manager.lease is None
        assert len(bridge.commands) == 1 and model.calls == 1
        assert all(item["control_epoch"] == 0 for item in bridge.heartbeat_leases)
        assert manager.journal.pending is None
    asyncio.run(scenario())


def test_lost_receipt_after_external_control_revocation_does_not_resume(tmp_path):
    class RevokedBridge(RecoverBridge):
        async def command(self, body):
            self.data["control_epoch"] += 1
            return await super().command(body)

    async def scenario():
        bridge = RevokedBridge("unknown")
        manager, _, model = manager_for(tmp_path, bridge=bridge, model=SequenceModel([move(), move()]))
        await manager.start(continuous(spending_mode="wallet"))
        await asyncio.wait_for(manager.task, 1)
        assert manager.mode == "idle" and len(bridge.commands) == 1 and model.calls == 1
        assert "控制已撤销" in manager.error and not manager.status()["needs_review"]
    asyncio.run(scenario())


def test_wallet_protocol_and_prompt_distinguish_unlimited_from_zero_budget(tmp_path):
    assert StartRequest(mode="autonomous").spending_mode == "wallet"
    with pytest.raises(ValidationError):
        StartRequest(mode="autonomous", spending_mode="anything")

    async def scenario():
        captured = []
        def handler(request):
            payload = json.loads(request.content)
            captured.append(payload)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"operation":"stop"}'}}]})
        model = ModelClient(store_for(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        model.store.settings.model = "mock"
        try:
            await model.decide(GameState.model_validate(state_data()), goal="经营营地", coin_budget=None)
            packet = json.loads(captured[0]["messages"][-1]["content"])
            assert packet["spending_mode"] == "wallet" and packet["remaining_coin_budget"] is None
            assert "不代表禁止付款" in captured[0]["messages"][0]["content"]
            assert "不再要求玩家人工解锁" in captured[0]["messages"][0]["content"]
        finally:
            await model.close()
    asyncio.run(scenario())
