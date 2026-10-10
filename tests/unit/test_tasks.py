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
async def test_process_invoice_settling_just_before_deadline_succeeds(monkeypatch):
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
