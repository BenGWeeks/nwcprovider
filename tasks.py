import asyncio
import time
from math import ceil
from typing import Any

from bolt11 import decode as bolt11_decode
from lnbits.core.crud import get_payments, get_wallet, get_wallet_payment
from lnbits.core.models import Payment
from lnbits.core.services import (
    check_transaction_status,
    create_invoice,
    pay_invoice,
)
from lnbits.db import Filters
from lnbits.exceptions import PaymentError
from lnbits.settings import settings
from lnbits.tasks import register_invoice_listener

try:
    from lnbits.task_manager import task_manager
except ImportError:  # older LNbits: fall back to the queue-based listener
    task_manager = None  # type: ignore[assignment]
from lnbits.wallets.base import PaymentStatus
from loguru import logger

from .crud import get_config_nwc, get_nwc, get_wallet_nwcs, tracked_spend_nwc
from .execution_queue import execution_queue
from .models import GetNWC, GetWalletNWC, NWCKey, TrackedSpendNWC
from .nwcp import NWCServiceProvider
from .paranoia import (
    assert_boolean,
    assert_sane_string,
    assert_valid_bolt11,
    assert_valid_expiration_seconds,
    assert_valid_msats,
    assert_valid_positive_int,
    assert_valid_pubkey,
    assert_valid_sha256,
    assert_valid_wallet_id,
)
from .permission import nwc_permissions

PAYMENT_STATUS_POLL_INITIAL_INTERVAL_SECONDS = 1.0
PAYMENT_STATUS_POLL_MAX_INTERVAL_SECONDS = 60.0
PAYMENT_STATUS_POLL_BACKOFF_MULTIPLIER = 2.0
# Hard cap on how long a single pay request keeps polling a payment that is
# neither settled nor failed (e.g. a stuck HTLC). Without it the request task
# lives forever. Most NWC clients give up long before this anyway.
PAYMENT_STATUS_POLL_DEADLINE_SECONDS = 300.0
# Notifications are best-effort: never let a stalled relay hold up the pay
# response or the notification worker for longer than this per event.
NOTIFICATION_SEND_TIMEOUT_SECONDS = 10.0


async def _check(nwc: NWCKey | None, method: str) -> dict | None:
    # check
    if not nwc:
        return {
            "code": "UNAUTHORIZED",
            "message": "This public key has no wallet connected.",
        }
    # check permissions
    allowed = False
    permissions = nwc.get_permissions()
    for p in permissions:
        permissions_data: dict[str, Any] = nwc_permissions.get(p, {})
        allowed_methods: list[str] = permissions_data.get("methods", [])
        if method in allowed_methods:
            allowed = True
            break
    if not allowed:
        return {
            "code": "RESTRICTED",
            "message": "This public key is not allowed to do this operation.",
        }
    return None


def _build_transaction_data(payment: Payment) -> dict:
    """
    Build the NIP-47 transaction data dict from a Payment object.
    Used for lookup_invoice, list_transactions, and notification payloads.
    """
    invoice_data = bolt11_decode(payment.bolt11)
    # Only a successful payment is settled; LNbits also marks failed (and
    # expired incoming) payments as not pending.
    is_settled = payment.success
    timestamp = int(payment.time.timestamp()) or int(invoice_data.date)
    expiry = int(payment.expiry.timestamp()) if payment.expiry else timestamp + 3600
    preimage = (
        payment.preimage
        or "0000000000000000000000000000000000000000000000000000000000000000"
    )
    # Derive NIP-47 state from Payment status
    if payment.success:
        state = "settled"
    elif payment.is_in and payment.is_expired:
        # LNbits marks unpaid expired invoices as failed; NIP-47 says expired.
        state = "expired"
    elif payment.failed:
        state = "failed"
    elif payment.is_expired:
        state = "expired"
    else:
        state = "pending"
    res: dict = {
        "type": "outgoing" if payment.is_out else "incoming",
        "state": state,
        "invoice": payment.bolt11,
        # Fallback chain so a human-readable description reaches the NWC client.
        "description": (
            (payment.extra or {}).get("comment")
            or invoice_data.description
            or payment.memo
        ),
        "preimage": preimage if is_settled or payment.is_in else None,
        "payment_hash": payment.payment_hash,
        "amount": abs(payment.msat),
        "fees_paid": abs(payment.fee),
        "created_at": timestamp,
        "expires_at": expiry,
        "settled_at": timestamp if is_settled else None,
        "metadata": {},
    }
    if invoice_data.description_hash:
        res["description_hash"] = invoice_data.description_hash
    return res


