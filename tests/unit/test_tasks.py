# pyright: reportArgumentType=false
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lnbits.core.models import Payment

from ... import tasks


def _payment(
    status: str,
    amount: int,
    expires_in: int | None = None,
    preimage: str | None = None,
) -> Payment:
    expiry = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if expires_in is not None
        else None
    )
    return Payment(
        checking_id="c" * 64,
        payment_hash="b" * 64,
        wallet_id="wallet123",
        amount=amount,
        fee=0,
        bolt11="lnbc1example",
        status=status,
        expiry=expiry,
        preimage=preimage,
    )


def _patch_nwc(monkeypatch, payments: list[Payment], permission: str) -> None:
    async def fake_get_nwc(*args, **kwargs):
        return SimpleNamespace(wallet="wallet123", get_permissions=lambda: [permission])

    async def fake_get_wallet_payment(*args, **kwargs):
        return payments[0]

    async def fake_get_payments(*args, **kwargs):
        return payments

    monkeypatch.setattr(tasks, "get_nwc", fake_get_nwc)
    monkeypatch.setattr(tasks, "get_wallet_payment", fake_get_wallet_payment)
    monkeypatch.setattr(tasks, "get_payments", fake_get_payments)
    monkeypatch.setattr(
        tasks,
        "bolt11_decode",
        lambda *args, **kwargs: SimpleNamespace(
            description="test 123", description_hash=None, date=0
        ),
    )


async def _lookup_invoice(monkeypatch, payments: list[Payment]) -> dict:
    _patch_nwc(monkeypatch, payments, permission="lookup")
    [(res, error, _)] = await tasks._on_lookup_invoice(
        cast(Any, None), "a" * 64, {"params": {"payment_hash": "b" * 64}}
    )
    assert error is None
    assert isinstance(res, dict)
    return res


async def _list_transactions(monkeypatch, payments: list[Payment]) -> dict:
    _patch_nwc(monkeypatch, payments, permission="history")
    [(res, error, _)] = await tasks._on_list_transactions(
        cast(Any, None), "a" * 64, {"params": {"limit": 10}}
    )
    assert error is None
    assert isinstance(res, dict)
    return res


@pytest.mark.asyncio
async def test_process_invoice_returns_payment_failed_on_failed_status(monkeypatch):
    async def fake_tracked_spend_nwc(*args, **kwargs):
        return True, "a" * 64

    async def fake_check_transaction_status(wallet_id: str, payment_hash: str):
        return SimpleNamespace(success=False, failed=True)

    monkeypatch.setattr(tasks, "tracked_spend_nwc", fake_tracked_spend_nwc)
    monkeypatch.setattr(
        tasks, "check_transaction_status", fake_check_transaction_status
    )

    result = await tasks._process_invoice(
        wallet_id="wallet123",
        pubkey="a" * 64,
        invoice="lnbc1example",
        amount_msats=1000,
        description="test",
    )

    assert result["error"]["code"] == "PAYMENT_FAILED"
    assert result["error"]["message"] == "Payment failed."
    assert result["in_budget"] is True


@pytest.mark.asyncio
async def test_process_invoice_backs_off_pending_payment_polling_to_configured_max(
    monkeypatch,
):
    async def fake_tracked_spend_nwc(*args, **kwargs):
        return True, "a" * 64

    pending = SimpleNamespace(success=False, failed=False)
    statuses = iter(
        [pending] * 8
        + [
            SimpleNamespace(
                success=True,
                failed=False,
                preimage="b" * 64,
                fee_msat=10,
                paid=True,
            )
        ]
    )

    async def fake_check_transaction_status(wallet_id: str, payment_hash: str):
        return next(statuses)

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)

    monkeypatch.setattr(tasks, "tracked_spend_nwc", fake_tracked_spend_nwc)
    monkeypatch.setattr(
        tasks, "check_transaction_status", fake_check_transaction_status
    )
    monkeypatch.setattr(tasks.asyncio, "sleep", fake_sleep)

    result = await tasks._process_invoice(
        wallet_id="wallet123",
        pubkey="a" * 64,
        invoice="lnbc1example",
        amount_msats=1000,
        description="test",
    )

    assert sleep_calls == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert result["preimage"] == "b" * 64
    assert result["fee_msats"] == 10
    assert result["paid"] is True


