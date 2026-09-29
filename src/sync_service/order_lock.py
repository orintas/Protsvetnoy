from __future__ import annotations

import threading

# One lock per pipeline (not per order): every webhook handler in these
# pipelines does a "check the log/MoySklad for prior action, then act if
# not" sequence — create an order, send a label, send a courier/cancel
# notice — none of it atomic against a duplicate delivery of the *same*
# webhook arriving concurrently. Two near-simultaneous requests can both
# pass the check before either acts, since the web server became
# multi-threaded — confirmed live 2026-09-29 (two Shopify order webhooks
# 4-6s apart both created a MoySklad customerorder for the same order).
# A single lock per pipeline fully serializes that pipeline's webhook
# handling end to end, which costs nothing in practice at this order
# volume, and is simpler and safer than locking each side effect
# individually or locking per-order-id.
shopify_order_creation = threading.Lock()
# Covers every Yandex Market order-webhook side effect, not just creation:
# process_new_order, handle_order_cancelled, notify_courier_arrived and
# retry_label_if_missing all have the same shape, so a duplicate
# ORDER_STATUS_UPDATED delivery could otherwise double-send a Telegram
# notice the same way a duplicate ORDER_CREATED could double-create an
# order.
yandex_market_order_pipeline = threading.Lock()