async def _send_notification_to_wallet(
    sp: NWCServiceProvider,
    wallet_id: str,
    notification_type: str,
    notification: dict,
    exclude_pubkey: str | None = None,
):
    """
    Send a notification to all NWC keys on a wallet that have the
    'notifications' permission. Best-effort: errors are logged, never raised.
    """
    try:
        nwc_keys = await get_wallet_nwcs(GetWalletNWC(wallet=wallet_id))
        for nwc_key in nwc_keys:
            if exclude_pubkey and nwc_key.pubkey == exclude_pubkey:
                continue
            permissions = nwc_key.get_permissions()
            if "notifications" not in permissions:
                continue
            try:
                await asyncio.wait_for(
                    sp.send_notification(
                        nwc_key.pubkey, notification_type, notification
                    ),
                    timeout=NOTIFICATION_SEND_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"nwcprovider: dropped {notification_type} notification to "
                    f"{nwc_key.pubkey[:8]}... (relay not reachable)"
                )
            except Exception as e:
                logger.debug(
                    f"Failed to send {notification_type} notification "
                    f"to {nwc_key.pubkey[:8]}...: {e}"
                )
    except Exception as e:
        logger.debug(f"Failed to send notifications for wallet {wallet_id}: {e}")


async def _wait_for_payment(
    wallet_id: str, payment_hash: str
) -> tuple[PaymentStatus | None, dict | None]:
    """
    Poll an outgoing payment (with back-off) until it settles, fails, or the
    deadline passes. Returns (status, None) on success or (None, error).
    """
    payment_status: PaymentStatus | None = None
    poll_interval = PAYMENT_STATUS_POLL_INITIAL_INTERVAL_SECONDS
    deadline = time.monotonic() + PAYMENT_STATUS_POLL_DEADLINE_SECONDS
    while True:  # NIP-47 currently requires waiting for the preimage
        try:
            # Read-only status query: safe to cancel when the deadline hits.
            payment_status = await asyncio.wait_for(
                check_transaction_status(wallet_id, payment_hash),
                timeout=max(deadline - time.monotonic(), 0.001),
            )
        except asyncio.TimeoutError:
            payment_status = None  # status check outlived the deadline
        if payment_status and payment_status.success:
            return payment_status, None
        if payment_status and payment_status.failed:
            return None, {"code": "PAYMENT_FAILED", "message": "Payment failed."}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Still pending: report an error rather than falling through to
            # the success path (which would hand back a zero preimage). The
            # payment may still settle later; lookup_invoice reports it.
            logger.warning(
                f"nwcprovider: payment {payment_hash} still pending after "
                f"{PAYMENT_STATUS_POLL_DEADLINE_SECONDS:.0f}s, giving up waiting"
            )
            return None, {
                "code": "OTHER",
                "message": (
                    "Payment is still pending. It may still complete; "
                    "check its status with lookup_invoice."
                ),
            }
        await asyncio.sleep(min(poll_interval, remaining))
        poll_interval = min(
            poll_interval * PAYMENT_STATUS_POLL_BACKOFF_MULTIPLIER,
            PAYMENT_STATUS_POLL_MAX_INTERVAL_SECONDS,
        )


