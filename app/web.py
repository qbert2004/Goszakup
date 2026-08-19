"""Локальная админка: ввод токена и настроек, проверка связи, диагностика.

Поднимается только на 127.0.0.1 — наружу не смотрит.
Секреты вводятся здесь и живут в config/settings.local.json (в .gitignore).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, diagnose, monitor, notify, scheduler, sheet
from .goszakup import GoszakupClient, GoszakupError
from .notify import TelegramError

log = logging.getLogger("monitor")

app = FastAPI(title="Мониторинг лотов goszakup")


@app.on_event("startup")
def _autostart_scheduler() -> None:
    """Запуск расписания без веб-админки.

    На headless-сервере нажать «Запустить» в браузере некому, а после
    перезагрузки (systemd поднимает сервис заново) расписание иначе не
    возобновилось бы. Поэтому: если токены уже заданы — стартуем сразу;
    если нет — молчим и ждём, пока их впишут в settings.local.json и
    перезапустят сервис. start() идемпотентен, повторно не навредит.
    """
    try:
        config.load_or_die()
    except Exception as exc:  # noqa: BLE001 — любой сбой конфига не должен ронять сервис
        # Не только «нет токенов» (RuntimeError), но и битый JSON, нет файла и т.п.
        # Веб-админка обязана подняться, чтобы настройки можно было починить.
        log.warning("расписание не запущено, проверьте settings.local.json: %s", exc)
        return
    scheduler.start()
    log.info("расписание запущено автоматически при старте сервиса")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/api/settings")
def get_settings() -> JSONResponse:
    settings = config.load()
    data = settings.masked()
    data["has_goszakup_token"] = bool(settings.goszakup_token)
    data["has_telegram_token"] = bool(settings.telegram_bot_token)
    return JSONResponse(data)


@app.post("/api/settings")
async def post_settings(payload: dict) -> JSONResponse:
    allowed = {
        "goszakup_token", "telegram_bot_token", "telegram_chat_id",
        "competition_bot_token", "competition_chat_id", "sheet_url",
        "window_hours_min", "respect_working_hours", "poll_interval_minutes",
        "max_applications", "min_competition", "min_amount",
    }
    changes = {k: v for k, v in payload.items() if k in allowed}
    for key in ("window_hours_min", "poll_interval_minutes", "max_applications"):
        if key in changes and changes[key] not in (None, ""):
            changes[key] = int(changes[key])
    # Пустое min_competition = выключить верхний диапазон (None), иначе целое.
    if "min_competition" in changes:
        value = changes["min_competition"]
        changes["min_competition"] = None if value in (None, "") else int(value)
    if "min_amount" in changes and changes["min_amount"] not in (None, ""):
        changes["min_amount"] = float(changes["min_amount"])
    if "respect_working_hours" in changes:
        changes["respect_working_hours"] = bool(changes["respect_working_hours"])
    settings = config.update(**changes)
    return JSONResponse({"ok": True, "settings": settings.masked()})


@app.post("/api/check-token")
def check_token() -> JSONResponse:
    token = config.load().goszakup_token
    if not token:
        return JSONResponse(
            {"ok": False, "message": "Токен ещё не сохранён."}, status_code=400
        )
    try:
        with GoszakupClient(token) as client:
            client.check_auth()
        return JSONResponse({"ok": True, "message": "Токен рабочий, площадка отвечает."})
    except GoszakupError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@app.post("/api/telegram/check")
def telegram_check() -> JSONResponse:
    token = config.load().telegram_bot_token
    try:
        me = notify.get_me(token)
        return JSONResponse({
            "ok": True,
            "message": f"Бот на связи: @{me.get('username')} ({me.get('first_name')}).",
        })
    except TelegramError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@app.post("/api/telegram/chats")
def telegram_chats() -> JSONResponse:
    token = config.load().telegram_bot_token
    try:
        chats = notify.discover_chats(token)
    except TelegramError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    if not chats:
        return JSONResponse({
            "ok": False,
            "message": (
                "Telegram не показал ни одного чата. Добавьте бота в группу и "
                "напишите там /start@имя_бота, потом нажмите ещё раз.\n"
                "Учтите: Telegram помнит сообщения примерно сутки."
            ),
        })
    return JSONResponse({"ok": True, "chats": chats})


@app.post("/api/telegram/test")
def telegram_test() -> JSONResponse:
    """Отправляет пробное сообщение. Вызывается только по кнопке в админке."""
    settings = config.load()
    if not settings.telegram_chat_id:
        return JSONResponse(
            {"ok": False, "message": "Сначала укажите Chat ID."}, status_code=400
        )
    sample = notify.format_lot({
        "name": "Коммутатор сетевой (управляемый, симметричный)",
        "applications": 0,
        "hours_left": 31,
        "customer": "ГУ «Пример-Тест»",
        "amount": 4_850_000,
        "lot_number": "12345678-ЗЦП1",
        "number_anno": "12345678-1",
        "url": "https://goszakup.gov.kz/ru/announce/index/12345678?tab=lots",
    })
    try:
        notify.send(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            "🔧 <i>Проверка связи. Так будут выглядеть уведомления о лотах:</i>\n\n"
            + sample,
        )
        return JSONResponse({"ok": True, "message": "Отправлено — проверьте чат."})
    except TelegramError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@app.get("/api/status")
def get_status() -> JSONResponse:
    return JSONResponse(scheduler.status())


@app.post("/api/scheduler/start")
def scheduler_start() -> JSONResponse:
    try:
        config.load_or_die()
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    scheduler.start()
    return JSONResponse({"ok": True, "message": "Расписание запущено."})


@app.post("/api/scheduler/stop")
def scheduler_stop() -> JSONResponse:
    scheduler.stop()
    return JSONResponse({"ok": True, "message": "Расписание остановлено."})


@app.post("/api/dry-run")
def dry_run() -> JSONResponse:
    """Полный проход БЕЗ отправки в Telegram и без отметок в базе."""
    settings = config.load()
    if not settings.goszakup_token:
        return JSONResponse(
            {"ok": False, "message": "Сначала сохраните токен площадки."},
            status_code=400,
        )
    try:
        codes = sheet.load_codes(settings.sheet_url)
    except sheet.SheetError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    try:
        found = monitor.dry_run(settings, codes)
    except GoszakupError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    return JSONResponse({
        "ok": True,
        "codes": len(codes),
        "names": len({c["name"] for c in codes if c.get("name")}),
        "matched": found.matched,
        "passed": found.passed,
        "wrong_code": found.wrong_code,
        "unverified": found.unverified,
        "name_mismatch": found.name_mismatch,
        "unreadable": found.unreadable,
        "lots": found.lots,
    })


@app.post("/api/diagnose")
def run_diagnose() -> JSONResponse:
    token = config.load().goszakup_token
    if not token:
        return JSONResponse(
            {"ok": False, "message": "Сначала сохраните токен."}, status_code=400
        )
    return JSONResponse(diagnose.run(token))


PAGE = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Мониторинг лотов goszakup</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #fff; --ink: #16181d; --muted: #6b7280;
    --line: #e3e6ea; --accent: #2563eb; --ok: #0f7b3f; --err: #b42318;
    --okbg: #eaf6ee; --errbg: #fdecea;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --card: #1c1f25; --ink: #e8eaed; --muted: #9aa2ae;
      --line: #2c3038; --accent: #5b8def; --ok: #4ade80; --err: #f87171;
      --okbg: #14301f; --errbg: #2f1616;
    }
  }
  * { box-sizing: border-box; }
  /* Атрибут hidden должен побеждать любой авторский display ниже. */
  [hidden] { display: none !important; }
  body {
    margin: 0; padding: 32px 20px; background: var(--bg); color: var(--ink);
    font: 15px/1.55 -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 28px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 22px; margin-bottom: 18px;
  }
  h2 { font-size: 15px; margin: 0 0 16px; text-transform: uppercase;
       letter-spacing: .05em; color: var(--muted); }
  label { display: block; font-weight: 600; margin: 0 0 6px; font-size: 14px; }
  .hint { color: var(--muted); font-size: 13px; margin: 6px 0 0; }
  input {
    width: 100%; padding: 10px 12px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--bg); color: var(--ink);
    font: inherit; font-family: ui-monospace, "Cascadia Code", monospace;
  }
  input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .field { margin-bottom: 18px; }
  /* Полей в строке стало пять — без переноса и минимума они схлопываются
     в колонки по одному слову. */
  .row { display: flex; gap: 14px; flex-wrap: wrap; }
  .row .field { flex: 1 1 200px; min-width: 180px; }
  .saved {
    display: inline-block; font-size: 12px; color: var(--ok);
    background: var(--okbg); padding: 2px 8px; border-radius: 999px;
    margin-left: 8px; font-weight: 500;
  }
  button {
    padding: 10px 18px; border-radius: 8px; border: 1px solid var(--line);
    background: var(--card); color: var(--ink); font: inherit; font-weight: 600;
    cursor: pointer;
  }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  button:disabled { opacity: .55; cursor: default; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .msg { margin-top: 14px; padding: 11px 14px; border-radius: 8px; font-size: 14px;
         display: none; white-space: pre-wrap; }
  .msg.ok { display: block; background: var(--okbg); color: var(--ok); }
  .msg.err { display: block; background: var(--errbg); color: var(--err); }
  .step { border-left: 3px solid var(--line); padding: 8px 0 8px 14px;
          margin-bottom: 12px; }
  .step.ok { border-color: var(--ok); }
  .step.bad { border-color: var(--err); }
  .step b { display: block; font-size: 14px; }
  .step span { color: var(--muted); font-size: 13px; }
  .verdict { padding: 14px; border-radius: 8px; background: var(--bg);
             border: 1px solid var(--line); margin-bottom: 18px; font-weight: 500; }
  .guide { margin-bottom: 20px; border: 1px solid var(--line); border-radius: 8px;
           padding: 12px 14px; background: var(--bg); }
  .guide summary { cursor: pointer; font-weight: 600; font-size: 14px; }
  .guide ol { margin: 12px 0 4px; padding-left: 22px; }
  .guide li { margin-bottom: 7px; font-size: 14px; }
  code { background: var(--line); padding: 1px 5px; border-radius: 4px;
         font-size: 13px; font-family: ui-monospace, monospace; }
  .chat {
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
    padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px;
    margin-top: 10px; font-size: 14px;
  }
  .chat b { display: block; }
  .chat span { color: var(--muted); font-size: 13px;
               font-family: ui-monospace, monospace; }
  .chat button { padding: 6px 12px; font-size: 13px; white-space: nowrap; }
  .chat a { text-decoration: none; }
  .check label { font-weight: 500; display: flex; align-items: center; gap: 8px;
                 cursor: pointer; }
  .check input { width: auto; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Мониторинг лотов goszakup</h1>
  <p class="sub">Лоты по вашим ЕНСТРУ · 0 заявок · за сутки до конца приёма</p>

  <div class="card">
    <h2>Доступ к площадке</h2>
    <div class="field">
      <label for="tok">Токен API goszakup <span id="tokSaved" class="saved" hidden>сохранён</span></label>
      <input id="tok" type="password" autocomplete="off" spellcheck="false"
             placeholder="вставьте токен сюда">
      <p class="hint">Токен из личного кабинета goszakup.gov.kz. Сохраняется локально в
         <code>config/settings.local.json</code>, в git не попадает. Поле пустое = оставить прежний.</p>
    </div>
    <div class="actions">
      <button class="primary" onclick="save()">Сохранить</button>
      <button onclick="checkToken()">Проверить связь</button>
      <button onclick="runDiag()" id="diagBtn">Запустить диагностику</button>
    </div>
    <div id="accessMsg" class="msg"></div>
  </div>

  <div class="card">
    <h2>Уведомления в Telegram</h2>

    <details class="guide">
      <summary>Как получить токен и chat_id — пошагово</summary>
      <ol>
        <li>В Telegram найдите <b>@BotFather</b> → <code>/newbot</code>.</li>
        <li>Придумайте имя (любое) и username (обязан кончаться на <code>bot</code>,
            например <code>tender_alert_bot</code>).</li>
        <li>BotFather пришлёт токен вида <code>123456789:AAF...</code> — вставьте его
            в поле ниже и нажмите «Сохранить», затем «Проверить бота».</li>
        <li>Создайте группу для менеджеров и <b>добавьте туда бота</b>.</li>
        <li>Напишите в группе <code>/start@имя_вашего_бота</code> — именно с
            упоминанием бота, иначе он сообщение не увидит.</li>
        <li>Нажмите «Найти chat_id» — он подставится сам.</li>
        <li>«Отправить тест» — и проверьте, что сообщение дошло.</li>
      </ol>
    </details>

    <div class="field">
      <label for="tgTok">Токен бота <span id="tgSaved" class="saved" hidden>сохранён</span></label>
      <input id="tgTok" type="password" autocomplete="off" placeholder="123456789:AAF...">
    </div>
    <div class="field">
      <label for="tgChat">Chat ID</label>
      <input id="tgChat" placeholder="-1001234567890">
      <p class="hint">ID группы менеджеров. У групп он отрицательный.</p>
    </div>
    <div class="actions">
      <button class="primary" onclick="save()">Сохранить</button>
      <button onclick="tgCheck()">Проверить бота</button>
      <button onclick="tgChats()">Найти chat_id</button>
      <button onclick="tgTest()">Отправить тест</button>
    </div>
    <div id="chats"></div>
    <div id="tgMsg" class="msg"></div>
  </div>

  <div class="card">
    <h2>Отбор лотов</h2>
    <div class="field">
      <label for="sheet">Google-таблица с ЕНСТРУ</label>
      <input id="sheet">
      <p class="hint">Список пополняется прямо в таблице — код трогать не нужно.
         Доступ: «Просматривать могут все, у кого есть ссылка».</p>
    </div>
    <div class="row">
      <div class="field">
        <label for="hmin">Запас до дедлайна, часов</label>
        <input id="hmin" type="number" min="1">
        <p class="hint">Позже этого срока молчать нельзя — уведомление уйдёт обязательно.</p>
      </div>
      <div class="field">
        <label for="maxapp">Заявок не больше</label>
        <input id="maxapp" type="number" min="0">
        <p class="hint">0 = только нулевая конкуренция, как в ТЗ. Больше — режим
           «низкая конкуренция», удобен и для проверки системы на живых данных.</p>
      </div>
      <div class="field">
        <label for="minamt">Сумма лота не меньше, ₸</label>
        <input id="minamt" type="number" min="0" step="100000">
        <p class="hint">0 — брать любые. Мелочь на 20–60 тысяч только зашумляет чат.</p>
      </div>
      <div class="field">
        <label for="interval">Опрос каждые, минут</label>
        <input id="interval" type="number" min="5">
        <p class="hint" id="intervalWarn"></p>
      </div>
    </div>
    <div class="field check">
      <label><input id="work" type="checkbox"> Считать запас по рабочим дням</label>
      <p class="hint">Дедлайны на выходные площадка почти не ставит (сб 18, вс 7 из 3272).
         Без этой поправки лот с дедлайном в понедельник попадёт в окно в воскресенье —
         менеджер увидит его в понедельник утром, когда осталась пара часов.
         С поправкой такой лот показывается с утра пятницы.</p>
    </div>
    <div class="actions">
      <button class="primary" onclick="save()">Сохранить</button>
      <button onclick="dryRun()" id="dryBtn">Проверить сейчас (без отправки)</button>
    </div>
    <div id="filtMsg" class="msg"></div>
    <div id="dryResult"></div>
  </div>

  <div class="card">
    <h2>Расписание</h2>
    <div id="schedState" class="verdict">загружаю…</div>
    <div class="actions">
      <button class="primary" onclick="schedStart()">Запустить</button>
      <button onclick="schedStop()">Остановить</button>
    </div>
    <div id="schedMsg" class="msg"></div>
    <div id="runs"></div>
  </div>

  <div class="card" id="diagCard" hidden>
    <h2>Результат диагностики</h2>
    <div id="verdict" class="verdict"></div>
    <div id="steps"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

function show(el, text, ok) {
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "err");
}

async function load() {
  const s = await (await fetch("/api/settings")).json();
  $("tgChat").value = s.telegram_chat_id || "";
  $("sheet").value = s.sheet_url || "";
  $("hmin").value = s.window_hours_min;
  $("maxapp").value = s.max_applications;
  $("minamt").value = s.min_amount;
  $("work").checked = !!s.respect_working_hours;
  $("interval").value = s.poll_interval_minutes;
  warnInterval(s);
  $("tokSaved").hidden = !s.has_goszakup_token;
  $("tgSaved").hidden = !s.has_telegram_token;
  if (s.has_goszakup_token) $("tok").placeholder = s.goszakup_token + " — оставьте пустым, чтобы не менять";
  if (s.has_telegram_token) $("tgTok").placeholder = s.telegram_bot_token + " — оставьте пустым, чтобы не менять";
}

async function save() {
  const body = {
    goszakup_token: $("tok").value,
    telegram_bot_token: $("tgTok").value,
    telegram_chat_id: $("tgChat").value,
    sheet_url: $("sheet").value,
    window_hours_min: $("hmin").value,
    max_applications: $("maxapp").value,
    min_amount: $("minamt").value,
    respect_working_hours: $("work").checked,
    poll_interval_minutes: $("interval").value,
  };
  const r = await fetch("/api/settings", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const d = await r.json();
  show($("accessMsg"), d.ok ? "Настройки сохранены." : "Не удалось сохранить.", d.ok);
  $("tok").value = ""; $("tgTok").value = "";
  await load();
}

async function post(url) {
  const r = await fetch(url, {method: "POST"});
  return await r.json();
}

async function tgCheck() {
  show($("tgMsg"), "Проверяю…", true);
  const d = await post("/api/telegram/check");
  show($("tgMsg"), d.message, d.ok);
}

async function tgChats() {
  show($("tgMsg"), "Спрашиваю Telegram…", true);
  const d = await post("/api/telegram/chats");
  if (!d.ok) { show($("tgMsg"), d.message, false); $("chats").innerHTML = ""; return; }
  $("tgMsg").className = "msg";
  $("chats").innerHTML = d.chats.map(c => `
    <div class="chat">
      <div><b>${c.title || "без названия"}</b><span>${c.id} · ${c.type}</span></div>
      <button onclick="useChat('${c.id}')">Взять этот</button>
    </div>`).join("");
}

async function useChat(id) {
  $("tgChat").value = id;
  await save();
  show($("tgMsg"), "Chat ID подставлен и сохранён. Теперь «Отправить тест».", true);
}

async function tgTest() {
  show($("tgMsg"), "Отправляю…", true);
  const d = await post("/api/telegram/test");
  show($("tgMsg"), d.message, d.ok);
}

function warnInterval(s) {
  // Худший запас у менеджера = запас минус интервал скана. Редкий скан молча
  // рушит обещание "за сутки": лот может прийти за час до дедлайна.
  const worst = s.window_hours_min - s.poll_interval_minutes / 60;
  const el = $("intervalWarn");
  if (worst < 1) {
    el.textContent = `Внимание: при таком интервале лот может прийти за ${Math.max(0, worst).toFixed(0)} ч ` +
      `до дедлайна — обещание «за сутки» не выполняется. Уменьшите интервал или увеличьте запас.`;
    el.style.color = "var(--err)";
  } else if (worst < s.window_hours_min * 0.8) {
    el.textContent = `В худшем случае лот придёт за ${worst.toFixed(0)} ч до дедлайна (запас ${s.window_hours_min} ч).`;
    el.style.color = "var(--muted)";
  } else {
    el.textContent = `В худшем случае лот придёт за ${worst.toFixed(0)} ч до дедлайна — запас соблюдается.`;
    el.style.color = "var(--ok)";
  }
}

async function loadStatus() {
  const s = await (await fetch("/api/status")).json();
  $("schedState").textContent = s.running
    ? "Работает. Последний проход: " + (s.last_run || "ещё не было") +
      (s.last_error ? " · последняя ошибка: " + s.last_error : "")
    : "Остановлено — уведомления не приходят.";
  $("runs").innerHTML = (s.runs || []).map(r => `
    <div class="chat">
      <div><b>${r.started_at}</b>
      <span>${r.error ? "ошибка: " + r.error
        : `в окне ${r.matched}, отправлено ${r.notified}`}</span></div>
    </div>`).join("");
}

async function schedStart() {
  const d = await post("/api/scheduler/start");
  show($("schedMsg"), d.message, d.ok);
  loadStatus();
}

async function schedStop() {
  const d = await post("/api/scheduler/stop");
  show($("schedMsg"), d.message, d.ok);
  loadStatus();
}

async function dryRun() {
  const btn = $("dryBtn");
  btn.disabled = true;
  show($("filtMsg"), "Обхожу площадку по всем ЕНСТРУ — это несколько минут…", true);
  try {
    const r = await fetch("/api/dry-run", {method: "POST"});
    const d = await r.json();
    if (!d.ok) { show($("filtMsg"), d.message, false); return; }

    let note = `В таблице ${d.codes} кодов / ${d.names} позиций. ` +
               `В окне уведомления: ${d.matched}. Из них с 0 заявок: ${d.passed}.`;
    if (d.wrong_code) note += `\nОтсеяно: код ЕНСТРУ не из нашей таблицы: ${d.wrong_code}.`;
    if (d.unverified) note += `\nКод не сверился, взяли по точному названию: ${d.unverified}.`;
    if (d.name_mismatch) note += `\nОтсеяно: ни код, ни название не наши: ${d.name_mismatch}.`;
    if (d.unreadable.length) note += `\nСчётчик не прочитался: ${d.unreadable.length} — по ним промолчали.`;
    show($("filtMsg"), note, true);

    $("dryResult").innerHTML = d.lots.length
      ? d.lots.map(l => `
          <div class="chat">
            <div>
              <b>${l.name || ""}</b>
              <span>${l.customer || ""} · ${l.method || ""} ·
                    до конца ${l.hours_left}ч · ${l.enstru_code || "код не сверен"}</span>
            </div>
            <a href="${l.url}" target="_blank"><button>Лот</button></a>
          </div>`).join("")
      : `<p class="hint">Ни одного лота с нулём заявок в окне — это нормально:
         значит прямо сейчас по вашим позициям нечего ловить.</p>`;
  } finally {
    btn.disabled = false;
  }
}

async function checkToken() {
  show($("accessMsg"), "Проверяю…", true);
  const r = await fetch("/api/check-token", {method: "POST"});
  const d = await r.json();
  show($("accessMsg"), d.message, d.ok);
}

async function runDiag() {
  const btn = $("diagBtn");
  btn.disabled = true;
  show($("accessMsg"), "Диагностика идёт — это до минуты, площадка отвечает медленно…", true);
  try {
    const r = await fetch("/api/diagnose", {method: "POST"});
    const d = await r.json();
    if (d.message && !d.steps) { show($("accessMsg"), d.message, false); return; }
    $("accessMsg").className = "msg";
    $("diagCard").hidden = false;
    $("verdict").textContent = d.verdict;
    $("steps").innerHTML = d.steps.map(s =>
      `<div class="step ${s.ok ? "ok" : "bad"}">
         <b>${s.ok ? "✓" : "✗"} ${s.name}</b><span>${s.detail}</span>
       </div>`).join("");
    $("diagCard").scrollIntoView({behavior: "smooth"});
  } finally {
    btn.disabled = false;
  }
}

load();
loadStatus();
setInterval(loadStatus, 30000);
</script>
</body>
</html>
"""
