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
- `DEPLOY_PORT` — optional SSH port (defaults to `22`).

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
- `YANDEX_MARKET_CAMPAIGN_ID` — the storefront (campaign) to sync, e.g. one
  FBS shop at a time during testing;
- `YANDEX_MARKET_BASE_URL` — defaults to `https://api.partner.market.yandex.ru`.

`offerId` in Yandex Market orders matches the MoySklad product `article`/
`code` directly, so no extra SKU mapping table is required.

## Web interface

Start the private web app on the VPS with `docker compose up -d --build`. Open
`http://SERVER_IP:8080/` and switch between the tabs, each covering a
distinct task:

- **Novicloud** — compares the Novicloud and MoySklad catalogs, generates the
  CSV/XLSX import file for Novicloud, and shows the read-only Novicloud
  sales/returns check log from the `worker` service (test mode, no documents
  are created yet);
- **МойСклад** — closes stale retail shifts for the Poland/Lithuania/Latvia/
  Estonia stores (see below);
- **OZON** — reserved placeholder tab for a future OZON integration;
- **Shopify** — reserved placeholder tab for a future Shopify integration;
- **Яндекс.Маркет** — shows the read-only Yandex Market orders check log from
  the `yandex-market-sync-worker` service (test mode);
- **Ошибки** — unified log of every error across the service (API failures,
  worker exceptions, web request errors), with a header indicator for unread
  entries.

The page keeps API credentials on the server and only returns generated files
or JSON summaries. Restrict port 8080 with the VPS firewall or put it behind
an HTTPS reverse proxy before exposing it publicly.

## MoySklad retail shift closing (PL/LT/LV/EE)

The `moysklad-shift-close-worker` service checks, once a day at 23:50
Europe/Warsaw time, whether any retail shift (`retailshift`) is still open
for the Poland/Lithuania/Latvia/Estonia stores and closes it by setting
`closeDate` to `23:50:00` of that day. Russian stores (and every other
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

Like every other write-capable feature in this project, it respects
`DRY_RUN`: while `DRY_RUN=true` (the default) it only detects and logs open
shifts without closing them. Set `DRY_RUN=false` once you've verified the
detection log looks right, to let it actually close shifts. The web
interface's "МойСклад" tab shows which mode is active and lets you trigger
an on-demand check.

## Store mapping

The verified Make Data Store mappings are kept in
`data/store-mappings.json`. Novicloud store IDs are mapped to MoySklad store
UUIDs; for example, Novicloud store `100` maps to Wroclavia.