async def _process_invoice(
    wallet_id: str,
    pubkey: str,
    invoice: str,
    amount_msats: int,
    description: str | None = None,
):

    # hardening #
    assert_valid_wallet_id(wallet_id)
    assert_valid_pubkey(pubkey)
    assert_valid_bolt11(invoice)
    assert_valid_msats(amount_msats)
    if description:
        assert_sane_string(description)
    # ## #

    async def execute_payment() -> str:
        payment = await pay_invoice(
            wallet_id=wallet_id,
            payment_request=invoice,
            max_sat=ceil(amount_msats / 1000),
            description=description or "",
        )
        return payment.payment_hash

    payment_hash = None
    try:
        in_budget, payment_hash = await tracked_spend_nwc(
            TrackedSpendNWC(pubkey=pubkey, amount_msats=amount_msats), execute_payment
        )
        if not in_budget:
            error = {
                "code": "QUOTA_EXCEEDED",
                "message": "The wallet has exceeded its spending quota.",
            }
            return {"error": error, "in_budget": False}
    except PaymentError as e:
        status = e.status
        message = e.message
        if status == "failed":
            error = {"code": "PAYMENT_FAILED", "message": message}
            return {"error": error, "in_budget": False}
        else:
            raise e
    if not payment_hash:
        raise Exception("Payment hash not found")
    payment_status, wait_error = await _wait_for_payment(wallet_id, payment_hash)
    if wait_error:
        return {"error": wait_error, "in_budget": in_budget}
    if not payment_status:
        raise Exception("Payment status not found")
    return {
        "preimage": payment_status.preimage
        or "0000000000000000000000000000000000000000000000000000000000000000",
        "fee_msats": payment_status.fee_msat,
        "paid": payment_status.paid,
        "payment_hash": payment_hash,
        "in_budget": in_budget,
    }


async def _on_pay_invoice(
    sp: NWCServiceProvider, pubkey: str, payload: dict
) -> list[tuple[dict | None, dict | None, list]]:

    # hardening #
    assert_valid_pubkey(pubkey)
    # ## #

    nwc = await get_nwc(GetNWC(pubkey=pubkey, refresh_last_used=True))
    error = await _check(nwc, "pay_invoice")
    if error:
        return [(None, error, [])]
    if not nwc:
        raise Exception("Pubkey has no associated wallet")
    params = payload.get("params", {})
    invoice = params.get("invoice", None)
    # Ensures invoice is provided
    if not invoice:
        raise Exception("Missing invoice")
    invoice_data = bolt11_decode(invoice)
    amount_msats = int(invoice_data.amount_msat or 0)

    # hardening #
    assert_valid_bolt11(invoice)
    assert_valid_msats(amount_msats)
    # ## #

    res = await _process_invoice(
        nwc.wallet, pubkey, invoice, amount_msats, invoice_data.description
    )
    error = res.get("error")
    if error:
        return [(None, error, [])]
    preimage = res.get("preimage")
    out = {
        "preimage": preimage,
    }
    await _notify_payment_sent(sp, nwc.wallet, pubkey, res)
    return [(out, None, [])]


def _settled_fee_msat(payment: Payment, routing_fee_msat: int | None) -> int:
    """
    Fee LNbits records when it settles an external payment: the routing fee
    plus its own service fee (mirrors lnbits.core.services.payments).
    """
    try:
        from lnbits.core.services.payments import service_fee

        lnbits_fee = abs(service_fee(payment.amount, internal=False))
    except Exception:
        lnbits_fee = 0
    return abs(int(routing_fee_msat or 0)) + lnbits_fee


async def _notify_payment_sent(
    sp: NWCServiceProvider,
    wallet_id: str,
    exclude_pubkey: str,
    res: dict,
) -> None:
    """
    Send payment_sent notification to other keys on this wallet. `res` is the
    successful result of _process_invoice for this payment.
    """
    try:
        payment_hash = res.get("payment_hash")
        if payment_hash:
            payment = await get_wallet_payment(wallet_id, payment_hash)
            if payment:
                notification = _build_transaction_data(payment)
                if notification["state"] != "settled":
                    # The funding source confirmed the payment but LNbits has
                    # not persisted it yet: report the confirmed outcome.
                    notification.update(
                        state="settled",
                        preimage=res.get("preimage"),
                        fees_paid=_settled_fee_msat(payment, res.get("fee_msats")),
                        settled_at=int(time.time()),
                    )
                await _send_notification_to_wallet(
                    sp,
                    wallet_id,
                    "payment_sent",
                    notification,
                    exclude_pubkey=exclude_pubkey,
                )
    except Exception as e:
        logger.debug(f"Failed to send payment_sent notification: {e}")


