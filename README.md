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

## Test synchronization worker

The `worker` service checks Novicloud sales and returns every five minutes and
shows the results in the web interface under “Журнал синхронизации”. In the
current test mode it only reads data and writes an idempotent SQLite log to the
shared `sync-data` volume; it does not create documents in MoySklad. Keep
`DRY_RUN=true` until document mapping and duplicate protection are explicitly
verified.

## Yandex Market synchronization (replacing TopSeller's connector)

`src/sync_service/yandex_market.py` is a Partner API client (`Api-Key` auth,
orders/status/stocks/prices/returns endpoints). `src/sync_service/
yandex_market_sync.py` runs a separate `yandex-market-sync-worker` service that
polls orders updated in the last 24 hours every five minutes and writes an
idempotent SQLite log (`data/yandex_market_sync.sqlite3`), shown in the web
interface under the “Яндекс.Маркет” tab. It is currently read-only (test
mode): orders and returns/cancellations are logged but no `customerorder`
documents are created in MoySklad yet.

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
Every notification
is logged into the same `data/yandex_market_sync.sqlite3` (kind `webhook`),
visible in the “Яндекс.Маркет” tab; nothing is created in MoySklad from it
yet, matching the read-only stance of the rest of this service.

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

## Web interface

Start the private web app on the VPS with `docker compose up -d --build`. Open
`https://protsvetnoy.us/` and switch between the tabs, each covering a
distinct task:

- **Novicloud** — compares the Novicloud and MoySklad catalogs, generates the
  CSV/XLSX import file for Novicloud, and shows the read-only Novicloud
  sales/returns check log from the `worker` service (test mode, no documents
  are created yet);
- **МойСклад** — closes stale retail shifts for the Poland/Lithuania/Latvia/
  Estonia stores (see below);
- **Категории** — checkboxes for which MoySklad product categories (group
  "ProTsvetnoy OU") sync to Novicloud and which to Shopify; replaces the
  categories that used to be hardcoded in `import_file.py`. The Novicloud
  compare/export endpoints read the saved selection
  (`data/category-sync.json`) instead of a fixed list. Shopify's selection is
  only stored for when that integration exists;
- **OZON** — reserved placeholder tab for a future OZON integration;
- **Shopify** — reserved placeholder tab for a future Shopify integration;
- **Яндекс.Маркет** — shows the read-only Yandex Market orders check log from
  the `yandex-market-sync-worker` service (test mode);
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
`data/store-mappings.json`. Novicloud store IDs are mapped to MoySklad store
UUIDs; for example, Novicloud store `100` maps to Wroclavia.
