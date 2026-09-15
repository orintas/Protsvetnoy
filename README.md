# Novicloud → МойСклад synchronization

Project scaffold for:

- reading sales and product data from Novicloud REST API;
- reading stock data from MoySklad;
- generating the CSV/XLSX import file used by Novicloud.

The repository is prepared for deployment to a Linux VPS. The current CLI
commands are read-only; the web button for generating import files can be added
on top of the same clients without exposing API credentials to the browser.

## Safety

Create a local `.env` from `.env.example`. Never commit or paste passwords,
tokens, or real customer data.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Keep `DRY_RUN=true` until the data mapping and duplicate protection are tested.

## Docker

Build and run a read-only check with credentials supplied at runtime:

```bash
docker build -t novicloud-moysklad-sync .
docker run --rm --env-file .env novicloud-moysklad-sync novicloud-products
```

Do not copy `.env`, generated files, or the `data/` directory into the image.

## Automatic deployment

The repository includes a GitHub Actions deployment workflow. Configure these
repository secrets before enabling it:

- `DEPLOY_HOST` — VPS public IP or hostname;
- `DEPLOY_USER` — deployment user, not necessarily `root`;
- `DEPLOY_SSH_KEY` — private key for a dedicated deploy key;
- `DEPLOY_PATH` — checkout path on the VPS, for example `/opt/novicloud-sync`;
- `DEPLOY_PORT` — optional SSH port (defaults to `22`);
- `MOYSKLAD_TOKEN` — kept in sync with the VPS `.env` on every deploy, so
  rotating the MoySklad API token only needs `gh secret set MOYSKLAD_TOKEN`
  followed by a deploy — no manual VPS access required.

The matching public key must be installed in the deployment user's
`~/.ssh/authorized_keys`. The VPS checkout must already exist and have its
GitHub remote configured. Keep API credentials only in the VPS `.env`; do not
put them in GitHub or the repository.

## Initial API checks

```bash
sync-cli novicloud-products
sync-cli novicloud-sales --from 2026-09-01T00:00:00
sync-cli moysklad-stocks
```

The commands only read data. They print a compact summary and do not create or
change documents.

## Novicloud sales/returns sync (retaildemand / retailsalesreturn)

The `worker` service (`src/sync_service/novicloud_retail_sync.py`, `sync-cli
sync-worker`) creates real `retaildemand` and `retailsalesreturn` documents in
MoySklad from Novicloud POS receipts, every 15 minutes. Ported field-for-field
from the "Sales from Navicloud to MoySklad" / "Returns from Navicloud to
MoySklad" Make.com scenarios (both disabled 2026-09-15 once this went live —
running both at once would double-create documents), verified against real
production data before cutover: computed positions/cashSum/noCashSum for an
already-Make-synced document matched MoySklad's actual stored document
byte-for-byte.

Scoped to the four Poland stores in `config/store-mappings.json` (Westfield
Mokotow, Manufaktura, Wola Park, Wroclavia — all under organization "Varvikas
Grupp OU Filiale Poland"); that mapping's fields (`retail_store_id`,
`organization_id`, `owner_id`, `department_id`, `currency_id`) came from
Make's own "Stores" data store (id `10562`, readable via
`MakeClient.datastore_records`), not from any MoySklad API — if a store's
config ever needs to change, re-derive it from there or from a real document
Make already created for that store, not by guessing.

Per store, per run:

1. Cursor: the `moment` of the most recent `retaildemand` (or
   `retailsalesreturn`) already in MoySklad for that store's `retailStore` —
   self-healing, since a failed creation never advances it.
2. Fetch Novicloud `/dokumenty` since that cursor — `typ_dok=21,112` for
   sales, `typ_dok=8` for returns.
3. Skip a document if one with the same `name` already exists in MoySklad for
   that `retailStore` (the boundary document at the cursor date always
   reappears in step 2's results — `min` is inclusive — and is skipped here,
   not treated as backlog).
