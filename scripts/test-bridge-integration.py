"""Real Python/C# HTTP protocol smoke test; all game state is synthetic."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import httpx

from companion.bridge import BridgeClient, BridgeError
from companion.config import ModelConfigStore
from companion.contracts import Decision
from companion.controller import ActionJournal, ControlManager
from companion.model import ModelClient


def spare_port() -> int:
    while True:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port not in {48860, 48861}:
            return port


class Checks:
    def __init__(self) -> None:
        self.passed: list[str] = []

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)
        self.passed.append(message)


async def smoke(config: Path, fixture: Path, process: subprocess.Popen, checks: Checks) -> None:
    model_calls = 0

    def forbid_model(_request: httpx.Request) -> httpx.Response:
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("The offline smoke test must never request a model")

    bridge = BridgeClient(config)
    store = ModelConfigStore(fixture / "model.local.json")
    model = ModelClient(store, httpx.AsyncClient(transport=httpx.MockTransport(forbid_model)))
    manager = ControlManager(bridge, model, store, ActionJournal(fixture / "session.local.json"))

    async def state_when(predicate, *, timeout: float = 5):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if process.poll() is not None:
                raise AssertionError("The synthetic C# host exited unexpectedly; inspect its temporary log")
            try:
                state = await bridge.state()
                if predicate(state):
                    return state
            except BridgeError:
                pass
            await asyncio.sleep(0.025)
        raise AssertionError("Timed out waiting for a synthetic bridge state")

    async def conflict(operation, message: str) -> None:
        try:
            await operation
        except BridgeError as exc:
            checks.check("HTTP 409" in str(exc), message)
        else:
            raise AssertionError(message)

    try:
        initial = await state_when(lambda state: state.ready and state.coop, timeout=8)
        checks.check(initial.scene == "SyntheticOfflineIsland", "Connected to the synthetic C# host")
        checks.check(initial.control_epoch >= 1, "C# publishes the engine's current control epoch")
        checks.check(initial.player(1).coins == 8, "Python parses P2's initial eight-coin wallet")
        checks.check(initial.player(1).current_payable.price == 3, "Python parses the three-coin current target")
        checks.check(initial.input_released, "The synthetic host starts without held input")
        await asyncio.sleep(0.04)
        updated = await bridge.state()
        checks.check(updated.observation_seq > initial.observation_seq, "Observations advance over real HTTP")
        checks.check(updated.captured_at > initial.captured_at, "C# publishes fresh UTC capture times")

        moved = await manager.manual(Decision(operation="move", direction="right", duration_ms=200))
        after_move = await bridge.state()
        checks.check(moved["status"] == "completed", "ControlManager receives completion of a short right move")
        checks.check(after_move.player(1).x - initial.player(1).x > 0.02, "The C# synthetic P2 position changes right")
        checks.check(after_move.player(0).x == initial.player(0).x, "P1's synthetic position remains untouched")
        checks.check(after_move.input_released, "Manual move finishes with confirmed input release")
        checks.check(after_move.control_epoch > initial.control_epoch, "Manual completion rotates the C# epoch")
        checks.check(manager.journal.pending is None, "A verified movement clears the real action journal")

        sprinted = await manager.manual(Decision(operation="move", direction="right",
                                                 duration_ms=2000, sprint=True))
        after_sprint = await bridge.state()
        checks.check(sprinted["status"] == "completed", "Actual Python/C# protocol completes a two-second sprint")
        checks.check(after_sprint.player(1).x - after_move.player(1).x > 12,
                     "Synthetic sprint input has a greater distance than synthetic walking")
        checks.check(after_sprint.player(0).x == initial.player(0).x, "Sprint still isolates P1")
        checks.check(after_sprint.input_released and manager.journal.pending is None,
                     "Long sprint finishes with released input and verified journal")

        paid = await manager.manual(Decision(operation="pay",
                                              target_id=after_move.player(1).current_payable.target_id,
                                              max_coins=3))
        after_pay = await bridge.state()
        checks.check(paid["status"] == "completed", "ControlManager receives a full three-coin payment completion")
        checks.check(after_pay.player(1).coins == 5, "Full payment consumes exactly three synthetic coins")
        checks.check(after_pay.player(1).current_payable is None, "The completed synthetic target disappears")
        checks.check(after_pay.player(1).transaction_pending is False, "Payment reports a settled native-style transaction")
        checks.check(after_pay.input_released, "Payment finishes with a fresh input release observation")
        checks.check(manager.coin_budget is None, "manual native payment has no accumulated spending limit")
        checks.check(manager.journal.pending is None, "A verified payment clears the action journal")
        payment_receipt = await bridge.receipt(paid["action_id"])
        checks.check(payment_receipt.status == "completed" and payment_receipt.after["coins"] == 5,
                     "Production C# receipt JSON matches Python's receipt contract")

        long_move = asyncio.create_task(manager.manual(
            Decision(operation="move", direction="left", duration_ms=5000, sprint=True)))
        moving = await state_when(lambda state: not state.input_released
                                 and state.player(1).x < after_pay.player(1).x - 0.03)
        old_lease = manager.lease
        checks.check(old_lease is not None, "Active manual control owns a frozen lease")
        stopped = await manager.stop()
        checks.check(stopped["release_error"] is None, "Actual ControlManager stop confirms main-thread-style release")
        interrupted = await asyncio.wait_for(long_move, timeout=3)
        checks.check(interrupted["status"] == "cancelled", "Stop cancels the in-flight Python manual task")
        released = await bridge.state()
        checks.check(released.input_released, "HTTP stop produces a fresh released C# observation")
        checks.check(released.control_epoch > old_lease.control_epoch, "Stopping revokes the previous control epoch")
        checks.check(released.captured_at > moving.captured_at, "Stop verification reads a newly captured state")
        await asyncio.sleep(0.2)
        still = await bridge.state()
        checks.check(abs(still.player(1).x - released.player(1).x) < 0.0001, "No synthetic movement remains held after stop")
        checks.check(manager.journal.pending is None, "Interrupted action does not require human review")

        fresh_id = str(uuid.uuid4())
        await conflict(bridge.heartbeat(control_id=fresh_id, session_id=released.session_id,
                                        control_epoch=old_lease.control_epoch),
                       "A delayed first heartbeat carrying the old epoch receives HTTP 409")
        await conflict(bridge.heartbeat(control_id=old_lease.control_id,
                                        session_id=released.session_id,
                                        control_epoch=released.control_epoch),
                       "A revoked control ID cannot return using the current epoch")
        await bridge.heartbeat(control_id=fresh_id, session_id=released.session_id,
                               control_epoch=released.control_epoch)
        old_command = {"action_id": str(uuid.uuid4()), "session_id": released.session_id,
                       "control_id": fresh_id, "control_epoch": old_lease.control_epoch,
                       "player_id": 1, "operation": "move", "direction": "right", "duration_ms": 150}
        await conflict(bridge.command(old_command),
                       "A fresh action ID carrying an old epoch receives HTTP 409")
        await conflict(bridge.heartbeat(control_id=fresh_id, session_id="previous-synthetic-session",
                                        control_epoch=released.control_epoch),
                       "A heartbeat from an old session receives HTTP 409")

        valid_command = {**old_command, "action_id": str(uuid.uuid4()),
                         "control_epoch": released.control_epoch, "direction": "left"}
        receipt = await bridge.command(valid_command)
        checks.check(receipt.status in {"queued", "running"}, "A new lease using the current epoch is accepted")
        deadline = asyncio.get_running_loop().time() + 3
        while receipt.status in {"queued", "running"} and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            receipt = await bridge.receipt(valid_command["action_id"])
        checks.check(receipt.status == "completed", "Current-epoch command completes through the real receipt endpoint")
        await bridge.stop()
        final = await bridge.state()
        checks.check(final.input_released, "Final test cleanup confirms input release over HTTP")
        checks.check(final.player(1).coins == 5, "Rejected stale commands do not repeat payment")
        checks.check(model_calls == 0, "No model HTTP request occurred")
    finally:
        await manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dotnet", type=Path, default=PROJECT / ".tools/dotnet/dotnet.exe")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    host_project = PROJECT / "mod/tests/KingdomAI.IntegrationHost/KingdomAI.IntegrationHost.csproj"
    host_dll = host_project.parent / "bin/Release/net8.0/KingdomAI.IntegrationHost.dll"
    if not args.dotnet.is_file():
        parser.error("The project .NET 8 SDK/runtime is missing; pass --dotnet explicitly")
    if not args.skip_build:
        subprocess.run([str(args.dotnet), "build", str(host_project), "-c", "Release", "--nologo", "-m:1"],
                       cwd=PROJECT, check=True)
    if not host_dll.is_file():
        parser.error("Build the synthetic host first")
    source_hashes = {name: hashlib.sha256((PROJECT / "mod/src/KingdomAI.Bridge" / name).read_bytes()).hexdigest()
                     for name in ("Models.cs", "CommandEngine.cs", "BridgeServer.cs")}
    fixture = Path(tempfile.mkdtemp(prefix="kingdom-ai-bridge-integration-"))
    config = fixture / "bridge.local.json"
    stop_file = fixture / "host.stop"
    config.write_text(json.dumps({"base_url": f"http://127.0.0.1:{spare_port()}",
                                  "token": secrets.token_urlsafe(32)}), encoding="utf-8")
    environment = {**os.environ, "DOTNET_ROOT": str(args.dotnet.resolve().parent)}
    checks = Checks()
    result = "failed"
    with (fixture / "synthetic-host.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen([str(args.dotnet), str(host_dll), "--bridge-config", str(config),
                                    "--stop-file", str(stop_file)], cwd=PROJECT, env=environment,
                                   stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                   text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            asyncio.run(smoke(config, fixture, process, checks))
            result = "passed"
        finally:
            stop_file.write_text("exit synthetic host", encoding="utf-8")
            if process.poll() is None:
                try:
                    process.communicate("exit\n", timeout=5)
                except subprocess.TimeoutExpired:
                    # Only terminate the exact synthetic subprocess created above.
                    process.terminate()
                    process.wait(timeout=5)
            if process.returncode == 0:
                checks.check(True, "The exact synthetic host subprocess exits cleanly after release")
            else:
                result = "failed"
            report = {"result": result, "passed": len(checks.passed), "assertions": checks.passed,
                      "created_at": datetime.now(UTC).isoformat(),
                      "scope": "Real cross-process HTTP using production Python and C# protocol; all game state synthetic; no game or model started",
                      "host_exit_code": process.returncode, "linked_source_sha256": source_hashes,
                      "host_dll_sha256": hashlib.sha256(host_dll.read_bytes()).hexdigest()}
            (fixture / "integration-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                            encoding="utf-8")
            print(f"Synthetic cross-process HTTP integration: {result}; {len(checks.passed)} checks.")
            print(f"Token-free report: {fixture / 'integration-result.json'}")
            if process.returncode != 0:
                raise RuntimeError("The synthetic host failed to exit cleanly; inspect its temporary log")


if __name__ == "__main__":
    main()
