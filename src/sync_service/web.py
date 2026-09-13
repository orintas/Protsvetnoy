from __future__ import annotations

from html import escape
from json import dumps
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from .config import Settings
from .error_log import ErrorLog
from .import_file import compare_catalogs, csv_bytes, rows_for_codes, xlsx_bytes
from .moysklad import MoySkladClient
from .novicloud import NovicloudClient
from .shift_closer import ShiftCloseLog, list_open_shifts
from .sync_log import SyncLog
from .yandex_market_sync import YandexMarketSyncLog


def _ndjson_line(payload: dict) -> bytes:
    return dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"


def _compare_stream():
    """Yield one JSON line per real comparison stage as it actually starts.

    Each stage is written to the socket before the blocking call for that
    stage runs, so the client sees the label change in step with the real
    work instead of a fake looping timer.
    """
    errors = ErrorLog()
    try:
        settings = Settings.from_env()
        novicloud = NovicloudClient(base_url=settings.novicloud_base_url, version=settings.novicloud_api_version, account=settings.novicloud_account, password=settings.novicloud_password)
        moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    except Exception as error:
        errors.log_exception("web_compare", error, context="Не удалось подготовить клиентов для сравнения каталогов")
        yield _ndjson_line({"stage": "error", "message": str(error)})
        return
    try:
        yield _ndjson_line({"stage": "moysklad"})
        moysklad_products = moysklad.products()
        yield _ndjson_line({"stage": "novicloud"})
        novicloud_products = novicloud.all_products()
        yield _ndjson_line({"stage": "matching"})
        comparison = compare_catalogs(moysklad_products, novicloud_products)
        public_rows = [{key: value for key, value in row.items() if key != "product"} for row in comparison]
        yield _ndjson_line({"stage": "done", "rows": public_rows})
    except Exception as error:
        errors.log_exception("web_compare", error, context="Ошибка при сравнении каталогов МойСклад/Novicloud")
        yield _ndjson_line({"stage": "error", "message": str(error)})
    finally:
        novicloud.close()
        moysklad.close()


def application(environ, start_response):
    """Top-level entry point: dispatch the request and log any escaped error.

    Every handler below builds its full response before calling
    start_response, so on an exception here we can still safely send a 500
    instead of leaving the connection hanging silently.
    """
    path = environ.get("PATH_INFO", "/")
    try:
        return _dispatch(path, environ, start_response)
    except Exception as error:
        ErrorLog().log_exception("web", error, context=f"{environ.get('REQUEST_METHOD', 'GET')} {path}")
        start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Internal error, see error log"]


def _dispatch(path, environ, start_response):
    if path == "/":
        body = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="https://static.tildacdn.com/tild3935-3263-4363-a333-393162643930/__-removebg-preview.png">