async def _on_multi_pay_invoice(
    sp: NWCServiceProvider, pubkey: str, payload: dict
) -> list[tuple[dict | None, dict | None, list]]:

    # hardening #
    assert_valid_pubkey(pubkey)
    # ## #

    nwc = await get_nwc(GetNWC(pubkey=pubkey, refresh_last_used=True))
    error = await _check(nwc, "multi_pay_invoice")
    if error:
        return [(None, error, [])]
    if not nwc:
        raise Exception("Pubkey has no associated wallet")
    params = payload.get("params", {})
    invoices = params.get("invoices", [])
    results: list[tuple[dict | None, dict | None, list]] = []

    # Ensures all invoices are provided
    for i in invoices:
        invoice = i.get("invoice", None)
        if not invoice:
            raise Exception("Missing invoice")

    for i in invoices:
        try:
            invoice_id = i.get("id", None)
            invoice = i.get("invoice", None)
            invoice_data = bolt11_decode(invoice)
            amount_msats = int(invoice_data.amount_msat or 0)

            # hardening #
            assert_valid_bolt11(invoice)
            assert_valid_msats(amount_msats)
            if invoice_id:
                assert_sane_string(invoice_id)
            # ## #

            res = await _process_invoice(
                nwc.wallet, pubkey, invoice, amount_msats, invoice_data.description
            )
            error = res.get("error")
            if error:
                results.append((None, error, []))
            else:
                payment_hash = res.get("payment_hash")
                r = (
                    {
                        "preimage": res.get("preimage"),
                    },
                    None,
                    [["d", invoice_id if invoice_id else payment_hash]],
                )
                results.append(r)
                await _notify_payment_sent(sp, nwc.wallet, pubkey, res)
        except Exception as e:
            results.append((None, {"code": "INTERNAL", "message": str(e)}, []))
        await asyncio.sleep(0)
    # await log_nwc(pubkey, payload)
    return results


async def _on_make_invoice(
    sp: NWCServiceProvider, pubkey: str, payload: dict
) -> list[tuple[dict | None, dict | None, list]]:

    # hardening #
    assert_valid_pubkey(pubkey)
    # ## #

    nwc = await get_nwc(GetNWC(pubkey=pubkey, refresh_last_used=True))
    error = await _check(nwc, "make_invoice")
    if error:
        return [(None, error, [])]
    if not nwc:
        raise Exception("Pubkey has no associated wallet")
    params = payload.get("params", {})
    amount_msats = params.get("amount", None)
    # Ensures amount is provided
    if not amount_msats:
        raise Exception("Missing amount")
    description = params.get("description", "")
    description_hash = params.get("description_hash", None)
    expiry = params.get("expiry", None)

    # hardening #
    assert_valid_msats(amount_msats)
    if description:
        assert_sane_string(description)
    if description_hash:
        assert_valid_sha256(description_hash)
    if expiry:
        assert_valid_expiration_seconds(expiry)
    # ## #

    payment = await create_invoice(
        wallet_id=nwc.wallet,
        amount=int(amount_msats / 1000),
        currency="sat",
        memo=description,
        description_hash=bytes.fromhex(description_hash) if description_hash else None,
        unhashed_description=description.encode("utf-8"),
        expiry=expiry,
    )
    payment_hash = payment.payment_hash
    payment_request = payment.bolt11
    payment_status = await check_transaction_status(
        wallet_id=nwc.wallet, payment_hash=payment_hash
    )
    preimage = payment_status.preimage
    if (
        not preimage
    ):  # Some backend do not return a preimage (eg. FakeWallet), so we fake it
        preimage = "0000000000000000000000000000000000000000000000000000000000000000"
    res = {
        "type": "incoming",
        "invoice": payment_request,
        "description": description,
        "description_hash": description_hash,
        "preimage": preimage,
        "payment_hash": payment_hash,
        "amount": amount_msats,
        # "fees_paid":None,
        "created_at": int(time.time()),
        "metadata": {},
    }
    if expiry:
        res["expires_at"] = int(time.time()) + int(expiry)
    # await log_nwc(pubkey, payload)
    return [(res, None, [])]


