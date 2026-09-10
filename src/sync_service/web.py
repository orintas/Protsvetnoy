from __future__ import annotations

from html import escape
from wsgiref.simple_server import make_server

from .config import Settings
from .import_file import build_rows, csv_bytes, xlsx_bytes
from .moysklad import MoySkladClient
from .novicloud import NovicloudClient


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    if path == "/":
        body = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Novicloud Sync</title>
<style>
:root{color-scheme:dark;--bg:#090b12;--panel:#121622;--line:#252b3b;--text:#f6f7fb;--muted:#9aa3b8;--accent:#8b7cff;--accent2:#5eead4}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 10% 0,#25204c 0,transparent 35%),var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:42px 22px 60px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:58px}
.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:.2px}.mark{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;background:linear-gradient(135deg,var(--accent),#c084fc);box-shadow:0 8px 24px #8b7cff44}
.status{color:var(--accent2);font-size:13px}.status:before{content:"";display:inline-block;width:7px;height:7px;margin:0 7px 1px 0;border-radius:50%;background:var(--accent2);box-shadow:0 0 12px var(--accent2)}
.hero{max-width:710px}.eyebrow{color:var(--accent2);font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}.hero h1{font-size:clamp(34px,6vw,64px);line-height:1.02;letter-spacing:-.05em;margin:14px 0 20px}.hero p{color:var(--muted);font-size:18px;max-width:610px;margin:0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:42px 0}.card{background:#121622cc;border:1px solid var(--line);border-radius:18px;padding:22px;backdrop-filter:blur(12px)}.card h2{font-size:17px;margin:0 0 6px}.card p{color:var(--muted);margin:0}.icon{color:var(--accent2);font-size:22px;margin-bottom:15px}
.actions{display:flex;gap:12px;flex-wrap:wrap}.button{display:inline-flex;align-items:center;justify-content:center;gap:9px;min-width:174px;padding:13px 18px;border:0;border-radius:11px;color:#fff;background:linear-gradient(135deg,var(--accent),#6d5dfc);font:600 14px inherit;text-decoration:none;cursor:pointer;box-shadow:0 10px 26px #6d5dfc33;transition:.2s transform,.2s filter}.button.secondary{background:#1b2130;box-shadow:none;border:1px solid #30384d}.button:hover{filter:brightness(1.12);transform:translateY(-2px)}.button:disabled{opacity:.65;cursor:wait;transform:none}
.note{border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:13px}.note strong{color:var(--text)}@media(max-width:650px){.wrap{padding-top:24px}.top{margin-bottom:42px}.grid{grid-template-columns:1fr}.actions{flex-direction:column}.button{width:100%}}
</style></head>
<body><main class="wrap">
<header class="top"><div class="brand"><span class="mark">↗</span><span>Novicloud Sync</span></div><span class="status">Система готова</span></header>
<section class="hero"><div class="eyebrow">Ассортимент · экспорт</div><h1>Готовьте импорт<br>без ручной работы.</h1><p>Соберите актуальный файл ассортимента для Novicloud из данных МойСклад и Novicloud за один клик.</p></section>
<section class="grid"><article class="card"><div class="icon">↔</div><h2>Два источника</h2><p>Синхронизация данных выполняется на сервере. Ваши API-ключи не покидают VPS.</p></article>
<article class="card"><div class="icon">✓</div><h2>Готово к импорту</h2><p>Поддерживаются CSV и XLSX с нужными колонками, ценами и статусами товаров.</p></article></section>
<section class="card"><h2 style="margin-bottom:6px">Сформировать файл</h2><p style="margin-bottom:20px">Выберите формат для загрузки в Novicloud.</p>
<div class="actions"><form action="/generate?format=xlsx" method="post"><button class="button" type="submit">↓&nbsp; Скачать XLSX</button></form>
<form action="/generate?format=csv" method="post"><button class="button secondary" type="submit">↓&nbsp; Скачать CSV</button></form></div></section>
<p class="note"><strong>Автоматические правила:</strong> учитываются выбранные категории, цены «Цена в Польше» и архивные товары помечаются как «не в продаже».</p>
</main><script>document.querySelectorAll('form').forEach(f=>f.addEventListener('submit',()=>{const b=f.querySelector('button');b.disabled=true;b.textContent='Формируем файл…'}));</script></body></html>""".encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body]
    if path != "/generate":
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not found"]
    settings = Settings.from_env()
    novicloud = NovicloudClient(base_url=settings.novicloud_base_url, version=settings.novicloud_api_version, account=settings.novicloud_account, password=settings.novicloud_password)
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    try:
        rows = build_rows(moysklad.products(), novicloud.all_products())
    finally:
        novicloud.close()
        moysklad.close()
    format_name = environ.get("QUERY_STRING", "").split("=")[-1] or "xlsx"
    if format_name == "csv":
        content, content_type, filename = csv_bytes(rows), "text/csv; charset=utf-8", "novicloud-import.csv"
    else:
        content, content_type, filename = xlsx_bytes(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "novicloud-import.xlsx"
    start_response("200 OK", [("Content-Type", content_type), ("Content-Disposition", f'attachment; filename="{escape(filename)}"')])
    return [content]


def main() -> None:
    make_server("0.0.0.0", 8080, application).serve_forever()
