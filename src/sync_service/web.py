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
        body = """<!doctype html><meta charset="utf-8"><title>Novicloud import</title>
        <h1>Импорт ассортимента Novicloud</h1>
        <p>Файл строится по текущим данным МойСклад и Novicloud.</p>
        <form action="/generate?format=xlsx" method="post"><button>Скачать XLSX</button></form>
        <form action="/generate?format=csv" method="post"><button>Скачать CSV</button></form>""".encode("utf-8")
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
