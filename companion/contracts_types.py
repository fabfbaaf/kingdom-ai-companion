"""Primitive protocol types shared without importing game/control modules."""

from typing import Annotated

from pydantic import Field

FiniteNumber = Annotated[float | int, Field(allow_inf_nan=False)]