4. Resolve each line item's MoySklad product via Novicloud's `opis_3` custom
   field (holds the MoySklad product UUID directly — no separate mapping
   table). A line with no `opis_3` is logged as an error and excluded; the
   document is still created with whichever lines did resolve, unless none
   did (then it's skipped entirely, retried next cycle).
5. Ensure an open retail shift exists for the store (reuse one with no
   `closeDate`, else create one) — looked up once per run, only when actually
   needed.
6. Split the paid amount into `cashSum`/`noCashSum` from the payments array
   (payment form id `2` = non-cash); returns negate both.
7. Create the document. A 2s pause between API calls throttles Novicloud,
   which is stricter than MoySklad about request rate.

Errors (a failed store, or a document with unmapped products) are logged to
the same `data/sync.sqlite3` log shown in the web interface's "Синхронизация
продаж" section, not silently retried into a growing backlog — they surface
for a human to fix (usually: set `opis_3` on the Novicloud product) and keep
being retried every cycle until then.

The log itself is never trimmed — every row stays forever. The web UI only
loads the most recent 100 by default, but the search box (`SyncLog.search`,
`GET /api/sync-log?q=...`) queries the full table, not just what's loaded, so
any document ever created is findable by its receipt number regardless of
age. The Yandex Market log (`YandexMarketSyncLog.search`,
`GET /api/yandex-market-sync-log?q=...`) works the same way.

## Shopify synchronization (catalog + stock; orders/refunds not yet ported)

Ported from a family of Make.com scenarios (all inactive, none deleted) for
the single real shop, `varvikas.myshopify.com` — its storefront domain is
`varvikas.shop` and it's Estonia-based (EUR), but the `.myshopify.com`
backend handle is `varvikas`, not `protsvetnoy` (the connection labeled
"protsvetnoy" in Make actually pointed at this domain; the one labeled
"Varvikas" pointed at `protsvetnoy.myshopify.com` — the names were swapped,
confirmed by the user). `SHOPIFY_ACCESS_TOKEN` is a static Admin API token
from a legacy custom app ("MoySklad") created directly in the store's own
admin — simpler than the OAuth flow a Partner-Dashboard/CLI app would need,
and this is a plain backend service with no embedded UI to justify OAuth.
The shop has exactly one Shopify Location ("Varvikas Main Warehouse"), so
stock from every selected MoySklad warehouse is summed into one number per
SKU rather than mapped to separate locations.

**Catalog** (`src/sync_service/shopify_sync.py`, `catalog_worker`,
`sync-cli shopify-catalog-sync-worker`) — once a night (03:00 Moscow time).
For every MoySklad category checked for "Shopify" on the existing
Категории tab (shared with Novicloud's selection, `CategorySyncConfig`,
`data/category-sync.json`; empty by default — nothing syncs until someone
checks a box), each product is created or updated in Shopify: title
`"{code} - {name}"`, price from the **"Цена ритэйл"** MoySklad price type
specifically (confirmed from the Make blueprint's own filter condition, not
guessed — a plausible-looking alternative, "Цена интернет", exists but
isn't what Make actually used), weight, EAN13 barcode, description, and the
product's first MoySklad image (fetched and re-uploaded as base64 — no
image hosting needed). Vendor is hardcoded `"TM Varvikas"`, matching Make.
An existing Shopify listing is matched by SKU via a GraphQL search (REST has
no product-by-SKU lookup) the first time a SKU is seen; after that,
`ShopifyProductMap` (`data/shopify_products.sqlite3`) remembers the
product/variant/inventory-item ids so neither the catalog nor the stock sync
ever needs to search again.

Each variant is created with `inventory_management: "shopify"` explicitly —
without it, Shopify silently leaves inventory tracking off and
`inventory_levels/set` fails with `422 Inventory item does not have
inventory tracking enabled` (hit this live before adding the field; Make's
own Shopify app module apparently sets this internally, since it isn't in
the blueprint's own mapper).

**Stock** (`stock_worker`, `sync-cli shopify-stock-sync-worker`) — every 30
minutes, but only 09:00–22:00 Moscow time (checked live each tick, not a
fixed schedule window). Warehouses feeding it are picked by hand on the
Shopify tab's "Синхронизация остатков" section — MoySklad has no
organization-level link from `entity/store` straight to a country, so the
picker goes through `entity/retailstore` (which does have one) and follows
its `store` link (`shopify_warehouses.py`, `COUNTRY_ORGANIZATIONS` — same
four org ids the shift-closer already uses for PL/LT/LV/EE), plus a fixed
"Общие склады" group for warehouses that aren't tied to any single mall
shop (Estonia's main "ProTsvetnoy OU" warehouse, named by the user, plus
"Основной склад" alongside it as the other plausible candidate rather than
guessed away). Selection is empty by default
(`data/shopify-warehouses.json`) — stock sync no-ops until warehouses are
picked, same as the catalog sync waiting on category checkboxes. For every
SKU the catalog sync already knows about, stock across every selected
warehouse is summed and pushed only if it changed since the last push (the
`last_available` column on `ShopifyProductMap` — mirrors the diff-only push
already used for Yandex Market stock sync); a SKU missing from a warehouse's
report (sold out there) counts as 0 rather than being skipped, so it still
registers as a real change.

Both workers log to `data/shopify_sync.sqlite3`
(`ShopifySyncLog`, same shape and full-history `search()` as the other
integration logs), shown on the Shopify tab with the same type-filter and
search-box pattern as the Yandex Market log.

Orders and refunds from Shopify aren't ported yet. Of the two source
scenarios, refunds has a complete working flow to copy from later; orders
does not — its real logic is disconnected ("orphaned") from the trigger in
Make, only a webhook trigger and two constants are still wired up.

## Yandex Market synchronization (replacing TopSeller's connector)

`src/sync_service/yandex_market.py` is a Partner API client (`Api-Key` auth,
orders/status/stocks/prices/returns endpoints). New orders are handled live
by the push-notification webhook (below), which creates the `customerorder`
in MoySklad, confirms assembly, and sends the label — this is the only path
for orders now. There used to also be a `yandex-market-sync-worker` service
that polled orders every five minutes in a read-only test mode; it's been
removed now that the webhook pipeline is the real, production path and the
poller's dry-run log entries were just noise duplicating it. `src/
sync_service/yandex_market_sync.py` now only holds `YandexMarketSyncLog`,
the shared SQLite log (`data/yandex_market_sync.sqlite3`) every Yandex
Market component (webhook, order pipeline, stock sync) writes to, shown in
the web interface under the “Яндекс.Маркет” tab with a filter by entry type
and a search box for order number.

Configuration (`.env`):

- `YANDEX_MARKET_API_KEY` — Partner API key created in the seller cabinet
  (Настройки → API и модули);
- `YANDEX_MARKET_BUSINESS_ID` — business/cabinet id (`GET /campaigns`);
- `YANDEX_MARKET_CAMPAIGN_ID` — comma-separated campaign (storefront) ids to
  sync, e.g. `149179204,149179258,149179260,149179270`. Order fetching is
  business-wide (`GET /campaigns` → `/v1/businesses/{id}/orders`) and already
  covers every campaign in one call; this list is only consulted by the
  per-campaign write endpoints (`update_order_status`, `update_stocks`,
  `update_prices`, `returns`), which take `campaign_id` as an explicit
  argument rather than being fixed per client instance;
- `YANDEX_MARKET_BASE_URL` — defaults to `https://api.partner.market.yandex.ru`.

### Push notifications

`POST /api/yandex-market/webhook/notification` receives Market's push
notifications (new orders, status changes, cancellations, returns, chats,
reviews, questions — see the
[notification API spec](https://github.com/yandex-market/yandex-market-notification-api))
as an alternative/complement to the 5-minute poll above. Market appends
`/notification` itself to whatever base URL you register — see below.
Every notification is logged into the same `data/yandex_market_sync.sqlite3`
(kind `webhook`), visible in the “Яндекс.Маркет” tab. `ORDER_CREATED`
additionally runs the full fulfillment pipeline (`src/sync_service/
yandex_market_order_sync.py`) — this is a real production write path, not
dry-run:

1. Skip entirely if a MoySklad `customerorder` with `externalCode` equal to
   the Yandex order id already exists (the only idempotency signal — see the
   docstring on `process_new_order` for what that does and doesn't cover on
   partial failures).
2. Fetch full order details (`POST /v1/businesses/{id}/orders` filtered by
   `orderIds`, since the push payload only carries `offerId`/`count`, not
   price) and look up each `offerId` as a MoySklad product `code`.
3. Create the `customerorder` — organization `ООО "ЦВЕТНОЙ МИР"`
   (`40b2d5fc-22a4-11ec-0a80-02b1002197e3`), agent `ООО "ЯНДЕКС.МАРКЕТ"`
   (ИНН `7704357909`, id `fa685205-1cbb-11e8-9107-5048000779ee` — there are
   several similarly-named counterparties in this account; this is the one
   confirmed by INN), sales channel "Яндекс Маркет FBS", and the store
   mapped from `campaignId` in `CAMPAIGN_STORES` (one physical shop per
   campaign — ТЦ Авиапарк/Саларис/Ривьера/Мега Химки).
4. Confirm assembly: `update_order_status(status=PROCESSING,
   substatus=READY_TO_SHIP)` — per Market's own docs, this substatus means
   "assembled and ready to ship". Must be `PUT`, not `POST` — `POST` on this
   path returns HTTP 405 (confirmed live, on a real order that got stuck
   here until fixed).
5. Fetch the shipping label PDF (`GET .../delivery/labels`) and send it to
   `TELEGRAM_LABEL_CHAT_ID` via a Telegram bot (`TELEGRAM_BOT_TOKEN`), with a
   caption listing the order id and each ordered `offerId` with its quantity,
   routed through `telegram-proxy` (see below — the VPS itself cannot reach
   `api.telegram.org` directly).

### Telegram proxy

The VPS cannot reach `api.telegram.org` at all (`Network is unreachable`,
confirmed live, while MoySklad/Yandex Market/general internet all work fine
from the same host) — Telegram is blocked at the network level for
Russian-hosted servers. `telegram-proxy` (`Dockerfile.xray`, built on
`teddysun/xray` — the official `ghcr.io/xtls/xray-core` image has no shell,
which a custom entrypoint needs) works around this: a VLESS client
([Xray-core](https://github.com/XTLS/Xray-core)) connecting out to a
third-party VLESS server over gRPC+Reality, exposing a local SOCKS5 proxy
(port 1080, reachable only inside the compose network — never published to
the host) that `TelegramClient` routes through
(`TELEGRAM_PROXY_URL=socks5://telegram-proxy:1080`).

gRPC+Reality specifically — the same provider's WS+TLS config (also in its
subscription link) returns `403 Forbidden` on the WebSocket handshake
regardless of header syntax; confirmed live that gRPC+Reality works and
WS+TLS doesn't, against the exact same server.

`xray/config.template.json` has no real values in it — the VLESS server's
connection details (`VLESS_UUID`/`VLESS_HOST`/`VLESS_PORT`/`VLESS_SNI`/
`VLESS_PBK`/`VLESS_SID`, from that provider's gRPC+Reality config link) live
only in the VPS `.env` and get substituted in at container start by
`xray/entrypoint.sh`.
`TelegramClient(proxy=...)` accepts `None`/empty to connect directly — that
still works for local development where Telegram isn't blocked.

These MoySklad entities (organization/agent/store mapping) were confirmed
against a real order from 2026-09-13 and explicit choices made when this was
built — not something the API exposes on its own, so don't re-derive them
from scratch if they ever need to change. TopSeller's own Yandex Market
integration was already disconnected before this went live, so there's no
double-order risk from that side; if it's ever reconnected, this and
TopSeller would both create a `customerorder` for the same sale.

There is no request signature in Market's push API — the only verification
it documents is filtering by source IP, so this endpoint rejects anything
outside Market's published ranges (`5.45.207.0/25`, `141.8.142.0/25`,
`5.255.253.0/25`). Caddy (see below) terminates TLS and proxies to `web`
inside the Docker network, so the app can't see the real client IP on
`REMOTE_ADDR` directly — it reads the last entry of `X-Forwarded-For`
instead (the one Caddy itself appended for the peer that connected to it,
not whatever a client claims), so this only stays correct as long as Caddy
remains the sole, directly-connected proxy in front of `web`.

Registration is manual, in the seller cabinet UI — there's no API for it:
**Аккаунт → Настройки → API и модули → API-уведомления → Подключить
уведомления**, entering `https://protsvetnoy.us/api/yandex-market/webhook`
as the base URL (confirmed from a live PING: Market itself appends
`/notification`, arriving at `.../webhook/notification`, which is the exact
path this app listens on) and picking which event types to send. Market
sends that `PING` once you save, expecting a `200` within 1 second to
confirm the endpoint is live; repeated failures on real notifications back
off from retrying every minute up to hourly, and disable the integration
after 14 days of being unreachable.

`offerId` in Yandex Market orders matches the MoySklad product `article`/
`code` directly, so no extra SKU mapping table is required.

### Stock sync

The `yandex-market-stock-sync-worker` service pushes sellable stock (MoySklad
`quantity` — `stock` minus `reserve`, so already-sold-but-unshipped units
aren't offered again) to Yandex Market every 10 minutes, one store per
campaign using the same `CAMPAIGN_STORES` mapping as order fulfillment
(`src/sync_service/yandex_market_stock_sync.py`, `PUT
/v2/campaigns/{id}/offers/stocks`). This is a real production write, not
dry-run. Each campaign is synced independently — a failure on one (MoySklad
or Yandex Market error) is logged and doesn't block the others. Results
appear in the same `data/yandex_market_sync.sqlite3` log shown in the
"Яндекс.Маркет" tab (`kind: stock_sync`).

Must be `PUT`, not `POST` — `POST` on this exact path silently no-ops and
returns an unrelated paginated stock listing instead of applying anything
(confirmed live: identical response body for any `POST` payload, including
an obviously-wrong sentinel count). Each item also needs `warehouseId`
(`CAMPAIGN_WAREHOUSES` in `yandex_market_order_sync.py` — confirmed both via
the API and a screenshot of Остатки → Склады; distinct from `campaignId` and
not derivable from it) and `type: "FIT"` (the physical/settable count;
Yandex derives `AVAILABLE` itself by subtracting its own pending
reservations). This whole feature silently pushed nothing for as long as it
used `POST` — worth knowing if a stock-related bug ever looks similar again.

Quantities are rounded to the nearest integer and clamped at 0 (Yandex Market
stock counts can't be fractional or negative, though MoySklad's report can
return either for weight-based goods or overselling).

The set of `offerId`s pushed per campaign comes from a local cache
(`data/yandex_market_assortment.sqlite3`, `AssortmentCache`), refreshed once
a day from `POST /v2/campaigns/{id}/offers` (paginated via `limit`/
`page_token` as query-string parameters — confirmed against the live API,
since this endpoint takes them there rather than in the JSON body like most
others in this client). This exists for two reasons: fetching a campaign's
full offer list (hundreds of offers, several paginated requests) is too slow
to redo on every 10-minute tick, and — more importantly — MoySklad's
per-store stock report only lists products with some stock; a product that
sells down to zero at a store drops out of that report entirely rather than
showing `0`, so building the sync list from MoySklad alone would silently
stop zeroing out sold-out offers on Yandex Market. Driving the sync from
Yandex's own offer list instead means every offer it knows about — including
ones with no current MoySklad stock — gets an explicit count every cycle.

`AssortmentCache` also keeps each campaign's last-pushed count per `offerId`
(`stock_state` table). Every run diffs the freshly computed counts against
it and only calls `update_stocks` for the offers whose count actually
changed — sending all ~490 offers every 10 minutes regardless of whether
anything moved was wasted API calls; in steady state it's usually a
handful, and a cycle with zero changes skips the Yandex request entirely.
The log entry lists what was pushed as `sku: before→after` (payload key
`changes`; the row itself shows the first few with a count of the rest). A
campaign's very first sync (and any newly-listed offer picked up by the
daily assortment refresh) has nothing to diff against, so those are pushed
as a full baseline and the entry says so explicitly rather than listing
every offer as "changed".

## Web interface

Start the private web app on the VPS with `docker compose up -d --build`. Open
`https://protsvetnoy.us/` and switch between the tabs, each covering a
distinct task:

- **Novicloud** — compares the Novicloud and MoySklad catalogs, generates the
  CSV/XLSX import file for Novicloud, and shows the `worker` service's live
  sales/returns sync log (real `retaildemand`/`retailsalesreturn` documents
  created in MoySklad for the four Poland stores);
- **МойСклад** — closes stale retail shifts for the Poland/Lithuania/Latvia/
  Estonia stores (see below);
- **Категории** — checkboxes for which MoySklad product categories (group
  "ProTsvetnoy OU") sync to Novicloud and which to Shopify; replaces the
  categories that used to be hardcoded in `import_file.py`. The Novicloud
  compare/export endpoints read the saved selection
  (`data/category-sync.json`) instead of a fixed list. Shopify's selection
  now actually drives its catalog sync too, not just stored;
- **OZON** — reserved placeholder tab for a future OZON integration;
- **Shopify** — MoySklad warehouse picker for the stock sync (grouped by
  country) and the shared catalog/stock sync log, filterable by type with a
  search box;
- **Яндекс.Маркет** — the shared log for order fulfillment (webhook →
  MoySklad → assembly confirmation → Telegram label) and the 10-minute stock
  sync, filterable by entry type with a search box for order number;
- **Ошибки** — unified log of every error across the service (API failures,
  worker exceptions, web request errors), with a header indicator for unread
  entries.

The page keeps API credentials on the server and only returns generated files
or JSON summaries.

### HTTPS (Caddy reverse proxy)

The `caddy` service (see `Caddyfile`) is the only container with published
ports (80/443); `web` has none — it's reachable solely through Caddy inside
the Docker network. Caddy automatically obtains and renews a Let's Encrypt
certificate for `protsvetnoy.us` (HTTP-01 challenge on port 80), redirects
plain HTTP to HTTPS, and forwards to `web:8080`. This requires a DNS `A`
record for `protsvetnoy.us` pointing at the VPS's public IP — without it,
the ACME challenge fails and Caddy falls back to serving over plain HTTP on
that certificate attempt until DNS resolves.

## MoySklad retail shift closing (PL/LT/LV/EE)

The `moysklad-shift-close-worker` service checks, once a day at 23:50 Moscow
time, whether any retail shift (`retailshift`) is still open for the
Poland/Lithuania/Latvia/Estonia stores and closes it by setting `closeDate`
to `23:50:00` of that day (that literal wall-clock value, taken from Moscow
time regardless of the store's own timezone). Russian stores (and every other
organization) are never touched — only shifts belonging to these four
MoySklad organizations are considered:

- `Varvikas Grupp OU Filiale Poland`
- `Varvikas Grupp OU Filiale Lithuania`
- `Varvikas Grupp OU Filiale Latvia`
- `Varvikas Grupp OU Filiale Estonia`

Closing uses the standard `PUT /entity/retailshift/{id}` remap API (not the
separate fiscal POS API) — these stores have no physical cash register
attached, so their shifts are closed the same way a human operator would in
the MoySklad web UI.

Real closing only ever happens once a day, from the scheduled worker — there
is no way to trigger it from the web interface or an API call. It is gated by
its own `MOYSKLAD_SHIFT_CLOSE_DRY_RUN` variable (default `true`), separate
from the shared `DRY_RUN` used by sales/order sync so enabling one doesn't
enable the other. While `MOYSKLAD_SHIFT_CLOSE_DRY_RUN=true` it only detects
and logs open shifts without closing them. The web interface's "МойСклад" tab
is read-only: it shows which mode is active, the shifts that are currently
open, and the closing log.

## Store mapping

The verified Make Data Store mappings are kept in
`config/store-mappings.json`. Novicloud store IDs are mapped to MoySklad store
UUIDs; for example, Novicloud store `100` maps to Wroclavia.
