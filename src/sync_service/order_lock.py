from __future__ import annotations

import threading

# One lock per pipeline (not per order): each webhook handler that does a
# "check MoySklad for an existing order, then create one if not" sequence
# needs it serialized against a duplicate delivery of the *same* webhook
# arriving concurrently, or two near-simultaneous requests can both pass the
# check before either creates the order — confirmed live 2026-09-29 (two
# Shopify order webhooks 4-6s apart both created a MoySklad customerorder
# for the same order, right after the web server became multi-threaded).
# A single lock per pipeline fully serializes that pipeline's order
# creation, which costs nothing in practice at this order volume, and is
# simpler and safer than per-order-id locking.
shopify_order_creation = threading.Lock()
yandex_market_order_creation = threading.Lock()