<title>Varvikas | Цветной</title>
<style>
:root{color-scheme:dark;--bg:#090b12;--panel:#121622;--line:#252b3b;--text:#f6f7fb;--muted:#9aa3b8;--accent:#8b7cff;--accent2:#5eead4}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 10% 0,#25204c 0,transparent 35%),var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:42px 22px 60px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:.2px;cursor:pointer;background:none;border:0;color:inherit;font:inherit;padding:0}.mark{display:grid;place-items:center;width:42px;height:42px;border-radius:12px;background:#fff;box-shadow:0 8px 24px #8b7cff44;object-fit:contain;padding:6px}
.status{color:var(--accent2);font-size:13px;background:none;border:0;font-family:inherit;cursor:pointer;padding:0}.status:before{content:"";display:inline-block;width:7px;height:7px;margin:0 7px 1px 0;border-radius:50%;background:var(--accent2);box-shadow:0 0 12px var(--accent2)}.status.has-errors{color:#f87171}.status.has-errors:before{background:#f87171;box-shadow:0 0 12px #f87171}
.tab-badge{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:10px;background:#dc2626;color:#fff;font-size:11px;font-weight:700}.log-row.unread{background:#dc262614}.log-row.unread .log-time{color:#f87171}
.hero{max-width:710px;transition:.2s max-height,.2s opacity,.2s margin}.eyebrow{color:var(--accent2);font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}.hero h1{font-size:clamp(34px,6vw,64px);line-height:1.02;letter-spacing:-.05em;margin:14px 0 20px}.hero p{color:var(--muted);font-size:18px;max-width:610px;margin:0}
.hero.compact{max-height:0;opacity:0;margin:0;overflow:hidden;pointer-events:none}
.card{background:#121622cc;border:1px solid var(--line);border-radius:18px;padding:22px;backdrop-filter:blur(12px)}.card h2{font-size:17px;margin:0 0 6px}.card p{color:var(--muted);margin:0}
.accordion+.accordion{margin-top:16px}.accordion-header{display:flex;align-items:center;justify-content:space-between;gap:15px;cursor:pointer}.accordion-chevron{color:var(--muted);font-size:14px;flex-shrink:0;transition:.2s transform}.accordion.open .accordion-chevron{transform:rotate(90deg)}.accordion-body{margin-top:18px}.accordion-body[hidden]{display:none}
.help-btn{width:22px;height:22px;border-radius:50%;border:1px solid var(--line);background:#1b2130;color:var(--muted);font:700 12px inherit;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;padding:0;line-height:1}.help-btn:hover{color:var(--text);border-color:var(--accent)}
.modal-overlay{position:fixed;inset:0;background:#05060bcc;display:none;align-items:center;justify-content:center;padding:20px;z-index:50}.modal-overlay.open{display:flex}.modal{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:26px;max-width:520px;width:100%;max-height:80vh;overflow:auto}.modal h3{margin:0 0 14px;font-size:18px}.modal ol{margin:0;padding-left:20px;color:var(--muted);font-size:14px;line-height:1.7}.modal ol li strong{color:var(--text)}.modal-close{margin-top:20px}.log-row.clickable{cursor:pointer}.log-row.clickable:hover{background:#ffffff08}.detail-grid{display:grid;grid-template-columns:auto 1fr;gap:8px 16px;font-size:14px}.detail-grid dt{color:var(--muted)}.detail-grid dd{margin:0;color:var(--text)}
.actions{display:flex;gap:12px;flex-wrap:wrap}.button{display:inline-flex;align-items:center;justify-content:center;gap:9px;min-width:174px;padding:13px 18px;border:0;border-radius:11px;color:#fff;background:linear-gradient(135deg,var(--accent),#6d5dfc);font:600 14px inherit;text-decoration:none;cursor:pointer;box-shadow:0 10px 26px #6d5dfc33;transition:.2s transform,.2s filter}.button.secondary{background:#1b2130;box-shadow:none;border:1px solid #30384d}.button:hover{filter:brightness(1.12);transform:translateY(-2px)}.button:disabled{opacity:.65;cursor:wait;transform:none}
.note{border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:13px}.note strong{color:var(--text)}.toolbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:24px 0 14px;color:var(--muted);font-size:13px}.toolbar input[placeholder],.toolbar select{min-width:190px;background:#0d111b;border:1px solid var(--line);border-radius:9px;padding:10px 12px;color:var(--text)}.toolbar input[placeholder]{flex:1}.table{overflow:auto;border:1px solid var(--line);border-radius:12px}.table table{border-collapse:collapse;width:100%;min-width:720px}.table th,.table td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line)}.table th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}.table td:first-child,.table th:first-child{width:55px;color:var(--muted);text-align:right}.table td span{color:var(--muted);font-size:13px}.badge{display:inline-block!important;padding:4px 8px;border-radius:7px;font-size:12px!important;color:#fff!important;background:#334155}.badge.missing{background:#2563eb}.badge.archive{background:#b45309}.badge.price{background:#7c3aed}.badge.no_price{background:#dc2626}.badge.success,.badge.sale{background:#16a34a}.badge.error{background:#dc2626}.badge.return{background:#d97706}.badge.dry-run{background:#475569}.table tr.blocked{opacity:.55}
.progress-wrap{display:flex;align-items:center;gap:10px;font-size:13px;margin-top:14px}.progress-wrap[hidden]{display:none}.progress-bar{width:170px;height:8px;border-radius:6px;background:#1b2130;overflow:hidden;position:relative;flex-shrink:0}.progress-fill{position:absolute;top:0;left:-40%;width:40%;height:100%;border-radius:6px;background:linear-gradient(90deg,var(--accent),var(--accent2));animation:progress-slide 1.1s ease-in-out infinite}@keyframes progress-slide{0%{left:-40%}50%{left:60%}100%{left:100%}}.export{margin-top:18px}.log{margin-top:20px;max-height:360px;overflow:auto;border-top:1px solid var(--line)}.log-row{display:flex;align-items:center;gap:10px;padding:11px 0;border-bottom:1px solid var(--line);font-size:13px}.log-time{color:var(--muted);min-width:150px}.muted{color:var(--muted)}.error{color:#fca5a5;margin-top:20px}
.tabs{display:flex;gap:8px;margin:18px 0 26px;border-bottom:1px solid var(--line);flex-wrap:wrap}.tab-btn{background:none;border:0;color:var(--muted);font:600 14px inherit;padding:12px 6px;cursor:pointer;border-bottom:2px solid transparent;transition:.15s color,.15s border-color}.tab-btn.active{color:var(--text);border-bottom-color:var(--accent)}.tab-btn:hover{color:var(--text)}.tab-panel{display:none}.tab-panel.active{display:block}
@media(max-width:650px){.wrap{padding-top:24px}.top{margin-bottom:18px}.grid{grid-template-columns:1fr}.actions{flex-direction:column}.button{width:100%}.log-row{align-items:flex-start;flex-wrap:wrap}.log-time{min-width:130px}}
</style></head>
<body><main class="wrap">
<header class="top"><button class="brand" id="brand-home" type="button"><img class="mark" src="https://static.tildacdn.com/tild3935-3263-4363-a333-393162643930/__-removebg-preview.png" alt="Varvikas"><span>Varvikas | Цветной</span></button><button class="status" id="status-indicator" type="button">Система готова</button></header>
<section class="hero" id="hero"><div class="eyebrow">Ассортимент · синхронизация</div><h1>Единый центр<br>управления интеграциями.</h1><p>Сравнение ассортимента с Novicloud, журнал синхронизации продаж и возвратов, а также синхронизация заказов Яндекс.Маркета.</p></section>
<nav class="tabs">
<button class="tab-btn active" data-tab="catalog" type="button">Novicloud</button>
<button class="tab-btn" data-tab="moysklad" type="button">МойСклад</button>
<button class="tab-btn" data-tab="ozon" type="button">OZON</button>
<button class="tab-btn" data-tab="shopify" type="button">Shopify</button>
<button class="tab-btn" data-tab="ym-log" type="button">Яндекс.Маркет</button>
<button class="tab-btn" data-tab="errors" type="button">Ошибки<span class="tab-badge" id="errors-tab-badge" hidden></span></button>
</nav>
<section id="tab-catalog" class="tab-panel active">
<section class="card accordion" id="section-catalog">
<div class="accordion-header" data-section="catalog" role="button" tabindex="0">
<div style="display:flex;align-items:center;gap:8px"><h2>Синхронизация ассортимента</h2><button class="help-btn" id="catalog-help" type="button" aria-label="Как это работает" title="Как это работает">?</button></div>
<div style="display:flex;align-items:center;gap:14px"><button class="button" id="compare" type="button">↻&nbsp; Сравнить каталоги</button><span class="accordion-chevron">▸</span></div>
</div>
<div class="accordion-body" id="body-catalog" hidden>
<div id="compare-progress" class="progress-wrap" hidden><div class="progress-bar"><div class="progress-fill"></div></div><span id="progress-label" class="muted"></span></div>
<div id="result"></div>
</div></section>
<section class="card accordion" id="section-sales">
<div class="accordion-header" data-section="sales" role="button" tabindex="0">
<div><h2>Синхронизация продаж</h2><p>Тестовый режим: документы в МойСклад пока не создаются. Проверка выполняется каждые 5 минут.</p></div>
<div style="display:flex;align-items:center;gap:14px"><button class="button secondary" id="refresh-log" type="button">Обновить</button><span class="accordion-chevron">▸</span></div>
</div>
<div class="accordion-body" id="body-sales" hidden>
<div style="display:flex;justify-content:flex-end;margin-bottom:14px"><input id="log-search" placeholder="Поиск по номеру документа" style="background:#0d111b;border:1px solid var(--line);border-radius:9px;padding:10px 12px;color:var(--text);min-width:220px"></div>
<div id="sync-log" class="log"></div>
</div></section>
</section>
<section id="tab-moysklad" class="tab-panel">
<section class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap">
<div><h2 style="margin:0 0 6px">Закрытие смен — Польша, Литва, Латвия, Эстония</h2><p style="margin:0">Каждый день в 23:50 сервис проверяет, не остались ли незакрытые смены в этих магазинах, и закрывает их с датой закрытия 23:50 того же дня. Магазины России не затрагиваются. Закрытие запускает только сам сервис по расписанию — из интерфейса его инициировать нельзя, здесь только просмотр.</p></div>
<div style="display:flex;align-items:center;gap:12px"><span class="badge dry-run" id="shift-mode-badge">…</span><button class="button secondary" id="refresh-shifts" type="button">Обновить</button></div>
</div>
<h3 style="margin:20px 0 8px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Сейчас открыты</h3>
<div id="shift-open-list" class="log"></div>
<h3 style="margin:20px 0 8px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Журнал закрытий</h3>
<div id="shift-close-log" class="log"></div></section>
</section>
<section id="tab-ozon" class="tab-panel">
<section class="card"><h2 style="margin:0 0 6px">OZON</h2><p style="margin:0" class="muted">Интеграция с OZON пока не настроена. Раздел зарезервирован для будущей синхронизации.</p></section>
</section>
<section id="tab-shopify" class="tab-panel">
<section class="card"><h2 style="margin:0 0 6px">Shopify</h2><p style="margin:0" class="muted">Интеграция с Shopify пока не настроена. Раздел зарезервирован для будущей синхронизации.</p></section>
</section>
<section id="tab-ym-log" class="tab-panel">
<section class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:15px"><div><h2 style="margin:0 0 6px">Яндекс.Маркет — журнал синхронизации</h2><p style="margin:0">Тестовый режим: заказы читаются каждые 5 минут, документы в МойСклад пока не создаются.</p></div><button class="button secondary" id="refresh-ym-log" type="button">Обновить</button></div><div id="ym-sync-log" class="log"></div></section>
</section>
<section id="tab-errors" class="tab-panel">
<section class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap"><div><h2 style="margin:0 0 6px">Журнал ошибок</h2><p style="margin:0">Все ошибки API и синхронизаций сервиса — МойСклад, Novicloud, Яндекс.Маркет, веб-интерфейс — с полной трассировкой. Нажмите на запись, чтобы увидеть подробности.</p></div><button class="button secondary" id="refresh-errors" type="button">Обновить</button></div><div id="error-log" class="log"></div></section>
</section>
<div class="modal-overlay" id="log-detail-modal"><div class="modal"><h3 id="log-detail-title">Детали записи</h3><dl class="detail-grid" id="log-detail-body"></dl><button class="button secondary modal-close" id="log-detail-close" type="button">Закрыть</button></div></div>
<div class="modal-overlay" id="catalog-help-modal"><div class="modal"><h3>Как работает синхронизация ассортимента</h3><ol>
<li><strong>Сравнение.</strong> Нажмите «Сравнить каталоги» — сервер загрузит текущий ассортимент из Novicloud и МойСклад и сопоставит товары по коду/артикулу.</li>
<li><strong>Проверка отличий.</strong> В таблице увидите позиции, которых нет в Novicloud, товары для архивации и товары с расхождением цены. Товары без цены «Цена в Польше» в МойСклад помечаются отдельно и недоступны для выгрузки — сначала задайте цену в МойСклад. Совпадающие товары можно скрыть галочкой «Только отличия», отфильтровать по категории или найти по коду/названию.</li>
<li><strong>Выбор позиций.</strong> Отметьте галочками нужные строки (по умолчанию отмечены все).</li>
<li><strong>Выгрузка файла.</strong> Нажмите «Скачать XLSX» или «Скачать CSV» — сформируется файл только с отмеченными позициями в формате, готовом для импорта.</li>
<li><strong>Загрузка в Novicloud.</strong> Зайдите в панель управления Novicloud → раздел импорта товаров → загрузите скачанный файл, чтобы применить изменения ассортимента и цен.</li>
</ol><button class="button secondary modal-close" id="catalog-help-close" type="button">Закрыть</button></div></div>
</main><script>
const result=document.getElementById('result'), compare=document.getElementById('compare');
const progressWrap=document.getElementById('compare-progress'), progressLabel=document.getElementById('progress-label');
let rows=[];
const labels={missing:'Нет в Novicloud',archive:'Архивировать',price:'Изменить цену',same:'Совпадает',no_price:'Не задана цена в МойСклад'};
const stageLabels={moysklad:'Загружаем каталог МойСклад…',novicloud:'Загружаем каталог Novicloud…',matching:'Сопоставляем товары по артикулу и считаем статусы…'};
async function fetchCompareStream(){
const response=await fetch('/api/compare-stream');if(!response.ok)throw new Error(await response.text());
const reader=response.body.getReader(), decoder=new TextDecoder();let buffer='';
while(true){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let newlineAt;
while((newlineAt=buffer.indexOf('\\n'))>=0){const line=buffer.slice(0,newlineAt).trim();buffer=buffer.slice(newlineAt+1);if(!line)continue;
const event=JSON.parse(line);
if(event.stage==='error')throw new Error(event.message);
if(event.stage==='done')return event.rows;
progressLabel.textContent=stageLabels[event.stage]||'';}}
throw new Error('Соединение прервано до получения результата');}
compare.onclick=async()=>{openAccordion('section-catalog');compare.disabled=true;result.innerHTML='';progressLabel.textContent=stageLabels.moysklad;progressWrap.hidden=false;
try{rows=await fetchCompareStream();render();}
catch(error){result.innerHTML='<p class="error">Не удалось сравнить каталоги: '+error.message+'<br><span class="muted">Novicloud иногда отвечает с временной ошибкой — сервер уже делает несколько попыток автоматически. Нажмите «Сравнить каталоги» ещё раз через минуту.</span></p>';}
progressWrap.hidden=true;compare.disabled=false;compare.textContent='↻  Обновить сравнение';};
function render(){const diff=rows.filter(r=>r.status!=='same');result.innerHTML=
'<div class="toolbar"><input id="search" placeholder="Поиск по коду или названию"><select id="category"><option value="">Все категории</option>'+[...new Set(rows.map(r=>r.category))].sort().map(c=>'<option>'+c+'</option>').join('')+'</select><label><input id="onlyDiff" type="checkbox" checked> Только отличия</label><span id="count"></span></div>'+
'<div class="table"><table><thead><tr><th>№</th><th><input id="all" type="checkbox" checked></th><th>Товар</th><th>Категория</th><th>Статус</th><th>Цена</th></tr></thead><tbody id="tbody"></tbody></table></div>'+
'<div class="actions export"><button class="button" data-format="xlsx">↓  Скачать XLSX</button><button class="button secondary" data-format="csv">↓  Скачать CSV</button></div>';
document.getElementById('search').oninput=draw;document.getElementById('category').onchange=draw;document.getElementById('onlyDiff').onchange=draw;document.getElementById('all').onchange=e=>document.querySelectorAll('.pick').forEach(x=>x.checked=e.target.checked);
document.querySelectorAll('[data-format]').forEach(b=>b.onclick=()=>download(b.dataset.format));draw();}
function visible(){const q=(document.getElementById('search')?.value||'').toLowerCase(), category=document.getElementById('category')?.value, only=document.getElementById('onlyDiff')?.checked;return rows.filter(r=>(!only||r.status!=='same')&&(!category||r.category===category)&&(!q||(r.code+' '+r.name).toLowerCase().includes(q)));}
function draw(){const visibleRows=visible(), tbody=document.getElementById('tbody');tbody.innerHTML=visibleRows.map((r,index)=>{const blocked=r.status==='no_price';return '<tr'+(blocked?' class="blocked"':'')+'><td>'+String(index+1)+'</td><td>'+(blocked?'<input type="checkbox" disabled title="Сначала задайте цену в МойСклад">':'<input class="pick" type="checkbox" value="'+encodeURIComponent(r.code)+'" checked>')+'</td><td><strong>'+r.code+'</strong><br><span>'+r.name+'</span></td><td>'+r.category+'</td><td><span class="badge '+r.status+'">'+labels[r.status]+'</span></td><td>'+r.price.toFixed(2)+' PLN</td></tr>';}).join('');document.getElementById('count').textContent=visibleRows.length+' позиций';}
function download(format){const codes=[...document.querySelectorAll('.pick:checked')].map(x=>x.value).join(',');if(!codes)return;location.href='/generate?format='+format+'&codes='+codes;}
async function loadLog(){const target=document.getElementById('sync-log');try{const response=await fetch('/api/sync-log');allLogEntries=await response.json();renderLog();}catch(error){allLogEntries=[];target.innerHTML='<p class="error">Журнал недоступен: '+error.message+'</p>';}}
function docNumber(entry){try{const payload=JSON.parse(entry.payload);return payload&&payload.nr_dok?String(payload.nr_dok):(entry.external_id||'');}catch(e){return entry.external_id||'';}}
function renderLog(){const target=document.getElementById('sync-log'), query=(document.getElementById('log-search')?.value||'').trim().toLowerCase();
const filtered=allLogEntries.filter(e=>!query||docNumber(e).toLowerCase().includes(query));
target.innerHTML=filtered.length?filtered.map((e,i)=>'<div class="log-row clickable" data-log-index="'+i+'"><span class="log-time">'+new Date(e.created_at).toLocaleString()+'</span><b class="badge '+e.status+'">'+e.kind+'</b><span>'+e.message+(docNumber(e)?' · '+docNumber(e):'')+'</span></div>').join(''):'<p class="muted">'+(query?'Ничего не найдено.':'Проверок пока не было.')+'</p>';
target.querySelectorAll('[data-log-index]').forEach(row=>row.onclick=()=>showLogDetail(filtered[Number(row.dataset.logIndex)]));}
let allLogEntries=[];
document.getElementById('log-search').oninput=renderLog;
function fieldLabels(){return {id:'ID продажи',data:'Дата и время',nr_dok:'Номер документа',typ_dok:'Тип операции',nr_systemowy:'Системный номер',nr_fiskalny:'Фискальный номер',nr_rap_dobowego:'Номер суточного отчёта',ilosc:'Количество',cena:'Цена за ед.',cena_przed_rab:'Цена до скидки',stawka_vat:'Ставка НДС',brutto:'Сумма (брутто)',podatek:'Налог',rabat:'Скидка',orderId:'ID заказа',status:'Статус заказа',substatus:'Подстатус',createdAt:'Создан',updatedAt:'Обновлён',itemsTotal:'Сумма товаров',buyerTotal:'Сумма к оплате покупателем',name:'Номер смены',opened:'Открыта',retailStore:'Точка продаж'};}
function formatValue(key,value){if(value===null||value===undefined)return '—';
if(typeof value==='object'){if(key==='towar')return 'товар #'+(value.id??'');if(key==='sklep')return 'магазин #'+(value.id??'');if(key==='kasa')return 'касса #'+(value.id??'');if(key==='kasjer')return 'кассир #'+(value.id??'');if(Array.isArray(value)){if(key==='items')return value.map(it=>(it.offerId||it.offer_id||'?')+' × '+(it.count??it.quantity??'?')).join(', ');if(key==='platnosci')return value.map(p=>(p.wplata_waluta??'?')+' '+(p.kod_waluty??'')).join(', ');return value.length+' элемент(ов)';}return JSON.stringify(value);}
return String(value);}
function showLogDetail(entry){if(!entry)return;const modal=document.getElementById('log-detail-modal'), body=document.getElementById('log-detail-body'), title=document.getElementById('log-detail-title');
const doc=docNumber(entry);
title.textContent=(entry.kind==='sale'?'Продажа':entry.kind==='return'?'Возврат':entry.kind==='order'?'Заказ':'Событие')+(doc?' · '+doc:'');
let payload={};try{payload=JSON.parse(entry.payload);}catch(e){payload={};}
const labels=fieldLabels();
let rowsHtml='<dt>Сообщение</dt><dd>'+entry.message+'</dd><dt>Время проверки</dt><dd>'+new Date(entry.created_at).toLocaleString()+'</dd>';
if(payload&&typeof payload==='object'&&!Array.isArray(payload)){
rowsHtml+=Object.keys(payload).filter(k=>k!=='platnosci'||true).map(k=>'<dt>'+(labels[k]||k)+'</dt><dd>'+formatValue(k,payload[k])+'</dd>').join('');
}
body.innerHTML=rowsHtml;modal.classList.add('open');}
document.getElementById('log-detail-close').onclick=()=>document.getElementById('log-detail-modal').classList.remove('open');
document.getElementById('log-detail-modal').onclick=e=>{if(e.target.id==='log-detail-modal')e.currentTarget.classList.remove('open');};
document.getElementById('refresh-log').onclick=()=>{openAccordion('section-sales');loadLog();};loadLog();
const catalogHelpBtn=document.getElementById('catalog-help'), catalogHelpModal=document.getElementById('catalog-help-modal');
catalogHelpBtn.onclick=()=>catalogHelpModal.classList.add('open');
document.getElementById('catalog-help-close').onclick=()=>catalogHelpModal.classList.remove('open');
catalogHelpModal.onclick=e=>{if(e.target===catalogHelpModal)catalogHelpModal.classList.remove('open');};
async function loadYmLog(){const target=document.getElementById('ym-sync-log');try{const response=await fetch('/api/yandex-market-sync-log');const entries=await response.json();target.innerHTML=entries.length?entries.map((e,i)=>'<div class="log-row clickable" data-ym-log-index="'+i+'"><span class="log-time">'+new Date(e.created_at).toLocaleString()+'</span><b class="badge '+e.status+'">'+e.kind+'</b><span>'+e.message+(e.external_id?' · '+e.external_id:'')+'</span></div>').join(''):'<p class="muted">Проверок пока не было.</p>';target.querySelectorAll('[data-ym-log-index]').forEach(row=>row.onclick=()=>showLogDetail(entries[Number(row.dataset.ymLogIndex)]));}catch(error){target.innerHTML='<p class="error">Журнал недоступен: '+error.message+'</p>';}}
document.getElementById('refresh-ym-log').onclick=loadYmLog;loadYmLog();
async function loadShiftCloseLog(){const target=document.getElementById('shift-close-log'), badge=document.getElementById('shift-mode-badge');
try{const response=await fetch('/api/shift-close-log');const data=await response.json();const entries=data.entries||[];
badge.textContent=data.dry_run?'Тестовый режим':'Реальное закрытие';badge.className='badge '+(data.dry_run?'dry-run':'success');
target.innerHTML=entries.length?entries.map((e,i)=>'<div class="log-row clickable" data-shift-log-index="'+i+'"><span class="log-time">'+new Date(e.created_at).toLocaleString()+'</span><b class="badge '+e.status+'">'+e.kind+'</b><span>'+e.message+'</span></div>').join(''):'<p class="muted">Проверок пока не было.</p>';
target.querySelectorAll('[data-shift-log-index]').forEach(row=>row.onclick=()=>showLogDetail(entries[Number(row.dataset.shiftLogIndex)]));}
catch(error){target.innerHTML='<p class="error">Журнал недоступен: '+error.message+'</p>';}}
async function loadOpenShifts(){const target=document.getElementById('shift-open-list');
try{const response=await fetch('/api/shift-open');const data=await response.json();const shifts=data.shifts||[];
target.innerHTML=shifts.length?shifts.map(s=>'<div class="log-row"><span class="log-time">'+escapeHtml(s.opened||'')+'</span><b class="badge missing">'+escapeHtml(s.country)+'</b><span>Смена №'+escapeHtml(s.name)+'</span></div>').join(''):'<p class="muted">Незакрытых смен нет.</p>';}
catch(error){target.innerHTML='<p class="error">Список недоступен: '+escapeHtml(error.message)+'</p>';}}
document.getElementById('refresh-shifts').onclick=async()=>{const btn=document.getElementById('refresh-shifts');btn.disabled=true;
await Promise.all([loadOpenShifts(),loadShiftCloseLog()]);
btn.disabled=false;};
loadOpenShifts();loadShiftCloseLog();
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
let lastErrors=[];
function renderErrors(){const target=document.getElementById('error-log');
target.innerHTML=lastErrors.length?lastErrors.map((e,i)=>'<div class="log-row clickable'+(!e.read_at?' unread':'')+'" data-error-index="'+i+'"><span class="log-time">'+new Date(e.created_at).toLocaleString()+'</span><b class="badge error">'+escapeHtml(e.source)+'</b><span>'+escapeHtml(e.message)+'</span></div>').join(''):'<p class="muted">Ошибок не было.</p>';
target.querySelectorAll('[data-error-index]').forEach(row=>row.onclick=()=>showErrorDetail(lastErrors[Number(row.dataset.errorIndex)]));}
function showErrorDetail(entry){if(!entry)return;const modal=document.getElementById('log-detail-modal'), body=document.getElementById('log-detail-body'), title=document.getElementById('log-detail-title');
title.textContent='Ошибка · '+entry.source;
body.innerHTML='<dt>Время</dt><dd>'+new Date(entry.created_at).toLocaleString()+'</dd><dt>Источник</dt><dd>'+escapeHtml(entry.source)+'</dd><dt>Сообщение</dt><dd>'+escapeHtml(entry.message)+'</dd><dt>Подробности</dt><dd><pre style="white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px;max-height:320px;overflow:auto">'+escapeHtml(entry.details||'')+'</pre></dd>';
modal.classList.add('open');}
function updateErrorStatus(count){const indicator=document.getElementById('status-indicator'), badge=document.getElementById('errors-tab-badge');
if(count>0){indicator.textContent='Есть ошибки ('+count+')';indicator.classList.add('has-errors');badge.textContent=String(count);badge.hidden=false;}
else{indicator.textContent='Система готова';indicator.classList.remove('has-errors');badge.hidden=true;}}
async function refreshErrorStatus(){try{const response=await fetch('/api/errors');const data=await response.json();updateErrorStatus(data.unread_count);}catch(error){}}
async function openErrorsTab(){activateTab('errors');hero.classList.add('compact');
try{const response=await fetch('/api/errors');const data=await response.json();lastErrors=data.errors||[];renderErrors();
fetch('/api/errors/read',{method:'POST'}).then(()=>updateErrorStatus(0));}
catch(error){document.getElementById('error-log').innerHTML='<p class="error">Журнал ошибок недоступен: '+escapeHtml(error.message)+'</p>';}}
document.getElementById('refresh-errors').onclick=async()=>{const response=await fetch('/api/errors');const data=await response.json();lastErrors=data.errors||[];renderErrors();};
document.getElementById('status-indicator').onclick=openErrorsTab;
refreshErrorStatus();setInterval(refreshErrorStatus,60000);
const hero=document.getElementById('hero');
function activateTab(name){document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.id==='tab-'+name));}
function closeAccordions(){document.querySelectorAll('.accordion').forEach(s=>{s.classList.remove('open');s.querySelector('.accordion-body').hidden=true;});}
function openAccordion(id){closeAccordions();const section=document.getElementById(id);section.classList.add('open');section.querySelector('.accordion-body').hidden=false;hero.classList.add('compact');}
function toggleAccordion(section){if(section.classList.contains('open')){closeAccordions();}else{openAccordion(section.id);}}
document.querySelectorAll('.accordion-header').forEach(header=>{
header.onclick=e=>{if(e.target.closest('button'))return;toggleAccordion(header.closest('.accordion'));};
header.onkeydown=e=>{if(e.target.closest('button'))return;if(e.key==='Enter'||e.key===' '){e.preventDefault();toggleAccordion(header.closest('.accordion'));}};
});
document.querySelectorAll('.tab-btn').forEach(btn=>{btn.onclick=btn.dataset.tab==='errors'?openErrorsTab:()=>{activateTab(btn.dataset.tab);hero.classList.add('compact');};});
document.getElementById('brand-home').onclick=()=>{activateTab('catalog');hero.classList.remove('compact');closeAccordions();};
</script></body></html>""".encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body]
    if path == "/api/compare-stream":
        start_response("200 OK", [("Content-Type", "application/x-ndjson; charset=utf-8")])
        return _compare_stream()
    if path == "/api/sync-log":
        payload = dumps(SyncLog().recent(), ensure_ascii=False, default=str).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [payload]
    if path == "/api/yandex-market-sync-log":
        payload = dumps(YandexMarketSyncLog().recent(), ensure_ascii=False, default=str).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [payload]
    if path == "/api/shift-close-log":
        settings = Settings.from_env()
        payload = dumps(
            {"dry_run": settings.moysklad_shift_close_dry_run, "entries": ShiftCloseLog().recent()},
            ensure_ascii=False, default=str,
        ).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [payload]
    if path == "/api/shift-open":
        # Read-only: shows what's currently open. Closing only ever happens
        # from the scheduled worker at 23:50 Europe/Warsaw — there is no way
        # for a user action to trigger a real close.
        settings = Settings.from_env()
        client = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
        try:
            shifts = list_open_shifts(client)
        finally:
            client.close()
        payload = dumps({"shifts": shifts}, ensure_ascii=False, default=str).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [payload]
    if path == "/api/errors":
        error_log = ErrorLog()
        payload = dumps(
            {"unread_count": error_log.unread_count(), "errors": error_log.recent()},
            ensure_ascii=False, default=str,
        ).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [payload]
    if path == "/api/errors/read" and environ.get("REQUEST_METHOD") == "POST":
        ErrorLog().mark_all_read()
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8")])
        return [b'{"ok": true}']
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
