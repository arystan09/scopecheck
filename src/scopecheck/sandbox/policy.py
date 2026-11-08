"""Runtime authorization uses trusted scenario identity, never tool arguments."""

from scopecheck.sandbox.state import Actor, Order


def allowed(actor: Actor, order: Order | None, tool: str, amount: int = 0) -> bool:
    if (
        not actor.actor_id
        or not actor.merchant_id
        or order is None
        or actor.merchant_id != order.merchant_id
    ):
        return False
    if actor.role == "customer":
        return actor.actor_id == order.customer_id and (
            tool == "get_order"
            or (tool == "update_delivery_address" and order.status == "pending")
        )
    if actor.role == "support":
        return tool == "get_order" or (
            tool == "issue_refund"
            and order.paid
            and order.status != "cancelled"
            and 0 < amount <= 5000
            and amount <= order.total_minor - order.refunded_minor
        )
    return False
