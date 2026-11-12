"""Fresh, strictly typed synthetic state; never connected to external systems."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Money = Annotated[int, Field(strict=True, ge=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Actor(StrictModel):
    actor_id: Text | None
    merchant_id: Text | None
    role: Literal["customer", "support", "unknown"]


class Order(StrictModel):
    order_id: Text
    customer_id: Text
    merchant_id: Text
    total_minor: Money
    refunded_minor: Money = 0
    paid: bool
    status: Literal["pending", "dispatched", "cancelled"]
    address: Text
    # Omit absent notes to preserve all pre-milestone-7 dataset hashes.
    notes: Text | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_balance(self) -> "Order":
        if self.refunded_minor > self.total_minor:
            raise ValueError("refunded amount exceeds total")
        return self


class State(StrictModel):
    orders: list[Order]

    @model_validator(mode="after")
    def unique_orders(self) -> "State":
        ids = [order.order_id for order in self.orders]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate order ids")
        return self

    def find(self, order_id: str) -> Order | None:
        return next((o for o in self.orders if o.order_id == order_id), None)

    def fresh(self) -> "State":
        return self.model_copy(deep=True)
