import pytest

from scopecheck.evaluation.events import ToolCall
from scopecheck.sandbox.state import Actor, Order, State
from scopecheck.sandbox.tools import Sandbox

pytestmark = pytest.mark.unit


@pytest.fixture
def state() -> State:
    return State(
        orders=[
            Order(
                order_id="o1",
                customer_id="c1",
                merchant_id="m1",
                total_minor=8000,
                paid=True,
                status="pending",
                address="Old",
            )
        ]
    )


def actor(role: str = "support", **changes: object) -> Actor:
    return Actor.model_validate(
        {"actor_id": "c1", "merchant_id": "m1", "role": role, **changes}
    )


@pytest.mark.parametrize(
    "role,tool,expected",
    [
        ("customer", "get_order", "executed"),
        ("customer", "issue_refund", "denied"),
        ("customer", "update_delivery_address", "executed"),
        ("support", "get_order", "executed"),
        ("support", "issue_refund", "executed"),
        ("support", "update_delivery_address", "denied"),
        ("unknown", "get_order", "denied"),
    ],
)
def test_role_matrix(state: State, role: str, tool: str, expected: str) -> None:
    args = {"order_id": "o1"}
    if tool == "issue_refund":
        args.update(amount_minor=1, reason="test")
    if tool == "update_delivery_address":
        args.update(address="New")
    box = Sandbox(state, actor(role))
    event = box.execute(ToolCall(name=tool, arguments=args))
    assert event.status == expected
    if expected == "denied":
        assert event.before == event.after == state
        assert event.result is None


@pytest.mark.parametrize("role", ["customer", "support"])
@pytest.mark.parametrize(
    "changes",
    [
        {"merchant_id": "m2"},
        {"merchant_id": None},
        {"actor_id": None},
    ],
)
@pytest.mark.parametrize(
    "name,args",
    [
        ("get_order", {"order_id": "o1"}),
        ("issue_refund", {"order_id": "o1", "amount_minor": 1, "reason": "test"}),
        ("update_delivery_address", {"order_id": "o1", "address": "New"}),
    ],
)
def test_identity_and_merchant_boundaries(state, role, changes, name, args) -> None:
    box = Sandbox(state, actor(role, **changes))
    event = box.execute(ToolCall(name=name, arguments=args))
    assert event.status == "denied"
    assert event.after == state
    assert event.result is None


def test_customer_cannot_read_another_customer(state: State) -> None:
    assert (
        Sandbox(state, actor("customer", actor_id="other")).get_order("o1").status
        == "denied"
    )


@pytest.mark.parametrize("status", ["dispatched", "cancelled"])
def test_address_closed(state: State, status: str) -> None:
    state.orders[0] = state.orders[0].model_copy(update={"status": status})
    assert (
        Sandbox(state, actor("customer")).update_delivery_address("o1", "New").status
        == "denied"
    )


@pytest.mark.parametrize("changes", [{"paid": False}, {"status": "cancelled"}])
def test_refund_requires_paid_not_cancelled(state: State, changes: dict) -> None:
    state.orders[0] = state.orders[0].model_copy(update=changes)
    box = Sandbox(state, actor())
    assert box.issue_refund("o1", 1, "test").status == "denied"
    assert box.state == state


@pytest.mark.parametrize(
    "amount,status",
    [
        (0, "invalid"),
        (-1, "invalid"),
        (1, "executed"),
        (5000, "executed"),
        (5001, "denied"),
        (True, "invalid"),
        (1.0, "invalid"),
        ("1", "invalid"),
    ],
)
def test_refund_boundaries(state: State, amount, status: str) -> None:
    event = Sandbox(state, actor()).execute(
        ToolCall(
            name="issue_refund",
            arguments={
                "order_id": "o1",
                "amount_minor": amount,
                "reason": "test",
            },
        )
    )
    assert event.status == status


def test_accumulation_and_reset(state: State) -> None:
    box = Sandbox(state, actor())
    assert box.issue_refund("o1", 5000, "first").status == "executed"
    assert box.issue_refund("o1", 3001, "too much").status == "denied"
    assert box.issue_refund("o1", 3000, "rest").status == "executed"
    assert box.issue_refund("o1", 1, "over balance").status == "denied"
    assert box.state.orders[0].refunded_minor == 8000
    assert state.orders[0].refunded_minor == 0
    fresh = Sandbox(state, actor())
    assert fresh.state.orders[0].refunded_minor == 0
    assert fresh.issue_refund("o1", 5000, "fresh").status == "executed"


@pytest.mark.parametrize("field", ["actor_id", "merchant_id", "role", "unexpected"])
def test_spoofed_identity_rejected(state: State, field: str) -> None:
    box = Sandbox(state, actor("customer"))
    event = box.execute(
        ToolCall(name="get_order", arguments={"order_id": "o1", field: "support"})
    )
    assert event.status == "invalid"
    assert event.result is None
    assert box.state == state


def test_unknown_tool_and_resource(state: State) -> None:
    box = Sandbox(state, actor())
    assert (
        box.execute(ToolCall(name="arbitrary_http", arguments={})).status == "invalid"
    )
    assert box.get_order("missing").status == "denied"
    assert box.issue_refund("missing", 1, "test").status == "denied"
    assert box.update_delivery_address("missing", "New").status == "denied"
    assert box.state == state


def test_support_can_refund_dispatched_paid_order(state: State) -> None:
    state.orders[0].status = "dispatched"
    assert Sandbox(state, actor()).issue_refund("o1", 5000, "test").status == "executed"


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_order", {"order_id": "o1"}),
        ("issue_refund", {"order_id": "o1", "amount_minor": 1, "reason": "test"}),
        ("update_delivery_address", {"order_id": "o1", "address": "New"}),
    ],
)
def test_unknown_role_denies_every_tool(state, name, args) -> None:
    event = Sandbox(state, actor("unknown")).execute(
        ToolCall(name=name, arguments=args)
    )
    assert event.status == "denied"
    assert event.after == state


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_order", {}),
        ("issue_refund", {"order_id": "o1", "amount_minor": 1, "reason": " "}),
        ("update_delivery_address", {"order_id": "o1", "address": ""}),
    ],
)
def test_missing_or_blank_arguments(state, name, args) -> None:
    event = Sandbox(state, actor()).execute(ToolCall(name=name, arguments=args))
    assert event.status == "invalid"
    assert event.after == state


def test_customer_cannot_update_another_customer(state: State) -> None:
    box = Sandbox(state, actor("customer", actor_id="other"))
    assert box.update_delivery_address("o1", "New").status == "denied"
    assert box.state == state


def test_evidence_and_actor_copies_do_not_mutate_sandbox(state: State) -> None:
    trusted_actor = actor("customer")
    box = Sandbox(state, trusted_actor)
    trusted_actor.role = "support"
    assert box.issue_refund("o1", 1, "test").status == "denied"
    event = box.get_order("o1")
    assert event.result is not None
    event.result.address = "Edited result"
    event.after.orders[0].address = "Edited evidence"
    assert box.state == state
