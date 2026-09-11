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

## Web interface

Start the private web app on the VPS with `docker compose up -d --build`. Open
`http://SERVER_IP:8080/` and switch between three tabs, each covering a
distinct task:

- **Ассортимент** — compares the Novicloud and MoySklad catalogs and generates
  the CSV/XLSX import file for Novicloud;
- **Журнал синхронизации** — shows the read-only sales/returns check log from
  the `worker` service (test mode, no documents are created yet);
- **Аналитика продаж** — aggregates MoySklad `retaildemand`/`retailsalesreturn`
  sums and counts per country storefront (Poland, Lithuania, Latvia, Estonia)
  for a selected date range.

The page keeps API credentials on the server and only returns generated files
or JSON summaries. Restrict port 8080 with the VPS firewall or put it behind
an HTTPS reverse proxy before exposing it publicly.

## Sales analytics by country

`src/sync_service/sales_analytics.py` maps each country storefront to its
MoySklad organization (legal entity):

- Poland — `Varvikas Grupp OU Filiale Poland`;
- Lithuania — `Varvikas Grupp OU Filiale Lithuania`;
- Latvia — `Varvikas Grupp OU Filiale Latvia`;
- Estonia — `Varvikas Grupp OU Filiale Estonia`.

Novicloud is only used for Poland; the other three countries are read
directly from MoySklad `retaildemand` (sales) and `retailsalesreturn`
(returns), filtered by organization and `moment` range.

## Store mapping

The verified Make Data Store mappings are kept in
`data/store-mappings.json`. Novicloud store IDs are mapped to MoySklad store
UUIDs; for example, Novicloud store `100` maps to Wroclavia.