async def _on_lookup_invoice(
    sp: NWCServiceProvider, pubkey: str, payload: dict
) -> list[tuple[dict | None, dict | None, list]]:

    # hardening #
    assert_valid_pubkey(pubkey)
    # ## #

    nwc = await get_nwc(GetNWC(pubkey=pubkey, refresh_last_used=True))
    error = await _check(nwc, "lookup_invoice")
    if error:
        return [(None, error, [])]
    if not nwc:
        raise Exception("Pubkey has no associated wallet")
    params = payload.get("params", {})
    payment_hash = params.get("payment_hash", None)
    invoice = params.get("invoice", None)
    # Ensure payment_hash or invoice are provided
    if not payment_hash and not invoice:
        raise Exception("Missing payment_hash or invoice")
    # Extract hash from invoice if not provided
    if not payment_hash:
        invoice_data = bolt11_decode(invoice)
        payment_hash = invoice_data.payment_hash

    # hardening #
    if payment_hash:
        assert_valid_sha256(payment_hash)
    if invoice:
        assert_valid_bolt11(invoice)
    # ## #

    # Get payment data
    payment = await get_wallet_payment(nwc.wallet, payment_hash)
    if not payment:
        raise Exception("Payment not found")
    res = _build_transaction_data(payment)
    # await log_nwc(pubkey, payload)
    return [(res, None, [])]


async def _on_list_transactions(
    sp: NWCServiceProvider, pubkey: str, payload: dict
) -> list[tuple[dict | None, dict | None, list]]:
    # hardening #
    assert_valid_pubkey(pubkey)
    # ## #

    nwc = await get_nwc(GetNWC(pubkey=pubkey, refresh_last_used=True))
    error = await _check(nwc, "list_transactions")
    if error:
        return [(None, error, [])]
    if not nwc:
        raise Exception("Pubkey has no associated wallet")
    params = payload.get("params", 0)
    tfrom = params.get("from", 0)
    tuntil = params.get("until", int(time.time()))
    limit = params.get("limit", 10)
    offset = params.get("offset", 0)
    unpaid = params.get("unpaid", False)
    tx_type = params.get("type", "")

    # hardening #
    assert_valid_positive_int(tfrom)
    assert_valid_positive_int(tuntil)
    assert_valid_positive_int(limit)
    assert_valid_positive_int(offset)
    assert_boolean(unpaid)
    assert_sane_string(tx_type)
    # ## #

    filters: Filters = Filters()
    filters.where(["time <= :tuntil"])
    filters.values({"tuntil": tuntil})
    history = await get_payments(
        wallet_id=nwc.wallet,
        complete=True,
        pending=unpaid,
        outgoing=not tx_type or tx_type == "outgoing",
        incoming=not tx_type or tx_type == "incoming",
        since=tfrom,
        exclude_uncheckable=False,
        filters=filters,
        limit=limit,
        offset=offset,
    )
    transactions: list[dict] = []
    for p in history:
        transactions.append(_build_transaction_data(p))
        await asyncio.sleep(0)
    # await log_nwc(pubkey, payload)
    return [({"transactions": transactions}, None, [])]


async def _on_get_balance(
    sp: NWCServiceProvider, pubkey: str, payload: dict
) -> list[tuple[dict | None, dict | None, list]]:

    # hardening #
    assert_valid_pubkey(pubkey)
    # ## #

    nwc = await get_nwc(GetNWC(pubkey=pubkey, refresh_last_used=True))
    error = await _check(nwc, "get_balance")
    if error:
        return [(None, error, [])]
    if not nwc:
        raise Exception("Pubkey has no associated wallet")
    balance = 0
    wallet = await get_wallet(nwc.wallet)
    if not wallet:
        raise Exception("Wallet not found")
    balance = wallet.balance_msat
    # await log_nwc(pubkey, payload)
    return [({"balance": balance}, None, [])]