@pytest.mark.asyncio
async def test_process_invoice_gives_up_on_pending_payment_after_deadline(
    monkeypatch,
):
    async def fake_tracked_spend_nwc(*args, **kwargs):
        return True, "a" * 64

    async def fake_check_transaction_status(wallet_id: str, payment_hash: str):
        return SimpleNamespace(success=False, failed=False)

    clock = {"now": 1000.0}
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(tasks, "tracked_spend_nwc", fake_tracked_spend_nwc)
    monkeypatch.setattr(
        tasks, "check_transaction_status", fake_check_transaction_status
    )
    monkeypatch.setattr(tasks.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(tasks.time, "monotonic", lambda: clock["now"])

    result = await tasks._process_invoice(
        wallet_id="wallet123",
        pubkey="a" * 64,
        invoice="lnbc1example",
        amount_msats=1000,
        description="test",
    )

    # Back-off is respected, and the last sleep is clipped to the deadline.
    assert sleep_calls[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert all(d <= tasks.PAYMENT_STATUS_POLL_MAX_INTERVAL_SECONDS for d in sleep_calls)
    assert sum(sleep_calls) == pytest.approx(tasks.PAYMENT_STATUS_POLL_DEADLINE_SECONDS)
    assert "preimage" not in result
    assert result["error"]["code"] == "OTHER"
    assert result["in_budget"] is True


@pytest.mark.asyncio
async def test_process_invoice_settling_on_final_poll_at_deadline_succeeds(
    monkeypatch,
):
    async def fake_tracked_spend_nwc(*args, **kwargs):
        return True, "a" * 64

    clock = {"now": 0.0}
    settled = SimpleNamespace(
        success=True, failed=False, preimage="c" * 64, fee_msat=0, paid=True
    )

    async def fake_check_transaction_status(wallet_id: str, payment_hash: str):
        if clock["now"] >= tasks.PAYMENT_STATUS_POLL_DEADLINE_SECONDS:
            return settled
        return SimpleNamespace(success=False, failed=False)

    async def fake_sleep(delay: float):
        clock["now"] += delay

    monkeypatch.setattr(tasks, "tracked_spend_nwc", fake_tracked_spend_nwc)
    monkeypatch.setattr(
        tasks, "check_transaction_status", fake_check_transaction_status
    )
    monkeypatch.setattr(tasks.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(tasks.time, "monotonic", lambda: clock["now"])

    result = await tasks._process_invoice(
        wallet_id="wallet123",
        pubkey="a" * 64,
        invoice="lnbc1example",
        amount_msats=1000,
        description="test",
    )

    # The final poll at the deadline still observes the settlement.
    assert result["preimage"] == "c" * 64
    assert result["paid"] is True


@pytest.mark.asyncio
async def test_wait_for_payment_bounds_and_cancels_a_hanging_status_call(
    monkeypatch,
):
    monkeypatch.setattr(tasks, "PAYMENT_STATUS_POLL_DEADLINE_SECONDS", 0.05)
    cancelled = asyncio.Event()

    async def hanging_check_transaction_status(wallet_id: str, payment_hash: str):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        tasks, "check_transaction_status", hanging_check_transaction_status
    )

    status, error = await asyncio.wait_for(
        tasks._wait_for_payment("wallet123", "a" * 64), timeout=2
    )

    assert status is None
    assert error is not None
    assert error["code"] == "OTHER"
    # The status query is cancelled, not left running in the background.
    assert cancelled.is_set()


def _fake_payment(**kw):
    base = {
        "bolt11": "lnbc1example",
        "success": False,
        "failed": False,
        "pending": True,
        "is_expired": False,
        "is_in": False,
        "is_out": True,
        "msat": -21000,
        "fee": -2000,
        "payment_hash": "a" * 64,
        "preimage": None,
        "memo": "memo",
        "extra": {},
        "expiry": None,
        "wallet_id": "wallet123",
        "amount": -21000,
    }
    base.update(kw)
    base.setdefault("time", datetime(2026, 1, 1, tzinfo=timezone.utc))
    return SimpleNamespace(**base)


@pytest.fixture
def fake_bolt11(monkeypatch):
    monkeypatch.setattr(
        tasks,
        "bolt11_decode",
        lambda _: SimpleNamespace(
            description="desc", description_hash=None, date=1767225600
        ),
    )


def test_failed_payment_is_not_settled(fake_bolt11):
    data = tasks._build_transaction_data(
        _fake_payment(failed=True, pending=False, is_expired=False)
    )
    assert data["state"] == "failed"
    assert "settled_at" not in data
    assert "preimage" not in data


def test_expired_incoming_invoice_reports_expired(fake_bolt11):
    data = tasks._build_transaction_data(
        _fake_payment(
            is_in=True,
            is_out=False,
            msat=21000,
            fee=0,
            failed=True,
            pending=False,
            is_expired=True,
        )
    )
    assert data["state"] == "expired"
    assert "settled_at" not in data
    assert "preimage" not in data


@pytest.mark.asyncio
async def test_payment_sent_reports_confirmed_outcome_when_row_still_pending(
    monkeypatch, fake_bolt11
):
    async def fake_get_wallet_payment(wallet_id, payment_hash):
        return _fake_payment()  # LNbits row not yet updated

    sent = []

    async def fake_send(sp, wallet_id, notification_type, notification, **kw):
        sent.append((notification_type, notification, kw))

    monkeypatch.setattr(tasks, "get_wallet_payment", fake_get_wallet_payment)
    monkeypatch.setattr(tasks, "_send_notification_to_wallet", fake_send)

    await tasks._notify_payment_sent(
        None,
        "wallet123",
        "b" * 64,
        {"payment_hash": "a" * 64, "preimage": "c" * 64, "fee_msats": 1000},
    )

    assert len(sent) == 1
    notification_type, notification, kw = sent[0]
    assert notification_type == "payment_sent"
    assert notification["state"] == "settled"
    assert notification["preimage"] == "c" * 64
    assert notification["fees_paid"] == 1000
    assert notification["settled_at"]
    assert kw["exclude_pubkey"] == "b" * 64


@pytest.mark.asyncio
async def test_invoice_listener_ignores_unsettled_or_outgoing(monkeypatch):
    sent = []

    async def fake_send(*a, **kw):
        sent.append(a)

    monkeypatch.setattr(tasks, "_send_notification_to_wallet", fake_send)
    await tasks._on_invoice_paid(None, _fake_payment(is_in=True, is_out=False))
    await tasks._on_invoice_paid(None, _fake_payment(success=True, pending=False))
    assert sent == []


@pytest.mark.asyncio
async def test_payment_sent_overlay_includes_lnbits_service_fee(
    monkeypatch, fake_bolt11
):
    import importlib

    lnbits_payments = importlib.import_module("lnbits.core.services.payments")

    async def fake_get_wallet_payment(wallet_id, payment_hash):
        return _fake_payment()

    sent = []

    async def fake_send(sp, wallet_id, notification_type, notification, **kw):
        sent.append(notification)

    monkeypatch.setattr(tasks, "get_wallet_payment", fake_get_wallet_payment)
    monkeypatch.setattr(tasks, "_send_notification_to_wallet", fake_send)
    monkeypatch.setattr(
        lnbits_payments, "service_fee", lambda amount, internal=False: -210
    )

    await tasks._notify_payment_sent(
        None, "wallet123", "b" * 64, {"payment_hash": "a" * 64, "fee_msats": 10}
    )

    assert sent[0]["fees_paid"] == 220


@pytest.mark.asyncio
async def test_notification_send_is_bounded_when_relay_stalls(monkeypatch):
    monkeypatch.setattr(tasks, "NOTIFICATION_SEND_TIMEOUT_SECONDS", 0.01)

    async def fake_get_wallet_nwcs(_):
        return [
            SimpleNamespace(
                pubkey="c" * 64, get_permissions=lambda: ["info", "notifications"]
            )
        ]

    class StalledProvider:
        async def send_notification(self, *a):
            await asyncio.Event().wait()

    monkeypatch.setattr(tasks, "get_wallet_nwcs", fake_get_wallet_nwcs)

    await asyncio.wait_for(
        tasks._send_notification_to_wallet(
            StalledProvider(), "wallet123", "payment_received", {}
        ),
        timeout=2,
    )


@pytest.mark.asyncio
async def test_notification_listener_is_bounded_owned_and_removed(monkeypatch):
    calls = []
    callbacks = []

    class FakeTaskManager:
        def register_invoice_listener(self, func, name=None):
            calls.append(("register", name))
            callbacks.append(func)
            return "listener-task"

        def cancel_task(self, task):
            calls.append(("cancel", task))

    stalled = asyncio.Event()

    async def stalled_on_invoice_paid(sp, payment):
        stalled.set()
        await asyncio.Event().wait()  # relay outage: publishing never returns

    monkeypatch.setattr(tasks, "task_manager", FakeTaskManager())
    monkeypatch.setattr(tasks, "NOTIFICATION_QUEUE_MAXSIZE", 3)
    monkeypatch.setattr(tasks, "_on_invoice_paid", stalled_on_invoice_paid)

    handles = tasks._start_notification_listener(None)
    worker = handles[0]
    on_paid = callbacks[0]

    # LNbits dispatches far more payments than can be published.
    for i in range(50):
        await on_paid(SimpleNamespace(payment_hash=f"{i:064x}"))
    await asyncio.wait_for(stalled.wait(), timeout=2)
    for i in range(50, 100):
        await on_paid(SimpleNamespace(payment_hash=f"{i:064x}"))

    queue = worker.get_coro().cr_frame.f_locals["queue"]
    assert queue.qsize() <= 3

    tasks._stop_notification_listener(handles)
    await asyncio.sleep(0)
    assert worker.cancelled()
    assert calls == [
        ("register", "ext_nwcprovider_notifications"),
        ("cancel", "listener-task"),
    ]


@pytest.mark.asyncio
async def test_legacy_listener_is_unregistered_on_stop(monkeypatch):
    import importlib

    lnbits_tasks = importlib.import_module("lnbits.tasks")
    registry: dict = {}

    def fake_register_invoice_listener(queue, name=None):
        registry[name] = queue

    monkeypatch.setattr(lnbits_tasks, "invoice_listeners", registry, raising=False)
    monkeypatch.setattr(
        tasks, "register_invoice_listener", fake_register_invoice_listener
    )
    monkeypatch.setattr(tasks, "task_manager", None)

    handles = tasks._start_notification_listener(None)
    await asyncio.sleep(0)
    assert "ext_nwcprovider" in registry

    tasks._stop_notification_listener(handles)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "ext_nwcprovider" not in registry


@pytest.mark.parametrize(
    "status,amount,expires_in,expected",
    [
        ("success", 123000, 3600, "settled"),  # settled incoming invoice
        ("success", -123000, None, "settled"),  # settled outgoing payment
        ("pending", 123000, 3600, "pending"),  # unpaid incoming invoice
        ("pending", -123000, None, "pending"),  # in-flight outgoing payment
        ("failed", 123000, -3600, "expired"),  # expired incoming invoice
        ("failed", 123000, 3600, "failed"),  # canceled incoming payment
        ("failed", -123000, -3600, "failed"),  # failed outgoing payment
    ],
)
def test_nip47_state(status, amount, expires_in, expected):
    payment = _payment(status, amount, expires_in)
    assert tasks._nip47_state(payment) == expected


def test_failed_incoming_invoice_is_not_reported_as_settled():
    """`is_settled = not payment.pending` was true for a failed payment too."""
    payment = _payment("failed", 123000, expires_in=-3600)
    assert not payment.pending
    assert tasks._nip47_state(payment) == "expired"


@pytest.mark.asyncio
async def test_lookup_invoice_expired_has_no_settlement_fields(monkeypatch):
    res = await _lookup_invoice(monkeypatch, [_payment("failed", 123000, -3600)])

    assert res["state"] == "expired"
    assert "settled_at" not in res
    assert "preimage" not in res


@pytest.mark.asyncio
async def test_lookup_invoice_pending_has_no_settlement_fields(monkeypatch):
    res = await _lookup_invoice(monkeypatch, [_payment("pending", 123000, 3600)])

    assert res["state"] == "pending"
    assert "settled_at" not in res
    assert "preimage" not in res


@pytest.mark.asyncio
async def test_lookup_invoice_settled_keeps_settlement_fields(monkeypatch):
    payment = _payment("success", 123000, expires_in=3600, preimage="d" * 64)
    res = await _lookup_invoice(monkeypatch, [payment])

    assert res["state"] == "settled"
    assert res["settled_at"] == int(payment.time.timestamp())
    assert res["preimage"] == "d" * 64


@pytest.mark.asyncio
async def test_list_transactions_states(monkeypatch):
    pending_outgoing = _payment("pending", -123000)
    settled_incoming = _payment("success", 123000, preimage="d" * 64)
    res = await _list_transactions(monkeypatch, [pending_outgoing, settled_incoming])

    transactions = res["transactions"]
    assert [tx["state"] for tx in transactions] == ["pending", "settled"]
    assert "settled_at" not in transactions[0]
    assert "preimage" not in transactions[0]
    assert transactions[1]["settled_at"] == int(settled_incoming.time.timestamp())
    assert transactions[1]["preimage"] == "d" * 64


@pytest.mark.parametrize(
    "amount,expected",
    [
        (123000, "expired"),  # unpaid incoming invoice LNbits left pending
        (-123000, "pending"),  # outgoing HTLC may still be in flight
    ],
)
def test_nip47_state_pending_past_expiry(amount, expected):
    """black-panther rule on top of upstream's mapping."""
    payment = _payment("pending", amount, expires_in=-3600)
    assert payment.pending
    assert tasks._nip47_state(payment) == expected
