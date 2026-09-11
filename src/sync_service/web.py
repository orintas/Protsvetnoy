from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from json import dumps
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from .config import Settings
from .import_file import compare_catalogs, csv_bytes, rows_for_codes, xlsx_bytes
from .moysklad import MoySkladClient
from .novicloud import NovicloudClient
from .sales_analytics import country_sales_summary
from .sync_log import SyncLog
from .yandex_market_sync import YandexMarketSyncLog


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    if path == "/":
        body = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="https://static.tildacdn.com/tild3935-3263-4363-a333-393162643930/__-removebg-preview.png">
<title>Varvikas Grupp System</title>
<style>
:root{color-scheme:dark;--bg:#090b12;--panel:#121622;--line:#252b3b;--text:#f6f7fb;--muted:#9aa3b8;--accent:#8b7cff;--accent2:#5eead4}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 10% 0,#25204c 0,transparent 35%),var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:42px 22px 60px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:58px}
.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:.2px}.mark{display:grid;place-items:center;width:42px;height:42px;border-radius:12px;background:#fff;box-shadow:0 8px 24px #8b7cff44;object-fit:contain;padding:6px}
.status{color:var(--accent2);font-size:13px}.status:before{content:"";display:inline-block;width:7px;height:7px;margin:0 7px 1px 0;border-radius:50%;background:var(--accent2);box-shadow:0 0 12px var(--accent2)}
.hero{max-width:710px}.eyebrow{color:var(--accent2);font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}.hero h1{font-size:clamp(34px,6vw,64px);line-height:1.02;letter-spacing:-.05em;margin:14px 0 20px}.hero p{color:var(--muted);font-size:18px;max-width:610px;margin:0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:42px 0}.card{background:#121622cc;border:1px solid var(--line);border-radius:18px;padding:22px;backdrop-filter:blur(12px)}.card h2{font-size:17px;margin:0 0 6px}.card p{color:var(--muted);margin:0}.icon{color:var(--accent2);font-size:22px;margin-bottom:15px}
.actions{display:flex;gap:12px;flex-wrap:wrap}.button{display:inline-flex;align-items:center;justify-content:center;gap:9px;min-width:174px;padding:13px 18px;border:0;border-radius:11px;color:#fff;background:linear-gradient(135deg,var(--accent),#6d5dfc);font:600 14px inherit;text-decoration:none;cursor:pointer;box-shadow:0 10px 26px #6d5dfc33;transition:.2s transform,.2s filter}.button.secondary{background:#1b2130;box-shadow:none;border:1px solid #30384d}.button:hover{filter:brightness(1.12);transform:translateY(-2px)}.button:disabled{opacity:.65;cursor:wait;transform:none}
.note{border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:13px}.note strong{color:var(--text)}.toolbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:24px 0 14px;color:var(--muted);font-size:13px}.toolbar input[placeholder],.toolbar select{min-width:190px;background:#0d111b;border:1px solid var(--line);border-radius:9px;padding:10px 12px;color:var(--text)}.toolbar input[placeholder]{flex:1}.table{overflow:auto;border:1px solid var(--line);border-radius:12px}.table table{border-collapse:collapse;width:100%;min-width:720px}.table th,.table td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line)}.table th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}.table td:first-child,.table th:first-child{width:55px;color:var(--muted);text-align:right}.table td span{color:var(--muted);font-size:13px}.badge{display:inline-block!important;padding:4px 8px;border-radius:7px;font-size:12px!important;color:#fff!important;background:#334155}.badge.missing{background:#2563eb}.badge.archive{background:#b45309}.badge.price{background:#7c3aed}.badge.success,.badge.sale{background:#16a34a}.badge.error{background:#dc2626}.badge.return{background:#d97706}.badge.dry-run{background:#475569}.export{margin-top:18px}.log{margin-top:20px;max-height:360px;overflow:auto;border-top:1px solid var(--line)}.log-row{display:flex;align-items:center;gap:10px;padding:11px 0;border-bottom:1px solid var(--line);font-size:13px}.log-time{color:var(--muted);min-width:150px}.muted{color:var(--muted)}.error{color:#fca5a5;margin-top:20px}
.tabs{display:flex;gap:8px;margin:34px 0 26px;border-bottom:1px solid var(--line);flex-wrap:wrap}.tab-btn{background:none;border:0;color:var(--muted);font:600 14px inherit;padding:12px 6px;cursor:pointer;border-bottom:2px solid transparent;transition:.15s color,.15s border-color}.tab-btn.active{color:var(--text);border-bottom-color:var(--accent)}.tab-btn:hover{color:var(--text)}.tab-panel{display:none}.tab-panel.active{display:block}
.periodbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:18px;color:var(--muted);font-size:13px}.periodbar input[type=date]{background:#0d111b;border:1px solid var(--line);border-radius:9px;padding:10px 12px;color:var(--text)}
.country-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:22px}.country-card{background:#0d111bcc;border:1px solid var(--line);border-radius:14px;padding:18px}.country-card h3{margin:0 0 10px;font-size:15px;display:flex;align-items:center;gap:8px}.country-card .flag{font-size:20px}.country-card .metric{display:flex;justify-content:space-between;font-size:13px;color:var(--muted);padding:4px 0}.country-card .metric b{color:var(--text);font-weight:600}.country-card .net{margin-top:8px;padding-top:8px;border-top:1px solid var(--line);font-size:16px;font-weight:700}
@media(max-width:650px){.wrap{padding-top:24px}.top{margin-bottom:42px}.grid{grid-template-columns:1fr}.actions{flex-direction:column}.button{width:100%}.log-row{align-items:flex-start;flex-wrap:wrap}.log-time{min-width:130px}}
</style></head>
<body><main class="wrap">
<header class="top"><div class="brand"><img class="mark" src="https://static.tildacdn.com/tild3935-3263-4363-a333-393162643930/__-removebg-preview.png" alt="Varvikas"><span>Varvikas Grupp System</span></div><span class="status">Система готова</span></header>
<section class="hero"><div class="eyebrow">Ассортимент · синхронизация · аналитика</div><h1>Единый центр<br>управления продажами.</h1><p>Сравнение ассортимента с Novicloud, журнал синхронизации продаж и возвратов, а также аналитика продаж по Польше, Литве, Латвии и Эстонии.</p></section>
<nav class="tabs">
<button class="tab-btn active" data-tab="catalog" type="button">Ассортимент</button>
<button class="tab-btn" data-tab="log" type="button">Журнал синхронизации</button>
<button class="tab-btn" data-tab="analytics" type="button">Аналитика продаж</button>
<button class="tab-btn" data-tab="ym-log" type="button">Яндекс.Маркет</button>
</nav>
<section id="tab-catalog" class="tab-panel active">
<section class="grid"><article class="card"><div class="icon">↔</div><h2>Два источника</h2><p>Синхронизация данных выполняется на сервере. Ваши API-ключи не покидают VPS.</p></article>
<article class="card"><div class="icon">✓</div><h2>Готово к импорту</h2><p>Поддерживаются CSV и XLSX с нужными колонками, ценами и статусами товаров.</p></article></section>
<section class="card"><div style="display:flex;justify-content:space-between;gap:15px;align-items:center;flex-wrap:wrap">
<div><h2 style="margin:0 0 6px">Сравнение каталогов</h2><p style="margin:0">Сначала загрузите актуальные данные из двух систем.</p></div>
<button class="button" id="compare" type="button">↻&nbsp; Сравнить каталоги</button></div>
<div id="result"></div></section>
<p class="note"><strong>Как это работает:</strong> отмеченные отличия попадут в файл. Совпадающие товары можно скрыть.</p>
</section>
<section id="tab-log" class="tab-panel">
<section class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:15px"><div><h2 style="margin:0 0 6px">Журнал синхронизации</h2><p style="margin:0">Тестовый режим: документы в МойСклад пока не создаются. Проверка выполняется каждые 5 минут.</p></div><button class="button secondary" id="refresh-log" type="button">Обновить</button></div><div id="sync-log" class="log"></div></section>
</section>
<section id="tab-analytics" class="tab-panel">
<section class="card">
<div style="display:flex;justify-content:space-between;gap:15px;align-items:center;flex-wrap:wrap">
<div><h2 style="margin:0 0 6px">Аналитика продаж по странам</h2><p style="margin:0">Продажи и возвраты по юрлицам Польши, Литвы, Латвии и Эстонии в МойСклад.</p></div>
<button class="button" id="load-analytics" type="button">↻&nbsp; Загрузить</button>
</div>
<div class="periodbar"><label>С <input type="date" id="date-from"></label><label>По <input type="date" id="date-to"></label></div>
<div id="analytics-result"></div>
</section>
</section>
<section id="tab-ym-log" class="tab-panel">
<section class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:15px"><div><h2 style="margin:0 0 6px">Яндекс.Маркет — журнал синхронизации</h2><p style="margin:0">Тестовый режим: заказы читаются каждые 5 минут, документы в МойСклад пока не создаются.</p></div><button class="button secondary" id="refresh-ym-log" type="button">Обновить</button></div><div id="ym-sync-log" class="log"></div></section>
</section>
</main><script>
const result=document.getElementById('result'), compare=document.getElementById('compare');
let rows=[];
const labels={missing:'Нет в Novicloud',archive:'Архивировать',price:'Изменить цену',same:'Совпадает'};
compare.onclick=async()=>{compare.disabled=true;compare.textContent='Загружаем каталоги…';result.innerHTML='';
try{const response=await fetch('/api/compare');if(!response.ok)throw new Error(await response.text());rows=await response.json();render();}
catch(error){result.innerHTML='<p class="error">Не удалось сравнить каталоги: '+error.message+'<br><span class="muted">Novicloud иногда отвечает с временной ошибкой — сервер уже делает несколько попыток автоматически. Нажмите «Сравнить каталоги» ещё раз через минуту.</span></p>';}
compare.disabled=false;compare.textContent='↻  Обновить сравнение';};
function render(){const diff=rows.filter(r=>r.status!=='same');result.innerHTML=
'<div class="toolbar"><input id="search" placeholder="Поиск по коду или названию"><select id="category"><option value="">Все категории</option>'+[...new Set(rows.map(r=>r.category))].sort().map(c=>'<option>'+c+'</option>').join('')+'</select><label><input id="onlyDiff" type="checkbox" checked> Только отличия</label><span id="count"></span></div>'+
'<div class="table"><table><thead><tr><th>№</th><th><input id="all" type="checkbox" checked></th><th>Товар</th><th>Категория</th><th>Статус</th><th>Цена</th></tr></thead><tbody id="tbody"></tbody></table></div>'+
'<div class="actions export"><button class="button" data-format="xlsx">↓  Скачать XLSX</button><button class="button secondary" data-format="csv">↓  Скачать CSV</button></div>';
document.getElementById('search').oninput=draw;document.getElementById('category').onchange=draw;document.getElementById('onlyDiff').onchange=draw;document.getElementById('all').onchange=e=>document.querySelectorAll('.pick').forEach(x=>x.checked=e.target.checked);
document.querySelectorAll('[data-format]').forEach(b=>b.onclick=()=>download(b.dataset.format));draw();}
function visible(){const q=(document.getElementById('search')?.value||'').toLowerCase(), category=document.getElementById('category')?.value, only=document.getElementById('onlyDiff')?.checked;return rows.filter(r=>(!only||r.status!=='same')&&(!category||r.category===category)&&(!q||(r.code+' '+r.name).toLowerCase().includes(q)));}
function draw(){const visibleRows=visible(), tbody=document.getElementById('tbody');tbody.innerHTML=visibleRows.map((r,index)=>'<tr><td>'+String(index+1)+'</td><td><input class="pick" type="checkbox" value="'+encodeURIComponent(r.code)+'" checked></td><td><strong>'+r.code+'</strong><br><span>'+r.name+'</span></td><td>'+r.category+'</td><td><span class="badge '+r.status+'">'+labels[r.status]+'</span></td><td>'+r.price.toFixed(2)+' PLN</td></tr>').join('');document.getElementById('count').textContent=visibleRows.length+' позиций';}
function download(format){const codes=[...document.querySelectorAll('.pick:checked')].map(x=>x.value).join(',');if(!codes)return;location.href='/generate?format='+format+'&codes='+codes;}
async function loadLog(){const target=document.getElementById('sync-log');try{const response=await fetch('/api/sync-log');const entries=await response.json();target.innerHTML=entries.length?entries.map(e=>'<div class="log-row"><span class="log-time">'+new Date(e.created_at).toLocaleString()+'</span><b class="badge '+e.status+'">'+e.kind+'</b><span>'+e.message+(e.external_id?' · '+e.external_id:'')+'</span></div>').join(''):'<p class="muted">Проверок пока не было.</p>';}catch(error){target.innerHTML='<p class="error">Журнал недоступен: '+error.message+'</p>';}}
document.getElementById('refresh-log').onclick=loadLog;loadLog();
async function loadYmLog(){const target=document.getElementById('ym-sync-log');try{const response=await fetch('/api/yandex-market-sync-log');const entries=await response.json();target.innerHTML=entries.length?entries.map(e=>'<div class="log-row"><span class="log-time">'+new Date(e.created_at).toLocaleString()+'</span><b class="badge '+e.status+'">'+e.kind+'</b><span>'+e.message+(e.external_id?' · '+e.external_id:'')+'</span></div>').join(''):'<p class="muted">Проверок пока не было.</p>';}catch(error){target.innerHTML='<p class="error">Журнал недоступен: '+error.message+'</p>';}}
document.getElementById('refresh-ym-log').onclick=loadYmLog;loadYmLog();
document.querySelectorAll('.tab-btn').forEach(btn=>btn.onclick=()=>{document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));btn.classList.add('active');document.getElementById('tab-'+btn.dataset.tab).classList.add('active');});
const flags={PL:'🇵🇱',LT:'🇱🇹',LV:'🇱🇻',EE:'🇪🇪'};
function defaultPeriod(){const to=new Date(),from=new Date();from.setDate(to.getDate()-30);const fmt=d=>d.toISOString().slice(0,10);document.getElementById('date-from').value=fmt(from);document.getElementById('date-to').value=fmt(to);}
defaultPeriod();
const loadAnalyticsBtn=document.getElementById('load-analytics'), analyticsResult=document.getElementById('analytics-result');
loadAnalyticsBtn.onclick=async()=>{loadAnalyticsBtn.disabled=true;loadAnalyticsBtn.textContent='Считаем…';analyticsResult.innerHTML='';
const from=document.getElementById('date-from').value, to=document.getElementById('date-to').value;
try{const response=await fetch('/api/sales-analytics?from='+from+'&to='+to);if(!response.ok)throw new Error(await response.text());const data=await response.json();renderAnalytics(data);}
catch(error){analyticsResult.innerHTML='<p class="error">Не удалось загрузить аналитику: '+error.message+'</p>';}
loadAnalyticsBtn.disabled=false;loadAnalyticsBtn.textContent='↻  Загрузить';};
function renderAnalytics(data){
const totalNet=data.reduce((sum,c)=>sum+c.net_sum,0);
analyticsResult.innerHTML='<div class="country-grid">'+data.map(c=>
'<div class="country-card"><h3><span class="flag">'+(flags[c.country]||'')+'</span>'+c.label+'</h3>'+
'<div class="metric"><span>Продажи</span><b>'+c.sales_sum.toLocaleString('ru-RU',{minimumFractionDigits:2})+' · '+c.sales_count+' шт</b></div>'+
'<div class="metric"><span>Возвраты</span><b>'+c.returns_sum.toLocaleString('ru-RU',{minimumFractionDigits:2})+' · '+c.returns_count+' шт</b></div>'+
'<div class="net">Итого: '+c.net_sum.toLocaleString('ru-RU',{minimumFractionDigits:2})+'</div></div>'
).join('')+'</div><p class="note"><strong>Итого по всем странам:</strong> '+totalNet.toLocaleString('ru-RU',{minimumFractionDigits:2})+' (в валюте каждого юрлица)</p>';
}
</script></body></html>""".encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body]
    if path == "/api/compare":
        settings = Settings.from_env()
        novicloud = NovicloudClient(base_url=settings.novicloud_base_url, version=settings.novicloud_api_version, account=settings.novicloud_account, password=settings.novicloud_password)
        moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
        try:
            comparison = compare_catalogs(moysklad.products(), novicloud.all_products())
        finally:
            novicloud.close()
            moysklad.close()
        public_rows = [{key: value for key, value in row.items() if key != "product"} for row in comparison]
        payload = dumps(public_rows, ensure_ascii=False).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [payload]
    if path == "/api/sync-log":
        payload = dumps(SyncLog().recent(), ensure_ascii=False, default=str).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [payload]
    if path == "/api/yandex-market-sync-log":
        payload = dumps(YandexMarketSyncLog().recent(), ensure_ascii=False, default=str).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [payload]
    if path == "/api/sales-analytics":
        params = parse_qs(environ.get("QUERY_STRING", ""))
        default_to = datetime.now()
        default_from = default_to - timedelta(days=30)
        date_from_raw = params.get("from", [""])[0]
        date_to_raw = params.get("to", [""])[0]
        try:
            date_from = datetime.fromisoformat(date_from_raw) if date_from_raw else default_from
            date_to = datetime.fromisoformat(date_to_raw) if date_to_raw else default_to
        except ValueError:
            start_response("400 Bad Request", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"Invalid date format, expected YYYY-MM-DD"]
        date_to = date_to.replace(hour=23, minute=59, second=59)
        settings = Settings.from_env()
        moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
        try:
            summary = country_sales_summary(moysklad, moment_from=date_from, moment_to=date_to)
        finally:
            moysklad.close()
        payload = dumps(summary, ensure_ascii=False).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [payload]
    if path != "/generate":
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not found"]
    settings = Settings.from_env()
    novicloud = NovicloudClient(base_url=settings.novicloud_base_url, version=settings.novicloud_api_version, account=settings.novicloud_account, password=settings.novicloud_password)
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    try:
        params = parse_qs(environ.get("QUERY_STRING", ""))
        codes = set(params.get("codes", [""])[0].split(",")) if params.get("codes") else set()
        rows = rows_for_codes(moysklad.products(), novicloud.all_products(), codes)
    finally:
        novicloud.close()
        moysklad.close()
    params = parse_qs(environ.get("QUERY_STRING", ""))
    format_name = params.get("format", ["xlsx"])[0]
    if format_name == "csv":
        content, content_type, filename = csv_bytes(rows), "text/csv; charset=utf-8", "novicloud-import.csv"
    else:
        content, content_type, filename = xlsx_bytes(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "novicloud-import.xlsx"
    start_response("200 OK", [("Content-Type", content_type), ("Content-Disposition", f'attachment; filename="{escape(filename)}"')])
    return [content]


def main() -> None:
    make_server("0.0.0.0", 8080, application).serve_forever()
