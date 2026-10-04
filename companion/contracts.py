"""Wire types for the local Kingdom bridge and model decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from companion.planning import PlanProposal


class Payable(BaseModel):
    target_id: str = Field(min_length=1, max_length=200)
    name: str | None = None
    x: float | None = Field(default=None, allow_inf_nan=False)
    price: int | None = Field(default=None, ge=0)
    currency: str | None = None
    can_pay: bool | None = None


class Player(BaseModel):
    player_id: int = Field(ge=0, le=1)
    x: float | None = Field(default=None, allow_inf_nan=False)
    coins: int | None = Field(default=None, ge=0)
    stamina: float | None = Field(default=None, allow_inf_nan=False)
    can_sprint: bool | None = None
    crown: bool | None = None
    current_payable: Payable | None = None
    transaction_pending: bool | None = None
    currencies: dict[str, int | None] | None = None
    steed_type: str | None = None
    payment_timeout_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class GameState(BaseModel):
    bridge_version: str
    game_version: str
    session_id: str = Field(min_length=1, max_length=200)
    observation_seq: int = Field(ge=0)
    control_epoch: int = Field(ge=0, strict=True)
    captured_at: datetime
    scene: str
    ready: bool
    reason: str | None = None
    coop: bool
    controlled_player_id: int
    input_released: bool
    players: list[Player] = Field(max_length=2)
    capabilities: list[str] = Field(max_length=64)
    diagnostics: list[str] = Field(default_factory=list, max_length=32)
    world: dict | None = None
    coop_request_result: str | None = None
    ui_ready: bool = False

    @model_validator(mode="after")
    def identity(self) -> GameState:
        if self.captured_at.tzinfo is None:
            raise ValueError("桥接状态必须包含 UTC 时区")
        ids = [player.player_id for player in self.players]
        if len(set(ids)) != len(ids):
            raise ValueError("桥接状态重复声明玩家")
        return self

    def player(self, player_id: int) -> Player | None:
        return next((player for player in self.players if player.player_id == player_id), None)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan: PlanProposal | None = Field(default=None, exclude=True)

    operation: Literal["move", "move_to", "stop", "pay", "pay_coin", "drop", "ability", "map", "sail"]
    direction: Literal["left", "right"] | None = None
    duration_ms: int | None = Field(default=None, ge=100, le=2_147_483_647)
    sprint: bool = False
    target_id: str | None = Field(default=None, min_length=1, max_length=200)
    max_coins: int | None = Field(default=None, ge=1, le=2_147_483_647)
    target_x: float | int | None = Field(default=None, allow_inf_nan=False)
    currency: str | None = Field(default=None, min_length=1, max_length=32)
    amount: int | None = Field(default=None, ge=1, le=2_147_483_647)
    ability: str | None = Field(default=None, min_length=1, max_length=200)
    ability_action: Literal["activate", "deactivate", "cancel", "attack", "release_attack", "channel"] | None = None
    map_action: Literal["open", "close", "left", "right", "select", "confirm"] | None = None
    land: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def operation_parameters(self) -> Decision:
        if self.operation not in {"move", "move_to"} and "sprint" in self.model_fields_set:
            raise ValueError("只有移动可以包含 sprint 参数")
        if self.operation == "move":
            if (self.direction is None or self.duration_ms is None or self.target_id is not None
                    or self.max_coins is not None):
                raise ValueError("移动需提供方向与正整数时长")
        elif self.operation == "move_to":
            if self.target_x is None:
                raise ValueError("移动到位置需要 target_x")
        elif self.operation in {"pay", "pay_coin"}:
            if self.target_id is None or self.direction is not None or self.duration_ms is not None:
                raise ValueError("投币仅接受当前目标标识")
            if self.operation == "pay_coin" and self.max_coins not in {None, 1}:
                raise ValueError("单枚兼容操作最多一枚金币")
        elif self.operation == "drop":
            if self.currency is None or self.amount is None:
                raise ValueError("丢币需要 currency 和 amount")
        elif self.operation == "ability":
            if self.ability is None:
                raise ValueError("技能操作需要当前 ability_id")
        elif self.operation == "map":
            if self.map_action is None or self.map_action == "select" and self.land is None:
                raise ValueError("地图操作需要 map_action，选岛还需 land")
        elif any(item is not None for item in (
            self.direction, self.duration_ms, self.target_id, self.max_coins,
        )):
            raise ValueError("停止不能包含其他操作参数")
        allowed = {
            "move": {"direction", "duration_ms", "sprint"}, "move_to": {"target_x", "sprint"},
            "pay": {"target_id", "max_coins", "currency"}, "pay_coin": {"target_id", "max_coins", "currency"},
            "drop": {"currency", "amount"}, "ability": {"ability", "ability_action"},
            "map": {"map_action", "land"}, "stop": set(), "sail": set(),
        }[self.operation]
        for name, value in self.__dict__.items():
            if name not in allowed | {"operation", "sprint", "plan"} and value is not None:
                raise ValueError(f"{self.operation} 不接受 {name}")
        if self.operation == "map" and self.map_action != "select" and self.land is not None:
            raise ValueError("只有选岛需要 land")
        return self

    @model_serializer(mode="wrap")
    def wire_parameters(self, handler: SerializerFunctionWrapHandler) -> dict:
        result = handler(self)
        if self.operation not in {"move", "move_to"}:
            result.pop("sprint", None)
        return result


class Receipt(BaseModel):
    action_id: str
    session_id: str
    status: Literal["queued", "running", "completed", "cancelled", "failed", "unknown"]
    message: str | None = None
    before: dict | None = None
    after: dict | None = None
    effect: dict | None = None


CAMPAIGN_GOAL = "自主完成当前战役：依据真实地图、资源、建设、兵力、任务和岛屿进度，发展经济与防御、清除贪婪威胁、准备出航并推进各岛目标；以游戏报告战役完成为准，自主使用当前货币和技能。"
COOPERATE_GOAL = CAMPAIGN_GOAL + "与P1协作，但不等待逐句指令。"
INDEPENDENT_GOAL = CAMPAIGN_GOAL + "P2自由行动，不以P1是否移动作为行动条件。"


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["follow", "autonomous"]
    goal: str = Field(default=COOPERATE_GOAL, min_length=1, max_length=500)
    play_style: Literal["cooperate", "independent"] = "cooperate"
    coin_budget: int = Field(default=0, ge=0)
    spending_mode: Literal["budgeted", "wallet"] = "wallet"
    decision_budget: int = Field(default=30, ge=1, le=100)
    continuous: bool = False
    decision_interval_seconds: float = Field(default=1.0, ge=0.2, le=60)
    follow_distance: float = Field(default=3.0, ge=0.5, le=20)
    allow_sprint: bool = True

    @model_validator(mode="after")
    def default_play_goal(self) -> StartRequest:
        if (self.mode == "autonomous" and self.play_style == "independent"
                and "goal" not in self.model_fields_set):
            self.goal = INDEPENDENT_GOAL
        return self
