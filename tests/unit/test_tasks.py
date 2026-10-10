import asyncio
from types import SimpleNamespace

import pytest

from ... import tasks


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
async def test_wait_for_payment_bounds_a_hanging_status_call(monkeypatch):
    monkeypatch.setattr(tasks, "PAYMENT_STATUS_POLL_DEADLINE_SECONDS", 0.05)
    release = asyncio.Event()

    async def hanging_check_transaction_status(wallet_id: str, payment_hash: str):
        await release.wait()
        return SimpleNamespace(success=True, failed=False)

    monkeypatch.setattr(
        tasks, "check_transaction_status", hanging_check_transaction_status
    )

    status, error = await asyncio.wait_for(
        tasks._wait_for_payment("wallet123", "a" * 64), timeout=2
    )

    assert status is None
    assert error is not None
    assert error["code"] == "OTHER"
    release.set()  # let the shielded status check finish cleanly
    await asyncio.sleep(0)


def _payment(**kw):
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
    }
    base.update(kw)
    from datetime import datetime, timezone

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
        _payment(failed=True, pending=False, is_expired=False)
    )
    assert data["state"] == "failed"
    assert data["settled_at"] is None
    assert data["preimage"] is None


def test_expired_incoming_invoice_reports_expired(fake_bolt11):
    data = tasks._build_transaction_data(
        _payment(
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
    assert data["settled_at"] is None


@pytest.mark.asyncio
async def test_payment_sent_reports_confirmed_outcome_when_row_still_pending(
    monkeypatch, fake_bolt11
):
    async def fake_get_wallet_payment(wallet_id, payment_hash):
        return _payment()  # LNbits row not yet updated

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
    await tasks._on_invoice_paid(None, _payment(is_in=True, is_out=False))
    await tasks._on_invoice_paid(None, _payment(success=True, pending=False))
    assert sent == []
