"""The three local tools validate untrusted arguments before authorization."""

from typing import Annotated

from pydantic import Field, ValidationError

from scopecheck.evaluation.events import Event, ToolCall
from scopecheck.sandbox import policy
from scopecheck.sandbox.state import Actor, State, StrictModel, Text


class GetOrder(StrictModel):
    order_id: Text


class IssueRefund(GetOrder):
    amount_minor: Annotated[int, Field(strict=True, gt=0)]
    reason: Text


class UpdateAddress(GetOrder):
    address: Text


def parse_arguments(call: ToolCall) -> GetOrder | IssueRefund | UpdateAddress:
    match call.name:
        case "get_order":
            return GetOrder.model_validate(call.arguments)
        case "issue_refund":
            return IssueRefund.model_validate(call.arguments)
        case "update_delivery_address":
            return UpdateAddress.model_validate(call.arguments)
        case _:
            raise ValueError("unknown tool")


class Sandbox:
    def __init__(self, initial: State, actor: Actor) -> None:
        self.state = initial.fresh()
        self.actor = actor.model_copy(deep=True)

    def execute(self, call: ToolCall) -> Event:
        before = self.state.fresh()
        call = call.model_copy(deep=True)
        try:
            arguments = parse_arguments(call)
        except (ValidationError, ValueError):
            return Event(
                call=call, status="invalid", before=before, after=before.fresh()
            )
        order = self.state.find(arguments.order_id)
        amount = arguments.amount_minor if isinstance(arguments, IssueRefund) else 0
        if not policy.allowed(self.actor, order, call.name, amount):
            return Event(
                call=call, status="denied", before=before, after=before.fresh()
            )
        # Resource existence is still required if a test deliberately weakens the gate.
        if order is None:
            return Event(
                call=call, status="denied", before=before, after=before.fresh()
            )
        result = None
        if isinstance(arguments, IssueRefund):
            order.refunded_minor += arguments.amount_minor
        elif isinstance(arguments, UpdateAddress):
            order.address = arguments.address
        else:
            result = order.model_copy(deep=True)
        return Event(
            call=call,
            status="executed",
            before=before,
            after=self.state.fresh(),
            result=result,
        )

    def get_order(self, order_id: str) -> Event:
        return self.execute(
            ToolCall(name="get_order", arguments={"order_id": order_id})
        )

    def issue_refund(self, order_id: str, amount_minor: int, reason: str) -> Event:
        return self.execute(
            ToolCall(
                name="issue_refund",
                arguments={
                    "order_id": order_id,
                    "amount_minor": amount_minor,
                    "reason": reason,
                },
            )
        )

    def update_delivery_address(self, order_id: str, address: str) -> Event:
        return self.execute(
            ToolCall(
                name="update_delivery_address",
                arguments={
                    "order_id": order_id,
                    "address": address,
                },
            )
        )
