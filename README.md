<a href="https://lnbits.com" target="_blank" rel="noopener noreferrer">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://i.imgur.com/QE6SIrs.png">
    <img src="https://i.imgur.com/fyKPgVT.png" alt="LNbits" style="width:280px">
  </picture>
</a>

[![License: MIT](https://img.shields.io/badge/License-MIT-success?logo=open-source-initiative&logoColor=white)](./LICENSE)
[![Built for LNbits](https://img.shields.io/badge/Built%20for-LNbits-4D4DFF?logo=lightning&logoColor=white)](https://github.com/lnbits/lnbits)

> [!IMPORTANT]
>
> ## Deployment branch: `black-panther`
>
> This is **not** the upstream repo. `black-panther` is the build that runs on the
> two LNbits 1.6.1 instances on the host `black-panther` (`lnbits-family` and
> `lnbits-business`). It is shown in LNbits as **NWC Service Provider (BP)** and
> is versioned `<upstream>+bpN`. It **supersedes `weeks-family`** (`1.1.1+wf2`).

### What it carries vs upstream

Base: upstream [`lnbits/nwcprovider` v1.1.3](https://github.com/lnbits/nwcprovider/releases/tag/v1.1.3).

| Patch                                                                                                                                                                                                                                                                                                                                                                                                     | Status in `black-panther`                                                                                                                                                                                                                                                     | Upstream status                                                                                                                                                                  |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [#30](https://github.com/lnbits/nwcprovider/pull/30) NIP-47 notifications (@DoktorShift): `payment_received` / `payment_sent` (kind 23196, NIP-04, which matches the provider's NIP-04-only request handling), a `notifications` permission, the `notifications` tag on the kind-13194 info event, the "all wallets" connection list, and an optional `lud16` on connections (migration `m007_add_lud16`) | **Carried.** Conflicts against v1.1.3 resolved on branch [`nip47-notifications-rebase`](https://github.com/BenGWeeks/nwcprovider/tree/nip47-notifications-rebase)                                                                                                             | Open, approved, conflicting                                                                                                                                                      |
| 300 s hard deadline on the pay wait loop (from `weeks-family` `9ea6c45` / closed #40)                                                                                                                                                                                                                                                                                                                     | **Carried, re-implemented** on top of v1.1.3's back-off polling. When the deadline hits, the request returns an `OTHER` error ("still pending, check lookup_invoice"). It never falls through to the success path with a zero preimage, which was upstream's objection to #40 | Not upstream. The `.failed` break is upstream (`bb94f38`), and back-off is upstream (`f4b9610`, `e9d3c48`)                                                                       |
| `.failed` break in the pay wait loop (the other half of #40)                                                                                                                                                                                                                                                                                                                                              | Dropped from our patch set because it is already upstream                                                                                                                                                                                                                     | Upstream (`bb94f38`)                                                                                                                                                             |
| Offload `bolt11_decode` to a worker thread (`weeks-family` `582de54` / closed #37)                                                                                                                                                                                                                                                                                                                        | **Dropped**                                                                                                                                                                                                                                                                   | Superseded by #46 (`asyncio.sleep(0)` per row) and `d432d74` (concurrent request dispatch). **If `list_transactions` stalls under load during the soak test, re-evaluate this.** |
| BP branding: display name `NWC Service Provider (BP)`, version `<upstream>+bpN`, `repo` pointing here, and `extensions.json` (the LNbits manifest source)                                                                                                                                                                                                                                                 | Carried                                                                                                                                                                                                                                                                       | Black-panther only                                                                                                                                                               |
| [#38](https://github.com/lnbits/nwcprovider/pull/38) NIP-42 relay AUTH                                                                                                                                                                                                                                                                                                                                    | **Not carried yet.** Candidate for later, because relays such as `relay.damus.io` need NIP-42 AUTH                                                                                                                                                                            | Open                                                                                                                                                                             |

`weeks-family` (`1.1.1+wf2`) is superseded by this branch and kept only for history.

### How it is installed

Each instance lists `https://raw.githubusercontent.com/BenGWeeks/nwcprovider/black-panther/extensions.json`
as an extension source (Admin → Extensions → Sources). The manifest points at the tag archive
`v<version>` and pins its sha256. LNbits keeps the zip in `/data/zips/nwcprovider.zip` (a persisted volume)
and restores the extension from it when the container is recreated.

### How to update

1. `git fetch upstream --tags`, then rebase `black-panther` onto the new upstream tag. If #30 is merged
   upstream, drop the #30 commits. If not, re-resolve them on `nip47-notifications-rebase` first.
2. Bump `config.json` → `"version": "<upstream>+bp1"`. If you change the same upstream base again,
   bump `+bp2`, `+bp3` and so on.
3. Run the tests: `pytest tests/unit tests/standalone` in an LNbits venv. Run the integration tests
   if the change touches request handling.
4. Tag `v<version>` (for example `v1.1.3+bp1`) and push the tag. Then update `extensions.json`: set the
   `version`, `archive` and `hash` (`curl -sL <archive> | sha256sum`), and push to `black-panther`.
5. Before upgrading each instance (family first, then business), back up `/app/lnbits/extensions/nwcprovider`
   and `/data/ext_nwcprovider.sqlite3`. Install the new release through Admin → Extensions, picking the
   `black-panther` source, and check that the info event and `get_balance` still work.

### Soak-test plan for PR #30 (before endorsing it upstream)

We run #30 in production here, but we don't endorse it upstream until every item below has passed.
Tracking issue: see the "Soak-test PR #30" issue on this repo.

- [ ] **Real usage**: at least 7 days on both `lnbits-family` and `lnbits-business`, with no notification-related errors in the logs and no regressions reported by connected apps.
- [ ] **Multiple connections per wallet**: 2 or more connections on one wallet, some with and some without `notifications`. Only the permitted ones receive events, and each receives exactly one event per payment.
- [ ] **`payment_sent` fan-out**: the connection that paid gets no notification (by design), and the other permitted connections on the same wallet get one. Payments made outside NWC (LNbits UI, LNURL-withdraw, other extensions) produce **no** `payment_sent`. Document this as a known limitation.
- [ ] **Isolation**: a payment on wallet A never produces an event for wallet B's connections, including across LNbits users and on the business instance.
- [ ] **Internal vs external**: LNbits-to-LNbits payments (same instance, and across `lnbits-family` ↔ `lnbits-business` over the shared LND, which is a self-payment) and external Lightning payments each give the right `type`, `amount`, `fees_paid` and `payment_hash`.
- [ ] **No false notifications**: failed sends (no route, insufficient balance, budget exceeded), expired invoices and cancelled or held invoices produce no `payment_received` or `payment_sent`.
- [ ] **Hold / Boltz swaps**: Boltz reverse and submarine swaps through the boltz extension. A notification fires only on final settlement, never on the hold-invoice accept.
- [ ] **Zaps**: incoming zaps to a lnurlp/Lightning address on a wallet with a notifications connection produce a correct `payment_received`. Check the description / description_hash.
- [ ] **Budget / expiry-limited connections**: expired connections receive nothing. Budget-limited connections still receive notifications, and notifications never affect the budget.
- [ ] **Relay outages**: stop or block the relay while payments happen, then restore it. Record whether notifications are lost, delayed or duplicated (the publish is fire-and-forget, so loss is expected). Check that clients fall back to polling (`list_transactions` / `lookup_invoice`).
- [ ] **LNbits restarts**: a restart during a pending payment, and payments that arrive while the container is down. Check for missed or duplicated notifications after it comes back.
- [ ] **High volume**: a burst of 50 or more small payments within a minute on one wallet. Every notification must be delivered once, the event loop must not stall (other NWC requests still answer in under 5 s), and the relay must not rate-limit us.
- [ ] **Migrations**: upgrade from `1.1.1+wf2` and from upstream `1.1.2` (adds the `lud16` column, while existing keys keep `notifications` off). Rolling back to the old build with the new column in place, and restoring the pre-upgrade DB backup, must both work.
- [ ] **NIP-04 vs NIP-44 clients**: Alby Go / Alby Hub, @getalby/sdk `NWCClient.subscribeNotifications`, Lightning Piggy Mobile, Amethyst and Primal. Clients that only support NIP-44 (kind 23197) get nothing, because the provider does not advertise `encryption`. Record which clients work.
- [ ] **Notifications watcher**: with `lightning-piggy-watcher` registered for the same wallet, the user gets **one** push per payment, not one from the watcher plus one from the app's own notification subscription.
- [ ] **Existing methods**: `get_info`, `get_balance`, `make_invoice`, `pay_invoice`, `multi_pay_invoice`, `lookup_invoice` and `list_transactions` (with paging and filters) behave as on v1.1.3. `get_info` lists `notifications` only for permitted keys.
- [ ] **UI**: create, edit and delete connections. The "all wallets" view lists only the current user's wallets. The `lud16` picker works with and without the lnurlp extension.

# NWC Service Provider Extension for [LNbits](https://github.com/lnbits/lnbits)

Easily connect your LNbits wallets via [NWC](https://nwc.dev/).

## Installation

Install the extension via the .env file or through the admin UI on your LNbits server. More details can be found [here](https://github.com/lnbits/lnbits/wiki/LNbits-Extensions).

# Configuration

The **LNbits NWC Service Provider** requires a one-time setup before it can be used.  
It relies on a Nostr relay, which can be either:

- The **LNbits Nostrclient** browser extension
- A **third-party Nostr relay** of your choice

## Relay Configuration

Before you can start using the extension, you need to configure a Nostr relay.

### Option 1: Use a third-party Nostr relay (recommended)

This is the easiest option for most users. It allows you to run LNbits on a private network while connecting to NWC apps through a public Nostr relay.

1. Choose a Nostr relay that supports NWC connections.
2. Navigate to the **NWC Service Provider admin page** at `/nwcprovider/admin` (requires LNbits admin privileges).
   1. Enter your chosen relay URL in the **Nostr Relay URL** field (e.g. `wss://relay.nostrconnect.com`).
   2. Click **Save**.

### Option 2: Use the LNbits Nostrclient extension

> **Note:** This option only works if your LNbits instance is publicly accessible on the internet. Refer to the [nostrclient documentation](https://github.com/lnbits/nostrclient) for more information.

1. Install the **Nostrclient** extension in your browser.
2. Open the extension.
   1. Add at least one relay (e.g. `wss://relay.nostrconnect.com` is a good choice for NWC connections).
   2. Open **Settings** and enable **Expose Public WebSocket**.

---

## Connecting a NWC App

1. In the **NWC Service Provider** extension, select the wallet you want to connect.
2. Click the **+** button to add a new connection.
3. Enter a description, expiry date (optional), permissions, and limits.
4. Click **Connect** to create the connection.
5. Use the generated **pairing URL** or **QR code** to connect your chosen app.

---

# Extension Configuration

The configuration page of the NWC Service Provider extension is available at `/nwcprovider/admin` and requires LNbits admin privileges.

### Configuration Options:

| Key                  | Description                                                                                                                                                                                                 | Default                         |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| relay                | URL of the nostr relay for dispatching and receiving NWC events. Use public relays or a custom one. Specify `nostrclient` to connect to the [nostrclient extension](https://github.com/lnbits/nostrclient). | nostrclient                     |
| provider_key         | Nostr secret key of the NWC Service Provider.                                                                                                                                                               | Random key generated on install |
| relay_alias          | Relay URL to display in pairing URLs. Set if different from `relay`.                                                                                                                                        | Empty (uses the `relay` value)  |
| handle_missed_events | Number of seconds to look back for processing events missed while offline. Setting it to 0 disables this functionality.                                                                                     | 0                               |

> [!WARNING]
>
> Do not change `handle_missed_events` from its default value of `0` unless you fully understand its implications.
> While a non-zero value may improve service quality under unstable conditions (e.g., poor connectivity or unreliable power), it can also lead to unexpected behavior.
> For example, in shared or community lnbits instances, where users are unaware of this functionality, they might assume a payment has failed and attempt to pay a new invoice with a different wallet, only for the instance to come back online and process the original payment request, potentially leading to duplicate payments.
>
> For this reason, unless you are trying to tackle this specific issue, it is recommended to leave this setting at `0`.

## Powered by LNbits

[LNbits](https://lnbits.com) is a free and open-source lightning accounts system.

[![Visit LNbits Shop](https://img.shields.io/badge/Visit-LNbits%20Shop-7C3AED?logo=shopping-cart&logoColor=white&labelColor=5B21B6)](https://shop.lnbits.com/)
[![Try myLNbits SaaS](https://img.shields.io/badge/Try-myLNbits%20SaaS-2563EB?logo=lightning&logoColor=white&labelColor=1E40AF)](https://my.lnbits.com/login)
