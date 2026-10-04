"""Validated conversation output; a reply can request only a bounded control intent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChatReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reply: str = Field(min_length=1, max_length=1500)
    intent: Literal["chat", "stop", "follow", "goal"] = "chat"
    goal: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("reply", "goal")
    @classmethod
    def nonempty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("对话内容不能为空")
        return value

    @model_validator(mode="after")
    def valid_goal(self) -> ChatReply:
        if (self.intent == "goal") != (self.goal is not None):
            raise ValueError("只有 goal 意图必须并且可以包含目标")
        return self