async def _on_get_info(
    sp: NWCServiceProvider, pubkey: str, payload: dict
) -> list[tuple[dict | None, dict | None, list]]:

    # hardening #
    assert_valid_pubkey(pubkey)
    # ## #

    nwc = await get_nwc(GetNWC(pubkey=pubkey, refresh_last_used=True))
    error = await _check(nwc, "get_info")
    if error:
        return [(None, error, [])]
    if not nwc:
        raise Exception("Pubkey has no associated wallet")
    sp_methods = sp.get_supported_methods()
    permissions = nwc.get_permissions()
    # Filter only methods supported by the extension and allowed by the permissions
    account_methods = []
    for spm in sp_methods:
        for p in permissions:
            permissions_data: dict[str, Any] = nwc_permissions.get(p, {})
            allowed_methods: list[str] = permissions_data.get("methods", [])
            if spm in allowed_methods:
                account_methods.append(spm)
                break
    # Include supported notifications if key has notifications permission
    notifications = []
    if "notifications" in permissions:
        notifications = sp.supported_notifications
    # await log_nwc(pubkey, payload)
    return [
        (
            {
                "alias": settings.lnbits_site_title,
                "color": "",
                "network": "mainnet",
                "block_height": 0,
                "block_hash": "",
                "methods": account_methods,
                "notifications": notifications,
            },
            None,
            [],
        )
    ]


async def _on_invoice_paid(sp: NWCServiceProvider, payment: Payment):
    """Handle an incoming payment by sending payment_received notifications."""
    if not (payment.is_in and payment.success):
        return
    try:
        notification = _build_transaction_data(payment)
        await _send_notification_to_wallet(
            sp, payment.wallet_id, "payment_received", notification
        )
    except Exception as e:
        logger.debug(f"Failed to handle invoice paid notification: {e}")


async def _notification_listener(sp: NWCServiceProvider):
    """
    Legacy (pre-task_manager LNbits) listener for incoming paid invoices.
    """
    queue: asyncio.Queue = asyncio.Queue()
    register_invoice_listener(queue, "ext_nwcprovider")
    while True:
        payment = await queue.get()
        await _on_invoice_paid(sp, payment)


def _start_notification_listener(sp: NWCServiceProvider) -> Any:
    """
    Subscribe to incoming payments for payment_received notifications. With
    task_manager the listener is an owned task that _stop_notification_listener
    removes, so LNbits stops queueing payments for it when we stop.
    """
    if task_manager is None:
        return asyncio.create_task(_notification_listener(sp))

    async def on_paid(payment: Payment) -> None:
        await _on_invoice_paid(sp, payment)

    return task_manager.register_invoice_listener(
        on_paid, name="ext_nwcprovider_notifications"
    )


def _stop_notification_listener(listener: Any) -> None:
    try:
        if isinstance(listener, asyncio.Task):
            listener.cancel()
        elif task_manager is not None:
            task_manager.cancel_task(listener)
    except Exception as e:  # already removed / shutting down
        logger.debug(f"nwcprovider: stopping notification listener: {e}")


async def handle_nwc():
    priv_key = await get_config_nwc("provider_key")
    relay = await get_config_nwc("relay")
    handle_missed_events = int(await get_config_nwc("handle_missed_events") or 0)
    nwcsp = NWCServiceProvider(priv_key, relay, handle_missed_events)
    nwcsp.add_request_listener("pay_invoice", _on_pay_invoice)
    nwcsp.add_request_listener("multi_pay_invoice", _on_multi_pay_invoice)
    nwcsp.add_request_listener("make_invoice", _on_make_invoice)
    nwcsp.add_request_listener("lookup_invoice", _on_lookup_invoice)
    nwcsp.add_request_listener("list_transactions", _on_list_transactions)
    nwcsp.add_request_listener("get_balance", _on_get_balance)
    nwcsp.add_request_listener("get_info", _on_get_info)
    # currently not supported by lnbits
    # nwcsp.addRequestListener("pay_keysend", _on_pay_keysend)
    # nwcsp.addRequestListener("multi_pay_keysend", _on_multi_pay_keysend)
    ###
    await nwcsp.start()
    listener = _start_notification_listener(nwcsp)
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await nwcsp.cleanup()
        raise
    finally:
        # Always unsubscribe, also if this task dies and is restarted, so we
        # never leave an orphaned listener (or a duplicate one) behind.
        _stop_notification_listener(listener)


async def handle_execution_queue():
    while True:
        try:
            task = await execution_queue.get()
            action = task.get("action")
            future = task.get("future")
            try:
                if not action:
                    raise Exception("Invalid action")
                res = await action()
                if future:
                    future.set_result(res)
            except Exception as e:
                if future:
                    future.set_exception(e)
        except Exception as e:
            logger.error(str(e))
