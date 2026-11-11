# -*- coding: utf-8 -*-
"""MTS Receipt Generator — Telegram Bot v4. Удобства, история, шаблоны, /help, /stats."""
import asyncio, io, json, os, sys, logging, re, random, uuid, time, zlib
import html
from datetime import datetime, timedelta, timezone
from functools import partial
from collections import deque, Counter
from concurrent.futures import ThreadPoolExecutor

# ── Feature flags: всё обратимо — выключить фичу = вернуть старое поведение ──
# Глобальные (gfeat) — мастер-переключатели разработчика.
# Пользовательские (feat(uid,name)) — выдаёт админ через /grant.
#   feat(uid,name) = gfeat(name) AND (is_admin(uid) OR data['feature_flags'][uid][name])
FEATURE_FLAGS = {
    # ── глобальные переключатели (влияют на всех) ──
    "help_cmd":        True,   # /help — список возможностей
    "history_btn":     True,   # «История» — последние 10 чеков
    "templates":       True,   # Шаблоны получателей (СБП)
    "log_all_banks":   True,   # писать в logs чеки ВБ/ВТБ/РСХБ/Озон (для истории/статистики)
    "rich_menu":       True,   # группировка меню по секциям с заголовками
    "check_pdf_fix":   True,   # починить сломанный обработчик проверки чека
    "free_text":       True,   # свободный ввод на шаге name: «перевод 2500 Пете в тбанк»
    # ── per-user фичи (мастер-флаг; доступ выдаётся через /grant) ──
    "fast_gen":        True,   # ⚡ Быстрая генерация (рандомные параметры одной кнопкой)
    "batch":           True,   # 📦 Пакет чеков (N чеков сразу)
    "check_pdf":       True,   # 🔍 Проверить чек
    "stats":           True,   # 📊 Статистика
    "dark_theme":      True,   # 🌙 Тёмная тема (переключатель)
    "notifications":   True,   # 🔔 Уведомления о готовности чека
    "api_token":       True,   # 🔑 Персональный API-токен
    "ozon_v2":         True,   # 🧾 Озон v2: отдельный отправитель + preview (ozon_ux)
}
PER_USER_FEATURES = ["fast_gen", "batch", "check_pdf", "stats",
                      "dark_theme", "notifications", "api_token", "ozon_v2"]
FEATURE_LABELS = {
    "fast_gen":      "⚡ Быстрая генерация",
    "batch":         "📦 Пакет чеков",
    "check_pdf":     "🔍 Проверить чек",
    "stats":         "📊 Статистика",
    "dark_theme":    "🌙 Тёмная тема",
    "notifications": "🔔 Уведомления",
    "api_token":     "🔑 API-токен",
    "ozon_v2":       "🧾 Озон v2 (preview/отправитель)",
}
def gfeat(name): return FEATURE_FLAGS.get(name, False)
def feat(uid, name):
    """Per-user feature gate. Админ всегда имеет доступ. Глобальный флаг = мастер."""
    if not gfeat(name):
        return False
    if is_admin(uid):
        return True
    return bool(_fflags(uid).get(name, False))

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (Message, CallbackQuery, BufferedInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup)
from aiogram.filters import Command, CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

import aiohttp

# ── Свободный ввод: «перевод 2500 Пете в тбанк» — автодетекция банка/суммы/имени ──
# Модуль чистый (без I/O), покрыт 36/36 юнит-тестов (test_bank_detector.py).
try:
    import bank_detector as _bd
except Exception:
    _bd = None
    logging.exception("bank_detector import failed; free-text flow disabled")

# ── Ozon v2 UX-ядро (чистое, без I/O): отдельный отправитель, preview,
# edit-single-field, безопасный gate ozon_edit. За флагом ozon_v2;
# при недоступности модуля старый Ozon-flow работает как раньше (rollback).
try:
    import ozon_ux as ozx
except Exception:
    ozx = None
    logging.exception("ozon_ux import failed; ozon v2 flow disabled")

TOKEN = "9003512034:AAHtnt0lEMEAO2f99mS6fLwRv0bkOb9C8kc"
ADMIN_ID = 7076663447
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mts_bot_data.json")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

API_BASE = os.environ.get("API_BASE", "http://localhost:3012/api")
API_TOKEN = os.environ.get("INTERNAL_TOKEN", "internal-change-me")

async def api_call(method: str, path: str, json_data=None):
    """Никогда не бросает: (None, {"error": ...}) при сетевом сбое, иначе
    (HTTP-статус, dict). Терпит не-JSON ответы (502/HTML-страницы ошибок)."""
    headers = {"X-Internal-Token": API_TOKEN, "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, f"{API_BASE}{path}",
                                       json=json_data, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                try:
                    return resp.status, await resp.json(content_type=None)
                except Exception:
                    return resp.status, {"error": (await resp.text())[:200]}
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}

BANKS = ["Т-Банк", "Сбербанк", "Совкомбанк", "ВТБ", "Альфа-Банк",
         "ЮМани", "Райффайзен Банк", "Озон Банк", "Газпромбанк"]

def load():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            # Битый JSON не должен ронять бота в boot-loop: откладываем файл.
            try: os.replace(DATA_FILE, DATA_FILE + ".broken")
            except OSError: pass
            return {"users": [ADMIN_ID], "logs": [], "templates": {}}
    else:
        d = {"users": [ADMIN_ID], "logs": []}
    # lazy migrations — старые файлы данных не имеют этих ключей
    if "templates" not in d or not isinstance(d.get("templates"), dict):
        d["templates"] = {}
    if "feature_flags" not in d or not isinstance(d.get("feature_flags"), dict):
        d["feature_flags"] = {}
    # гарантируем запись для каждого юзера (пустая = все фичи закрыты)
    for u in d["users"]:
        key = str(u)
        if not isinstance(d["feature_flags"].get(key), dict):
            d["feature_flags"][key] = {}
    return d
def save(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

data = load()

# ── Sessions with idle TTL (before: a forgotten session hung forever) ──
SESSION_TTL = int(os.environ.get("SESSION_TTL_MIN", "15")) * 60

class SessionStore(dict):
    """uid -> flow state with an idle timeout. Reads auto-expire stale entries
    and touch live ones, so an abandoned flow can't swallow the next message."""

    def _stale(self, uid) -> bool:
        ts = (dict.get(self, uid) or {}).get("_ts")
        return ts is not None and (time.time() - ts) > SESSION_TTL

    def __setitem__(self, uid, s):
        if isinstance(s, dict):
            s["_ts"] = time.time()
        dict.__setitem__(self, uid, s)

    def __getitem__(self, uid):
        if self._stale(uid):
            dict.pop(self, uid, None)
            raise KeyError(uid)
        s = dict.__getitem__(self, uid)
        if isinstance(s, dict):
            s["_ts"] = time.time()  # touch: an active flow never expires
        return s

    def get(self, uid, default=None):
        try:
            return self[uid]
        except KeyError:
            return default

    def __contains__(self, uid):
        if self._stale(uid):
            dict.pop(self, uid, None)
            return False
        return dict.__contains__(self, uid)

sessions = SessionStore()

# ── Generation queue: dedicated pool + bounded concurrency + metrics ──
GEN_WORKERS = int(os.environ.get("GEN_WORKERS", "2"))
_gen_sem: asyncio.Semaphore | None = None
_gen_pool: ThreadPoolExecutor | None = None
_gen_waiting = 0  # jobs queued, waiting for a free worker

PERF = {"gen_times": deque(maxlen=100), "queue_waits": deque(maxlen=100),
        "gens_total": 0, "sessions_expired": 0}

def _gs():
    global _gen_sem
    if _gen_sem is None:
        _gen_sem = asyncio.Semaphore(GEN_WORKERS)  # first use is inside the running loop
    return _gen_sem

def _gp():
    global _gen_pool
    if _gen_pool is None:
        _gen_pool = ThreadPoolExecutor(max_workers=GEN_WORKERS, thread_name_prefix="pdfgen")
    return _gen_pool

def gen_queue_ahead() -> int:
    """How many generation jobs are queued ahead of a new one."""
    return _gen_waiting

async def run_gen(func, /, *args, **kw):
    """Run a blocking PDF generator off the event loop with at most GEN_WORKERS
    concurrent jobs; the rest wait in a queue. Records PERF timings."""
    global _gen_waiting
    loop = asyncio.get_running_loop()
    call = partial(func, *args, **kw)
    _gen_waiting += 1
    t0 = time.monotonic()
    acquired = False
    try:
        async with _gs():
            acquired = True
            _gen_waiting -= 1
            waited = time.monotonic() - t0
            PERF["queue_waits"].append(waited)
            t1 = time.monotonic()
            try:
                return await loop.run_in_executor(_gp(), call)
            finally:
                dt = time.monotonic() - t1
                PERF["gen_times"].append(dt)
                PERF["gens_total"] += 1
                logging.info("gen done in %.2fs (queue wait %.2fs, workers=%d)",
                             dt, waited, GEN_WORKERS)
    finally:
        if not acquired:
            _gen_waiting -= 1

def _queue_text(ahead: int) -> str:
    return (f"\U0001f465 <b>Очередь на генерацию</b> — перед тобой: {ahead}\n"
            "\u23f3 Обычно ожидание < 10 сек\u2026")

def _perf_summary() -> str:
    def stat(dq):
        if not dq:
            return "—"
        return f"ср {sum(dq)/len(dq):.2f}с / макс {max(dq):.2f}с (n={len(dq)})"
    return (f"Генерации: {PERF['gens_total']} шт, {stat(PERF['gen_times'])}\n"
            f"Ожидание в очереди: {stat(PERF['queue_waits'])}\n"
            f"Воркеров генерации: {GEN_WORKERS}\n"
            f"Сессий истекло по таймауту: {PERF['sessions_expired']}\n"
            f"Активных сессий: {len(sessions)}")

lk_pendings: dict = data.get("lk_pendings") or {}
_lk_busy: set = set()  # gen_ids with an add-to-LK request in flight (anti double-tap)

async def _session_sweeper():
    """Раз в минуту выбрасывает сессии, неактивные дольше SESSION_TTL
    (метка _ts обновляется SessionStore на каждое сообщение и нажатие),
    и честно говорит пользователю, что ввод истёк, вместо молчаливого сброса."""
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            stale = [uid for uid, s in list(sessions.items())
                     if isinstance(s, dict) and s.get("_ts")
                     and now - s["_ts"] > SESSION_TTL]
            for uid in stale:
                s = sessions.pop(uid, None) or {}
                PERF["sessions_expired"] += 1
                logging.info("session expired uid=%s step=%s", uid, s.get("step"))
                if s.get("step") and s.get("step") != "gen_busy":
                    try:
                        await bot.send_message(
                            uid,
                            f"\u23f1 Сессия истекла ({SESSION_TTL // 60} мин бездействия). Начни заново:",
                            reply_markup=main_menu(uid))
                    except Exception:
                        pass
        except Exception:
            logging.exception("session sweeper")


def _lk_store(uid, payload, again) -> str:
    gen_id = uuid.uuid4().hex[:10]
    lk_pendings[gen_id] = {"uid": uid, "payload": payload, "again": again,
                           "done": False, "ts": datetime.now().timestamp()}
    if len(lk_pendings) > 60:
        for k, _ in sorted(lk_pendings.items(), key=lambda kv: kv[1].get("ts", 0))[:len(lk_pendings) - 60]:
            lk_pendings.pop(k, None)
    data["lk_pendings"] = lk_pendings
    save(data)
    return gen_id

def _iso_from_msk(dt) -> str:
    """Naive MSK datetime -> UTC ISO string for the LK API."""
    return (dt - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

def _lk_owner(uid) -> str | None:
    """Tenant for the 'В ЛК' button: ADMIN for the owner, the bound invite
    label for everyone else (None = not bound yet)."""
    if uid == ADMIN_ID:
        return "ADMIN"
    return (data.get("lk_bindings") or {}).get(str(uid))


def _lk_payload_sbp(s, meta, owner=None) -> dict:
    payload = {
        "bankType": "mts",
        "date": _iso_from_msk(meta["dt"]),
        "amount": s["amount"],
        "direction": "OUTCOME",
        "description": s["name"],
        "category": "transfer",
        "recipientName": s["name"],
        "recipientPhone": s["phone"],
        "recipientBank": s["bank"],
        "metadata": {
            "comment": "Перевод СБП",
            "commission": "0 ₽",
            "paymentIdentifier": meta["tr"],
            "clickReference": meta["sbp"],
            "debitAccount": meta["account"],
            "recipientFullName": s["name"],
        },
    }
    if owner:
        payload["owner"] = owner
    return payload

def _lk_payload_card(amount: int, meta, debit_mask: str, credit_mask: str, owner=None) -> dict:
    payload = {
        "bankType": "mts",
        "date": _iso_from_msk(meta["dt"]),
        "amount": meta["total"],
        "direction": "OUTCOME",
        "description": "Перевод с карты",
        "category": "transfer",
        "recipientName": "Перевод с карты",
        "metadata": {
            "transferKind": "card",
            "paymentIdentifier": meta["tr"],
            "debitCard": debit_mask,
            "creditCard": credit_mask,
            "detailAmount": str(amount),
            "commission": f"{meta['comm']} ₽",
            "comment": "",
            "paymentDetails": "",
        },
    }
    if owner:
        payload["owner"] = owner
    return payload

_gen = None
def gen():
    global _gen
    if _gen is None:
        from mts_generator import generate, check_name, format_amount
        _gen = {"generate": generate, "check": check_name, "fmt": format_amount}
    return _gen

_mtsgen = None
def mgen():
    """Модуль mts_generator целиком (для якорей кода транзакции)."""
    global _mtsgen
    if _mtsgen is None:
        import mts_generator as _m
        _mtsgen = _m
    return _mtsgen

_cgen = None
def cgen():
    global _cgen
    if _cgen is None:
        from card_generator import generate_card, format_card_amount, commission
        _cgen = {"generate": generate_card, "fmt": format_card_amount, "comm": commission}
    return _cgen

_wgen = None
def wgen():
    global _wgen
    if _wgen is None:
        from web_card_generator import generate_web_card, fmt_amount, fmt_total, commission as _wcomm
        _wgen = {"generate": generate_web_card, "fmt_amt": fmt_amount,
                 "fmt_tot": fmt_total, "comm": _wcomm}
    return _wgen

def _mask_pan(text):
    """Normalize user input to 'dddddd******dddd' (web form needs full mask)."""
    t = re.sub(r"[\s\-]", "", text.strip())
    if re.fullmatch(r"\d{6}\*+\d{4}", t):
        return t[:6] + "******" + t[-4:]
    d = re.sub(r"\D", "", t)
    if 16 <= len(d) <= 19 or len(d) == 10:
        return d[:6] + "******" + d[-4:]
    return None

_sgen = None
def sgen():
    global _sgen
    if _sgen is None:
        from statement_generator import generate_statement, check_text
        _sgen = {"generate": generate_statement, "check": check_text}
    return _sgen

_wbgen = None
def wbgen():
    global _wbgen
    if _wbgen is None:
        import wb_generator as wb
        _wbgen = wb
    return _wbgen

_vtbgen = None
def vtbgen():
    global _vtbgen
    if _vtbgen is None:
        import vtb_generator as vtb
        _vtbgen = vtb
    return _vtbgen

_rshbgen = None
def rshbgen():
    global _rshbgen
    if _rshbgen is None:
        import rshb_generator as rshb
        _rshbgen = rshb
    return _rshbgen

_ozongen = None
def ozongen():
    global _ozongen
    if _ozongen is None:
        import ozon_generator as ozon
        _ozongen = ozon
    return _ozongen

# Lazy loader для ozon_editor (edit-single-field стратегия: берёт настоящий
# чек Озона и переписывает ровно одно поле — сумму, оставляя SBP ID и весь
# остальной PDF байт-в-байт нетронутым). Только этот flow проходит внешнюю
# сверку Ozon с БД банка: сгенерированный «с нуля» чек несёт выдуманный
# op_id, которого в БД Озона нет (локальный валидатор грамматики ID это
# проверить не может — он и не должен «чиниться» под это ослаблением).
_ozon_editor = None
def ozon_editor_mod():
    global _ozon_editor
    if _ozon_editor is None:
        import ozon_editor
        _ozon_editor = ozon_editor
    return _ozon_editor

_yandexgen = None
def yandexgen():
    global _yandexgen
    if _yandexgen is None:
        import yandex_generator as yandex
        _yandexgen = yandex
    return _yandexgen

# Lazy loader генератора СБП-квитанции Яндекс Банка (donor-template,
# yandex_receipt_generator). НЕ путать с yandexgen() выше: тот рисует
# выписку «Счёт ЭДС» (yandex_statement), этот — исходящую квитанцию СБП.
_yrcptgen = None
def yrcptgen():
    global _yrcptgen
    if _yrcptgen is None:
        import yandex_receipt_generator as yrg
        _yrcptgen = yrg
    return _yrcptgen

_RSHB_BANKS = [
    ("Т-Банк", "Сбербанк", "Альфа-Банк"),
    ("ВТБ", "Совкомбанк", "Райффайзенбанк"),
    ("Озон Банк", "Газпромбанк", "Россельхозбанк"),
    ("Почта Банк", "МТС Банк", "Банк Санкт-Петербург"),
    ("УБРиР", "РНКБ", "Хоум Кредит"),
    ("Яндекс Банк", "Авангард", "Ситибанк"),
    ("БКС Банк", "ПСБ", None),
]
def _rshb_bank_kb():
    rows = []
    for r in _RSHB_BANKS:
        row = [(b, f"rshb_bank:{b}") for b in r if b]
        rows.append(row)
    rows.append([(u"\u274c Отмена", "cancel")])
    return kb(*rows)

_OZON_BANKS_FALLBACK = ["Т-Банк", "Сбербанк", "Альфа-Банк", "ВТБ", "Озон Банк",
                       "Газпромбанк", "РСХБ", "Почта Банк", "МТС Банк"]
def _ozon_bank_list():
    """Из ozon_generator.BANKS если объявлены, иначе фиксированный список."""
    try:
        banks = getattr(ozongen(), "BANKS", None)
        if banks:
            return list(banks)
    except Exception:
        pass
    return list(_OZON_BANKS_FALLBACK)

def _ozon_bank_kb():
    banks = _ozon_bank_list()
    rows, row = [], []
    for b in banks:
        row.append((b, f"ozon_bank:{b}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([(u"\u270f Ввести свой", "ozon_bank_custom")])
    rows.append([(u"\u274c Отмена", "cancel")])
    return kb(*rows)

_YANDEX_BANKS = [
    ("Т-Банк", "Сбербанк", "Альфа-Банк"),
    ("ВТБ", "Совкомбанк", "Райффайзенбанк"),
    ("Озон Банк", "Газпромбанк", "Россельхозбанк"),
    ("Почта Банк", "МТС Банк", "Банк Санкт-Петербург"),
    ("УБРиР", "РНКБ", "Хоум Кредит"),
    ("Яндекс Банк", "Авангард", "Ситибанк"),
    ("БКС Банк", "ПСБ", None),
]
def _yandex_bank_kb():
    rows = []
    for r in _YANDEX_BANKS:
        row = [(b, f"yandex_bank:{b}") for b in r if b]
        rows.append(row)
    rows.append([(u"\u270f Ввести свой", "yandex_bank_custom")])
    rows.append([(u"\u274c Отмена", "cancel")])
    return kb(*rows)

def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])

# ── LK button (add receipt operation to the caller's ЛК) ──
LK_BTN_ADD = "\u2795 Добавить перевод в ЛК"
LK_BTN_BUSY = "\u23f3 Добавляю в ЛК\u2026"
LK_BTN_DONE = "\u2705 В ЛК"
_LK_STATUS_RE = re.compile(
    r"\n\n(?:\u2705 Добавлено в ЛК|\u26a0\ufe0f Такой перевод уже есть в ЛК|\u274c ЛК:)[^\n]*$")

def _lk_markup(again: str, gen_id: str | None = None, state: str = "add"):
    """Keyboard under a generated receipt. state: 'add' | 'progress' | 'done'.
    again: 'new' (СБП) | 'new_card' (карта) — определяет кнопку «Ещё чек».
    Кнопка «🔄 С другими параметрами» запускает флоу заново с предзаполненными данными."""
    if again == "new_card":
        top = [(u"\U0001f504 Ещё чек", "new_card"), (u"\U0001f3e0 Меню", "home")]
        params = [(u"\U0001f501 С другими параметрами", f"again_params:card")]
    elif again == "new":
        top = [(u"\U0001f504 Ещё чек", "new"), (u"\U0001f3e0 Меню", "home")]
        params = [(u"\U0001f501 С другими параметрами", f"again_params:sbp")]
    else:
        top = [(u"\U0001f504 Ещё чек", "new"), (u"\U0001f3e0 Меню", "home")]
        params = []
    if state == "done":
        lk_row = [(LK_BTN_DONE, "lk_done")]          # inert, guards repeat taps
    elif state == "progress":
        lk_row = [(LK_BTN_BUSY, "lk_busy")]          # inert spinner
    else:
        lk_row = [(LK_BTN_ADD, f"lk_add:{gen_id}")]
    rows = [top]
    if params:
        rows.append(params)
    rows.append(lk_row)
    return kb(*rows)

async def _lk_card(cb: CallbackQuery, p: dict, gen_id: str, state: str, status: str | None = None):
    """Redraw the receipt message: LK status line in the caption + LK button state.
    The receipt is a document, so caption+markup are edited together."""
    markup = _lk_markup(p.get("again") or "new", gen_id, state)
    try:
        cap = _LK_STATUS_RE.sub("", cb.message.html_text or "")
        if status:
            cap += f"\n\n{status}"
        await cb.message.edit_caption(caption=cap, reply_markup=markup)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=markup)
        except Exception:
            pass

def is_ok(uid): return uid in data["users"]
def is_admin(uid): return uid == ADMIN_ID

def _fflags(uid) -> dict:
    """per-user feature_flags словарь для uid (создаёт пустой если нет)."""
    ff = data.get("feature_flags") or {}
    return ff.get(str(uid)) or {}

def _ff_set(uid: int, name: str, value: bool):
    """Установить per-user фичу и сохранить. Только для per-user фич."""
    if not isinstance(name, str) or name not in PER_USER_FEATURES:
        return False
    ff = data.setdefault("feature_flags", {})
    if not isinstance(ff.get(str(uid)), dict):
        ff[str(uid)] = {}
    ff[str(uid)][name] = bool(value)
    save(data)
    return True

def _ff_pref_set(uid: int, key: str, value):
    """Сохранить per-user предпочтение (dark_theme_on, notifications_on, api_token_value).
    Не валидируется по PER_USER_FEATURES — это настройки, не фичи."""
    ff = data.setdefault("feature_flags", {})
    if not isinstance(ff.get(str(uid)), dict):
        ff[str(uid)] = {}
    ff[str(uid)][key] = value
    save(data)

def get_uid(e):
    return e.get("uid", e.get("user_id", 0))

# ── Структура меню: корень → банк / «Другое» / «Управление» ──
# Корень (main_menu): только банки + «Другое» (+ «Управление» у админа).
# Каждый банк — подменю (bank_menu) с его существующими flow-callback'ами;
# все небанковские кнопки живут в other_menu с прежними feature-гейтами
# (feat/gfeat/is_ok не меняются). Навигация: bankmenu:<key> — подменю банка,
# menu_other — «Другое», home (существующий) — главное меню. Префикс
# bankmenu: намеренно не пересекается с существующим bank: (выбор банка
# получателя в СБП-флоу МТС).
MENU_BANKS = [
    ("mts",    u"\U0001f534 МТС Банк"),
    ("wb",     u"\U0001f7e3 WB"),
    ("vtb",    u"\U0001f535 ВТБ"),
    ("rshb",   u"\U0001f33e РСХБ"),
    ("ozon",   u"\U0001f9fe Озон"),
    ("yandex", u"\U0001f7e1 Яндекс"),
]

_BANK_MENU_TEXT = {
    "mts":    u"\U0001f534 <b>МТС Банк</b>\n\nВыбери действие:",
    "wb":     u"\U0001f7e3 <b>WB (Wildberries)</b>\n\nВыбери действие:",
    "vtb":    u"\U0001f535 <b>ВТБ</b>\n\nВыбери действие:",
    "rshb":   u"\U0001f33e <b>РСХБ (Россельхозбанк)</b>\n\nВыбери действие:",
    "ozon":   (u"\U0001f9fe <b>Озон Банк</b>\n\n"
               u"✅ <b>Для внешней проверки банком</b> — «✏️ Изменить сумму "
               u"настоящего чека»: ID операции останется настоящим.\n"
               u"⚠️ «🧾 Новый чек» — фабричный: ID операции выдуман, в БД "
               u"Озон Банка его нет, внешняя проверка будет красной.\n\n"
               u"Выбери действие:"),
    # Яндекс: два разных flow — выписка «Счёт ЭДС» (new_yandex,
    # yandex_generator) и исходящая СБП-квитанция (new_yrcpt,
    # yandex_receipt_generator). Не смешивать: у них разные генераторы.
    "yandex": (u"\U0001f7e1 <b>Яндекс Банк</b>\n\n"
               u"Выбери действие: выписка по договору "
               u"\u00abСчёт ЭДС\u00bb или квитанция СБП о переводе."),
}

# Кнопки подменю банков: только существующие callback'и flow (new*, ozon_edit).
_BANK_MENU_BUTTONS = {
    "mts": [
        [(u"\U0001f9fe Чек СБП", "new"), (u"\U0001f4b3 По карте", "new_card")],
        [(u"\U0001f4c4 Выписка ЭКСИ", "new_stmt")],
    ],
    "wb": [
        [(u"\U0001f9fe Чек СБП", "new_wb")],
    ],
    "vtb": [
        [(u"\U0001f9fe Чек СБП", "new_vtb")],
    ],
    "rshb": [
        [(u"\U0001f9fe Чек СБП", "new_rshb")],
    ],
    "ozon": [
        [(u"\u270f\ufe0f Изменить сумму настоящего чека", "ozon_edit")],
        [(u"\U0001f9fe Новый чек (фабричный)", "new_ozon")],
    ],
    "yandex": [
        [(u"\U0001f4c4 Выписка Яндекс Банк", "new_yandex")],
        [(u"\U0001f9fe Квитанция СБП", "new_yrcpt")],
    ],
}

OTHER_MENU_TEXT = u"\U0001f4c2 <b>Другое</b>\n\nИнструменты и удобства:"


def bank_menu(bank_key, uid=None):
    """Подменю одного банка: его flow-кнопки + «⬅ Главное меню» (home).
    uid не гейтит банки (доступ проверяет is_ok в хендлерах flow),
    принят для симметрии с main_menu/other_menu и будущих per-bank гейтов.
    Исключение — Озон: у юзеров с флагом ozon_v2 подменю строится из
    ozon_ux.scenario_kb_spec() (новый чек / правка суммы / история);
    без флага — прежние кнопки new_ozon/ozon_edit (rollback)."""
    if bank_key == "ozon" and uid is not None and ozx is not None \
            and feat(uid, "ozon_v2"):
        rows = [list(row) for row in ozx.scenario_kb_spec()]
        # Ведём в edit-flow: правка настоящего чека — первой и с честной
        # подписью, фабричный новый чек — вторым (его ID в БД банка нет).
        for _row in rows:
            for _i, (_txt, _cb) in enumerate(_row):
                if _cb == "oz:edit":
                    _row[_i] = (u"\u270f\ufe0f Изменить сумму настоящего чека", _cb)
                elif _cb == "oz:new":
                    _row[_i] = (u"\U0001f9fe Новый чек (фабричный)", _cb)
            _cbs = [b[1] for b in _row]
            if "oz:new" in _cbs and "oz:edit" in _cbs:
                _row.sort(key=lambda b: 0 if b[1] == "oz:edit" else 1)
        rows.append([(u"\u2b05 Главное меню", "home")])
        return kb(*rows)
    rows = [list(row) for row in _BANK_MENU_BUTTONS.get(bank_key, [])]
    rows.append([(u"\u2b05 Главное меню", "home")])
    return kb(*rows)


def other_menu(uid):
    """«Другое»: все небанковские кнопки с прежними feature-гейтами.
    Порядок — как в задании: проверка, история, шаблоны, помощь,
    быстрая генерация, пакет, статистика, API-токен, переключатели, ЛК."""
    items = []
    if feat(uid, "check_pdf"):
        items.append((u"\U0001f50d Проверить чек", "check_pdf"))
    if gfeat("history_btn"):
        items.append((u"\U0001f4dc История", "history"))
    if gfeat("templates"):
        items.append((u"\U0001f4cc Шаблоны", "templates"))
    if gfeat("help_cmd"):
        items.append((u"\u2753 Помощь", "help"))
    if feat(uid, "fast_gen"):
        items.append((u"\u26a1 Быстрая генерация", "fast_gen"))
    if feat(uid, "batch"):
        items.append((u"\U0001f4e6 Пакет чеков", "batch"))
    if feat(uid, "stats"):
        items.append((u"\U0001f4ca Статистика", "stats_btn"))
    if feat(uid, "api_token"):
        items.append((u"\U0001f511 API-токен", "api_token"))
    if feat(uid, "dark_theme"):
        theme_on = _fflags(uid).get("dark_theme_on", False)
        items.append((u"\U0001f313 Тёмная тема" if theme_on else u"\U0001f31e Тёмная тема", "dark_theme"))
    if feat(uid, "notifications"):
        notif_on = _fflags(uid).get("notifications_on", True)
        items.append((u"\U0001f515 Уведомления: " + ("вкл" if notif_on else "выкл"), "notifications"))
    if not is_admin(uid):
        bound = (data.get("lk_bindings") or {}).get(str(uid))
        if bound:
            items.append((f"\U0001f310 Моя админка: {bound}", "lk_bind"))
        else:
            items.append((u"\U0001f517 Привязать свою админку", "lk_bind"))
    rows = []
    if gfeat("rich_menu"):
        rows.append(_hdr(u"\U0001f4c2 \u2003Другое"))
    for i in range(0, len(items), 2):
        rows.append([(t, d) for t, d in items[i:i + 2]])
    rows.append([(u"\u2b05 Главное меню", "home")])
    return kb(*rows)


def _root_menu(uid, rich: bool):
    """Корень: только банки + «Другое» (+ «Управление» у админа)."""
    rows = []
    if rich:
        rows.append(_hdr(u"\U0001f3e6 \u2003Банки"))
    bank_btns = [(label, f"bankmenu:{key}") for key, label in MENU_BANKS]
    for i in range(0, len(bank_btns), 2):
        rows.append(bank_btns[i:i + 2])
    rows.append([(u"\U0001f4c2 Другое", "menu_other")])
    if is_admin(uid):
        rows.append([(u"\U0001f6e0\ufe0f Управление", "admin")])
    return kb(*rows)


def main_menu(uid):
    if gfeat("rich_menu"):
        return _rich_menu(uid)
    return _root_menu(uid, rich=False)


def _hdr(text: str) -> list:
    """Инертная кнопка-заголовок секции (callback=noop). Не реагирует на тап."""
    return [(text, "noop")]


def _rich_menu(uid):
    """Корень с заголовками секций (флаг rich_menu) — та же иерархия,
    что и обычный main_menu: банки + «Другое» (+ «Управление» у админа)."""
    return _root_menu(uid, rich=True)

MSK = timezone(timedelta(hours=3))

def now_msk():
    """Naive Moscow time. Generators treat this as UTC+3 and subtract 3h for CreationDate."""
    return datetime.now(MSK).replace(tzinfo=None)

def rand5():
    """Pick a 5-digit suffix that preserves CBR check digit '2' on new-era stem 40817 81020 00025."""
    stem = "889408178102000025"
    while True:
        suf = f"{random.randint(10000, 99999)}"
        s = stem + suf
        if sum((int(ch) * (7, 1, 3)[i % 3]) % 10 for i, ch in enumerate(s)) % 10 == 0:
            return suf

def format_phone(raw: str) -> str | None:
    """Convert any phone input to +7 (XXX) XXX-XX-XX format. Returns None if invalid."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return None

_PATR = re.compile(r"(вич|вна|ич|ична|инична)$", re.IGNORECASE)

# Только имя (1–4 слова, опциональные точки у инициалов) без цифр/банка/суммы —
# штатный ввод шага «Имя получателя», а не свободная фраза для переспроса.
_PLAIN_NAME_RE = re.compile(r"[A-Za-zА-ЯЁа-яё]+\.?(?:\s+[A-Za-zА-ЯЁа-яё]+\.?){0,3}")

def bank_name(raw: str) -> str:
    """Normalize a recipient name to the bank's receipt format 'Имя Отчество Ф'.
    Accepts 'Фамилия Имя Отчество' or 'Имя Отчество Фамилия' in any case."""
    # The MTS subset font has no capital Ё glyph; passports write Е anyway.
    raw = raw.replace("Ё", "Е")
    w = [p for p in re.split(r"\s+", raw.strip()) if p]
    if len(w) < 3:
        if len(w) == 2 and len(w[1]) > 1:
            return f"{w[0]} {w[1][0].upper()}"
        return raw.strip()
    w = w[:3]
    if len(w[2]) == 1:  # already 'Имя Отчество Ф'
        return " ".join(w)
    if _PATR.search(w[2]):  # Ф И О order
        return f"{w[1]} {w[2]} {w[0][0].upper()}"
    # И О Ф order (or unknown): truncate the surname word
    return f"{w[0]} {w[1]} {w[2][0].upper()}"

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
router = Router()

async def _progress_action(uid):
    """'Отправляет документ…' status in the client while a PDF is rendering."""
    try:
        await bot.send_chat_action(uid, "upload_document")
    except Exception:
        pass

async def _queue_notice(uid):
    """Transient queue message, shown only when all generation workers are busy."""
    if not gen_queue_ahead():
        return None
    try:
        return await bot.send_message(uid, u"\u23f3 Очередь на генерацию, несколько секунд\u2026")
    except Exception:
        return None

async def _clear_note(m):
    if m is not None:
        try:
            await m.delete()
        except Exception:
            pass

async def edit(cb_or_msg, text, markup=None, uid=None):
    if isinstance(cb_or_msg, CallbackQuery):
        try:
            m = await cb_or_msg.message.edit_text(text, reply_markup=markup)
        except Exception as e:
            if "message is not modified" in str(e).lower():
                # Двойной тап с тем же рендером: сообщение уже в нужном виде,
                # новое слать не надо (иначе дубли в чате).
                m = cb_or_msg.message
            else:
                # Сообщение без текста (документ/фото) нельзя редактировать — шлём новое.
                m = await bot.send_message(cb_or_msg.message.chat.id, text, reply_markup=markup)
        if uid and uid in sessions:
            sessions[uid]["mid"] = m.message_id
            sessions[uid]["ts"] = datetime.now().timestamp()
        return m
    else:
        if uid and uid in sessions and sessions[uid].get("mid"):
            try: await bot.delete_message(cb_or_msg.chat.id, sessions[uid]["mid"])
            except: pass
        m = await bot.send_message(cb_or_msg.chat.id, text, reply_markup=markup)
        if uid and uid in sessions:
            sessions[uid]["mid"] = m.message_id
            sessions[uid]["ts"] = datetime.now().timestamp()
        return m

# ══════════════════════════════════════
# ── HELP / STATS / HISTORY / TEMPLATES — новые удобства (v4) ──
# ─────────────────────────────────────

# Карта типов логов → человеко-читаемое название банка/операции
_LOG_TYPE_LABEL = {
    None:      "Чек СБП (МТС)",
    "card":    "Перевод по карте",
    "stmt":    "Выписка ЭКСИ-Банк",
    "anchor":  "Якорь (обновление)",
    "wb":      "Чек Wildberries",
    "vtb":     "Чек ВТБ",
    "rshb":    "Чек РСХБ",
    "ozon":    "Чек Озон Банк",
    "ozon_edit": "Правка чека Озон",
    "yandex":  "Выписка Яндекс Банк",
    "yandex_receipt": "Квитанция Яндекс Банк",
}

def _log_label(e: dict) -> str:
    """Короткое описание одной записи лога для истории/статистики."""
    t = e.get("type")
    if t == "card":
        return f"💳 {e.get('card_from','••')}→{e.get('card_to','••')} • {e.get('amount','?')}₽"
    if t == "stmt":
        return f"📄 Выписка • {e.get('name','?')} • оп:{e.get('ops',0)} х:{e.get('holds',0)}"
    if t == "anchor":
        return f"⚓ Якорь {e.get('kind','?')} • {e.get('num','?')}"
    if t == "wb":
        return f"🧾 WB • {e.get('name','?')} • {e.get('amount','?')}₽ • {e.get('bank','?')}"
    if t == "vtb":
        return f"🧾 ВТБ • {e.get('name','?')} • {e.get('amount','?')}₽ • {e.get('bank','?')}"
    if t == "rshb":
        return f"🧾 РСХБ • {e.get('name','?')} • {e.get('amount','?')}₽ • {e.get('bank','?')}"
    if t == "ozon":
        return f"🧾 Озон • {e.get('name','?')} • {e.get('amount','?')}₽ • {e.get('bank','?')}"
    if t == "ozon_edit":
        return f"✏️ Озон правка • {e.get('amount','?')}₽"
    if t == "yandex":
        return f"🧾 Яндекс выписка • {e.get('name','?')} • {e.get('amount','?')}₽ • {e.get('bank','?')}"
    if t == "yandex_receipt":
        return f"🧾 Яндекс квитанция • {e.get('name','?')} • {e.get('amount','?')}₽ • {e.get('bank','?')}"
    # SBP (type отсутствует)
    return f"🧾 СБП • {e.get('name','?')} • {e.get('amount','?')}₽ • {e.get('bank','?')}"

def _user_logs(uid: int) -> list:
    """Только записи этого пользователя (новые в конце)."""
    return [e for e in data["logs"] if get_uid(e) == uid]

HELP_TEXT = (
    "<b>ℹ️ Помощь — генератор чеков</b>\n\n"
    "<b>Главное меню — банки:</b> выбери банк, откроется его подменю.\n"
    "• <code>🔴 МТС Банк</code> — <code>🧾 Чек СБП</code> (перевод по номеру телефона), "
    "<code>💳 По карте</code> (перевод с карты на карту, 2 версии), "
    "<code>📄 Выписка ЭКСИ</code> (выписка с операциями и холдами)\n"
    "• <code>🟣 WB</code> — <code>🧾 Чек СБП</code> (Wildberries)\n"
    "• <code>🔵 ВТБ</code> — <code>🧾 Чек СБП</code>\n"
    "• <code>🌾 РСХБ</code> — <code>🧾 Чек СБП</code> (Россельхозбанк)\n"
    "• <code>🧾 Озон</code> — <code>✏️ Изменить сумму настоящего чека</code> "
    "(ID настоящий, подтверждается банком) и <code>🧾 Новый чек</code> "
    "(фабричный, ID в БД банка нет — внешняя проверка красная)\n"
    "• <code>🟡 Яндекс</code> — <code>📄 Выписка Яндекс Банк</code> "
    "(выписка по договору «Счёт ЭДС» с входящим СБП-переводом) и "
    "<code>🧾 Квитанция СБП</code> (исходящая квитанция о переводе)\n\n"
    "<b>📂 Другое (в главном меню):</b> проверить чек, история, шаблоны, помощь, "
    "инструменты ниже и переключатели.\n\n"
    "<b>Инструменты (выдаются админом через /grant):</b>\n"
    "• <code>⚡ Быстрая генерация</code> — чек с рандомными параметрами одной кнопкой\n"
    "• <code>📦 Пакет чеков</code> — N чеков сразу пачкой\n"
    "• <code>🔍 Проверить чек</code> — сверить чужой PDF с законами генератора\n"
    "• <code>📊 Статистика</code> — сколько чеков ты сделал\n"
    "• <code>🌙 Тёмная тема</code> — переключатель темы\n"
    "• <code>🔔 Уведомления</code> — вкл/выкл уведомления о готовности чека\n"
    "• <code>🔑 API-токен</code> — персональный токен для API-доступа\n\n"
    "<b>Удобства:</b>\n"
    "• <code>📜 История</code> — последние 10 твоих чеков\n"
    "• <code>📌 Шаблоны</code> — сохранённые получатели для быстрой генерации\n"
    "• <code>/stats</code> — твоя статистика по банкам\n"
    "• <code>/help</code> — эта справка\n"
    "• <code>/cancel</code> — отменить любой шаг\n\n"
    "<b>Подсказки:</b> в любом шаге есть кнопка <code>❌ Отмена</code>. "
    "Сессия хранится 15 мин бездействия. Длинные операции показывают прогресс.\n\n"
    "<i>Фичи выдаёт админ командами <code>/grant &lt;uid&gt; &lt;фича&gt;</code> и "
    "<code>/revoke &lt;uid&gt; &lt;фича&gt;</code>. Список — <code>/features</code>.</i>"
)

def _stats_text(uid: int) -> str:
    logs = _user_logs(uid)
    if not logs:
        return "📊 <b>Статистика пуста</b>\n\nТы ещё не генерировал чеки. Жми «🧾 Чек СБП»."
    total = len(logs)
    by_type = Counter()
    by_bank = Counter()
    total_amount = 0
    for e in logs:
        t = e.get("type") or "sbp"
        by_type[t] += 1
        b = e.get("bank") or ({"card": "МТС (карта)", "stmt": "ЭКСИ-Банк",
                              "anchor": "—", "wb": "Wildberries"}.get(t) or "МТС")
        by_bank[b] += 1
        a = e.get("amount")
        if isinstance(a, (int, float)):
            total_amount += a
    type_lines = "\n".join(f"  {_LOG_TYPE_LABEL.get(k, k)}: <b>{v}</b>"
                           for k, v in by_type.most_common())
    bank_lines = "\n".join(f"  {k}: <b>{v}</b>" for k, v in by_bank.most_common(8))
    return (
        f"📊 <b>Твоя статистика</b>\n\n"
        f"Всего операций: <b>{total}</b>\n"
        f"Сумма переводов: <b>{total_amount:,} ₽</b>\n\n"
        f"<b>По типам:</b>\n{type_lines}\n\n"
        f"<b>По банкам (топ-8):</b>\n{bank_lines}"
    )

def _history_text(uid: int) -> str:
    logs = _user_logs(uid)
    if not logs:
        return "📜 <b>История пуста</b>\n\nТы ещё не генерировал чеки."
    last = logs[-10:][::-1]  # последние 10, новые сверху
    lines = []
    for i, e in enumerate(last, 1):
        lines.append(f"<b>{i}.</b> {_log_label(e)}\n   🕐 {e.get('time','?')}")
    return "📜 <b>Последние 10 чеков</b>\n\n" + "\n\n".join(lines)

def _templates_list(uid: int) -> list:
    return (data.get("templates") or {}).get(str(uid)) or []

def _templates_save(uid: int, tpls: list):
    data.setdefault("templates", {})[str(uid)] = tpls
    save(data)

# ── «С другими параметрами»: сохраняем последние параметры чека ──
def _save_last_params(uid: int, flow: str, params: dict):
    """Запоминает параметры последнего чека (для кнопки «С другими параметрами»).
    flow: 'sbp' | 'card' | 'wb' | 'vtb' | 'rshb' | 'ozon' | 'stmt'."""
    data.setdefault("last_params", {})[str(uid)] = {"flow": flow, "params": params}
    save(data)

def _get_last_params(uid: int) -> dict | None:
    entry = (data.get("last_params") or {}).get(str(uid))
    return entry if entry else None

def _templates_text(uid: int) -> str:
    tpls = _templates_list(uid)
    if not tpls:
        return ("📌 <b>Шаблоны</b>\n\n"
                "Сохраняй часто используемых получателей (СБП), чтобы не вводить ФИО и банк каждый раз.\n\n"
                "Чтобы сохранить — дойди до шага «Проверь данные» в чеке СБП и нажми "
                "<code>📌 Сохранить шаблон</code>.")
    lines = []
    for i, t in enumerate(tpls):
        lines.append(f"<b>{i+1}.</b> {t['name']} • {t['phone']} • {t['bank']}")
    return "📌 <b>Твои шаблоны</b>\n\n" + "\n".join(lines)

def _templates_kb(uid: int):
    tpls = _templates_list(uid)
    rows = []
    for i, t in enumerate(tpls):
        rows.append([(f"⚡ {t['name']} • {t['bank']}", f"tpl_use:{i}")])
    rows.append([(u"\u274c Отмена", "cancel")])
    return kb(*rows)

@router.message(CommandStart())
async def cmd_start(msg: Message):
    uid = msg.from_user.id
    if not is_ok(uid):
        await msg.answer(u"\U0001f512 <b>Доступ закрыт.</b>\nОбратись к администратору.")
        return
    sessions.pop(uid, None)
    name = msg.from_user.first_name or "бро"
    await msg.answer(
        f"\U0001f44b <b>Привет, {name}!</b>\n\n"
        "\U0001f3e6 Генератор чеков МТС Банк\n"
        "Полный русский алфавит \u2022 Любые суммы\n\n"
        "Жми кнопку \U0001f447",
        reply_markup=main_menu(uid))

# ── /help, /stats — команды ──
@router.message(Command("help"))
async def cmd_help(msg: Message):
    if not gfeat("help_cmd") or not is_ok(msg.from_user.id): return
    await msg.answer(HELP_TEXT, reply_markup=main_menu(msg.from_user.id))

@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not feat(msg.from_user.id, "stats") or not is_ok(msg.from_user.id): return
    await msg.answer(_stats_text(msg.from_user.id), reply_markup=main_menu(msg.from_user.id))

# ── Кнопки «Помощь», «История», «Шаблоны» ──
@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not gfeat("help_cmd"): await cb.answer("Выключено"); return
    sessions.pop(uid, None)
    await edit(cb, HELP_TEXT, other_menu(uid), uid)
    await cb.answer()

@router.callback_query(F.data == "history")
async def cb_history(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not gfeat("history_btn"): await cb.answer("Выключено"); return
    sessions.pop(uid, None)
    await edit(cb, _history_text(uid), other_menu(uid), uid)
    await cb.answer()

@router.callback_query(F.data == "templates")
async def cb_templates(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not gfeat("templates"): await cb.answer("Выключено"); return
    sessions.pop(uid, None)
    tpls = _templates_list(uid)
    rows = []
    if tpls:
        for i, t in enumerate(tpls):
            rows.append([(f"⚡ {t['name']} • {t['bank']}", f"tpl_use:{i}")])
        rows.append([(u"\U0001f5d1 Удалить шаблон", "tpl_del_menu")])
    rows.append([(u"\U0001f3e0 Меню", "home")])
    await edit(cb, _templates_text(uid), kb(*rows), uid)
    await cb.answer()

@router.callback_query(F.data == "tpl_del_menu")
async def cb_tpl_del_menu(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    tpls = _templates_list(uid)
    rows = []
    for i, t in enumerate(tpls):
        rows.append([(f"🗑 {t['name']} • {t['bank']}", f"tpl_del:{i}")])
    rows.append([(u"\u2b05 К шаблонам", "templates")])
    await edit(cb, "🗑 <b>Удаление шаблона</b>\n\nВыбери какой убрать:", kb(*rows), uid)
    await cb.answer()

@router.callback_query(F.data.startswith("tpl_del:"))
async def cb_tpl_del(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    try: idx = int(cb.data.split(":", 1)[1])
    except ValueError: await cb.answer("Ошибка"); return
    tpls = _templates_list(uid)
    if 0 <= idx < len(tpls):
        removed = tpls.pop(idx)
        _templates_save(uid, tpls)
        await cb.answer(f"Удалён: {removed['name']}", show_alert=False)
    else:
        await cb.answer("Не найден")
    # возвращаемся к списку шаблонов
    tpls = _templates_list(uid)
    rows = []
    if tpls:
        for i, t in enumerate(tpls):
            rows.append([(f"⚡ {t['name']} • {t['bank']}", f"tpl_use:{i}")])
        rows.append([(u"\U0001f5d1 Удалить шаблон", "tpl_del_menu")])
    rows.append([(u"\U0001f3e0 Меню", "home")])
    await edit(cb, _templates_text(uid), kb(*rows), uid)

@router.callback_query(F.data.startswith("tpl_use:"))
async def cb_tpl_use(cb: CallbackQuery):
    """Применить шаблон: запускает флоу СБП с предзаполненными name+phone+bank,
    сразу переводит на шаг ввода суммы."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not gfeat("templates"): await cb.answer("Выключено"); return
    try: idx = int(cb.data.split(":", 1)[1])
    except ValueError: await cb.answer("Ошибка"); return
    tpls = _templates_list(uid)
    if not (0 <= idx < len(tpls)):
        await cb.answer("Шаблон не найден", show_alert=True); return
    t = tpls[idx]
    sessions[uid] = {"step": "amount", "mid": cb.message.message_id,
                     "name": t["name"], "phone": t["phone"], "bank": t["bank"],
                     "_from_tpl": True}
    await edit(cb,
        f"⚡ <b>Шаблон применён</b>\n\n"
        f"\U0001f464 {t['name']}\n"
        f"\U0001f4f1 {t['phone']}\n"
        f"\U0001f3e6 {t['bank']} \u2705\n\n"
        "\U0001f4b0 <b>Сумма перевода (руб):</b>\nНапример: <code>5000</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data == "tpl_save")
async def cb_tpl_save(cb: CallbackQuery):
    """Сохранить текущие name+phone+bank из сессии СБП как шаблон.
    Не трогает сессию — юзер может сразу нажать «Сгенерировать»."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not gfeat("templates"): await cb.answer("Выключено"); return
    s = sessions.get(uid) or {}
    name = s.get("name"); phone = s.get("phone"); bank = s.get("bank")
    if not (name and phone and bank):
        await cb.answer("Сначала заполни данные", show_alert=True); return
    tpls = _templates_list(uid)
    # не дублируем
    if any(t["name"] == name and t["phone"] == phone and t["bank"] == bank for t in tpls):
        await cb.answer("Такой шаблон уже есть"); return
    if len(tpls) >= 20:
        await cb.answer("Максимум 20 шаблонов — удали лишние", show_alert=True); return
    tpls.append({"name": name, "phone": phone, "bank": bank})
    _templates_save(uid, tpls)
    await cb.answer(f"✅ Сохранён: {name} • {bank}", show_alert=False)
    # обновляем клавиатуру — убираем кнопку «Сохранить шаблон», оставляем генерацию
    g = gen()
    await edit(cb,
        f"\U0001f4cb <b>Проверь данные:</b>\n\n"
        f"\U0001f464 {s['name']}\n"
        f"\U0001f4f1 {s['phone']}\n"
        f"\U0001f3e6 {s['bank']}\n"
        f"\U0001f4b0 {g['fmt'](s['amount'])}\n"
        f"\U0001f4b3 Счёт: ...{s.get('acc4','****')}\n"
        f"\U0001f552 Текущее время\n\n"
        f"\U0001f4cc Шаблон сохранён ({len(tpls)} шт.)",
        kb([(u"\u2705 Сгенерировать", "confirm")],
           [(u"\u274c Отмена", "cancel")]), uid)

# ── NEW CHECK FLOW ──
@router.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions[uid] = {"step": "name", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f464 <b>Имя получателя</b>\n\n"
        "Введи как на чеке:\n<code>Сергей Михайлович К</code>",
        kb([(u"\u2b05 В банк", "bankmenu:mts")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery):
    uid = cb.from_user.id
    sessions.pop(uid, None)
    await edit(cb, u"\u274c Отменено.", main_menu(uid), uid)
    await cb.answer()

@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    """Инертная кнопка-заголовок секции меню — просто закрывает callback."""
    await cb.answer()

# ── /cancel — отмена любого шага ──
@router.message(Command("cancel"))
async def cmd_cancel(msg: Message):
    uid = msg.from_user.id
    if not is_ok(uid): return
    sessions.pop(uid, None)
    await msg.answer(u"\u274c Отменено.", reply_markup=main_menu(uid))

# ── ⚡ БЫСТРАЯ ГЕНЕРАЦИЯ (per-user: fast_gen) ──
_FAST_NAMES = ["Иван Сергеевич П", "Анна Михайловна К", "Дмитрий Андреевич В",
               "Елена Викторовна С", "Сергей Николаевич Р", "Ольга Александровна Б",
               "Павел Дмитриевич М", "Мария Игоревна Л", "Артём Павлович Т",
               "Виктория Сергеевна Г"]
def _rand_phone():
    return f"+7 (9{random.randint(10,99)}) {random.randint(100,999)}-{random.randint(10,99)}-{random.randint(10,99)}"

@router.callback_query(F.data == "fast_gen")
async def cb_fast_gen(cb: CallbackQuery):
    """Быстрая генерация СБП-чека с рандомными параметрами одной кнопкой."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not feat(uid, "fast_gen"): await cb.answer("Фича не выдана", show_alert=True); return
    await cb.answer("⚡ Генерирую…")
    name = random.choice(_FAST_NAMES)
    phone = _rand_phone()
    bank = random.choice(BANKS)
    amount = random.randint(1000, 50000)
    await edit(cb, f"\u26a1 <b>Быстрая генерация</b>\n\n"
                   f"\U0001f464 {name}\n\U0001f4f1 {phone}\n"
                   f"\U0001f3e6 {bank}\n\U0001f4b0 {amount} \u20bd\n\n"
                   u"\u23f3 <b>Генерация чека…</b> (3-5 сек)", uid=uid)
    try:
        now = now_msk()
        meta = {}
        g = gen()
        pdf = await run_gen(g["generate"], name, phone, bank, amount, now,
                           account_last4=None, meta_out=meta)
        gen_id = _lk_store(uid, _lk_payload_sbp({"name": name, "phone": phone,
                           "bank": bank, "amount": amount}, meta, owner=_lk_owner(uid)), "new")
        await bot.send_document(cb.message.chat.id,
            BufferedInputFile(pdf, filename=f"receipt_{amount}.pdf"),
            caption=(f"\u2705 <b>Быстрый чек готов</b>\n\n"
                     f"\U0001f464 {name}\n\U0001f4f1 {phone}\n"
                     f"\U0001f3e6 {bank}\n\U0001f4b0 {g['fmt'](amount)}\n"
                     f"\U0001f552 {now.strftime('%d.%m.%Y %H:%M:%S')}"),
            reply_markup=_lk_markup("new", gen_id))
        data["logs"].append({"uid": uid, "name": name, "phone": phone,
            "bank": bank, "amount": amount, "acc": "", "type": "sbp",
            "time": now.strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        _save_last_params(uid, "sbp", {"name": name, "phone": phone, "bank": bank, "acc4": ""})
        save(data)
    except Exception as e:
        await bot.send_message(cb.message.chat.id,
            f"\u274c <b>Ошибка:</b> <code>{html.escape(str(e))[:300]}</code>",
            reply_markup=main_menu(uid))

# ── 📦 ПАКЕТ ЧЕКОВ (per-user: batch) ──
@router.callback_query(F.data == "batch")
async def cb_batch(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not feat(uid, "batch"): await cb.answer("Фича не выдана", show_alert=True); return
    sessions[uid] = {"step": "batch_count", "mid": cb.message.message_id}
    await edit(cb, u"\U0001f4e6 <b>Пакет чеков</b>\n\n"
               "Сколько чеков сгенерировать? Выбери или пришли число (2-10):",
               kb([(u"\U0001f4e6 3 чека", "batch_n:3"), (u"\U0001f4e6 5 чеков", "batch_n:5")],
                  [(u"\U0001f4e6 10 чеков", "batch_n:10")],
                  [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

async def _run_batch(msg_or_cb, uid, n):
    """Общая логика пакетной генерации N чеков СБП с рандомными параметрами."""
    g = gen()
    ok = 0
    chat_id = msg_or_cb.message.chat.id if isinstance(msg_or_cb, CallbackQuery) else msg_or_cb.chat.id
    for i in range(n):
        try:
            name = random.choice(_FAST_NAMES)
            phone = _rand_phone()
            bank = random.choice(BANKS)
            amount = random.randint(1000, 50000)
            now = now_msk()
            meta = {}
            pdf = await run_gen(g["generate"], name, phone, bank, amount, now,
                               account_last4=None, meta_out=meta)
            gen_id = _lk_store(uid, _lk_payload_sbp({"name": name, "phone": phone,
                               "bank": bank, "amount": amount}, meta,
                               owner=_lk_owner(uid)), "new")
            await bot.send_document(chat_id,
                BufferedInputFile(pdf, filename=f"receipt_{i+1}_{amount}.pdf"),
                caption=(f"\U0001f4e6 <b>Чек {i+1}/{n}</b>\n"
                         f"\U0001f464 {name}\n\U0001f4f1 {phone}\n"
                         f"\U0001f3e6 {bank}\n\U0001f4b0 {g['fmt'](amount)}\n"
                         f"\U0001f552 {now.strftime('%d.%m.%Y %H:%M:%S')}"),
                reply_markup=_lk_markup("new", gen_id))
            data["logs"].append({"uid": uid, "name": name, "phone": phone,
                "bank": bank, "amount": amount, "acc": "", "type": "sbp",
                "time": now.strftime("%d.%m.%Y %H:%M:%S")})
            if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
            save(data)
            ok += 1
        except Exception as e:
            logging.getLogger("bot").error("batch item %d failed: %s", i+1, e)
    await bot.send_message(chat_id,
        f"\u2705 <b>Пакет готов</b>\n\nСгенерировано: <b>{ok}/{n}</b>",
        reply_markup=main_menu(uid))

@router.callback_query(F.data.startswith("batch_n:"))
async def cb_batch_n(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not feat(uid, "batch"): await cb.answer("Фича не выдана", show_alert=True); return
    try: n = int(cb.data.split(":", 1)[1])
    except ValueError: await cb.answer("Ошибка"); return
    if not (2 <= n <= 10): await cb.answer("От 2 до 10"); return
    sessions.pop(uid, None)
    await cb.answer(u"\u23f3 Генерирую пачку…")
    await edit(cb, f"\U0001f4e6 <b>Пакет: {n} чеков</b>\n\n\u23f3 <b>Генерация…</b>", uid=uid)
    await _run_batch(cb, uid, n)

# ── 📊 СТАТИСТИКА (per-user: stats) — кнопка ──
@router.callback_query(F.data == "stats_btn")
async def cb_stats_btn(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not feat(uid, "stats"): await cb.answer("Фича не выдана", show_alert=True); return
    sessions.pop(uid, None)
    await edit(cb, _stats_text(uid), other_menu(uid), uid)
    await cb.answer()

# ── 🌙 ТЁМНАЯ ТЕМА (per-user: dark_theme) — переключатель ──
@router.callback_query(F.data == "dark_theme")
async def cb_dark_theme(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not feat(uid, "dark_theme"): await cb.answer("Фича не выдана", show_alert=True); return
    cur = _fflags(uid).get("dark_theme_on", False)
    _ff_pref_set(uid, "dark_theme_on", not cur)
    state = u"включена 🌙" if not cur else u"выключена ☀️"
    await cb.answer(f"Тёмная тема {state}")
    await edit(cb, f"\U0001f313 <b>Тёмная тема {state}</b>\n\n"
                   "Переключатель сохранён.", other_menu(uid), uid)

# ── 🔔 УВЕДОМЛЕНИЯ (per-user: notifications) — переключатель ──
@router.callback_query(F.data == "notifications")
async def cb_notifications(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not feat(uid, "notifications"): await cb.answer("Фича не выдана", show_alert=True); return
    cur = _fflags(uid).get("notifications_on", True)
    _ff_pref_set(uid, "notifications_on", not cur)
    state = u"включены 🔔" if not cur else u"выключены 🔕"
    await cb.answer(f"Уведомления {state}")
    await edit(cb, f"\U0001f515 <b>Уведомления {state}</b>\n\n"
                   "Когда чек готов — бот пришлёт отдельное сообщение (если включено).",
               other_menu(uid), uid)

# ── 🔑 API-ТОКЕН (per-user: api_token) ──
@router.callback_query(F.data == "api_token")
async def cb_api_token(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not feat(uid, "api_token"): await cb.answer("Фича не выдана", show_alert=True); return
    token = _fflags(uid).get("api_token_value")
    if not token:
        token = "mts_" + uuid.uuid4().hex[:24]
        _ff_pref_set(uid, "api_token_value", token)
    await edit(cb, f"\U0001f511 <b>Твой персональный API-токен</b>\n\n"
                   f"<code>{html.escape(token)}</code>\n\n"
                   "Используй его в заголовке <code>X-Internal-Token</code> для запросов к API.\n"
                   "<i>Не показывай токен посторонним.</i>",
               kb([(u"\U0001f504 Новый токен", "api_token_new")],
                  [(u"\U0001f3e0 Меню", "home")]), uid)
    await cb.answer()

@router.callback_query(F.data == "api_token_new")
async def cb_api_token_new(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not feat(uid, "api_token"): await cb.answer("Фича не выдана", show_alert=True); return
    token = "mts_" + uuid.uuid4().hex[:24]
    _ff_pref_set(uid, "api_token_value", token)
    await cb.answer(u"\U0001f511 Новый токен выдан")
    await edit(cb, f"\U0001f511 <b>Новый API-токен</b>\n\n"
                   f"<code>{html.escape(token)}</code>\n\nСтарый токен больше не работает.",
               kb([(u"\U0001f3e0 Меню", "home")]), uid)

@router.callback_query(F.data.startswith("again_params:"))
async def cb_again_params(cb: CallbackQuery):
    """«🔄 С другими параметрами» — запускает флоу с предзаполненными данными
    последнего чека. Пользователь меняет только то, что нужно (сумму/счёт/имя)."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    flow = cb.data.split(":", 1)[1]
    last = _get_last_params(uid)
    if not last or last.get("flow") != flow:
        # Нет подходящих параметров — запускаем обычный флоу
        if flow == "card":
            await cb.answer(); 
            sessions[uid] = {"step": "card_form", "mode": "card", "mid": cb.message.message_id}
            await edit(cb,
                "\U0001f4b3 <b>Перевод по карте</b>\n\n\U0001f4c4 <b>Версия чека:</b>",
                kb([("\U0001f195 Новая версия (сайт)", "cform:web")],
                   [("\u26a0\ufe0f Старая (приложение) — чекер режет", "cform:app")],
                   [(u"\u274c Отмена", "cancel")]), uid)
        else:
            await cb.answer()
            sessions[uid] = {"step": "name", "mid": cb.message.message_id}
            await edit(cb,
                "\U0001f464 <b>Имя получателя</b>\n\nВведи как на чеке:\n<code>Сергей Михайлович К</code>",
                kb([(u"\u274c Отмена", "cancel")]), uid)
        return
    p = last["params"]
    if flow == "sbp":
        # СБП: предзаполняем name+phone+bank, переход на шаг ввода суммы
        sessions[uid] = {"step": "amount", "mid": cb.message.message_id,
                         "name": p.get("name", ""), "phone": p.get("phone", ""),
                         "bank": p.get("bank", ""), "acc4": p.get("acc4", ""),
                         "_again": True}
        await edit(cb,
            f"\U0001f501 <b>С другими параметрами</b>\n\n"
            f"Старые данные подставлены — измени что нужно:\n\n"
            f"\U0001f464 {p.get('name','?')}\n"
            f"\U0001f4f1 {p.get('phone','?')}\n"
            f"\U0001f3e6 {p.get('bank','?')} \u2705\n\n"
            f"\U0001f4b0 <b>Введи новую сумму (руб):</b>",
            kb([(u"\u274c Отмена", "cancel")]), uid)
    elif flow == "card":
        # Карта: предзаполняем pan_from/pan_to, переход на шаг ввода суммы
        form = "web" if p.get("pan_from") else "app"
        sessions[uid] = {"step": "card_amount", "mode": "card", "form": form,
                         "mid": cb.message.message_id,
                         "pan_from": p.get("pan_from", ""), "pan_to": p.get("pan_to", ""),
                         "card_from": p.get("card_from", ""), "card_to": p.get("card_to", ""),
                         "_again": True}
        cards_line = (f"\U0001f4b3 {p.get('pan_from','?')} \u2192 {p.get('pan_to','?')}"
                     if form == "web" else
                     f"\U0001f4b3 •• {p.get('card_from','?')} \u2192 •• {p.get('card_to','?')}")
        await edit(cb,
            f"\U0001f501 <b>С другими параметрами</b>\n\n"
            f"Старые карты подставлены — измени что нужно:\n\n"
            f"{cards_line} \u2705\n\n"
            f"\U0001f4b0 <b>Введи новую сумму (руб):</b>",
            kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data.startswith("bank:"))
async def cb_bank(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid]["step"] != "bank":
        await cb.answer("Начни заново"); return
    sessions[uid]["bank"] = cb.data.split(":",1)[1]
    sessions[uid]["step"] = "amount"
    await edit(cb,
        f"\U0001f3e6 Банк: <b>{sessions[uid]['bank']}</b> \u2705\n\n"
        "\U0001f4b0 <b>Сумма перевода (руб):</b>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data == "bank_custom")
async def cb_bank_custom(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid]["step"] != "bank":
        await cb.answer("Начни заново"); return
    sessions[uid]["step"] = "bank_input"
    await edit(cb,
        "\U0001f3e6 <b>Введи название банка:</b>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

# ── Free-text confirmation: «перевод 2500 Пете в тбанк» ──
@router.callback_query(F.data == "ft_yes")
async def cb_ft_yes(cb: CallbackQuery):
    """Юзер подтвердил распознанные данные. Заполняем сессию и переходим
    к первому незаполненному шагу: name → phone → bank → amount → account."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    s = sessions.get(uid)
    if not s or s.get("step") != "name" or not s.get("_ft"):
        await cb.answer("Начни заново"); return
    ft = s.pop("_ft")
    g = gen()
    if ft.get("name"):
        s["name"] = ft["name"]
    if ft.get("phone"):
        s["phone"] = ft["phone"]
    if ft.get("bank"):
        s["bank"] = ft["bank"]
    if ft.get("amount") is not None:
        try:
            amt = int(ft["amount"])
        except (TypeError, ValueError):
            amt = None
        if amt is not None and 1 <= amt <= 999999:
            s["amount"] = amt
        # иначе оставим amount незаполненным — юзер введёт вручную на шаге amount

    # Определяем следующий незаполненный шаг в каноническом порядке.
    if not s.get("name"):
        s["step"] = "name"
        await edit(cb,
            "\U0001f464 <b>Имя получателя</b>\n\nВведи как на чеке:\n<code>Сергей Михайлович К</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid)
    elif not s.get("phone"):
        s["step"] = "phone"
        await edit(cb,
            f"\U0001f464 {s['name']} \u2705\n\n"
            "\U0001f4f1 <b>Телефон получателя:</b>\nВведи номер, например: <code>+79161234567</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid)
    elif not s.get("bank"):
        s["step"] = "bank"
        bank_rows = [[(b, f"bank:{b}")] for b in BANKS]
        bank_rows.append([(u"\u270f Ввести свой", "bank_custom")])
        bank_rows.append([(u"\u274c Отмена", "cancel")])
        await edit(cb,
            f"\U0001f464 {s['name']}\n\U0001f4f1 {s['phone']} \u2705\n\n\U0001f3e6 <b>Банк получателя:</b>",
            kb(*bank_rows), uid)
    elif s.get("amount") is None:
        s["step"] = "amount"
        await edit(cb,
            f"\U0001f464 {s['name']}\n\U0001f4f1 {s['phone']}\n\U0001f3e6 {s['bank']} \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода (руб):</b>",
            kb([(u"\u274c Отмена", "cancel")]), uid)
    else:
        # Всё заполнено — переходим к шагу счёта (последний перед confirm).
        s["step"] = "account"
        r1, r2, r3 = rand5(), rand5(), rand5()
        await edit(cb,
            f"\U0001f4cb <b>Проверь данные:</b>\n\n"
            f"\U0001f464 {s['name']}\n\U0001f4f1 {s['phone']}\n"
            f"\U0001f3e6 {s['bank']}\n\U0001f4b0 {g['fmt'](s['amount'])}\n\n"
            "\U0001f4b3 <b>Последние 5 цифр счёта списания:</b>\n\n"
            "Выбери или введи свои:",
            kb([(r1, f"acc:{r1}"), (r2, f"acc:{r2}"), (r3, f"acc:{r3}")],
               [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data == "ft_no")
async def cb_ft_no(cb: CallbackQuery):
    """Юзер отклонил распознанное — возвращаем к ручному вводу имени."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    s = sessions.get(uid)
    if not s:
        await cb.answer("Начни заново"); return
    s.pop("_ft", None)
    s["step"] = "name"
    await edit(cb,
        "\U0001f464 <b>Имя получателя</b>\n\nВведи как на чеке:\n<code>Сергей Михайлович К</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data.startswith("acc:"))
async def cb_acc(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid]["step"] != "account":
        await cb.answer("Начни заново"); return
    sessions[uid]["acc4"] = cb.data.split(":",1)[1]
    sessions[uid]["step"] = "confirm"
    s = sessions[uid]; g = gen()
    tpl_btn = [(u"\U0001f4cc Сохранить шаблон", "tpl_save")] if (gfeat("templates") and not s.get("_from_tpl")) else []
    await edit(cb,
        f"\U0001f441 <b>Превью чека СБП</b>\n\n"
        f"\U0001f464 <b>Получатель:</b> {s['name']}\n"
        f"\U0001f4f1 <b>Телефон:</b> {s['phone']}\n"
        f"\U0001f3e6 <b>Банк:</b> {s['bank']}\n"
        f"\U0001f4b0 <b>Сумма:</b> {g['fmt'](s['amount'])}\n"
        f"\U0001f4b3 <b>Счёт:</b> ...{s['acc4']}\n"
        f"\U0001f552 <b>Время:</b> текущее (МСК)\n\n"
        u"\u2705 Всё верно — жми «Сгенерировать»",
        kb([(u"\u2705 Сгенерировать", "confirm")],
           *([tpl_btn] if tpl_btn else []),
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data == "confirm")
async def cb_confirm(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid]["step"] != "confirm":
        await cb.answer("Начни заново"); return
    sessions[uid]["step"] = "gen_busy"  # анти-двойной тап: повторное нажатие во время генерации
    s = sessions[uid]; g = gen()
    ahead = gen_queue_ahead()
    await edit(cb, _queue_text(ahead) if ahead else u"\u23f3 <b>Генерация чека…</b>\nШаг 2/3: рендер PDF (3-5 сек)", uid=uid)
    await cb.answer()
    try:
        now = now_msk()
        meta = {}
        pdf = await run_gen(g["generate"],
                            s["name"], s["phone"], s["bank"], s["amount"],
                            now, account_last4=s.get("acc4"), meta_out=meta)
        gen_id = _lk_store(uid, _lk_payload_sbp(s, meta, owner=_lk_owner(uid)), "new")
        try: await cb.message.delete()
        except: pass
        await bot.send_document(cb.message.chat.id,
            BufferedInputFile(pdf, filename=f"receipt_{s['amount']}.pdf"),
            caption=(
                f"\u2705 <b>Чек готов</b>\n\n"
                f"\U0001f464 {s['name']}\n\U0001f4f1 {s['phone']}\n"
                f"\U0001f3e6 {s['bank']}\n\U0001f4b0 {g['fmt'](s['amount'])}\n"
                f"\U0001f4b3 ...{s.get('acc4','****')}\n"
                f"\U0001f552 {now.strftime('%d.%m.%Y %H:%M:%S')}"),
            reply_markup=_lk_markup("new", gen_id))
        data["logs"].append({"uid": uid, "name": s["name"], "phone": s["phone"],
            "bank": s["bank"], "amount": s["amount"], "acc": s.get("acc4",""),
            "time": now.strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        # Сохраняем параметры для «🔄 С другими параметрами»
        _save_last_params(uid, "sbp", {"name": s["name"], "phone": s["phone"],
                                       "bank": s["bank"], "acc4": s.get("acc4", "")})
        save(data)
        # 🔔 Уведомление о готовности (если per-user фича выдана и включена)
        if feat(uid, "notifications") and _fflags(uid).get("notifications_on", True):
            try:
                await bot.send_message(cb.message.chat.id,
                    u"\U0001f514 <b>Чек готов!</b>\n"
                    f"\U0001f3e6 {s['bank']} \u2022 \U0001f4b0 {g['fmt'](s['amount'])} \u20bd \u2022 "
                    f"\U0001f552 {now.strftime('%H:%M:%S')}")
            except Exception:
                pass
    except Exception as e:
        await bot.send_message(cb.message.chat.id,
            f"\u274c <b>Ошибка:</b> <code>{html.escape(str(e))[:300]}</code>\n\n"
            u"\U0001f4a1 Попробуй уменьшить сумму или проверить имя/телефон.",
            reply_markup=main_menu(uid))
    finally:
        # Не сносим новую сессию, если юзер за время генерации начал другой флоу.
        if sessions.get(uid) is s:
            sessions.pop(uid, None)

# ── CARD CHECK FLOW ──
def rand4():
    return f"{random.randint(1000,9999)}"

@router.callback_query(F.data == "new_card")
async def cb_new_card(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions[uid] = {"step": "card_form", "mode": "card", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f4b3 <b>Перевод по карте</b>\n\n"
        "\U0001f4c4 <b>Версия чека:</b>",
        kb([("\U0001f195 Новая версия (сайт)", "cform:web")],
           [("\u26a0\ufe0f Старая (приложение) — чекер режет", "cform:app")],
           [(u"\u2b05 В банк", "bankmenu:mts")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data.startswith("cform:"))
async def cb_card_form(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "card_form":
        await cb.answer("Начни заново"); return
    form = cb.data.split(":", 1)[1]
    sessions[uid]["form"] = form
    if form == "web":
        sessions[uid]["step"] = "card_pan_from"
        r = rand4()
        await edit(cb,
            "\U0001f195 <b>Новая версия чека</b>\n\n"
            "\U0001f4b3 <b>Карта списания</b> — пришли маску или номер целиком:\n"
            "<code>220383******7826</code>\n\n"
            "Выбери или введи свою:",
            kb([(f"220383******{r}", f"cpanf:220383******{r}")],
               [(u"\u274c Отмена", "cancel")]), uid)
    else:
        sessions[uid]["step"] = "card_from"
        r1, r2, r3 = rand4(), rand4(), rand4()
        await edit(cb,
            "\U0001f4b3 <b>Перевод по карте</b>\n\n"
            "\U0001f4b3 <b>Последние 4 цифры карты списания:</b>\n\n"
            "Выбери или введи свои:",
            kb([(r1, f"cfrom:{r1}"), (r2, f"cfrom:{r2}"), (r3, f"cfrom:{r3}")],
               [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data.startswith("cpanf:"))
async def cb_pan_from(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "card_pan_from":
        await cb.answer("Начни заново"); return
    sessions[uid]["pan_from"] = cb.data.split(":", 1)[1]
    sessions[uid]["step"] = "card_pan_to"
    r = rand4()
    await edit(cb,
        f"\U0001f4b3 Списание: <b>{sessions[uid]['pan_from']}</b> \u2705\n\n"
        "\U0001f4b3 <b>Карта зачисления</b> — пришли маску или номер целиком:\n"
        "<code>220070******8429</code>\n\n"
        "Выбери или введи свою:",
        kb([(f"220070******{r}", f"cpant:220070******{r}")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data.startswith("cpant:"))
async def cb_pan_to(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "card_pan_to":
        await cb.answer("Начни заново"); return
    sessions[uid]["pan_to"] = cb.data.split(":", 1)[1]
    sessions[uid]["step"] = "card_amount"
    await edit(cb,
        f"\U0001f4b3 Списание: <b>{sessions[uid]['pan_from']}</b>\n"
        f"\U0001f4b3 Зачисление: <b>{sessions[uid]['pan_to']}</b> \u2705\n\n"
        "\U0001f4b0 <b>Сумма перевода (руб):</b>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data.startswith("cfrom:"))
async def cb_card_from(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "card_from":
        await cb.answer("Начни заново"); return
    sessions[uid]["card_from"] = cb.data.split(":", 1)[1]
    sessions[uid]["step"] = "card_to"
    r1, r2, r3 = rand4(), rand4(), rand4()
    await edit(cb,
        f"\U0001f4b3 Карта списания: <b>•• {sessions[uid]['card_from']}</b> \u2705\n\n"
        "\U0001f4b3 <b>Последние 4 цифры карты зачисления:</b>\n\n"
        "Выбери или введи свои:",
        kb([(r1, f"cto:{r1}"), (r2, f"cto:{r2}"), (r3, f"cto:{r3}")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data.startswith("cto:"))
async def cb_card_to(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "card_to":
        await cb.answer("Начни заново"); return
    sessions[uid]["card_to"] = cb.data.split(":", 1)[1]
    sessions[uid]["step"] = "card_amount"
    await edit(cb,
        f"\U0001f4b3 Списание: <b>•• {sessions[uid]['card_from']}</b>\n"
        f"\U0001f4b3 Зачисление: <b>•• {sessions[uid]['card_to']}</b> \u2705\n\n"
        "\U0001f4b0 <b>Сумма перевода (руб):</b>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data == "card_confirm")
async def cb_card_confirm(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "card_confirm":
        await cb.answer("Начни заново"); return
    sessions[uid]["step"] = "gen_busy"  # анти-двойной тап
    s = sessions[uid]
    web = s.get("form") == "web"
    cg = wgen() if web else cgen()
    ahead = gen_queue_ahead()
    await edit(cb, _queue_text(ahead) if ahead else u"\u23f3 <b>Генерация карточного чека...</b> 3-5 сек", uid=uid)
    await cb.answer()
    try:
        now = now_msk()
        meta = {}
        if web:
            pdf = await run_gen(cg["generate"],
                                s["amount"], pan_from=s["pan_from"],
                                pan_to=s["pan_to"], dt=now, meta_out=meta)
            comm = cg["comm"](s["amount"])
            total = s["amount"] + comm
            cards_line = f"\U0001f4b3 {s['pan_from']} \u2192 {s['pan_to']}"
            debit_mask, credit_mask = s["pan_from"], s["pan_to"]
            money = (f"\U0001f4b0 Сумма: {cg['fmt_amt'](s['amount'])}\n"
                     f"\U0001f4b8 Комиссия: {cg['fmt_amt'](comm)}\n"
                     f"\U0001f4b5 Итого: {cg['fmt_tot'](total)}")
        else:
            pdf = await run_gen(cg["generate"],
                                s["amount"], card_from=s["card_from"],
                                card_to=s["card_to"], dt=now, meta_out=meta)
            comm = cg["comm"](s["amount"])
            total = s["amount"] + comm
            cards_line = f"\U0001f4b3 •• {s['card_from']} \u2192 •• {s['card_to']}"
            debit_mask = f"•• {s['card_from']}"
            credit_mask = f"•• {s['card_to']}"
            money = (f"\U0001f4b0 Сумма: {cg['fmt'](s['amount'])}\n"
                     f"\U0001f4b8 Комиссия: {cg['fmt'](comm)}\n"
                     f"\U0001f4b5 Итого: {cg['fmt'](total)}")
        gen_id = _lk_store(uid, _lk_payload_card(s["amount"], meta, debit_mask, credit_mask, owner=_lk_owner(uid)), "new_card")
        try: await cb.message.delete()
        except: pass
        await bot.send_document(cb.message.chat.id,
            BufferedInputFile(pdf, filename=f"card_{s['amount']}.pdf"),
            caption=(
                f"\u2705 <b>Карточный чек готов</b> "
                f"({'новая версия' if web else 'старая версия'})\n\n"
                f"{cards_line}\n"
                f"{money}\n"
                f"\U0001f552 {now.strftime('%d.%m.%Y %H:%M:%S')}"),
            reply_markup=_lk_markup("new_card", gen_id))
        data["logs"].append({"uid": uid, "type": "card",
            "form": "web" if web else "app",
            "card_from": s.get("pan_from") or s.get("card_from"),
            "card_to": s.get("pan_to") or s.get("card_to"),
            "amount": s["amount"], "comm": comm,
            "time": now.strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        # Сохраняем параметры для «🔄 С другими параметрами»
        _save_last_params(uid, "card", {
            "form": "web" if web else "app",
            "pan_from": s.get("pan_from", ""), "pan_to": s.get("pan_to", ""),
            "card_from": s.get("card_from", ""), "card_to": s.get("card_to", ""),
        })
        save(data)
    except Exception as e:
        await bot.send_message(cb.message.chat.id,
            f"\u274c <b>Ошибка:</b> <code>{html.escape(str(e))[:300]}</code>\n\n"
            u"\U0001f4a1 Попробуй другую сумму или проверь номера карт.",
            reply_markup=main_menu(uid))
    finally:
        # Не сносим новую сессию, если юзер за время генерации начал другой флоу.
        if sessions.get(uid) is s:
            sessions.pop(uid, None)


async def _lk_finish_bind(msg, uid, label):
    data.setdefault("lk_bindings", {})[str(uid)] = label
    save(data)
    gen_id = (sessions.get(uid) or {}).get("gen")
    sessions.pop(uid, None)
    p = lk_pendings.get(gen_id) if gen_id else None
    if p is None:
        cand = [(g, v) for g, v in lk_pendings.items() if v.get("uid") == uid and not v.get("done")]
        p = cand[-1][1] if cand else None
    added = False
    if p and not p.get("done"):
        p["payload"]["owner"] = label
        p["done"] = True
        try:
            _st, _r = await api_call("POST", "/payments/", p["payload"])
            added = _st in (200, 201)
            if not added:
                p["done"] = False
        except Exception:
            p["done"] = False
        data["lk_pendings"] = lk_pendings
        save(data)
    await bot.send_message(msg.chat.id,
        f"\u2705 Админка <b>{label}</b> привязана." + (" Перевод добавлен в твою ЛК." if added else " Теперь нажми «Добавить перевод в ЛК» у чека."),
        reply_markup=main_menu(uid))


async def _lk_bind_prompt(ev, uid, gen_id=None):
    """Старт привязки: пользователь выбирает свой логин панели из списка
    (или вводит вручную), затем подтверждает паролем."""
    users = await _fetch_users()
    mid = ev.message.message_id if isinstance(ev, CallbackQuery) else None
    s = {"step": "lk_bind_pick", "gen": gen_id}
    if mid:
        s["mid"] = mid
    sessions[uid] = s
    rows = []
    if users:
        s["lk_choices"] = {u["label"]: u["login"] for u in users if not u.get("banned")}
        for u in users:
            if u.get("banned"):
                continue
            rows.append([(f"\U0001f464 {u['login']} [{u['label']}]", f"lk_pick:{u['label']}")])
    rows.append([(u"\u270f Ввести логин вручную", "lk_manual")])
    rows.append([(u"\u274c Отмена", "cancel")])
    text = ("\U0001f517 <b>Привязка своей админки</b>\n\n"
            "Выбери свой <b>логин</b> от админки — переводы из бота будут падать "
            "только в твою админку и выписки.")
    if not users:
        text += "\n\n\u26a0\ufe0f Список недоступен (API) — введи логин вручную."
    if isinstance(ev, CallbackQuery) and ev.message.text is None:
        # The receipt is a document — its text can't be edited. Send a fresh
        # prompt so the receipt (and its buttons) stays intact.
        m = await ev.message.answer(text, reply_markup=kb(*rows))
        s["mid"] = m.message_id
    else:
        await edit(ev, text, kb(*rows), uid)


@router.callback_query(F.data == "lk_bind")
async def cb_lk_bind(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid):
        await cb.answer("Нет доступа"); return
    await cb.answer()  # мгновенный ack: список логинов тянется из API до 15 с
    await _lk_bind_prompt(cb, uid)


@router.callback_query(F.data == "lk_manual")
async def cb_lk_manual(cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in sessions or sessions[uid].get("step") != "lk_bind_pick":
        await cb.answer("Начни заново"); return
    sessions[uid]["step"] = "lk_bind_login"
    await edit(cb,
        "\U0001f517 <b>Привязка своей админки</b>\n\n"
        "Пришли свой <b>логин</b> от админки (или старый инвайт-код <code>MTONS-...</code>):",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data.startswith("lk_pick:"))
async def cb_lk_pick(cb: CallbackQuery):
    uid = cb.from_user.id
    s = sessions.get(uid)
    if not s or s.get("step") != "lk_bind_pick":
        await cb.answer("Начни заново"); return
    label = cb.data.split(":", 1)[1]
    login = (s.get("lk_choices") or {}).get(label)
    if not login:
        await cb.answer("Логин не найден — обнови список", show_alert=True); return
    s["lk_login"] = login
    s["step"] = "lk_bind_pass"
    await edit(cb,
        f"\U0001f464 Логин: <b>{login}</b> [{label}] \u2705\n\n"
        "\U0001f511 Пришли <b>пароль</b> от админки:",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "lk_add")
async def cb_lk_add_stale(cb: CallbackQuery):
    await cb.answer("Эта кнопка устарела — сгенерируй чек заново", show_alert=True)


async def _lk_do_add(cb: CallbackQuery, uid, gen_id):
    """Add the stored operation to the caller's ЛК: progress -> API -> result."""
    p = lk_pendings.get(gen_id)
    if not p:
        await cb.answer("Этот чек устарел — сгенерируй новый", show_alert=True); return
    if p.get("done"):
        await cb.answer("✅ Этот перевод уже в ЛК")
        await _lk_card(cb, p, gen_id, "done")
        return
    if gen_id in _lk_busy:
        await cb.answer("⏳ Уже добавляю, пару секунд…"); return
    _lk_busy.add(gen_id)
    try:
        # (a) progress: instant toast + the button flips to an inert spinner
        await cb.answer("⏳ Добавляю в ЛК…")
        await _lk_card(cb, p, gen_id, "progress")
        # (b) call the LK API for the current user's tenant
        p["payload"]["owner"] = _lk_owner(uid) or p["payload"].get("owner")
        try:
            status, resp = await api_call("POST", "/payments/", p["payload"])
        except Exception:
            status, resp = None, None
        # (c) result: caption status line + final button state
        if status in (200, 201):
            p["done"] = True
            data["lk_pendings"] = lk_pendings
            save(data)
            await _lk_card(cb, p, gen_id, "done", "\u2705 Добавлено в ЛК")
            return
        err = str((resp or {}).get("error") or "")
        det = (resp or {}).get("details")
        if status == 409 or re.search(r"duplicat|already exist|уже (есть|существует|добавлен)", err, re.I):
            # duplicate operation: it is already in the ЛК — treat as done
            p["done"] = True
            data["lk_pendings"] = lk_pendings
            save(data)
            await _lk_card(cb, p, gen_id, "done", "\u26a0\ufe0f Такой перевод уже есть в ЛК")
            return
        if status is None:
            line = "\u274c ЛК: API недоступен — попробуй позже"
        else:
            reason = err or (json.dumps(det, ensure_ascii=False) if det else "") or f"HTTP {status}"
            reason = html.escape(" ".join(str(reason).split()))[:120]
            line = f"\u274c ЛК: ошибка ({reason})"
        await _lk_card(cb, p, gen_id, "add", line)
    finally:
        _lk_busy.discard(gen_id)


@router.callback_query(F.data == "lk_done")
async def cb_lk_done(cb: CallbackQuery):
    await cb.answer("✅ Этот перевод уже добавлен в ЛК")


@router.callback_query(F.data == "lk_busy")
async def cb_lk_busy(cb: CallbackQuery):
    await cb.answer("⏳ Добавляю в ЛК, пару секунд…")


@router.callback_query(F.data.startswith("lk_add:"))
async def cb_lk_add(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    gen_id = cb.data.split(":", 1)[1]
    p = lk_pendings.get(gen_id)
    if not p or p.get("uid") != uid:
        await cb.answer("Этот чек устарел — сгенерируй новый", show_alert=True); return
    if p.get("done"):
        await cb.answer("✅ Этот перевод уже в ЛК")
        await _lk_card(cb, p, gen_id, "done")
        return
    if not _lk_owner(uid):
        # Not bound to a tenant yet — explain how to bind (prompt goes as a
        # separate message; the receipt document itself is not touched).
        s = sessions.get(uid) or {}
        if str(s.get("step") or "").startswith("lk_bind"):
            await cb.answer("🔗 Привязка уже ждёт твой ответ ниже 👇"); return
        await cb.answer("🔗 Сначала привяжи свою админку")
        await _lk_bind_prompt(cb, uid, gen_id)
        return
    await _lk_do_add(cb, uid, gen_id)

# ── TEXT INPUT ──
# ─────────────────────────── EXI-BANK statement ───────────────────────────
STMT_HINT = (
    "\U0001f4dd <b>Операции</b> — по одной в строке:\n"
    "<code>-2500 Яна Юрьевна З Переводы</code>\n"
    "<code>+9800 Перевод на карту Переводы</code>\n"
    "<code>-1350 Пятёрочка Супермаркеты</code>\n\n"
    "Знак задаёт направление. До 9 операций."
)

def jdouble(v):
    """Render like Java Double.toString — the form the originals print."""
    return repr(float(v))

def acct_with_key(acc20: str) -> str:
    """Fix the CBR check digit (position 9) for EXI-Bank BIK 044030889.

    Genuine accounts satisfy sum((d*w)%10) % 10 == 0 over "889"+account with
    weights 7,1,3; a template account with random tail digits does not.
    """
    for d in "0123456789":
        cand = acc20[:8] + d + acc20[9:]
        s = "889" + cand
        if sum((int(ch) * (7, 1, 3)[i % 3]) % 10 for i, ch in enumerate(s)) % 10 == 0:
            return cand
    return acc20

def parse_ops(text, signed=True):
    """'-2500 Описание' -> ('-2500.00 RUR', 'Описание', 2500.0, False).

    Table amounts carry two decimals; hold amounts use the Java double form.
    Returns (rows, error_message).
    """
    rows = []
    for ln in text.strip().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^([+-]?)\s*(\d+(?:[.,]\d{1,2})?)\s+(.+)$", ln)
        if not m:
            return None, f"Не разобрал строку: <code>{ln[:40]}</code>"
        sign, num, desc = m.group(1), m.group(2).replace(",", "."), m.group(3).strip().replace("Ё", "Е")
        val = float(num)
        if val <= 0 or val > 9_999_999:
            return None, "Сумма должна быть от 0.01 до 9 999 999"
        amt = f"{sign}{val:.2f} RUR" if signed else f"{jdouble(val)} RUR"
        rows.append((amt, desc, val, sign != "-"))
    if not rows:
        return None, "Пусто"
    if len(rows) > 9:
        return None, "Максимум 9 операций"
    return rows, None

def stmt_dates(n, base):
    """n timestamps, newest first, walking back in plausible steps."""
    out = []
    t = base
    for _ in range(n):
        out.append(t)
        t = t - timedelta(minutes=random.randint(7, 230), seconds=random.randint(0, 59))
    return out

async def _stmt_summary(ev, uid):
    s = sessions[uid]; s["step"] = "st_confirm"
    inc = sum(v for _a, _d, v, pos in s["ops"] if pos)
    exp = sum(v for _a, _d, v, pos in s["ops"] if not pos)
    lines = "\n".join(f"  {a}  {d[:34]}" for a, d, _v, _p in s["ops"])
    hl = "\n".join(f"  {a}  {d[:34]}" for a, d, _v, _p in (s.get("holds") or [])) or "  —"
    await edit(ev,
        f"\U0001f4cb <b>Проверь выписку:</b>\n\n"
        f"\U0001f464 {s['name']}\n"
        f"\U0001f4b3 (MIR) 220383*{s['card4']}\n"
        f"\U0001f4b3 Счёт: ...{s['acc7']}\n"
        f"\U0001f4b0 Остаток: {s['balance']:.2f} RUR\n\n"
        f"\U0001f9fe <b>Операции:</b>\n<code>{lines}</code>\n"
        f"\U0001f4c8 + {jdouble(inc)} / \U0001f4c9 - {jdouble(exp)} RUR\n\n"
        f"\U0001f512 <b>Холды:</b>\n<code>{hl}</code>",
        kb([(u"\u2705 Сгенерировать", "stmt_confirm")],
           [(u"\u274c Отмена", "cancel")]), uid)

async def _wb_finish(msg_or_cb, uid):
    s = sessions.get(uid)
    if not s:
        # Сессия потеряна (рестарт/дворник) — не генерируем чек с пустыми данными.
        await bot.send_message(uid, "\u274c Сессия устарела — начни заново: /start",
                               reply_markup=main_menu(uid))
        return
    wb = wbgen()
    await _progress_action(uid)
    qmsg = await _queue_notice(uid)
    try:
        name = bank_name(s.get("wb_name", ""))
        if name and not name.endswith("."):
            name += "."
        meta = {}
        pdf = await run_gen(wb.generate, name, s.get("wb_phone", ""), s.get("wb_bank", ""),
                            int(s.get("wb_amount", 0)), message=s.get("wb_msg", ""),
                            meta_out=meta)
    except Exception as e:
        await _clear_note(qmsg)
        await bot.send_message(uid, f"\u274c Ошибка генерации WB: <code>{html.escape(str(e))[:300]}</code>",
                               reply_markup=main_menu(uid))
        if sessions.get(uid) is s:
            sessions.pop(uid, None)
        return
    await _clear_note(qmsg)
    if sessions.get(uid) is s:  # не сносим новую сессию, начатую за время генерации
        sessions.pop(uid, None)
    await bot.send_document(
        uid,
        BufferedInputFile(pdf, filename=f"wb_{meta.get('receipt_no')}.pdf"),
        caption=(f"\U0001f9fe WB \u2022 {s.get('wb_amount')} \u20bd \u2022 {s.get('wb_bank', '')}\n"
                 f"Квитанция № {meta.get('receipt_no')}"),
        reply_markup=kb([(u"\U0001f504 Ещё чек ВБ", "new_wb")], [(u"\U0001f3e0 Меню", "home")]))
    if gfeat("log_all_banks"):
        data["logs"].append({"uid": uid, "type": "wb", "name": name,
                             "phone": s.get("wb_phone", ""), "bank": s.get("wb_bank", ""),
                             "amount": int(s.get("wb_amount", 0)),
                             "time": now_msk().strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        save(data)


@router.callback_query(F.data == "new_wb")
async def cb_new_wb(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions[uid] = {"step": "wb_name", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f9fe <b>Чек ВБ Банка (Wildberries)</b>\n\n"
        "\U0001f464 <b>ФИО получателя:</b>\nНапример: <code>Демид Андреевич Васильев</code>",
        kb([(u"\u2b05 В банк", "bankmenu:wb")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "wb_skip")
async def cb_wb_skip(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "wb_msg":
        await cb.answer("Начни заново"); return
    sessions[uid]["wb_msg"] = ""
    sessions[uid]["step"] = "gen_busy"  # анти-двойной тап по «Без сообщения»
    await cb.answer()
    await _wb_finish(cb, uid)


async def _vtb_finish(msg_or_cb, uid):
    s = sessions.get(uid)
    if not s:
        # Сессия потеряна (рестарт/дворник) — не генерируем чек с пустыми данными.
        await bot.send_message(uid, "\u274c Сессия устарела — начни заново: /start",
                               reply_markup=main_menu(uid))
        return
    vtb = vtbgen()
    await _progress_action(uid)
    qmsg = await _queue_notice(uid)
    try:
        rec = bank_name(s.get("vtb_name", ""))
        if rec and not rec.endswith("."):
            rec += "."
        payer = bank_name(s.get("vtb_payer", ""))
        if payer and not payer.endswith("."):
            payer += "."
        bank = s.get("vtb_bank", "Т-Банк")
        if not payer:
            payer = vtb.pick_payer_for(rec, bank)
        phone = format_phone(s.get("vtb_phone", "")) or s.get("vtb_phone", "")
        amount = int(s.get("vtb_amount", 0))
        acc = s.get("vtb_acc") or "*0298"
        meta = {}
        pdf = await run_gen(vtb.generate,
                            payer=payer, recipient=rec, phone=phone,
                            bank=bank, amount_rub=amount, account=acc,
                            message=None, meta_out=meta)
    except Exception as e:
        await _clear_note(qmsg)
        await bot.send_message(uid, f"\u274c Ошибка генерации ВТБ: <code>{html.escape(str(e))[:300]}</code>",
                               reply_markup=main_menu(uid))
        if sessions.get(uid) is s:
            sessions.pop(uid, None)
        return
    await _clear_note(qmsg)
    if sessions.get(uid) is s:  # не сносим новую сессию, начатую за время генерации
        sessions.pop(uid, None)
    fn = meta.get("dt", now_msk()).strftime("%d-%m-%y_%H-%M.pdf")
    await bot.send_document(
        uid,
        BufferedInputFile(pdf, filename=fn),
        caption=(f"\U0001f9fe ВТБ \u2022 {meta.get('amount', f'{amount} ₽')} \u2022 {bank}\n"
                 f"\U0001f464 Отправитель: {payer} ({meta.get('account', acc)})\n"
                 f"\U0001f464 Получатель: {rec}\n"
                 f"\U0001f194 <code>{meta.get('op_id', '')}</code>"),
        reply_markup=kb([(u"\U0001f504 Ещё чек ВТБ", "new_vtb")], [(u"\U0001f3e0 Меню", "home")]))
    if gfeat("log_all_banks"):
        data["logs"].append({"uid": uid, "type": "vtb", "name": rec,
                             "phone": phone, "bank": bank, "amount": amount,
                             "time": now_msk().strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        save(data)


@router.callback_query(F.data == "new_vtb")
async def cb_new_vtb(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions[uid] = {"step": "vtb_name", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f9fe <b>Чек ВТБ (СБП)</b>\n"
        "Полный русский + латинский алфавит (33/33 + 52/52)\n\n"
        "\U0001f464 <b>ФИО получателя:</b>\n"
        "Например: <code>Григорий Сергеевич Ж.</code> или <code>Денис Викторович П.</code>",
        kb([(u"\u2b05 В банк", "bankmenu:vtb")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data.startswith("vtb_bank:"))
async def cb_vtb_bank(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "vtb_bank":
        await cb.answer("Начни заново"); return
    bank = cb.data.split(":", 1)[1]
    sessions[uid]["vtb_bank"] = bank
    sessions[uid]["step"] = "vtb_payer"
    await edit(cb,
        f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n"
        "\U0001f464 <b>ФИО плательщика (отправителя):</b>\n"
        "Нажми кнопку случайного плательщика или введи своё ФИО:",
        kb([(u"\U0001f3b2 Случайный плательщик", "vtb_payer_auto")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "vtb_bank_custom")
async def cb_vtb_bank_custom(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "vtb_bank":
        await cb.answer("Начни заново"); return
    sessions[uid]["step"] = "vtb_bank_input"
    await edit(cb,
        "\U0001f3e6 <b>Введи название банка получателя:</b>\n"
        "Например: <code>Сбербанк</code>, <code>Альфа-Банк</code>, <code>Озон Банк (Ozon)</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "vtb_payer_auto")
async def cb_vtb_payer_auto(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "vtb_payer":
        await cb.answer("Начни заново"); return
    s = sessions[uid]
    vtb = vtbgen()
    payer = vtb.pick_payer_for(s.get("vtb_name", ""), s.get("vtb_bank", "Т-Банк"))
    s["vtb_payer"] = payer
    s["step"] = "vtb_amount"
    await edit(cb,
        f"\U0001f464 Плательщик: <b>{payer}</b> \u2705\n\n"
        "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data.startswith("vtb_acc:"))
async def cb_vtb_acc(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "vtb_account":
        await cb.answer("Начни заново"); return
    digits = cb.data.split(":", 1)[1]
    sessions[uid]["vtb_acc"] = f"*{digits}"
    sessions[uid]["step"] = "gen_busy"  # анти-двойной тап
    await cb.answer()
    await _vtb_finish(cb, uid)


async def _rshb_finish(msg_or_cb, uid):
    s = sessions.get(uid)
    if not s:
        # Сессия потеряна (рестарт/дворник) — не генерируем чек с пустыми данными.
        await bot.send_message(uid, "\u274c Сессия устарела — начни заново: /start",
                               reply_markup=main_menu(uid))
        return
    rshb = rshbgen()
    await _progress_action(uid)
    qmsg = await _queue_notice(uid)
    try:
        rec = bank_name(s.get("rshb_name", ""))
        # No trailing dot: genuine RSHB receipts have none.
        bank = s.get("rshb_bank", "Т-Банк")
        phone = s.get("rshb_phone", "")
        amount = int(s.get("rshb_amount", 0))
        acc = s.get("rshb_acc") or "1438"
        meta = {}
        pdf = await run_gen(rshb.generate,
                            recipient=rec, phone=phone, bank=bank,
                            amount_rub=amount, account=acc, show_message=False,
                            meta_out=meta)
    except Exception as e:
        await _clear_note(qmsg)
        await bot.send_message(uid, f"\u274c Ошибка генерации РСХБ: <code>{html.escape(str(e))[:300]}</code>",
                               reply_markup=main_menu(uid))
        if sessions.get(uid) is s:
            sessions.pop(uid, None)
        return
    await _clear_note(qmsg)
    if sessions.get(uid) is s:  # не сносим новую сессию, начатую за время генерации
        sessions.pop(uid, None)
    fn = meta.get("dt", now_msk()).strftime("%d-%m-%y_%H-%M.pdf")
    await bot.send_document(
        uid,
        BufferedInputFile(pdf, filename=fn),
        caption=(f"\U0001f9fe РСХБ \u2022 {meta.get('amount', f'{amount} ₽')} \u2022 {bank}\n"
                 f"\U0001f464 Получатель: {rec}\n"
                 f"\U0001f4b3 Счёт: {meta.get('account', acc)}\n"
                 f"\U0001f194 <code>{meta.get('op_id', '')}</code>"),
        reply_markup=kb([(u"\U0001f504 Ещё чек РСХБ", "new_rshb")], [(u"\U0001f3e0 Меню", "home")]))
    if gfeat("log_all_banks"):
        data["logs"].append({"uid": uid, "type": "rshb", "name": rec,
                             "phone": phone, "bank": bank, "amount": amount,
                             "time": now_msk().strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        save(data)


@router.callback_query(F.data == "new_rshb")
async def cb_new_rshb(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions[uid] = {"step": "rshb_name", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f9fe <b>Чек РСХБ (СБП)</b>\n"
        "Полный русский алфавит (33/33)\n\n"
        "\U0001f464 <b>ФИО получателя:</b>\n"
        "Например: <code>Денис Викторович П.</code> или <code>Григорий Сергеевич Ж.</code>",
        kb([(u"\u2b05 В банк", "bankmenu:rshb")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data.startswith("rshb_bank:"))
async def cb_rshb_bank(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "rshb_bank":
        await cb.answer("Начни заново"); return
    bank = cb.data.split(":", 1)[1]
    sessions[uid]["rshb_bank"] = bank
    sessions[uid]["step"] = "rshb_amount"
    await edit(cb,
        f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n"
        "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data.startswith("rshb_acc:"))
async def cb_rshb_acc(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "rshb_account":
        await cb.answer("Начни заново"); return
    digits = cb.data.split(":", 1)[1]
    sessions[uid]["rshb_acc"] = digits
    sessions[uid]["step"] = "gen_busy"  # анти-двойной тап
    await cb.answer()
    await _rshb_finish(cb, uid)


async def _ozon_finish(msg_or_cb, uid):
    s = sessions.get(uid)
    if not s:
        # Сессия потеряна (рестарт/дворник) — не генерируем чек с пустыми данными.
        await bot.send_message(uid, "\u274c Сессия устарела — начни заново: /start",
                               reply_markup=main_menu(uid))
        return
    ozon = ozongen()
    await _progress_action(uid)
    qmsg = await _queue_notice(uid)
    try:
        rec = bank_name(s.get("ozon_name", ""))
        bank = s.get("ozon_bank", "Сбербанк")
        phone = format_phone(s.get("ozon_phone", "")) or s.get("ozon_phone", "")
        amount = int(s.get("ozon_amount", 0))
        dt = now_msk()
        meta = {}
        pdf = await run_gen(ozon.generate,
                            rec, phone, bank, amount, dt,
                            meta_out=meta)
    except Exception as e:
        await _clear_note(qmsg)
        await bot.send_message(uid, f"\u274c Ошибка генерации Озон: <code>{html.escape(str(e))[:300]}</code>",
                               reply_markup=main_menu(uid))
        if sessions.get(uid) is s:
            sessions.pop(uid, None)
        return
    await _clear_note(qmsg)
    if sessions.get(uid) is s:  # не сносим новую сессию, начатую за время генерации
        sessions.pop(uid, None)
    fn = meta.get("dt", now_msk()).strftime("%d-%m-%y_%H-%M.pdf")
    await bot.send_document(
        uid,
        BufferedInputFile(pdf, filename=fn),
        caption=(f"\U0001f9fe Озон \u2022 {meta.get('amount', f'{amount} ₽')} \u2022 {bank}\n"
                 f"\U0001f464 Получатель: {rec}\n"
                 f"\U0001f4f1 {phone}\n"
                 f"\U0001f194 <code>{html.escape(meta.get('op_id', ''))}</code>\n"
                 f"⚠️ Фабричный чек: этого ID нет в БД Озон Банка, внешняя "
                 f"проверка будет красной. Подтверждается только правка "
                 f"настоящего чека («✏️ Изменить сумму»)."),
        reply_markup=kb([(u"\U0001f504 Ещё чек Озон", "new_ozon")], [(u"\U0001f3e0 Меню", "home")]))
    if gfeat("log_all_banks"):
        data["logs"].append({"uid": uid, "type": "ozon", "name": rec,
                             "phone": phone, "bank": bank, "amount": amount,
                             "time": now_msk().strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        save(data)


# ── Озон v2 (за флагом ozon_v2): отдельный отправитель, preview,
# edit-single-field, сохранение полей при ошибке, безопасный ozon_edit.
# Ядро — ozon_ux (ozx); старые new_ozon/ozon_bank:*/ozon_edit ниже НЕ
# удалены и остаются флоу юзеров без флага и rollback'ом.
_OZON_SBP_ID_RE = re.compile(r"[AB]\d{14}[\dA-Z]0[GB]\d{14}")


def _ozon_v2_enabled(uid) -> bool:
    return ozx is not None and feat(uid, "ozon_v2")


def _extract_pdf_text(pdf_bytes: bytes):
    """Текст PDF для проверки SBP ID (PyMuPDF, фолбэк — pypdf)."""
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    except Exception:
        pass
    try:
        import io as _io
        from pypdf import PdfReader
        reader = PdfReader(_io.BytesIO(pdf_bytes))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        return None


def _ozon_known_generated(pdf_bytes: bytes, extracted_text) -> bool:
    """Слой 2 genuine-проверки: SBP ID из PDF есть в журнале собственных
    генераций бота (op_id пишется в лог v2-чеков). По байтам реплику
    этого проекта от genuine не отличить — см. ozon_ux."""
    if not extracted_text:
        return False
    m = _OZON_SBP_ID_RE.search(extracted_text)
    if not m:
        return False
    op_id = m.group(0)
    return any(e.get("op_id") == op_id for e in data.get("logs", []))


def _ozon_v2_prompt(draft) -> str:
    code = draft.editing or draft.step
    fdef = ozx.FIELDS[code]
    head = (f"\u270f\ufe0f <b>Исправь поле \u00ab{fdef.label}\u00bb</b>"
            if draft.editing else f"\U0001f9fe <b>Чек Озон Банк (СБП)</b>")
    return (f"{head}\n\n<b>{fdef.prompt}:</b>\n"
            f"Например: <code>{html.escape(fdef.example)}</code>")


def _ozon_v2_kb_for(draft):
    code = draft.editing or draft.step
    if code == "bank":
        return _ozon_bank_kb()
    return kb([(u"\u274c Отмена", "cancel")])


async def _ozon_v2_show(cb_or_msg, uid, draft):
    if draft.step == ozx.STEP_PREVIEW:
        await edit(cb_or_msg, ozx.preview_text(draft),
                   kb(*ozx.preview_kb_spec(draft)), uid)
    else:
        await edit(cb_or_msg, _ozon_v2_prompt(draft),
                   _ozon_v2_kb_for(draft), uid)


async def _ozon_v2_finish(cb: CallbackQuery, uid):
    s = sessions.get(uid)
    if not s or s.get("step") != "ozon_flow":
        await cb.answer("Сессия устарела — начни заново", show_alert=True)
        return
    draft = ozx.Draft.from_session(s.get("oz"))
    if not ozx.is_complete(draft):
        await cb.answer("Заполни все поля", show_alert=True)
        return
    s["step"] = "gen_busy"  # анти-двойной тап
    await _progress_action(uid)
    qmsg = await _queue_notice(uid)
    try:
        args, kw = ozx.build_generator_call(draft)  # kw == {"sender": ...} всегда
        meta = {}
        pdf = await run_gen(ozongen().generate, *args, now_msk(),
                            meta_out=meta, **kw)
    except Exception as e:
        logging.exception("ozon v2 generation failed uid=%s", uid)
        await _clear_note(qmsg)
        ozx.apply_generation_error(draft, e)  # поля НЕ теряются
        if sessions.get(uid) is s:
            s["oz"] = draft.to_session()
            s["step"] = "ozon_flow"
            await bot.send_message(uid, ozx.preview_text(draft),
                                   reply_markup=kb(*ozx.preview_kb_spec(draft)))
        return
    await _clear_note(qmsg)
    if sessions.get(uid) is s:
        sessions.pop(uid, None)
    amount = draft.fields.get("amount", 0)
    fn = meta.get("dt", now_msk()).strftime("%d-%m-%y_%H-%M.pdf")
    await bot.send_document(
        uid,
        BufferedInputFile(pdf, filename=fn),
        caption=(f"\U0001f9fe Озон \u2022 {meta.get('amount', f'{amount} ₽')} \u2022 "
                 f"{html.escape(str(draft.fields.get('bank', '')))}\n"
                 f"\U0001f464 Получатель: {html.escape(str(draft.fields.get('name', '')))}\n"
                 f"\U0001f4e4 Отправитель: {html.escape(str(draft.fields.get('sender', '')))}\n"
                 f"\U0001f4f1 {html.escape(str(draft.fields.get('phone', '')))}\n"
                 f"\U0001f194 <code>{html.escape(str(meta.get('op_id', '')))}</code>\n"
                 f"⚠️ Фабричный чек: этого ID нет в БД Озон Банка, внешняя "
                 f"проверка будет красной. Подтверждается только правка "
                 f"настоящего чека («✏️ Изменить сумму»)."),
        reply_markup=kb([(u"\U0001f504 Ещё чек Озон", "oz:new")],
                        [(u"\U0001f3e0 Меню", "home")]))
    if gfeat("log_all_banks"):
        data["logs"].append(ozx.history_entry(
            draft, meta, now_msk().strftime("%d.%m.%Y %H:%M:%S"), uid))
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        save(data)


async def _ozon_v2_edit_start(cb: CallbackQuery, uid):
    sessions[uid] = {"step": "ozon_edit_flow", "mid": cb.message.message_id,
                     "oz_edit": ozx.EditDraft(pdf_present=False).to_session()}
    await edit(cb,
        "\u270f\ufe0f <b>Изменение суммы в чеке Озон</b>\n\n"
        "Пришли <b>настоящий PDF чека Озон Банк (СБП)</b>.\n"
        "Проверю, что это чек Озон Банка поддерживаемого формата, "
        "и перепишу только сумму — ID операции не трону.",
        kb([(u"\u2b05 В банк", "bankmenu:ozon")],
           [(u"\u274c Отмена", "cancel")]), uid)


@router.callback_query(F.data.startswith("oz:"))
async def cb_ozon_v2(cb: CallbackQuery):
    """Диспетчер Ozon v2 (namespace oz: ядра ozon_ux)."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if not _ozon_v2_enabled(uid):
        await cb.answer("Фича не выдана", show_alert=True); return
    action, arg = ozx.parse_callback(cb.data)
    if action == "new":
        draft = ozx.new_draft()
        sessions[uid] = {"step": "ozon_flow", "mid": cb.message.message_id,
                         "oz": draft.to_session()}
        await _ozon_v2_show(cb, uid, draft)
        await cb.answer(); return
    if action == "edit_start":
        await _ozon_v2_edit_start(cb, uid)
        await cb.answer(); return
    if action == "history":
        await edit(cb, ozx.history_text(data.get("logs", []), uid),
                   kb([(u"\u2b05 В банк", "bankmenu:ozon")],
                      [(u"\U0001f3e0 Меню", "home")]), uid)
        await cb.answer(); return
    if action == "blocked":
        await cb.answer("Заполни все поля", show_alert=True); return
    # Дальше нужна живая сессия флоу нового чека.
    s = sessions.get(uid)
    if not s or s.get("step") != "ozon_flow":
        await cb.answer("Сессия устарела — начни заново", show_alert=True); return
    draft = ozx.Draft.from_session(s.get("oz"))
    if action == "edit":
        try:
            ozx.start_edit(draft, arg)
        except ValueError:
            await cb.answer("Неизвестное поле", show_alert=True); return
        s["oz"] = draft.to_session()
        await _ozon_v2_show(cb, uid, draft)
        await cb.answer(); return
    if action == "generate":
        await cb.answer()
        await _ozon_v2_finish(cb, uid); return
    if action == "preview":
        await _ozon_v2_show(cb, uid, draft)
        await cb.answer(); return
    await cb.answer("Неизвестное действие", show_alert=True)


async def _handle_ozon_edit_pdf_v2(msg: Message, uid, s):
    """Приём genuine-чека Озон для v2-правки суммы: полный gate ozon_ux
    (маркеры, раскладка, SBP ID, журнал собственных генераций)."""
    doc = msg.document
    fname = (doc.file_name or "").lower()
    if doc.mime_type != "application/pdf" and not fname.endswith(".pdf"):
        await msg.answer("\u274c Нужен именно PDF-файл чека Озон.")
        return
    try:
        f = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(f.file_path, destination=buf)
        pdf = buf.getvalue()
    except Exception as e:
        await msg.answer(f"\u274c Не скачал файл: <code>{html.escape(str(e))}</code>")
        return
    if not pdf or not pdf.startswith(b"%PDF"):
        await msg.answer("\u274c Это не PDF (нет заголовка <code>%PDF</code>).")
        return
    if len(pdf) > _PDF_CHECK_MAX:
        await msg.answer(f"\u274c Файл больше {_PDF_CHECK_MAX // (1024*1024)} МБ — пришли PDF чека Озон как есть из приложения банка.")
        return
    try:
        ozon_editor_mod()._ensure_supported_layout(pdf)
        layout_ok = True
    except Exception:
        layout_ok = False
    extracted = _extract_pdf_text(pdf)
    chk = ozx.check_edit_donor(
        pdf, layout_ok=layout_ok, extracted_text=extracted,
        known_generated=_ozon_known_generated(pdf, extracted))
    if not chk.ok:
        # Сессию правки не начинаем, но и не сносим: можно прислать другой PDF.
        await msg.answer(f"\u274c {html.escape(chk.user_error or 'Чек не подходит.')}")
        return
    ed = ozx.EditDraft.from_session(s.get("oz_edit"))
    ed.pdf_present = True
    ed.step = "amount"
    ed.last_error = None
    s["oz_edit"] = ed.to_session()
    s["ozon_edit_pdf"] = pdf
    s["step"] = "ozon_edit_v2_amount"
    s["ts"] = datetime.now().timestamp()
    await edit(msg,
        "\u2705 PDF принят — похож на чек Озон Банка поддерживаемого формата\n\n"
        "\U0001f4b0 <b>Новая сумма (рубли):</b>\nНапример: <code>14500</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)


@router.callback_query(F.data == "ozon_edit")
async def cb_ozon_edit(cb: CallbackQuery):
    """✏️ Изменить сумму: юзер кидает настоящий чек Озона, бот переписывает
    только сумму (MCID 3 + MCID 9), оставляя SBP ID (MCID 21) и весь остальной
    PDF байт-в-байт нетронутым. См. ozon_editor.edit_amount. Только так ID
    операции остаётся настоящим и проходит сверку с БД Озон Банка."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if _ozon_v2_enabled(uid):
        await _ozon_v2_edit_start(cb, uid)
        await cb.answer(); return
    sessions[uid] = {"step": "ozon_edit_pdf", "mid": cb.message.message_id}
    await edit(cb,
        "\u270f\ufe0f <b>Изменение суммы в чеке Озон</b>\n\n"
        "Пришли <b>настоящий PDF чека Озон Банк (СБП)</b>.\n"
        "Я перепишу только сумму — ID операции и всё остальное останутся "
        "нетронутыми, поэтому чек пройдёт сверку ID с банком.",
        kb([(u"\u2b05 В банк", "bankmenu:ozon")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "new_ozon")
async def cb_new_ozon(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions[uid] = {"step": "ozon_name", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f9fe <b>Чек Озон Банк (СБП)</b>\n\n"
        "⚠️ <b>Это фабричный чек:</b> ID операции будет выдуман, в БД "
        "Озон Банка его нет — внешняя проверка такой чек не подтвердит "
        "(будет красным). Для подтверждённого чека вернись и выбери "
        "«✏️ Изменить сумму настоящего чека».\n\n"
        "\U0001f464 <b>ФИО получателя:</b>\n"
        "Например: <code>Иван Иванович И.</code> или <code>Денис Викторович П.</code>",
        kb([(u"\u2b05 В банк", "bankmenu:ozon")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data.startswith("ozon_bank:"))
async def cb_ozon_bank(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    # Ozon v2: тот же список банков, но банк пишется в Draft (ozon_ux),
    # а не в s["ozon_bank"], и следующий шаг берётся из ядра.
    _s = sessions.get(uid) or {}
    if _s.get("step") == "ozon_flow" and ozx is not None:
        bank = cb.data.split(":", 1)[1]
        draft = ozx.Draft.from_session(_s.get("oz"))
        if (draft.editing or draft.step) != "bank":
            await cb.answer("Сейчас не шаг банка", show_alert=True); return
        draft, err = ozx.apply_text_input(
            draft, bank, check_text=ozongen().check_text)
        _s["oz"] = draft.to_session()
        if err:
            await edit(cb, f"\u274c {html.escape(err)}",
                       _ozon_bank_kb(), uid)
            await cb.answer(); return
        await _ozon_v2_show(cb, uid, draft)
        await cb.answer(); return
    if uid not in sessions or sessions[uid].get("step") != "ozon_bank":
        await cb.answer("Начни заново"); return
    bank = cb.data.split(":", 1)[1]
    sessions[uid]["ozon_bank"] = bank
    sessions[uid]["step"] = "ozon_amount"
    await edit(cb,
        f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n"
        "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "ozon_bank_custom")
async def cb_ozon_bank_custom(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    # Ozon v2: шаг сессии остаётся ozon_flow, текст заберёт Draft.
    _s = sessions.get(uid) or {}
    if _s.get("step") == "ozon_flow" and ozx is not None:
        draft = ozx.Draft.from_session(_s.get("oz"))
        if (draft.editing or draft.step) != "bank":
            await cb.answer("Сейчас не шаг банка", show_alert=True); return
        await edit(cb,
            "\U0001f3e6 <b>Введи название банка получателя:</b>\n"
            "Например: <code>Т-Банк</code>, <code>Альфа-Банк</code>, <code>Озон Банк (Ozon)</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid)
        await cb.answer(); return
    if uid not in sessions or sessions[uid].get("step") != "ozon_bank":
        await cb.answer("Начни заново"); return
    sessions[uid]["step"] = "ozon_bank_input"
    await edit(cb,
        "\U0001f3e6 <b>Введи название банка получателя:</b>\n"
        "Например: <code>Тинькофф</code>, <code>Альфа-Банк</code>, <code>Озон Банк (Ozon)</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


# ── Яндекс Банк (выписка по договору «Счёт ЭДС») ──
async def _yandex_finish(msg_or_cb, uid):
    s = sessions.get(uid)
    if not s:
        await bot.send_message(uid, "\u274c Сессия устарела — начни заново: /start",
                               reply_markup=main_menu(uid))
        return
    yg = yandexgen()
    await _progress_action(uid)
    qmsg = await _queue_notice(uid)
    try:
        rec = bank_name(s.get("yandex_name", ""))
        bank = s.get("yandex_bank", "Т-Банк")
        phone = format_phone(s.get("yandex_phone", "")) or s.get("yandex_phone", "")
        amount = int(s.get("yandex_amount", 0))
        dt = now_msk()
        meta = {}
        pdf = await run_gen(yg.generate,
                            rec, phone, bank, amount, dt,
                            meta_out=meta)
    except Exception as e:
        await _clear_note(qmsg)
        await bot.send_message(uid, f"\u274c Ошибка генерации Яндекс: <code>{html.escape(str(e))[:300]}</code>",
                               reply_markup=main_menu(uid))
        if sessions.get(uid) is s:
            sessions.pop(uid, None)
        return
    await _clear_note(qmsg)
    if sessions.get(uid) is s:
        sessions.pop(uid, None)
    fn = meta.get("op_dt", now_msk()).strftime("vypiska-yandex_%d-%m-%y_%H-%M.pdf")
    await bot.send_document(
        uid,
        BufferedInputFile(pdf, filename=fn),
        caption=(u"\U0001f4c4 <b>Выписка Яндекс Банк</b> \u2022 договор «Счёт ЭДС»\n"
                 f"\U0001f4b0 Входящий перевод СБП: {meta.get('amount_fmt', f'{amount} ₽')}\n"
                 f"\U0001f464 Отправитель: {rec}\n"
                 f"\U0001f4f1 {phone} \u2022 {bank}\n"
                 f"\U0001f194 Исх. № <code>{html.escape(meta.get('op_id', ''))}</code>"),
        reply_markup=kb([(u"\U0001f504 Ещё выписка Яндекс", "new_yandex")], [(u"\U0001f3e0 Меню", "home")]))
    if gfeat("log_all_banks"):
        data["logs"].append({"uid": uid, "type": "yandex", "name": rec,
                             "phone": phone, "bank": bank, "amount": amount,
                             "time": now_msk().strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        save(data)


# ── Яндекс Банк: исходящая СБП-квитанция (yandex_receipt_generator) ──
# Отдельный flow «Квитанция СБП» рядом с выпиской (new_yandex выше):
# отправитель/телефон отправителя/получатель/телефон получателя/банк/
# сумма/сообщение -> preview -> generate. Генератор, check_text и
# meta_out — yandex_receipt_generator; с выпиской не смешивается.
def _yrcpt_bank_kb():
    rows = []
    for r in _YANDEX_BANKS:
        row = [(b, f"yrcpt_bank:{b}") for b in r if b]
        rows.append(row)
    rows.append([(u"\u270f Ввести свой", "yrcpt_bank_custom")])
    rows.append([(u"\u274c Отмена", "cancel")])
    return kb(*rows)


def _yrcpt_parse_amount(raw):
    """Сумма квитанции: рубли с необязательными копейками (как умеет
    yandex_receipt_generator.format_amount) или None."""
    text = str(raw or "").strip().replace("\u00a0", "").replace(" ", "")
    text = text.replace("\u20bd", "")
    if not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", text):
        return None
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None
    if value <= 0 or value > 999999.99:
        return None
    return text


def _yrcpt_preview_text(s) -> str:
    yrg = yrcptgen()
    msg = s.get("yrcpt_message") or ""
    try:
        amount_fmt = yrg.format_amount(s.get("yrcpt_amount", "0"))
    except Exception:
        amount_fmt = str(s.get("yrcpt_amount", ""))
    return (
        u"\U0001f7e1 <b>Квитанция СБП Яндекс Банка — проверь данные</b>\n\n"
        f"\U0001f4e4 <b>Отправитель:</b> {html.escape(str(s.get('yrcpt_sender', '')))}\n"
        f"\U0001f4f1 <b>Телефон отправителя:</b> {html.escape(str(s.get('yrcpt_sender_phone', '')))}\n"
        f"\U0001f464 <b>Получатель:</b> {html.escape(str(s.get('yrcpt_name', '')))}\n"
        f"\U0001f4f1 <b>Телефон получателя:</b> {html.escape(str(s.get('yrcpt_phone', '')))}\n"
        f"\U0001f3e6 <b>Банк получателя:</b> {html.escape(str(s.get('yrcpt_bank', '')))}\n"
        f"\U0001f4b0 <b>Сумма:</b> {html.escape(amount_fmt)}\n"
        f"\U0001f4ac <b>Сообщение:</b> {html.escape(msg) if msg else '—'}\n\n"
        u"Нажми \u2705 — сгенерировать, или заполни заново."
    )


def _yrcpt_preview_kb():
    return kb([(u"\u2705 Сгенерировать", "yrcpt_go")],
              [(u"\U0001f501 Заполнить заново", "new_yrcpt")],
              [(u"\u274c Отмена", "cancel")])


async def _yrcpt_finish(cb: CallbackQuery, uid):
    s = sessions.get(uid)
    if not s or s.get("step") != "yrcpt_preview":
        await cb.answer("Сессия устарела — начни заново", show_alert=True)
        return
    s["step"] = "gen_busy"  # анти-двойной тап
    await _progress_action(uid)
    qmsg = await _queue_notice(uid)
    yrg = yrcptgen()
    meta = {}
    try:
        pdf = await run_gen(
            yrg.generate,
            s.get("yrcpt_sender", ""), s.get("yrcpt_sender_phone", ""),
            s.get("yrcpt_name", ""), s.get("yrcpt_phone", ""),
            s.get("yrcpt_bank", ""), s.get("yrcpt_amount", ""),
            now_msk(),
            message=s.get("yrcpt_message") or None,
            meta_out=meta)
    except Exception as e:
        logging.exception("yandex receipt generation failed uid=%s", uid)
        await _clear_note(qmsg)
        if sessions.get(uid) is s:
            # Поля сохранены — вернуться в preview и исправить/повторить.
            s["step"] = "yrcpt_preview"
            await bot.send_message(
                uid,
                f"\u274c Ошибка генерации квитанции: <code>{html.escape(str(e))[:300]}</code>\n\n"
                + _yrcpt_preview_text(s),
                reply_markup=_yrcpt_preview_kb())
        return
    await _clear_note(qmsg)
    if sessions.get(uid) is s:
        sessions.pop(uid, None)
    fn = meta.get("op_dt", now_msk()).strftime("%d-%m-%y_%H-%M.pdf")
    await bot.send_document(
        uid,
        BufferedInputFile(pdf, filename=fn),
        caption=(f"\U0001f7e1 Квитанция СБП \u2022 {meta.get('amount_fmt', '')} \u2022 "
                 f"{html.escape(str(meta.get('recipient_bank', s.get('yrcpt_bank', ''))))}\n"
                 f"\U0001f4e4 Отправитель: {html.escape(str(meta.get('sender_name', '')))}\n"
                 f"\U0001f464 Получатель: {html.escape(str(meta.get('recipient_name', '')))}\n"
                 f"\U0001f194 <code>{html.escape(str(meta.get('sbp_id', '')))}</code>"),
        reply_markup=kb([(u"\U0001f504 Ещё квитанция", "new_yrcpt")],
                        [(u"\U0001f3e0 Меню", "home")]))
    if gfeat("log_all_banks"):
        data["logs"].append({"uid": uid, "type": "yandex_receipt",
                             "name": meta.get("recipient_name", s.get("yrcpt_name", "")),
                             "sender": meta.get("sender_name", s.get("yrcpt_sender", "")),
                             "phone": meta.get("recipient_phone", s.get("yrcpt_phone", "")),
                             "bank": meta.get("recipient_bank", s.get("yrcpt_bank", "")),
                             "amount": str(meta.get("amount", s.get("yrcpt_amount", ""))),
                             "op_id": meta.get("sbp_id", ""),
                             "time": now_msk().strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        save(data)


@router.callback_query(F.data == "new_yrcpt")
async def cb_new_yrcpt(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions[uid] = {"step": "yrcpt_sender", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f7e1 <b>Квитанция СБП Яндекс Банка</b>\n\n"
        "\U0001f4e4 <b>ФИО отправителя:</b>\n"
        "Например: <code>Демид Андреевич В.</code>",
        kb([(u"\u2b05 В банк", "bankmenu:yandex")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data.startswith("yrcpt_bank:"))
async def cb_yrcpt_bank(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "yrcpt_bank":
        await cb.answer("Начни заново"); return
    bank = cb.data.split(":", 1)[1]
    bad = yrcptgen().check_text(bank)
    if bad:
        await edit(cb, f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Яндекса.",
                   _yrcpt_bank_kb(), uid)
        await cb.answer(); return
    sessions[uid]["yrcpt_bank"] = bank
    sessions[uid]["step"] = "yrcpt_amount"
    await edit(cb,
        f"\U0001f3e6 Банк: <b>{html.escape(bank)}</b> \u2705\n\n"
        "\U0001f4b0 <b>Сумма перевода:</b>\nНапример: <code>14500</code> или <code>14500,50</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "yrcpt_bank_custom")
async def cb_yrcpt_bank_custom(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "yrcpt_bank":
        await cb.answer("Начни заново"); return
    sessions[uid]["step"] = "yrcpt_bank_input"
    await edit(cb,
        "\U0001f3e6 <b>Введи название банка получателя:</b>\n"
        "Например: <code>Т-Банк</code>, <code>Сбербанк</code>, <code>Озон Банк (Ozon)</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "yrcpt_msg_skip")
async def cb_yrcpt_msg_skip(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "yrcpt_message":
        await cb.answer("Начни заново"); return
    sessions[uid]["yrcpt_message"] = ""
    sessions[uid]["step"] = "yrcpt_preview"
    await edit(cb, _yrcpt_preview_text(sessions[uid]), _yrcpt_preview_kb(), uid)
    await cb.answer()


@router.callback_query(F.data == "yrcpt_go")
async def cb_yrcpt_go(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "yrcpt_preview":
        await cb.answer("Начни заново", show_alert=True); return
    await cb.answer()
    await _yrcpt_finish(cb, uid)


@router.callback_query(F.data == "new_yandex")
async def cb_new_yandex(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions[uid] = {"step": "yandex_name", "mid": cb.message.message_id}
    await edit(cb,
        u"\U0001f4c4 <b>Выписка Яндекс Банк</b> (договор «Счёт ЭДС»)\n"
        u"В выписке одна операция — входящий перевод СБП.\n\n"
        "\U0001f464 <b>ФИО отправителя перевода:</b>\n"
        "Например: <code>Иван Иванович И.</code> или <code>Денис Викторович П.</code>",
        kb([(u"\u2b05 В банк", "bankmenu:yandex")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data.startswith("yandex_bank:"))
async def cb_yandex_bank(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "yandex_bank":
        await cb.answer("Начни заново"); return
    bank = cb.data.split(":", 1)[1]
    sessions[uid]["yandex_bank"] = bank
    sessions[uid]["step"] = "yandex_amount"
    await edit(cb,
        f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n"
        "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "yandex_bank_custom")
async def cb_yandex_bank_custom(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "yandex_bank":
        await cb.answer("Начни заново"); return
    sessions[uid]["step"] = "yandex_bank_input"
    await edit(cb,
        "\U0001f3e6 <b>Введи название банка отправителя:</b>\n"
        "Например: <code>Тинькофф</code>, <code>Альфа-Банк</code>, <code>Озон Банк (Ozon)</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()


@router.callback_query(F.data == "new_stmt")
async def cb_new_stmt(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions[uid] = {"step": "st_name", "mode": "stmt", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f4c4 <b>Выписка \"ЭКСИ-БАНК\" (АО)</b>\n\n"
        "\U0001f464 <b>ФИО владельца счёта:</b>\n\n"
        "Например: <code>Васильев Роман Андреевич</code>",
        kb([(u"\u2b05 В банк", "bankmenu:mts")],
           [(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.callback_query(F.data == "stmt_noholds")
async def cb_stmt_noholds(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "st_holds":
        await cb.answer("Начни заново"); return
    sessions[uid]["holds"] = []
    await _stmt_summary(cb, uid)
    await cb.answer()

@router.callback_query(F.data == "stmt_confirm")
async def cb_stmt_confirm(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    if uid not in sessions or sessions[uid].get("step") != "st_confirm":
        await cb.answer("Начни заново"); return
    sessions[uid]["step"] = "gen_busy"  # анти-двойной тап
    s = sessions[uid]; sg = sgen()
    ahead = gen_queue_ahead()
    await edit(cb, _queue_text(ahead) if ahead else u"\u23f3 <b>Генерация выписки...</b> 3-6 сек", uid=uid)
    await cb.answer()
    try:
        # Statement timestamps are printed as (UTC) and the PDF CreationDate is
        # UTC too - build them from real UTC, not MSK (was +3h in the future).
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ops = s["ops"]; holds = s.get("holds") or []
        ts = stmt_dates(len(ops), now - timedelta(minutes=random.randint(12, 90)))
        rows = [(t.strftime("%d.%m.%Y %H:%M:%S (UTC)"), a, d)
                for t, (a, d, _v, _p) in zip(ts, ops)]
        hts = stmt_dates(len(holds), now - timedelta(minutes=random.randint(3, 30)))
        hrows = [(t.strftime("%d.%m.%Y %H:%M:%S (UTC)"), a, d)
                 for t, (a, d, _v, _p) in zip(hts, holds)]
        inc = sum(v for _a, _d, v, pos in ops if pos)
        exp = sum(v for _a, _d, v, pos in ops if not pos)
        stmt_date = now.strftime("%d.%m.%Y")
        per_from = min(ts).strftime("%d.%m.%Y") if ts else stmt_date
        pdf = await run_gen(sg["generate"],
                name=s["name"], card=f"220383*{s['card4']}",
                account=f"4091481050000{s['acc7']}",
                fix_account_key=False,
                balance=f"{s['balance']:.2f} RUR",
                operations=rows, holds=hrows,
                stmt_date=stmt_date, period_from=per_from, period_to=stmt_date,
                total_in=f"+ {jdouble(inc)} RUR", total_out=f"- {jdouble(exp)} RUR")
        try: await cb.message.delete()
        except: pass
        await bot.send_document(cb.message.chat.id,
            BufferedInputFile(pdf, filename=f"statement_{now.strftime('%d%m%Y')}.pdf"),
            caption=(
                f"\u2705 <b>Выписка готова</b>\n\n"
                f"\U0001f464 {s['name']}\n"
                f"\U0001f4b3 (MIR) 220383*{s['card4']}\n"
                f"\U0001f4b0 Остаток: {s['balance']:.2f} RUR\n"
                f"\U0001f4c8 Поступлений: + {jdouble(inc)} RUR\n"
                f"\U0001f4c9 Списаний: - {jdouble(exp)} RUR\n"
                f"\U0001f9fe Операций: {len(rows)}   \U0001f512 Холдов: {len(hrows)}\n"
                f"\U0001f552 {stmt_date}"),
            reply_markup=kb([(u"\U0001f504 Ещё выписка", "new_stmt"),
                             (u"\U0001f3e0 Меню", "home")]))
        data["logs"].append({"uid": uid, "type": "stmt", "name": s["name"],
                             "ops": len(rows), "holds": len(hrows),
                             "time": now.strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        save(data)
    except Exception as e:
        logging.exception("stmt")
        await bot.send_message(cb.message.chat.id,
            f"\u274c <b>Ошибка:</b> <code>{html.escape(str(e))[:300]}</code>",
            reply_markup=main_menu(uid))
    finally:
        # Не сносим новую сессию, если юзер за время генерации начал другой флоу.
        if sessions.get(uid) is s:
            sessions.pop(uid, None)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    uid = msg.from_user.id
    if not is_ok(uid): return
    # Команда «обновить якорь» (админ): вход во флоу переякорения даже без сессии
    if is_admin(uid) and re.sub(r"[\u2693\ufe0f\s]+", " ", msg.text.strip().lower()).strip() \
            in ("обновить якорь", "якоря", "якорь"):
        sessions[uid] = {"step": "anchor_wait"}
        await edit(msg, ANCHOR_PROMPT, kb([(u"\u274c Отмена", "cancel")]), uid)
        return
    if uid not in sessions: return
    s = sessions[uid]
    if not s.get("step"):
        # Мёртвая сессия (шаг снят, напр. после выдачи доступа) — не удаляем сообщение молча.
        sessions.pop(uid, None)
        return
    s["ts"] = datetime.now().timestamp()
    try: await msg.delete()
    except: pass

    if s["step"] == "usr_login":
        login = msg.text.strip()
        if not re.match(r"^[A-Za-z0-9_.\-]{3,32}$", login):
            await edit(msg, "\u274c Логин: 3-32 символа (латиница, цифры, _ . -). Пришли ещё раз:",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["new_login"] = login; s["step"] = "usr_pass"
        await edit(msg,
            f"\U0001f464 Логин: <b>{login}</b> \u2705\n\n\U0001f511 Пришли <b>пароль</b> (4-64 символа):",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "usr_pass":
        pw = msg.text.strip()
        if len(pw) < 4 or len(pw) > 64:
            await edit(msg, "\u274c Пароль: 4-64 символа. Пришли ещё раз:",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["new_pass"] = pw; s["step"] = "usr_dur"
        await edit(msg,
            "\U0001f511 Пароль задан \u2705\n\n\u23f0 <b>Срок доступа:</b>",
            kb([(u"\u23f0 5 часов", "usr_c:5h"), (u"\U0001f4c5 1 день", "usr_c:1d")],
               [(u"\U0001f4c5 3 дня", "usr_c:3d"), (u"\U0001f4c5 7 дней", "usr_c:7d")],
               [(u"\U0001f4c5 30 дней", "usr_c:30d")],
               [(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "lk_bind_login":
        text = msg.text.strip()
        if text.upper().startswith("MTONS-"):
            label = None
            try:
                _st, _resp = await api_call("GET", "/invites/users")
                for inv in (_resp.get("data") or []):
                    if str(inv.get("code", "")).upper() == text.upper() and not inv.get("banned"):
                        label = inv.get("label")
                        break
            except Exception:
                pass
            if not label:
                await edit(msg, "\u274c Код не найден или заблокирован. Проверь и пришли ещё раз.",
                           kb([(u"\u274c Отмена", "cancel")]), uid); return
            await _lk_finish_bind(msg, uid, label)
            return
        s["lk_login"] = text
        s["step"] = "lk_bind_pass"
        await edit(msg, "\U0001f511 Теперь пришли <b>пароль</b> от админки:",
                   kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "lk_bind_pass":
        label = None
        try:
            _st, _resp = await api_call("POST", "/users/verify",
                                         {"login": s.get("lk_login", ""), "password": msg.text.strip()})
            if _st == 200:
                label = _resp.get("label")
        except Exception:
            pass
        if not label:
            await edit(msg, "\u274c Неверный логин или пароль. Пришли пароль ещё раз:",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        await _lk_finish_bind(msg, uid, label)
        return

    if s["step"] == "wb_name":
        wb = wbgen()
        name = bank_name(msg.text.strip())
        if not name:
            await edit(msg, "\u274c Имя не может быть пустым.\nНапример: <code>Демид Андреевич Васильев</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = wb.check_text(name, "Regular")
        if bad:
            await edit(msg, f"\u274c Символы <code>{''.join(sorted(set(bad)))}</code> недоступны в шрифте WB.\nПопробуй другое имя.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["wb_name"] = name; s["step"] = "wb_phone"
        await edit(msg,
            f"\U0001f464 {name} \u2705\n\n\U0001f4f1 <b>Телефон получателя:</b>\nНапример: <code>+79161234567</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "wb_phone":
        phone = format_phone(msg.text.strip())
        if not phone:
            await edit(msg, "\u274c Не могу распознать номер.\nПример: <code>+79161234567</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["wb_phone"] = phone; s["step"] = "wb_bank"  # кладём нормализованный номер, не сырой ввод
        await edit(msg,
            "\U0001f3e6 <b>Банк получателя:</b>\nНапример: <code>Альфа-Банк</code>, <code>Т-Банк</code>, <code>Озон Банк (Ozon)</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "wb_bank":
        wb = wbgen()
        bank = msg.text.strip()
        if len(bank) > 28:
            await edit(msg, "\u274c Слишком длинное название банка (макс. 28 символов).",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = wb.check_text(bank, "Regular")
        if bad:
            await edit(msg, f"\u274c Символы <code>{''.join(sorted(set(bad)))}</code> недоступны в шрифте WB.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["wb_bank"] = bank; s["step"] = "wb_amount"
        await edit(msg,
            "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>5000</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "wb_amount":
        t = re.sub(r"\D", "", msg.text.strip())
        if not t or int(t) <= 0 or int(t) > 999999:
            await edit(msg, "\u274c Введи сумму от 1 до 999 999, например: <code>5000</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        wb = wbgen()
        bad_amt = wb.check_text(wb.format_amount(int(t)), "Medium")
        if bad_amt:
            await edit(msg, "\u26a0\ufe0f Такая сумма пока не делается: в шрифте WB нет нужных цифр для крупной суммы.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["wb_amount"] = int(t); s["step"] = "wb_msg"
        await edit(msg,
            "\U0001f4ac <b>Сообщение получателю</b> (можно пропустить):",
            kb([(u"\u23ed Без сообщения", "wb_skip")], [(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "wb_msg":
        wb = wbgen()
        m = msg.text.strip()
        bad = wb.check_text(m, "Regular")
        if bad:
            await edit(msg, f"\u274c Символы <code>{''.join(sorted(set(bad)))}</code> недоступны в шрифте WB.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["wb_msg"] = m
        s["step"] = "gen_busy"  # параллельное сообщение не запустит вторую генерацию
        await _wb_finish(msg, uid)
        return

    if s["step"] == "rshb_name":
        rshb = rshbgen()
        name = bank_name(msg.text.strip())
        if not name:
            await edit(msg, "\u274c Имя не может быть пустым.\nНапример: <code>Денис Викторович П.</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        # Genuine RSHB receipts print 'Имя Отчество Ф' WITHOUT a trailing dot
        # (verified on 1793982970121.pdf / 1793983027746.pdf). Do NOT append one.
        bad = rshb.check_text(name)
        if bad:
            await edit(msg,
                f"\u274c Символы <code>{''.join(sorted(set(bad)))}</code> недоступны в каталоге РСХБ.\n"
                "Попробуй другое имя:",
                kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["rshb_name"] = name; s["step"] = "rshb_phone"
        await edit(msg,
            f"\U0001f464 {name} \u2705\n\n\U0001f4f1 <b>Телефон получателя (только цифры):</b>\nНапример: <code>79265487319</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "rshb_phone":
        digits = re.sub(r"\D", "", msg.text.strip())
        if len(digits) != 11 or not digits.startswith("7"):
            await edit(msg, "\u274c Номер должен быть 11 цифр, начиная с 7.\nПример: <code>79265487319</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["rshb_phone"] = digits; s["step"] = "rshb_bank"
        await edit(msg,
            f"\U0001f4f1 {digits} \u2705\n\n\U0001f3e6 <b>Банк получателя:</b>\n"
            "Выбери из списка или напиши название:",
            _rshb_bank_kb(), uid); return

    if s["step"] == "rshb_bank":
        bank = msg.text.strip()
        if not bank or len(bank) > 40:
            await edit(msg, "\u274c Введи название банка (до 40 символов). Например: <code>Т-Банк</code>",
                       _rshb_bank_kb(), uid); return
        s["rshb_bank"] = bank; s["step"] = "rshb_amount"
        await edit(msg,
            f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "rshb_amount":
        t = re.sub(r"\D", "", msg.text.strip())
        if not t or int(t) <= 0 or int(t) > 999999:
            await edit(msg, "\u274c Введи сумму от 1 до 999 999, например: <code>14500</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["rshb_amount"] = int(t)
        s["step"] = "rshb_account"
        r1, r2, r3 = rand4(), rand4(), rand4()
        await edit(msg,
            f"\U0001f4b0 {int(t)} ₽ \u2705\n\n"
            "\U0001f4b3 <b>Последние 4 цифры счёта списания (40817...XXXX):</b>\n\n"
            "Выбери на кнопке или введи свои 4 цифры:",
            kb([(r1, f"rshb_acc:{r1}"), (r2, f"rshb_acc:{r2}"), (r3, f"rshb_acc:{r3}")],
               [(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "rshb_account":
        digits = re.sub(r"\D", "", msg.text.strip())
        if len(digits) != 4:
            r1, r2, r3 = rand4(), rand4(), rand4()
            await edit(msg, "\u274c Введи ровно 4 цифры (например <code>4821</code>) или выбери на кнопке:",
                       kb([(r1, f"rshb_acc:{r1}"), (r2, f"rshb_acc:{r2}"), (r3, f"rshb_acc:{r3}")],
                          [(u"\u274c Отмена", "cancel")]), uid); return
        s["rshb_acc"] = digits
        s["step"] = "gen_busy"  # параллельное сообщение не запустит вторую генерацию
        await _rshb_finish(msg, uid)
        return

    if s["step"] == "vtb_name":
        vtb = vtbgen()
        name = bank_name(msg.text.strip())
        if not name:
            await edit(msg, "\u274c Имя не может быть пустым.\nНапример: <code>Григорий Сергеевич Ж.</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        if name and not name.endswith("."):
            name += "."
        bad = vtb.check_text(name)
        if bad:
            await edit(msg,
                f"\u274c Символы <code>{''.join(sorted(set(bad)))}</code> недоступны в каталоге ВТБ.\n"
                "Попробуй другое имя:",
                kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["vtb_name"] = name; s["step"] = "vtb_phone"
        await edit(msg,
            f"\U0001f464 {name} \u2705\n\n\U0001f4f1 <b>Телефон получателя:</b>\nНапример: <code>+79265487319</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "vtb_phone":
        phone = format_phone(msg.text.strip())
        if not phone:
            await edit(msg, "\u274c Не могу распознать номер.\nПример: <code>+79265487319</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["vtb_phone"] = phone; s["step"] = "vtb_bank"
        vtb = vtbgen()
        b_rows = [[(b, f"vtb_bank:{b}")] for b in vtb.VTB_BANKS]
        b_rows.append([(u"\u270f Ввести свой", "vtb_bank_custom")])
        b_rows.append([(u"\u274c Отмена", "cancel")])
        await edit(msg,
            f"\U0001f4f1 {phone} \u2705\n\n\U0001f3e6 <b>Банк получателя:</b>",
            kb(*b_rows), uid); return

    if s["step"] == "vtb_bank_input":
        vtb = vtbgen()
        bank = " ".join(msg.text.strip().split())
        if len(bank) > 28:
            await edit(msg, "\u274c Слишком длинное название банка (макс. 28 символов).",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = vtb.check_text(bank)
        if bad:
            await edit(msg, f"\u274c Символы <code>{''.join(sorted(set(bad)))}</code> недоступны в каталоге ВТБ.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["vtb_bank"] = bank; s["step"] = "vtb_payer"
        await edit(msg,
            f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n"
            "\U0001f464 <b>ФИО плательщика (отправителя):</b>\n"
            "Нажми кнопку случайного плательщика или введи своё ФИО:",
            kb([(u"\U0001f3b2 Случайный плательщик", "vtb_payer_auto")],
               [(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "vtb_payer":
        vtb = vtbgen()
        payer = bank_name(msg.text.strip())
        if payer and not payer.endswith("."):
            payer += "."
        bad = vtb.check_text(payer, s.get("vtb_bank"))
        if bad:
            await edit(msg,
                f"\u274c Символы <code>{''.join(sorted(set(bad)))}</code> недоступны в каталоге ВТБ.\n"
                "Нажми кнопку случайного плательщика или введи другое имя:",
                kb([(u"\U0001f3b2 Случайный плательщик", "vtb_payer_auto")],
                   [(u"\u274c Отмена", "cancel")]), uid); return
        s["vtb_payer"] = payer; s["step"] = "vtb_amount"
        await edit(msg,
            f"\U0001f464 Плательщик: <b>{payer}</b> \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "vtb_amount":
        t = re.sub(r"\D", "", msg.text.strip())
        if not t or int(t) <= 0 or int(t) > 999999:
            await edit(msg, "\u274c Введи сумму от 1 до 999 999, например: <code>14500</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["vtb_amount"] = int(t)
        s["step"] = "vtb_account"
        vtb = vtbgen()
        r1, r2, r3 = rand4(), rand4(), rand4()
        await edit(msg,
            f"\U0001f4b0 {vtb.format_amount(int(t))} \u2705\n\n"
            "\U0001f4b3 <b>Последние 4 цифры счёта списания (*XXXX):</b>\n\n"
            "Выбери на кнопке или введи свои 4 цифры:",
            kb([(f"*{r1}", f"vtb_acc:{r1}"), (f"*{r2}", f"vtb_acc:{r2}"), (f"*{r3}", f"vtb_acc:{r3}")],
               [(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "vtb_account":
        digits = re.sub(r"\D", "", msg.text.strip())
        if len(digits) != 4:
            r1, r2, r3 = rand4(), rand4(), rand4()
            await edit(msg, "\u274c Введи ровно 4 цифры (например <code>4821</code>) или выбери на кнопке:",
                       kb([(f"*{r1}", f"vtb_acc:{r1}"), (f"*{r2}", f"vtb_acc:{r2}"), (f"*{r3}", f"vtb_acc:{r3}")],
                          [(u"\u274c Отмена", "cancel")]), uid); return
        s["vtb_acc"] = f"*{digits}"
        s["step"] = "gen_busy"  # параллельное сообщение не запустит вторую генерацию
        await _vtb_finish(msg, uid)
        return

    if s["step"] == "ozon_flow":
        # Ozon v2: ввод забирает ядро ozon_ux (нормализация имени с точкой,
        # отдельный отправитель, строгие сумма/телефон). При ошибке старое
        # значение поля и режим правки сохраняются внутри Draft.
        if ozx is None:
            await edit(msg, "\u274c Модуль Озон v2 недоступен — начни заново: /start",
                       main_menu(uid), uid); return
        draft = ozx.Draft.from_session(s.get("oz"))
        draft, err = ozx.apply_text_input(
            draft, msg.text.strip(), check_text=ozongen().check_text)
        s["oz"] = draft.to_session()
        if err:
            code = draft.editing or draft.step
            prompt = ozx.FIELDS[code].prompt if code in ozx.FIELDS else ""
            await edit(msg,
                f"\u274c {html.escape(err)}\n\n<b>{html.escape(prompt)}:</b>",
                _ozon_v2_kb_for(draft), uid); return
        await _ozon_v2_show(msg, uid, draft)
        return

    if s["step"] == "ozon_edit_v2_amount":
        # Ozon v2 правка суммы genuine-чека: PDF лежит в s["ozon_edit_pdf"]
        # и НЕ вынимается до успеха — при ошибке редактора можно ввести
        # другую сумму. Правится только сумма (ensure_editable_field).
        if ozx is None:
            await edit(msg, "\u274c Модуль Озон v2 недоступен — начни заново: /start",
                       main_menu(uid), uid); return
        ed = ozx.EditDraft.from_session(s.get("oz_edit"))
        ed, err = ozx.apply_edit_amount(ed, msg.text.strip())
        s["oz_edit"] = ed.to_session()
        if err:
            await edit(msg, f"\u274c {html.escape(err)}\n\n"
                            "\U0001f4b0 <b>Новая сумма (рубли):</b>\nНапример: <code>14500</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        pdf = s.get("ozon_edit_pdf")
        if pdf is None:
            await edit(msg, "\u274c PDF потерян — начни заново кнопкой «\u270f\ufe0f Изменить сумму».",
                       kb([(u"\u2b05 В банк", "bankmenu:ozon")],
                          [(u"\u274c Отмена", "cancel")]), uid); return
        ozx.ensure_editable_field("amount")
        s["step"] = "gen_busy"  # анти-двойной тап
        await _progress_action(uid)
        qmsg = await _queue_notice(uid)
        try:
            new_pdf = await run_gen(ozon_editor_mod().edit_amount, pdf, ed.amount)
        except Exception as e:
            logging.exception("ozon v2 edit failed uid=%s", uid)
            await _clear_note(qmsg)
            ozx.apply_edit_error(ed, e)  # PDF и сумма сохранены
            s["oz_edit"] = ed.to_session()
            if sessions.get(uid) is s:
                s["step"] = "ozon_edit_v2_amount"
            await bot.send_message(
                uid,
                f"\u274c {html.escape(ed.last_error or 'Не получилось изменить сумму.')}\n\n"
                "\U0001f4b0 <b>Введи другую сумму (рубли):</b>",
                reply_markup=kb([(u"\u274c Отмена", "cancel")])); return
        await _clear_note(qmsg)
        amount_done = ed.amount
        ozx.apply_edit_success(ed)
        s["oz_edit"] = ed.to_session()
        s.pop("ozon_edit_pdf", None)
        if sessions.get(uid) is s:
            sessions.pop(uid, None)
        await bot.send_document(uid,
            BufferedInputFile(new_pdf, filename=f"ozon_edited_{amount_done}.pdf"),
            caption=("\u2705 <b>Сумма изменена</b>\n\n"
                     f"\U0001f4b0 Новая сумма: {amount_done} \u20bd\n"
                     f"\U0001f194 ID операции не тронут"),
            reply_markup=kb([(u"\u270f\ufe0f Ещё изменить", "ozon_edit")],
                            [(u"\U0001f3e0 Меню", "home")]))
        if gfeat("log_all_banks"):
            data["logs"].append(ozx.edit_history_entry(
                amount_done, now_msk().strftime("%d.%m.%Y %H:%M:%S"), uid))
            if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
            save(data)
        return

    if s["step"] == "ozon_name":
        ozon = ozongen()
        name = bank_name(msg.text.strip())
        if not name:
            await edit(msg, "\u274c Имя не может быть пустым.\nНапример: <code>Иван Иванович И.</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = ozon.check_text(name)
        if bad:
            await edit(msg,
                f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Озон.\n"
                "Попробуй другое имя:",
                kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["ozon_name"] = name; s["step"] = "ozon_phone"
        await edit(msg,
            f"\U0001f464 {name} \u2705\n\n"
            "\U0001f4f1 <b>Телефон получателя:</b>\nНапример: <code>+79161234567</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "ozon_phone":
        phone = format_phone(msg.text.strip())
        if not phone:
            await edit(msg, "\u274c Не могу распознать номер.\nПример: <code>+79161234567</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["ozon_phone"] = phone; s["step"] = "ozon_bank"
        await edit(msg,
            f"\U0001f4f1 {phone} \u2705\n\n\U0001f3e6 <b>Банк получателя:</b>\nВыбери из списка или напиши название:",
            _ozon_bank_kb(), uid); return

    if s["step"] == "ozon_bank":
        # юзер прислал текст вместо кнопки → трактуем как custom bank (повтор паттерна РСХБ)
        ozon = ozongen()
        bank = " ".join(msg.text.strip().split())
        if not bank or len(bank) > 40:
            await edit(msg, "\u274c Введи название банка (до 40 символов). Например: <code>Т-Банк</code>",
                       _ozon_bank_kb(), uid); return
        bad = ozon.check_text(bank)
        if bad:
            await edit(msg, f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Озон.",
                       _ozon_bank_kb(), uid); return
        s["ozon_bank"] = bank; s["step"] = "ozon_amount"
        await edit(msg,
            f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "ozon_bank_input":
        ozon = ozongen()
        bank = " ".join(msg.text.strip().split())
        if not bank or len(bank) > 40:
            await edit(msg, "\u274c Введи название банка (до 40 символов).",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = ozon.check_text(bank)
        if bad:
            await edit(msg, f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Озон.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["ozon_bank"] = bank; s["step"] = "ozon_amount"
        await edit(msg,
            f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "ozon_amount":
        t = re.sub(r"\D", "", msg.text.strip())
        if not t or int(t) <= 0 or int(t) > 999999:
            await edit(msg, "\u274c Введи сумму от 1 до 999 999, например: <code>14500</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["ozon_amount"] = int(t)
        s["step"] = "gen_busy"  # параллельное сообщение не запустит вторую генерацию
        await _ozon_finish(msg, uid)
        return

    if s["step"] == "ozon_edit_amount":
        # ✏️ Изменить сумму: юзер прислал новую сумму для настоящего чека Озон.
        # ozon_editor.edit_amount переписывает только MCID 3 + MCID 9, SBP ID
        # (MCID 21) и весь остальной PDF остаются байт-в-байт нетронутыми.
        t = re.sub(r"\D", "", msg.text.strip())
        if not t or int(t) <= 0 or int(t) > 999999:
            await edit(msg, "\u274c Введи сумму от 1 до 999 999, например: <code>14500</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        new_amount = int(t)
        pdf = s.pop("ozon_edit_pdf", None)
        if pdf is None:
            await edit(msg, "\u274c PDF потерян — начни заново кнопкой «\u270f\ufe0f Изменить сумму».",
                       main_menu(uid), uid)
            if sessions.get(uid) is s:
                sessions.pop(uid, None)
            return
        s["step"] = "gen_busy"  # анти-двойной тап
        await _progress_action(uid)
        qmsg = await _queue_notice(uid)
        try:
            editor = ozon_editor_mod()
            new_pdf = await run_gen(editor.edit_amount, pdf, new_amount)
        except Exception as e:
            await _clear_note(qmsg)
            await bot.send_message(uid,
                f"\u274c Ошибка редактирования: <code>{html.escape(str(e))[:300]}</code>",
                reply_markup=main_menu(uid))
            if sessions.get(uid) is s:
                sessions.pop(uid, None)
            return
        await _clear_note(qmsg)
        if sessions.get(uid) is s:
            sessions.pop(uid, None)
        await bot.send_document(uid,
            BufferedInputFile(new_pdf, filename=f"ozon_edited_{new_amount}.pdf"),
            caption=(f"\u2705 <b>Сумма изменена</b>\n\n"
                     f"\U0001f4b0 Новая сумма: {new_amount} ₽\n"
                     f"\U0001f194 ID операции не тронут"),
            reply_markup=kb([(u"\u270f\ufe0f Ещё изменить", "ozon_edit")],
                            [(u"\U0001f3e0 Меню", "home")]))
        if gfeat("log_all_banks"):
            data["logs"].append({"uid": uid, "type": "ozon_edit", "amount": new_amount,
                                 "time": now_msk().strftime("%d.%m.%Y %H:%M:%S")})
            if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
            save(data)
        return

    if s["step"] == "yrcpt_sender":
        yrg = yrcptgen()
        name = bank_name(msg.text.strip())
        if not name:
            await edit(msg, "\u274c Имя не может быть пустым.\nНапример: <code>Демид Андреевич В.</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = yrg.check_text(name)
        if bad:
            await edit(msg,
                f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Яндекса.\n"
                "Попробуй другое имя:",
                kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yrcpt_sender"] = name; s["step"] = "yrcpt_sender_phone"
        await edit(msg,
            f"\U0001f4e4 {html.escape(name)} \u2705\n\n"
            "\U0001f4f1 <b>Телефон отправителя:</b>\nНапример: <code>+79211025853</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "yrcpt_sender_phone":
        phone = format_phone(msg.text.strip())
        if not phone:
            await edit(msg, "\u274c Не могу распознать номер.\nПример: <code>+79211025853</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yrcpt_sender_phone"] = phone; s["step"] = "yrcpt_name"
        await edit(msg,
            f"\U0001f4f1 {phone} \u2705\n\n"
            "\U0001f464 <b>ФИО получателя:</b>\nНапример: <code>Иван Иванович И.</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "yrcpt_name":
        yrg = yrcptgen()
        name = bank_name(msg.text.strip())
        if not name:
            await edit(msg, "\u274c Имя не может быть пустым.\nНапример: <code>Иван Иванович И.</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = yrg.check_text(name)
        if bad:
            await edit(msg,
                f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Яндекса.\n"
                "Попробуй другое имя:",
                kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yrcpt_name"] = name; s["step"] = "yrcpt_phone"
        await edit(msg,
            f"\U0001f464 {html.escape(name)} \u2705\n\n"
            "\U0001f4f1 <b>Телефон получателя:</b>\nНапример: <code>+79161234567</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "yrcpt_phone":
        phone = format_phone(msg.text.strip())
        if not phone:
            await edit(msg, "\u274c Не могу распознать номер.\nПример: <code>+79161234567</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yrcpt_phone"] = phone; s["step"] = "yrcpt_bank"
        await edit(msg,
            f"\U0001f4f1 {phone} \u2705\n\n\U0001f3e6 <b>Банк получателя:</b>\nВыбери из списка или напиши название:",
            _yrcpt_bank_kb(), uid); return

    if s["step"] == "yrcpt_bank":
        bank = " ".join(msg.text.strip().split())
        if not bank or len(bank) > 40:
            await edit(msg, "\u274c Введи название банка (до 40 символов). Например: <code>Т-Банк</code>",
                       _yrcpt_bank_kb(), uid); return
        bad = yrcptgen().check_text(bank)
        if bad:
            await edit(msg, f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Яндекса.",
                       _yrcpt_bank_kb(), uid); return
        s["yrcpt_bank"] = bank; s["step"] = "yrcpt_amount"
        await edit(msg,
            f"\U0001f3e6 Банк: <b>{html.escape(bank)}</b> \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода:</b>\nНапример: <code>14500</code> или <code>14500,50</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "yrcpt_bank_input":
        bank = " ".join(msg.text.strip().split())
        if not bank or len(bank) > 40:
            await edit(msg, "\u274c Введи название банка (до 40 символов).",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = yrcptgen().check_text(bank)
        if bad:
            await edit(msg, f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Яндекса.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yrcpt_bank"] = bank; s["step"] = "yrcpt_amount"
        await edit(msg,
            f"\U0001f3e6 Банк: <b>{html.escape(bank)}</b> \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода:</b>\nНапример: <code>14500</code> или <code>14500,50</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "yrcpt_amount":
        amount = _yrcpt_parse_amount(msg.text.strip())
        if amount is None:
            await edit(msg, "\u274c Введи сумму от 0,01 до 999 999,99, например: <code>14500</code> или <code>14500,50</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yrcpt_amount"] = amount; s["step"] = "yrcpt_message"
        await edit(msg,
            "\U0001f4ac <b>Сообщение получателю:</b>\n"
            "Напиши текст или нажми «Без сообщения».",
            kb([(u"\u23ed Без сообщения", "yrcpt_msg_skip")],
               [(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "yrcpt_message":
        message = " ".join(msg.text.strip().split())
        if message in ("-", "–", "—"):
            message = ""
        if len(message) > 140:
            await edit(msg, "\u274c Сообщение слишком длинное (макс. 140 символов). Пришли короче:",
                       kb([(u"\u23ed Без сообщения", "yrcpt_msg_skip")],
                          [(u"\u274c Отмена", "cancel")]), uid); return
        if message:
            bad = yrcptgen().check_text(message)
            if bad:
                await edit(msg,
                    f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Яндекса.\n"
                    "Исправь сообщение или нажми «Без сообщения».",
                    kb([(u"\u23ed Без сообщения", "yrcpt_msg_skip")],
                       [(u"\u274c Отмена", "cancel")]), uid); return
        s["yrcpt_message"] = message; s["step"] = "yrcpt_preview"
        await edit(msg, _yrcpt_preview_text(s), _yrcpt_preview_kb(), uid); return

    if s["step"] == "yrcpt_preview":
        await edit(msg, _yrcpt_preview_text(s), _yrcpt_preview_kb(), uid); return

    if s["step"] == "yandex_name":
        yg = yandexgen()
        name = bank_name(msg.text.strip())
        if not name:
            await edit(msg, "\u274c Имя не может быть пустым.\nНапример: <code>Иван Иванович И.</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = yg.check_text(name)
        if bad:
            await edit(msg,
                f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Яндекса.\n"
                "Попробуй другое имя:",
                kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yandex_name"] = name; s["step"] = "yandex_phone"
        await edit(msg,
            f"\U0001f464 {name} \u2705\n\n"
            "\U0001f4f1 <b>Телефон отправителя:</b>\nНапример: <code>+79161234567</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "yandex_phone":
        phone = format_phone(msg.text.strip())
        if not phone:
            await edit(msg, "\u274c Не могу распознать номер.\nПример: <code>+79161234567</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yandex_phone"] = phone; s["step"] = "yandex_bank"
        await edit(msg,
            f"\U0001f4f1 {phone} \u2705\n\n\U0001f3e6 <b>Банк отправителя:</b>\nВыбери из списка или напиши название:",
            _yandex_bank_kb(), uid); return

    if s["step"] == "yandex_bank":
        yg = yandexgen()
        bank = " ".join(msg.text.strip().split())
        if not bank or len(bank) > 40:
            await edit(msg, "\u274c Введи название банка (до 40 символов). Например: <code>Т-Банк</code>",
                       _yandex_bank_kb(), uid); return
        bad = yg.check_text(bank)
        if bad:
            await edit(msg, f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Яндекса.",
                       _yandex_bank_kb(), uid); return
        s["yandex_bank"] = bank; s["step"] = "yandex_amount"
        await edit(msg,
            f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "yandex_bank_input":
        yg = yandexgen()
        bank = " ".join(msg.text.strip().split())
        if not bank or len(bank) > 40:
            await edit(msg, "\u274c Введи название банка (до 40 символов).",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = yg.check_text(bank)
        if bad:
            await edit(msg, f"\u274c Символы <code>{html.escape(''.join(sorted(set(bad))))}</code> недоступны в каталоге Яндекса.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yandex_bank"] = bank; s["step"] = "yandex_amount"
        await edit(msg,
            f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода (рубли):</b>\nНапример: <code>14500</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid); return

    if s["step"] == "yandex_amount":
        t = re.sub(r"\D", "", msg.text.strip())
        if not t or int(t) <= 0 or int(t) > 999999:
            await edit(msg, "\u274c Введи сумму от 1 до 999 999, например: <code>14500</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["yandex_amount"] = int(t)
        s["step"] = "gen_busy"
        await _yandex_finish(msg, uid)
        return

    if s["step"] == "name":
        g = gen()
        raw_text = msg.text.strip()

        # ── Свободный ввод: «перевод 2500 Пете в тбанк» ──
        # Пробуем автодетекцию банка/суммы/имени. Если confidence >= 0.5 —
        # показываем подтверждение с кнопками ✅/❌. Иначе переспрашиваем имя.
        if gfeat("free_text") and _bd is not None and len(raw_text.split()) > 1:
            try:
                d = _bd.detect(raw_text)
            except Exception:
                d = None
                logging.exception("bank_detector.detect failed")
            # Чистое имя без банка («Сергей Михайлович К», «… К 5000») — штатный
            # ввод этого шага: без банка confidence у detect() по построению
            # < 0.5, поэтому раньше такое имя навсегда уходило в переспрос той
            # же фразой, что бот сам подсказывал. Показываем подтверждение.
            _plain_name = bool(_PLAIN_NAME_RE.fullmatch(raw_text))
            _ft_ok = bool(d and (d.get("confidence", 0.0) >= 0.5
                                 or (d.get("name") and not d.get("bank")
                                     and (d.get("amount") is not None
                                          or d.get("phone") or _plain_name))))
            if _ft_ok:
                # Валидируем имя и банк тем же чекером, что и в обычном флоу —
                # чтобы не подсунуть чекеру символы, которых нет в шрифте.
                det_name = bank_name(d.get("name") or "")
                det_bank = (d.get("bank") or "").strip()
                det_amount = d.get("amount")
                det_phone = d.get("phone")
                bad_name = g["check"](det_name) if det_name else None
                bad_bank = g["check"](det_bank) if det_bank else None
                if bad_name or bad_bank:
                    bad = "".join(sorted(set((bad_name or []) + (bad_bank or []))))
                    await edit(msg,
                        f"\u274c Символы <code>{html.escape(bad)}</code> недоступны.\n"
                        "Попробуй другое имя или банк.",
                        kb([(u"\u274c Отмена", "cancel")]), uid); return
                # Складываем распознанное в сессию — заберём по кнопке ✅.
                s["_ft"] = {
                    "name": det_name,
                    "bank": det_bank or None,
                    "amount": det_amount,
                    "phone": det_phone,
                }
                lines = ["\U0001f50d <b>Распознал из сообщения:</b>\n"]
                if det_name:   lines.append(f"\U0001f464 Имя: <b>{html.escape(det_name)}</b>")
                if det_bank:  lines.append(f"\U0001f3e6 Банк: <b>{html.escape(det_bank)}</b>")
                if det_amount is not None:
                    lines.append(f"\U0001f4b0 Сумма: <b>{g['fmt'](det_amount)}</b>")
                if det_phone: lines.append(f"\U0001f4f1 Телефон: <code>{det_phone}</code>")
                lines.append("\n\nВсё верно?")
                await edit(msg, "\n".join(lines),
                    kb([("\u2705 Да, верно", "ft_yes"),
                        (u"\u274c Нет, введу сам", "ft_no")]), uid)
                return
            # confidence < 0.5 — ввод неоднозначный, переспрашиваем имя по-нормальному.
            await edit(msg,
                "\U0001f914 <b>Не удалось разобрать фразу.</b>\n\n"
                "Введи имя получателя как на чеке:\n<code>Сергей Михайлович К</code>",
                kb([(u"\u274c Отмена", "cancel")]), uid); return

        name = bank_name(raw_text)
        if not name:
            await edit(msg, "\u274c Имя не может быть пустым.\nВведи как на чеке: <code>Сергей Михайлович К</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        if len(name) > 38:
            await edit(msg, "\u274c Слишком длинное имя для чека (макс. 38 символов).\nСократи, например: <code>Имя Отчество Ф</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = g["check"](name)
        if bad:
            await edit(msg, f"\u274c Символы <code>{''.join(bad)}</code> недоступны.\nПопробуй другое имя.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["name"] = name; s["step"] = "phone"
        await edit(msg,
            f"\U0001f464 {name} \u2705\n\n\U0001f4f1 <b>Телефон получателя:</b>\nВведи номер, например: <code>+79161234567</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "phone":
        phone = format_phone(msg.text.strip())
        if not phone:
            await edit(msg, "\u274c Не могу распознать номер.\nПример: <code>+79161234567</code> или <code>89161234567</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["phone"] = phone; s["step"] = "bank"
        bank_rows = [[(b, f"bank:{b}")] for b in BANKS]
        bank_rows.append([(u"\u270f Ввести свой", "bank_custom")])
        bank_rows.append([(u"\u274c Отмена", "cancel")])
        await edit(msg,
            f"\U0001f464 {s['name']}\n\U0001f4f1 {phone} \u2705\n\n\U0001f3e6 <b>Банк получателя:</b>",
            kb(*bank_rows), uid)

    elif s["step"] == "bank_input":
        g = gen()
        bank = " ".join(msg.text.strip().split())
        if len(bank) > 28:
            await edit(msg, "\u274c Слишком длинное название банка (макс. 28 символов).",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = g["check"](bank)
        if bad:
            await edit(msg, f"\u274c Символы <code>{''.join(bad)}</code> недоступны.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["bank"] = bank; s["step"] = "amount"
        await edit(msg,
            f"\U0001f3e6 Банк: <b>{bank}</b> \u2705\n\n\U0001f4b0 <b>Сумма перевода (руб):</b>",
            kb([(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "amount":
        g = gen()
        try:
            raw = msg.text.strip().replace(" ","").replace(u"\u20bd","").replace(",00","").replace(".","")
            amount = int(raw)
            if amount < 1 or amount > 999999: raise ValueError
        except ValueError:
            await edit(msg, "\u274c Сумма от 1 до 999 999", kb([(u"\u274c Отмена","cancel")]), uid); return
        s["amount"] = amount; s["step"] = "account"
        r1, r2, r3 = rand5(), rand5(), rand5()
        await edit(msg,
            f"\U0001f4b0 {g['fmt'](amount)} \u2705\n\n"
            "\U0001f4b3 <b>Последние 5 цифр счёта списания:</b>\n\n"
            "Выбери или введи свои:",
            kb([(r1, f"acc:{r1}"), (r2, f"acc:{r2}"), (r3, f"acc:{r3}")],
               [(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "account":
        g = gen()
        digits = msg.text.strip()
        if not re.match(r"^\d{5}$", digits):
            await edit(msg, "\u274c Введи ровно 5 цифр", kb([(u"\u274c Отмена","cancel")]), uid); return
        s["acc4"] = digits; s["step"] = "confirm"
        tpl_btn = [(u"\U0001f4cc Сохранить шаблон", "tpl_save")] if (gfeat("templates") and not s.get("_from_tpl")) else []
        await edit(msg,
            f"\U0001f4cb <b>Проверь данные:</b>\n\n"
            f"\U0001f464 {s['name']}\n\U0001f4f1 {s['phone']}\n"
            f"\U0001f3e6 {s['bank']}\n\U0001f4b0 {g['fmt'](s['amount'])}\n"
            f"\U0001f4b3 Счёт: ...{digits}\n\U0001f552 Текущее время",
            kb([(u"\u2705 Сгенерировать", "confirm")],
               *([tpl_btn] if tpl_btn else []),
               [(u"\u274c Отмена", "cancel")]), uid)

    # ── Card flow text inputs ──
    elif s["step"] == "st_name":
        sg = sgen()
        name = msg.text.strip().replace("Ё", "Е")
        if not name:
            await edit(msg, "\u274c Имя не может быть пустым.\nНапример: <code>Васильев Роман Андреевич</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = sg["check"](name, "Regular")
        if bad:
            await edit(msg, f"\u274c В шрифте выписки нет символов: <code>{''.join(sorted(set(bad)))}</code>\n"
                            "Попробуй другое написание.",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["name"] = name; s["step"] = "st_card"
        await edit(msg,
            f"\U0001f464 {name} \u2705\n\n"
            "\U0001f4b3 <b>Последние 4 цифры карты:</b>\n\n"
            "Карта (MIR) 220383*____",
            kb([(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "st_card":
        d = msg.text.strip()
        if not re.match(r"^\d{4}$", d):
            await edit(msg, "\u274c Введи ровно 4 цифры",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["card4"] = d; s["step"] = "st_acc"
        await edit(msg,
            f"\U0001f4b3 (MIR) 220383*{d} \u2705\n\n"
            "\U0001f4b3 <b>Последние 7 цифр счёта:</b>\n\n"
            "Счёт 4091481050000_______",
            kb([(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "st_acc":
        d = msg.text.strip()
        if not re.match(r"^\d{7}$", d):
            await edit(msg, "\u274c Введи ровно 7 цифр",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["acc7"] = d; s["step"] = "st_balance"
        await edit(msg,
            f"\U0001f4b3 Счёт: ...{d} \u2705\n\n"
            "\U0001f4b0 <b>Доступный остаток (RUR):</b>\n\n"
            "Например: <code>6451.00</code>",
            kb([(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "st_balance":
        try:
            bal = float(msg.text.strip().replace(" ", "").replace(",", "."))
            if not (0 <= bal <= 99_999_999): raise ValueError  # отсекает и NaN
        except ValueError:
            await edit(msg, "\u274c Остаток от 0 до 99 999 999",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["balance"] = bal; s["step"] = "st_ops"
        await edit(msg, f"\U0001f4b0 Остаток: {bal:.2f} RUR \u2705\n\n" + STMT_HINT,
                   kb([(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "st_ops":
        sg = sgen()
        rows, err = parse_ops(msg.text)
        if err:
            await edit(msg, f"\u274c {err}\n\n" + STMT_HINT,
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        bad = set()
        for a, d, _v, _p in rows:
            bad |= set(sg["check"](a, "Regular")) | set(sg["check"](d, "Regular"))
        if bad:
            await edit(msg, f"\u274c В шрифте нет символов: <code>{''.join(sorted(bad))}</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        generic = {"перевод сбп", "перевод", "перевод на карту", "сбп"}
        if any(d.strip().lower().replace("ё", "е") in generic for _a, d, _v, _p in rows):
            await edit(msg, "\u274c В описании нужен получатель, как в банке:\n"
                            "<code>-2500 Яна Юрьевна З Переводы</code>\n\n" + STMT_HINT,
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["ops"] = rows; s["step"] = "st_holds"
        await edit(msg,
            f"\U0001f9fe Операций: {len(rows)} \u2705\n\n"
            "\U0001f512 <b>Заблокированные средства (холды)</b> — по одной в строке:\n"
            "<code>199 Яндекс Такси Транспорт</code>\n\n"
            "Или нажми «Без холдов». Максимум 3.",
            kb([(u"\u27a1 Без холдов", "stmt_noholds")],
               [(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "st_holds":
        sg = sgen()
        rows, err = parse_ops(msg.text, signed=False)
        if err:
            await edit(msg, f"\u274c {err}",
                       kb([(u"\u27a1 Без холдов", "stmt_noholds")],
                          [(u"\u274c Отмена", "cancel")]), uid); return
        if len(rows) > 3:
            await edit(msg, "\u274c Максимум 3 холда",
                       kb([(u"\u27a1 Без холдов", "stmt_noholds")],
                          [(u"\u274c Отмена", "cancel")]), uid); return
        bad = set()
        for a, d, _v, _p in rows:
            bad |= set(sg["check"](a, "Regular")) | set(sg["check"](d, "Regular"))
        if bad:
            await edit(msg, f"\u274c В шрифте нет символов: <code>{''.join(sorted(bad))}</code>",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["holds"] = rows
        await _stmt_summary(msg, uid)

    elif s["step"] == "card_pan_from":
        pan = _mask_pan(msg.text)
        if not pan:
            await edit(msg, "\u274c Пришли маску <code>220383******7826</code> или номер карты целиком",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["pan_from"] = pan; s["step"] = "card_pan_to"
        r = rand4()
        await edit(msg,
            f"\U0001f4b3 Списание: <b>{pan}</b> \u2705\n\n"
            "\U0001f4b3 <b>Карта зачисления</b> — пришли маску или номер целиком:\n"
            "<code>220070******8429</code>\n\n"
            "Выбери или введи свою:",
            kb([(f"220070******{r}", f"cpant:220070******{r}")],
               [(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "card_pan_to":
        pan = _mask_pan(msg.text)
        if not pan:
            await edit(msg, "\u274c Пришли маску <code>220070******8429</code> или номер карты целиком",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["pan_to"] = pan; s["step"] = "card_amount"
        await edit(msg,
            f"\U0001f4b3 Списание: <b>{s['pan_from']}</b>\n"
            f"\U0001f4b3 Зачисление: <b>{pan}</b> \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода (руб):</b>",
            kb([(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "card_from":
        digits = msg.text.strip()
        if not re.match(r"^\d{4}$", digits):
            await edit(msg, "\u274c Введи ровно 4 цифры",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["card_from"] = digits; s["step"] = "card_to"
        r1, r2, r3 = rand4(), rand4(), rand4()
        await edit(msg,
            f"\U0001f4b3 Карта списания: <b>•• {digits}</b> \u2705\n\n"
            "\U0001f4b3 <b>Последние 4 цифры карты зачисления:</b>\n\n"
            "Выбери или введи свои:",
            kb([(r1, f"cto:{r1}"), (r2, f"cto:{r2}"), (r3, f"cto:{r3}")],
               [(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "card_to":
        digits = msg.text.strip()
        if not re.match(r"^\d{4}$", digits):
            await edit(msg, "\u274c Введи ровно 4 цифры",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["card_to"] = digits; s["step"] = "card_amount"
        await edit(msg,
            f"\U0001f4b3 Списание: <b>•• {s['card_from']}</b>\n"
            f"\U0001f4b3 Зачисление: <b>•• {digits}</b> \u2705\n\n"
            "\U0001f4b0 <b>Сумма перевода (руб):</b>",
            kb([(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "card_amount":
        try:
            raw = msg.text.strip().replace(" ", "").replace(u"\u20bd", "").replace(",00", "").replace(".", "")
            amount = int(raw)
            if amount < 1 or amount > 999999: raise ValueError
        except ValueError:
            await edit(msg, "\u274c Сумма от 1 до 999 999",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        s["amount"] = amount; s["step"] = "card_confirm"
        if s.get("form") == "web":
            wg = wgen()
            comm = wg["comm"](amount)
            total = amount + comm
            await edit(msg,
                f"\U0001f4cb <b>Проверь данные:</b> (\U0001f195 новая версия)\n\n"
                f"\U0001f4b3 {s['pan_from']} \u2192 {s['pan_to']}\n"
                f"\U0001f4b0 Сумма: {wg['fmt_amt'](amount)}\n"
                f"\U0001f4b8 Комиссия: {wg['fmt_amt'](comm)}\n"
                f"\U0001f4b5 Итого: {wg['fmt_tot'](total)}\n"
                f"\U0001f552 Текущее время",
                kb([(u"\u2705 Сгенерировать", "card_confirm")],
                   [(u"\u274c Отмена", "cancel")]), uid)
        else:
            cg = cgen()
            comm = cg["comm"](amount)
            total = amount + comm
            await edit(msg,
                f"\U0001f4cb <b>Проверь данные:</b>\n\n"
                f"\U0001f4b3 •• {s['card_from']} \u2192 •• {s['card_to']}\n"
                f"\U0001f4b0 Сумма: {cg['fmt'](amount)}\n"
                f"\U0001f4b8 Комиссия: {cg['fmt'](comm)}\n"
                f"\U0001f4b5 Итого: {cg['fmt'](total)}\n"
                f"\U0001f552 Текущее время",
                kb([(u"\u2705 Сгенерировать", "card_confirm")],
                   [(u"\u274c Отмена", "cancel")]), uid)

    elif s["step"] == "batch_count":
        # Пакет чеков: пользователь ввёл число N текстом
        try: n = int(msg.text.strip())
        except ValueError:
            await edit(msg, u"\u274c Введи число от 2 до 10",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        if not (2 <= n <= 10):
            await edit(msg, u"\u274c Только от 2 до 10",
                       kb([(u"\u274c Отмена", "cancel")]), uid); return
        sessions.pop(uid, None)
        await edit(msg, f"\U0001f4e6 <b>Пакет: {n} чеков</b>\n\n\u23f3 <b>Генерация…</b>", uid=uid)
        await _run_batch(msg, uid, n)

    elif s["step"] == "admin_grant":
        try: new_uid = int(msg.text.strip())
        except ValueError:
            await edit(msg, "\u274c Введи числовой ID", kb([(u"\u2b05 Назад","admin")]), uid); return
        if new_uid not in data["users"]:
            data["users"].append(new_uid); save(data)
        s["step"] = None
        await edit(msg, f"\u2705 Доступ выдан: <code>{new_uid}</code>",
                   kb([(u"\u2b05 Назад","admin")]), uid)

    elif s["step"] == "admin_revoke":
        try: rev_uid = int(msg.text.strip())
        except ValueError:
            await edit(msg, "\u274c Введи числовой ID", kb([(u"\u2b05 Назад","admin")]), uid); return
        if rev_uid in data["users"] and rev_uid != ADMIN_ID:
            data["users"].remove(rev_uid); save(data)
            await edit(msg, f"\U0001f6ab Забрано: <code>{rev_uid}</code>",
                       kb([(u"\u2b05 Назад","admin")]), uid)
        else:
            await edit(msg, "\u274c Не найден или админ", kb([(u"\u2b05 Назад","admin")]), uid)
        s["step"] = None

# ── ADMIN ──
@router.callback_query(F.data == "admin")
async def cb_admin(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет",show_alert=True); return
    sessions[cb.from_user.id] = {"step": None, "mid": cb.message.message_id}
    await edit(cb,
        f"\U0001f6e0 <b>Админ-панель</b>\n\n"
        f"\U0001f465 Пользователей бота: {len(data['users'])}\n"
        f"\U0001f4c4 Сгенерировано: {len(data['logs'])}",
        kb([(u"\U0001f4cb Пользователи","a_users")],
           [(u"\u2795 Выдать доступ","a_grant"),(u"\u2796 Забрать","a_revoke")],
           [(u"\U0001f4ca Логи","a_logs"),(u"\U0001f4c8 Статистика","a_stats")],
           [(u"\U0001f465 Пользователи","usr_list"),(u"\u2795 Создать доступ","usr_new")],
           [(u"\U0001f527 Фичи юзеров","users_list")],
           [(u"\U0001f517 Привязки ЛК","a_bind")],
           [(u"\u2693 Якоря номеров","a_anchors")],
           [(u"\u2b05 Главная","home")]))
    await cb.answer()

# ── ANCHORS (авто-переякорение WB квитанции № / МТС кода транзакции) ──
ANCHOR_PROMPT = (
    "\U0001f4e5 <b>Обновление якоря</b>\n\n"
    "Пришли <b>PDF свежего оригинала</b> — чек ВБ (\u00abКвитанция №\u00bb) "
    "или чек МТС (\u00abКод транзакции\u00bb).\n\n"
    "Бот сам распознает банк, вытащит номер и время, проверит "
    "правдоподобие и добавит якорь в ряд. Скорость сразу переоценится "
    "по последним якорям."
)

def _anchor_status() -> str:
    wb, mts = wbgen(), mgen()
    wa, ma = wb.get_anchors(), mts.get_refs()
    def fmt(rows, is_wb):
        out = []
        for a in rows[-3:]:
            mark = "№ " if is_wb else ""
            out.append(f"  {a[0]:%d.%m %H:%M:%S} \u2192 {mark}<code>{a[1]}</code>")
        return "\n".join(out)
    return (
        "\u2693 <b>Якоря номеров</b>\n\n"
        f"<b>ВБ квитанция №</b> — {len(wa)} шт:\n{fmt(wa, True)}\n"
        f"Скорость сейчас: <b>{wb.current_rate():.2f}/с</b> "
        f"(окно {getattr(wb, '_RATE_WINDOW', 4)})\n\n"
        f"<b>МТС код транзакции</b> — {len(ma)} шт:\n{fmt(ma, False)}\n"
        f"Скорость сейчас: <b>{mts.current_rate():.1f}/с</b> "
        f"(окно {getattr(mts, '_RATE_WINDOW', 4)})\n\n"
        "Скорость — медиана наклонов последних сегментов, не константа.\n"
        "Якоря хранятся в wb_used_no.txt / mts_used_tr.txt (строки «A …», "
        "история не стирается)."
    )

@router.callback_query(F.data == "a_anchors")
async def cb_a_anchors(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    await edit(cb, _anchor_status(),
               kb([(u"\U0001f4e5 Обновить якорь", "anchor_wait")],
                  [(u"\u2b05 Назад", "admin")]))
    await cb.answer()

@router.callback_query(F.data == "anchor_wait")
async def cb_anchor_wait(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_admin(uid): await cb.answer("Нет"); return
    sessions[uid] = {"step": "anchor_wait", "mid": cb.message.message_id}
    await edit(cb, ANCHOR_PROMPT, kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

@router.message(Command("anchor"))
async def cmd_anchor(msg: Message):
    uid = msg.from_user.id
    if not is_admin(uid): return
    sessions[uid] = {"step": "anchor_wait"}
    await edit(msg, ANCHOR_PROMPT, kb([(u"\u274c Отмена", "cancel")]), uid)

@router.message(Command("perf"))
async def cmd_perf(msg: Message):
    """Метрики отзывчивости (админ): время генераций, ожидание в очереди, сессии."""
    if not is_admin(msg.from_user.id): return
    await msg.answer("\U0001f4ca <b>Отзывчивость</b>\n\n" + _perf_summary())

# ── ADMIN: per-user feature flags (/grant, /revoke, /features, /users) ──
@router.message(Command("features"))
async def cmd_features(msg: Message):
    """Список всех per-user фич и у кого они включены. Доступ: админ."""
    if not is_admin(msg.from_user.id): return
    lines = [u"\U0001f527 <b>Per-user фичи</b>\n"]
    for f in PER_USER_FEATURES:
        holders = []
        for u in data["users"]:
            if u == ADMIN_ID:
                continue  # админ имеет все по умолчанию
            if _fflags(u).get(f, False):
                holders.append(str(u))
        label = FEATURE_LABELS[f]
        master = u"вкл" if gfeat(f) else u"ОТКЛ (мастер)"
        if holders:
            lines.append(f"<b>{label}</b> [{master}]:\n  \U0001f464 <code>{'</code>, <code>'.join(holders)}</code>")
        else:
            lines.append(f"<b>{label}</b> [{master}]:\n  <i>никому не выдана</i>")
    lines.append(u"\n<i>Админ имеет все фичи автоматически. Мастер-флаг выключает фичу для всех.</i>")
    await msg.answer("\n".join(lines))

@router.message(Command("grant"))
async def cmd_grant(msg: Message):
    """Выдать per-user фичу юзеру. /grant <uid> <feature>"""
    if not is_admin(msg.from_user.id): return
    try:
        parts = msg.text.split()
        tgt = int(parts[1])
        feat_name = parts[2].strip().lower()
    except (IndexError, ValueError):
        await msg.answer(u"\u274c Формат: <code>/grant &lt;uid&gt; &lt;фича&gt;</code>\n"
                         u"Фичи: <code>" + ", ".join(PER_USER_FEATURES) + "</code>")
        return
    if feat_name not in PER_USER_FEATURES:
        await msg.answer(u"\u274c Неизвестная фича: <code>" + html.escape(feat_name) + "</code>\n"
                         u"Доступные: <code>" + ", ".join(PER_USER_FEATURES) + "</code>")
        return
    if tgt not in data["users"]:
        await msg.answer(u"\u274c Юзер <code>" + html.escape(str(tgt)) + "</code> не в whitelist. "
                         u"Сначала выдай доступ через админ-панель.")
        return
    if not _ff_set(tgt, feat_name, True):
        await msg.answer(u"\u274c Не удалось сохранить.")
        return
    await msg.answer(u"\u2705 <b>Фича выдана</b>\n\n"
                     f"\U0001f464 Юзер: <code>{tgt}</code>\n"
                     f"\U0001f511 Фича: <b>{FEATURE_LABELS[feat_name]}</b>")

@router.message(Command("revoke"))
async def cmd_revoke(msg: Message):
    """Отозвать per-user фичу. /revoke <uid> <feature>"""
    if not is_admin(msg.from_user.id): return
    try:
        parts = msg.text.split()
        tgt = int(parts[1])
        feat_name = parts[2].strip().lower()
    except (IndexError, ValueError):
        await msg.answer(u"\u274c Формат: <code>/revoke &lt;uid&gt; &lt;фича&gt;</code>")
        return
    if feat_name not in PER_USER_FEATURES:
        await msg.answer(u"\u274c Неизвестная фича: <code>" + html.escape(feat_name) + "</code>")
        return
    if not _ff_set(tgt, feat_name, False):
        await msg.answer(u"\u274c Не удалось сохранить.")
        return
    if feat_name == "api_token":
        ff = data.get("feature_flags", {}).get(str(tgt))
        if isinstance(ff, dict):
            ff.pop("api_token_value", None)
            save(data)
    await msg.answer(u"\U0001f6ab <b>Фича отозвана</b>\n\n"
                     f"\U0001f464 Юзер: <code>{tgt}</code>\n"
                     f"\U0001f511 Фича: <b>{FEATURE_LABELS[feat_name]}</b>")

def _users_toggle_kb():
    """Кнопки: по юзеру → меню toggle фич."""
    rows = []
    for u in data["users"]:
        if u == ADMIN_ID:
            rows.append([(u"\U0001f451 ADMIN " + str(u) + " (все фичи)", "noop")])
        else:
            ff = _fflags(u)
            on_count = sum(1 for f in PER_USER_FEATURES if ff.get(f, False))
            rows.append([(f"\U0001f464 {u} ({on_count}/{len(PER_USER_FEATURES)} фич)", f"usr_ff:{u}")])
    rows.append([(u"\U0001f527 Все фичи", "features")])
    rows.append([(u"\U0001f3e0 Меню", "home")])
    return kb(*rows)

@router.message(Command("users"))
async def cmd_users(msg: Message):
    """Список юзеров с inline-кнопками toggle фич. Доступ: админ."""
    if not is_admin(msg.from_user.id): return
    await msg.answer(u"\U0001f465 <b>Юзеры и их фичи</b>\n\nНажми на юзера:",
                     reply_markup=_users_toggle_kb())

@router.callback_query(F.data.startswith("usr_ff:"))
async def cb_usr_ff(cb: CallbackQuery):
    """Меню toggle per-user фич для конкретного юзера."""
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    try: tgt = int(cb.data.split(":", 1)[1])
    except ValueError: await cb.answer("Ошибка"); return
    if tgt not in data["users"]:
        await cb.answer("Юзер не найден"); return
    await edit(cb, f"\U0001f464 <b>Юзер {tgt}</b>\n\nПереключи фичи:", _ff_toggle_menu(tgt))
    await cb.answer()

def _ff_toggle_menu(tgt: int):
    """Клавиатура toggle per-user фич для юзера."""
    ff = _fflags(tgt)
    rows = [[(f"\U0001f464 Юзер {tgt}", "noop")]]
    for f in PER_USER_FEATURES:
        on = ff.get(f, False)
        icon = u"\u2705" if on else u"\u23f9\ufe0f"
        rows.append([(f"{icon} {FEATURE_LABELS[f]}", f"ff_t:{tgt}:{f}")])
    rows.append([(u"\u2795 Все", f"ff_all:{tgt}:1"), (u"\u2796 Никаких", f"ff_all:{tgt}:0")])
    rows.append([(u"\u2b05 К списку", "users_list")])
    return kb(*rows)

@router.callback_query(F.data.startswith("ff_t:"))
async def cb_ff_toggle(cb: CallbackQuery):
    """Toggle одной per-user фичи."""
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    try:
        _, tgt_s, feat_name = cb.data.split(":", 2)
        tgt = int(tgt_s)
    except ValueError: await cb.answer("Ошибка"); return
    if feat_name not in PER_USER_FEATURES:
        await cb.answer("Неизвестная фича"); return
    cur = _fflags(tgt).get(feat_name, False)
    _ff_set(tgt, feat_name, not cur)
    if feat_name == "api_token" and cur:
        ff = data.get("feature_flags", {}).get(str(tgt))
        if isinstance(ff, dict):
            ff.pop("api_token_value", None)
            save(data)
    await cb.answer(f"{FEATURE_LABELS[feat_name]}: {'вкл' if not cur else 'выкл'}")
    await edit(cb, f"\U0001f464 <b>Юзер {tgt}</b>\n\nПереключи фичи:", _ff_toggle_menu(tgt))

@router.callback_query(F.data.startswith("ff_all:"))
async def cb_ff_all(cb: CallbackQuery):
    """Включить/выключить все per-user фичи для юзера."""
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    try:
        _, tgt_s, val_s = cb.data.split(":", 2)
        tgt = int(tgt_s)
        val = val_s == "1"
    except ValueError: await cb.answer("Ошибка"); return
    for f in PER_USER_FEATURES:
        _ff_set(tgt, f, val)
    if not val:
        ff = data.get("feature_flags", {}).get(str(tgt))
        if isinstance(ff, dict):
            ff.pop("api_token_value", None)
            save(data)
    await cb.answer("Все фичи: " + (u"вкл" if val else u"выкл"))
    await edit(cb, f"\U0001f464 <b>Юзер {tgt}</b>\n\nПереключи фичи:", _ff_toggle_menu(tgt))

@router.callback_query(F.data == "users_list")
async def cb_users_list(cb: CallbackQuery):
    """Назад к списку юзеров из меню toggle."""
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    await edit(cb, u"\U0001f465 <b>Юзеры и их фичи</b>", _users_toggle_kb())
    await cb.answer()

# ── PDF CHECK: verify any PDF against our generator's laws ──
_PDF_CHECK_MAX = 10 * 1024 * 1024  # 10 MB ceiling
_BANK_MARKERS = [
    ("mts",  [b"MTSSans"]),
    ("wb",   [b"GoogleSans"]),
    ("vtb",  [b"SFProDisplay"]),
    ("rshb", [b"Onest", b"JasperReports"]),
    ("ozon", [b"GTEestiProDisplay", b"Chromium", b"Skia"]),
    ("yandex", [b"YSText", b"YSLogotype", b"\xd0\xaf\xd0\xbd\xd0\xb4\xd0\xb5\xd0\xba\xd1\x81 \xd0\x91\xd0\xb0\xd0\xbd\xd0\xba"]),
]
_BANK_LABEL = {"mts": "МТС Банк", "wb": "Wildberries", "vtb": "ВТБ",
               "rshb": "Россельхозбанк", "ozon": "Озон Банк",
               "yandex": "Яндекс Банк"}
# SBP op_id: pfx(1) year(1) doy(3) hhmmss(6) mmm(3) flag(1) node(1) "0"(1)
#            route(1) gw(4) suffix(10)  = 32 chars
_OPID_RE = re.compile(
    rb"\b([AB]\d{3}\d{6}\d{3}[0-27][0-9A-Z]0[GB]\d{4}"
    rb"(?:0011890101|0011881102))\b")
_SUBSET_RE = re.compile(rb"/BaseFont\s*/([A-Z]{6})\+([A-Za-z0-9\-]+)")

@router.callback_query(F.data == "check_pdf")
async def cb_check_pdf(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_ok(uid):
        await cb.answer("Нет доступа", show_alert=True); return
    if not feat(uid, "check_pdf"):
        await cb.answer("Фича не выдана админом", show_alert=True); return
    sessions[uid] = {"step": "check_pdf_wait", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f50d <b>Проверка чека</b>\n\n"
        "Пришли <b>PDF-документ</b> — бот определит банк, сверит структуру "
        "и законы нашего генератора (op_id, эра, шлюзы/узлы, лаг CreationDate, "
        "дефлейт-диалект, шрифтовые таблицы) и вынесет вердикт.\n\n"
        f"\u26a0\ufe0f Лимит размера — {_PDF_CHECK_MAX // (1024*1024)} МБ.",
        kb([(u"\u274c Отмена", "cancel")]), uid)
    await cb.answer()

def _detect_bank(pdf_bytes: bytes):
    """Return bank key ('mts'/'wb'/'vtb'/'rshb'/'ozon') by raw-byte markers, or None."""
    for key, markers in _BANK_MARKERS:
        if all(m in pdf_bytes for m in markers):
            return key
    return None

def _bank_laws(bank: str):
    """Read the generator's era/nodes/gateways/flags constants for `bank`."""
    if bank == "mts":
        m = mgen()
        return {"era_flip": m._ERA_FLIP_UTC,
                "nodes_new": m._NODES_NEW, "nodes_old": None,
                "gw_new": m._GATEWAYS_NEW, "gw_old": None,
                "flags_new": "0", "flags_old": "017",
                "lag_min": 3, "lag_max": 30}
    if bank == "wb":
        w = wbgen()
        return {"era_flip": w._ERA_FLIP_UTC,
                "nodes_new": w._NODES_NEW, "nodes_old": w._NODES,
                "gw_new": {"A": w._GATEWAYS_A, "B": w._GATEWAYS_B},
                "gw_old": {"A": w._GATEWAYS_A, "B": w._GATEWAYS_B},
                "flags_new": "0", "flags_old": "01",
                "lag_min": 15, "lag_max": 75}
    if bank == "vtb":
        v = vtbgen()
        return {"era_flip": v._ERA_FLIP_UTC,
                "nodes_new": v._NODES_NEW, "nodes_old": v._NODES,
                "gw_new": {"A": [g for g, _ in v._GW_A_NEW],
                           "B": [g for g, _ in v._GW_B_NEW]},
                "gw_old": {"A": v._GW_A, "B": v._GW_B},
                "flags_new": "0", "flags_old": "012",
                "lag_min": 5, "lag_max": 25}
    if bank == "rshb":
        r = rshbgen()
        return {"era_flip": datetime(2026, 11, 6, 0, 0, 0),
                "nodes_new": r._NODES, "nodes_old": r._NODES,
                "gw_new": {"A": r._GW_A, "B": r._GW_B},
                "gw_old": {"A": r._GW_A, "B": r._GW_B},
                "flags_new": "012", "flags_old": "012",
                "lag_min": 12, "lag_max": 20}
    if bank == "ozon":
        o = ozongen()
        return {"era_flip": o._ERA_FLIP_UTC,
                "nodes_new": o._NODES_NEW, "nodes_old": o._NODES_OLD,
                "gw_new": {"A": [g for g, _ in o._GW_A_NEW], "B": None},
                "gw_old": {"A": o._GW_A_OLD, "B": o._GW_B_OLD},
                "flags_new": "0", "flags_old": "012",
                "lag_min": 5, "lag_max": 13}
    return None

def _parse_pdf_info(pdf_bytes: bytes):
    """Extract trailer /Info fields, /ID, object count, font stats from raw bytes."""
    info = {"producer": None, "creator": None, "creation_date": None,
            "id0": None, "id1": None, "n_objects": None,
            "type0_fonts": 0, "tounicode": 0, "cidset": 0, "fontfile2": 0,
            "fontfile3": 0, "subset_tags": [], "w_arrays": 0}
    m = re.search(rb"/Info\s*\d+\s+\d+\s+R", pdf_bytes)
    if m:
        # find the Info object body
        m2 = re.search(rb"(\d+)\s+(\d+)\s+obj\s*<<([^>]*?)/Producer\s*\(([^)]*)\)", pdf_bytes)
        if m2:
            info["producer"] = m2.group(4).decode("latin-1", "replace")
        m3 = re.search(rb"/Creator\s*\(([^)]*)\)", pdf_bytes)
        if m3:
            info["creator"] = m3.group(1).decode("latin-1", "replace")
        m4 = re.search(rb"/CreationDate\s*\((D:[^)]*)\)", pdf_bytes)
        if m4:
            info["creation_date"] = m4.group(1).decode("latin-1", "replace")
    m_id = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", pdf_bytes)
    if not m_id:
        m_id = re.search(rb"/ID\s*\[\s*\(([^)]*)\)\s*\(([^)]*)\)\s*\]", pdf_bytes)
    if m_id:
        info["id0"] = m_id.group(1).decode("latin-1", "replace")[:32]
        info["id1"] = m_id.group(2).decode("latin-1", "replace")[:32]
    m_size = re.search(rb"/Size\s+(\d+)", pdf_bytes)
    if m_size:
        info["n_objects"] = int(m_size.group(1)) - 1
    info["type0_fonts"] = len(re.findall(rb"/Subtype\s*/Type0", pdf_bytes))
    info["tounicode"]  = len(re.findall(rb"/ToUnicode\s+\d+\s+\d+\s+R", pdf_bytes))
    info["cidset"]     = len(re.findall(rb"/CIDSet\s+\d+\s+\d+\s+R", pdf_bytes))
    info["fontfile2"]   = len(re.findall(rb"/FontFile2\s+\d+\s+\d+\s+R", pdf_bytes))
    info["fontfile3"]  = len(re.findall(rb"/FontFile3\s+\d+\s+\d+\s+R", pdf_bytes))
    info["w_arrays"]   = len(re.findall(rb"/W\s+\[", pdf_bytes))
    info["subset_tags"] = [m.group(1).decode("latin-1", "replace")
                           for m in _SUBSET_RE.finditer(pdf_bytes)]
    return info

def _is_python_zlib(stream_bytes: bytes):
    """Heuristic: does this zlib stream look like Python zlib output (vs Java
    Deflater)? Re-compresses the decompressed data with Python zlib at the
    common levels and compares — exact match means Python zlib."""
    try:
        raw = zlib.decompress(stream_bytes)
    except Exception:
        return None
    for lvl in (6, -1, 9, 1, 7, 5):
        try:
            if zlib.compress(raw, lvl) == stream_bytes:
                return True
        except Exception:
            continue
    return False

def _verify_pdf(pdf_bytes: bytes):
    """Run all checks via receipt_validator.validate() and translate the result
    into the {bank, label, verdict, findings, info} shape the handler expects.
    receipt_validator is the standalone 5-bank validator (stdlib + PyMuPDF only,
    no generator imports). Verdict is binary: 'good' if all checks passed, else
    'bad' — matches the bot's ✅/❌ reply contract."""
    import receipt_validator as _rv
    info = _parse_pdf_info(pdf_bytes)  # footer stats (object count, fonts, dates)
    findings = []  # list of (kind, text) — kind: 'ok' | 'bad'
    try:
        res = _rv.validate(pdf_bytes)
    except Exception as e:
        return {"bank": None, "label": None, "verdict": "bad",
                "findings": [("bad", f"validator crashed: {html.escape(str(e)[:200])}")],
                "info": info}
    bank_up = res["bank"]            # "MTS"|"WB"|"VTB"|"RSHB"|"OZON"|None
    bank = bank_up.lower() if bank_up else None
    label = _BANK_LABEL.get(bank, bank_up or "?")
    for c in res["checks"]:
        kind = "ok" if c["passed"] else "bad"
        findings.append((kind, f"{c['name']}: {html.escape(c['detail'][:200])}"))
    verdict = "good" if res["passed"] else "bad"
    return {"bank": bank, "label": label, "verdict": verdict,
            "findings": findings, "info": info}


async def _handle_check_pdf(msg: Message, uid):
    """Download the user's PDF and run the verifier."""
    doc = msg.document
    fname = (doc.file_name or "").lower()
    if doc.mime_type != "application/pdf" and not fname.endswith(".pdf"):
        await msg.answer("\u274c Нужен именно PDF-документ.")
        return
    try:
        f = await bot.get_file(doc.file_id)
    except Exception as e:
        await msg.answer(f"\u274c Не скачал файл: <code>{html.escape(str(e))}</code>")
        return
    # Size guard via Telegram file_size (avoid downloading >10MB)
    if f.file_size and f.file_size > _PDF_CHECK_MAX:
        await msg.answer(
            f"\u274c Файл слишком большой ({f.file_size // (1024*1024)} МБ). "
            f"Лимит — {_PDF_CHECK_MAX // (1024*1024)} МБ.")
        return
    buf = io.BytesIO()
    try:
        await bot.download_file(f.file_path, destination=buf)
    except Exception as e:
        await msg.answer(f"\u274c Не скачал файл: <code>{html.escape(str(e))}</code>")
        return
    pdf = buf.getvalue()
    if not pdf:
        await msg.answer("\u274c Пустой PDF (0 байт).")
        return
    if len(pdf) > _PDF_CHECK_MAX:
        await msg.answer(
            f"\u274c Файл слишком большой ({len(pdf) // (1024*1024)} МБ). "
            f"Лимит — {_PDF_CHECK_MAX // (1024*1024)} МБ.")
        return
    if not pdf.startswith(b"%PDF"):
        await msg.answer("\u274c Это не PDF (нет заголовка <code>%PDF</code>).")
        return
    await msg.answer("\u23f3 Проверяю чек\u2026")
    try:
        res = await asyncio.get_running_loop().run_in_executor(
            None, _verify_pdf, pdf)
    except Exception as e:
        await msg.answer(
            f"\u274c Ошибка проверки: <code>{html.escape(str(e)[:200])}</code>")
        return
    sessions.pop(uid, None)
    if res["bank"] is None:
        await msg.answer(
            "\u26a0\ufe0f <b>Банк не распознан</b>\n\n"
            "Не найдены маркеры наших генераторов (MTSSans / GoogleSans / "
            "SFProDisplay / Onest+JasperReports / GTEestiProDisplay+Chromium+Skia). "
            "Это не чек нашего генератора.",
            reply_markup=kb([(u"\U0001f50d Проверить ещё", "check_pdf")],
                            [(u"\U0001f3e0 Меню", "home")]))
        return
    # Render verdict
    if res["verdict"] == "good":
        head = (f"\u2705 <b>Хороший</b> — {res['label']}\n\n"
                f"Соответствует нашему генератору по всем параметрам.")
    elif res["verdict"] == "warn":
        head = (f"\u26a0\ufe0f <b>Подозрительный</b> — {res['label']}\n\n"
                f"Найдены отклонения (см. ниже).")
    else:
        head = (f"\u26a0\ufe0f <b>Подозрительный</b> — {res['label']}\n\n"
                f"Найдены явные нарушения законов генератора.")
    lines = [head, ""]
    for kind, txt in res["findings"]:
        icon = "\u2705" if kind == "ok" else ("\u26a0\ufe0f" if kind == "warn" else "\u274c")
        lines.append(f"{icon} {txt}")
    # Footer: structural summary
    i = res["info"]
    lines.append("")
    lines.append(
        f"\U0001f4d6 Объектов: <b>{i['n_objects']}</b> \u2022 "
        f"Type0: <b>{i['type0_fonts']}</b> \u2022 "
        f"ToUnicode: <b>{i['tounicode']}</b> \u2022 "
        f"FontFile2: <b>{i['fontfile2']}</b>")
    if i["creation_date"]:
        lines.append(f"\U0001f4c5 CreationDate: <code>{html.escape(i['creation_date'])}</code>")
    await msg.answer("\n".join(lines),
        reply_markup=kb([(u"\U0001f50d Проверить ещё", "check_pdf")],
                        [(u"\U0001f3e0 Меню", "home")]))

async def _handle_ozon_edit_pdf(msg: Message, uid, s):
    """Скачивает присланный юзером настоящий чек Озон Банк и кладёт его в сессию.
    Следующий шаг — ввод новой суммы (см. step == "ozon_edit_amount" в handle_text).
    Раскладку чека проверяем сразу (ozon_editor._ensure_supported_layout),
    чтобы неподдерживаемый вариант отсечь до ввода суммы, а не после."""
    doc = msg.document
    fname = (doc.file_name or "").lower()
    if doc.mime_type != "application/pdf" and not fname.endswith(".pdf"):
        await msg.answer("\u274c Нужен именно PDF-файл чека Озон.")
        return
    try:
        f = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(f.file_path, destination=buf)
        pdf = buf.getvalue()
    except Exception as e:
        await msg.answer(f"\u274c Не скачал файл: <code>{html.escape(str(e))}</code>")
        return
    if not pdf or not pdf.startswith(b"%PDF"):
        await msg.answer("\u274c Это не PDF (нет заголовка <code>%PDF</code>).")
        return
    if len(pdf) > _PDF_CHECK_MAX:
        await msg.answer(f"\u274c Файл больше {_PDF_CHECK_MAX // (1024*1024)} МБ — пришли PDF чека Озон как есть из приложения банка.")
        return
    try:
        ozon_editor_mod()._ensure_supported_layout(pdf)
    except Exception as e:
        await msg.answer(f"\u274c Этот чек изменить не могу: <code>{html.escape(str(e))[:300]}</code>")
        return
    s["ozon_edit_pdf"] = pdf
    s["step"] = "ozon_edit_amount"
    s["ts"] = datetime.now().timestamp()
    await edit(msg,
        "\u2705 PDF принят\n\n"
        "\U0001f4b0 <b>Новая сумма (рубли):</b>\nНапример: <code>14500</code>",
        kb([(u"\u274c Отмена", "cancel")]), uid)

@router.message(F.document)
async def handle_doc(msg: Message):
    """Приём PDF: переякорение (admin, anchor_wait), проверка чека
    (check_pdf_wait) или изменение суммы в чеке Озон (ozon_edit_pdf)."""
    uid = msg.from_user.id
    s = sessions.get(uid) or {}
    step = s.get("step")
    if step == "ozon_edit_flow":
        if not is_ok(uid):
            return
        if ozx is None:
            await msg.answer("\u274c Модуль Озон v2 недоступен.")
            return
        await _handle_ozon_edit_pdf_v2(msg, uid, s)
        return
    if step == "ozon_edit_pdf":
        if not is_ok(uid):
            return
        await _handle_ozon_edit_pdf(msg, uid, s)
        return
    if step == "check_pdf_wait":
        if not is_ok(uid) or not feat(uid, "check_pdf"):
            await msg.answer(u"\u274c Фича «Проверить чек» не выдана.")
            return
        await _handle_check_pdf(msg, uid)
        return
    if step != "anchor_wait":
        return  # вне флоу документы игнорируем
    if not is_admin(uid):
        return
    doc = msg.document
    fname = (doc.file_name or "").lower()
    if doc.mime_type != "application/pdf" and not fname.endswith(".pdf"):
        await msg.answer("\u274c Нужен PDF-файл оригинала чека (ВБ или МТС).")
        return
    await msg.answer("\u23f3 Парсю оригинал...")
    try:
        f = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(f.file_path, destination=buf)
        pdf = buf.getvalue()
    except Exception as e:
        await msg.answer(f"\u274c Не скачал файл: <code>{html.escape(str(e))}</code>")
        return
    try:
        import anchor_lib
        info = await asyncio.get_running_loop().run_in_executor(
            None, anchor_lib.parse_pdf, pdf)
    except Exception as e:
        await msg.answer(
            f"\u274c Не распознал чек: <code>{html.escape(str(e))}</code>\n\n"
            "Нужен оригинальный PDF чека ВБ (\u00abКвитанция № …\u00bb) "
            "или МТС (\u00abКод транзакции …\u00bb).")
        return
    note = f"uid={uid} src=bot"
    if info["kind"] == "wb":
        ok, text = wbgen().add_anchor(info["dt"], info["num"], note=note)
        title = "ВБ квитанция №"
    else:
        ok, text = mgen().add_anchor(info["dt"], info["num"], note=note)
        title = "МТС код транзакции"
    sessions.pop(uid, None)
    if ok:
        data["logs"].append({"uid": uid, "type": "anchor", "kind": info["kind"],
                             "num": info["num"],
                             "time": info["dt"].strftime("%d.%m.%Y %H:%M:%S")})
        if len(data["logs"]) > 1000: data["logs"] = data["logs"][-1000:]
        save(data)
        await msg.answer(
            f"\u2693 <b>Якорь обновлён</b> ({title})\n\n"
            f"\U0001f552 {info['dt']:%d.%m.%Y %H:%M:%S} МСК\n"
            f"\U0001f522 <code>{info['num']}</code>\n"
            f"\U0001f4c8 {text}",
            reply_markup=kb([(u"\U0001f4e5 Ещё якорь", "anchor_wait")],
                            [(u"\u2693 Все якоря", "a_anchors")],
                            [(u"\U0001f3e0 Меню", "home")]))
    else:
        await msg.answer(
            f"\u26a0\ufe0f <b>Якорь не принят</b> ({title})\n\n"
            f"\U0001f552 {info['dt']:%d.%m.%Y %H:%M:%S} МСК\n"
            f"\U0001f522 <code>{info['num']}</code>\n"
            f"Причина: {html.escape(text)}",
            reply_markup=kb([(u"\U0001f4e5 Другой PDF", "anchor_wait")],
                            [(u"\u2693 Все якоря", "a_anchors")],
                            [(u"\U0001f3e0 Меню", "home")]))

# ── USER ACCOUNTS (login+password access, owner-managed) ──
DUR_MAP_TEXT = {"5h": "5 часов", "1d": "1 день", "3d": "3 дня", "7d": "7 дней", "30d": "30 дней"}

def _usr_status(u):
    if u.get("banned"):
        return "\U0001f6ab", "забанен"
    exp = u.get("expiresAt") or ""
    if exp:
        try:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            if exp_dt < now_dt:
                return "\u23f0", "истёк"
            h = int((exp_dt - now_dt).total_seconds() // 3600)
            return "\u2705", f"активен ({h}ч)"
        except Exception:
            pass
    return "\u2705", "активен"

async def _fetch_users():
    try:
        st, resp = await api_call("GET", "/users/list")
        if st == 200:
            return resp.get("data", [])
    except Exception:
        pass
    return None

@router.callback_query(F.data == "usr_list")
async def cb_usr_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    await cb.answer()  # мгновенный ack: список тянется из API до 15 с
    users = await _fetch_users()
    if users is None:
        await edit(cb, "\u274c API недоступен", kb([(u"\u2b05 Админ-панель", "admin")])); return
    if not users:
        await edit(cb, "\U0001f465 <b>Пользователей нет.</b>\n\nСоздай первого:",
                   kb([("\u2795 Создать", "usr_new")], [(u"\u2b05 Админ-панель", "admin")]))
        return
    lines, buttons = [], []
    for u in users[:14]:
        icon, stxt = _usr_status(u)
        lines.append(f"{icon} <b>{u['login']}</b> [{u['label']}] — {stxt}")
        buttons.append([(f"\U0001f464 {u['login']} [{u['label']}]", f"usr_v:{u['label']}")])
    buttons.append([("\u2795 Создать", "usr_new")])
    buttons.append([(u"\u2b05 Админ-панель", "admin")])
    await edit(cb, f"\U0001f465 <b>Пользователи ({len(users)}):</b>\n\n" + "\n".join(lines), kb(*buttons))

@router.callback_query(F.data.startswith("usr_v:"))
async def cb_usr_view(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    await cb.answer()  # мгновенный ack: список тянется из API до 15 с
    label = cb.data.split(":", 1)[1]
    users = await _fetch_users() or []
    u = next((x for x in users if x["label"] == label), None)
    if not u:
        await edit(cb, "\u274c Не найден", kb([(u"\u2b05 К списку", "usr_list")])); return
    icon, stxt = _usr_status(u)
    exp = (u.get("expiresAt") or "")[:16].replace("T", " ")
    text = (f"\U0001f464 <b>{u['login']}</b> [{u['label']}]\n\n"
            f"\U0001f511 Пароль: <code>{u['password']}</code>\n"
            f"\u23f0 Доступ до: {exp} UTC\n"
            f"Статус: {icon} {stxt}\n\n"
            f"\U0001f310 Его отдельный ЛК (открыть 1 раз в Safari):\n<code>https://online.mtsdengi.ru/?lk={label}</code>")
    ban_btn = ("\u2705 Разбанить", f"usr_ub:{label}") if u.get("banned") else ("\U0001f6ab Забанить", f"usr_b:{label}")
    await edit(cb, text, kb([ban_btn, ("\u23f3 Продлить", f"usr_e:{label}")],
                            [("\U0001f5d1 Удалить", f"usr_d:{label}")],
                            [(u"\u2b05 К списку", "usr_list")]))

@router.callback_query(F.data.startswith("usr_b:"))
async def cb_usr_ban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    await cb.answer()  # мгновенный ack: запрос уходит в API до 15 с
    label = cb.data.split(":", 1)[1]
    st, resp = await api_call("POST", f"/users/ban/{label}")
    await edit(cb, f"\U0001f6ab <b>{label}</b> заблокирован." if st == 200 else f"\u274c {html.escape(str(resp.get('error', 'Ошибка')))}",
               kb([(("⬅ К пользователю"), f"usr_v:{label}")]))

@router.callback_query(F.data.startswith("usr_ub:"))
async def cb_usr_unban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    await cb.answer()  # мгновенный ack: запрос уходит в API до 15 с
    label = cb.data.split(":", 1)[1]
    st, resp = await api_call("POST", f"/users/unban/{label}")
    await edit(cb, f"\u2705 <b>{label}</b> разблокирован." if st == 200 else f"\u274c {html.escape(str(resp.get('error', 'Ошибка')))}",
               kb([(("⬅ К пользователю"), f"usr_v:{label}")]))

@router.callback_query(F.data.startswith("usr_d:"))
async def cb_usr_delete(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    await cb.answer()  # мгновенный ack: запрос уходит в API до 15 с
    label = cb.data.split(":", 1)[1]
    st, resp = await api_call("POST", f"/users/delete/{label}")
    await edit(cb, f"\U0001f5d1 <b>{label}</b> удалён." if st == 200 else f"\u274c {html.escape(str(resp.get('error', 'Ошибка')))}",
               kb([(u"\u2b05 К списку", "usr_list")]))

@router.callback_query(F.data.startswith("usr_e:"))
async def cb_usr_extend_menu(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    label = cb.data.split(":", 1)[1]
    await edit(cb, f"\u23f3 <b>Продлить {label}</b> на:",
               kb([(u"\u23f0 5 часов", f"usr_x:{label}:5h"), (u"\U0001f4c5 1 день", f"usr_x:{label}:1d")],
                  [(u"\U0001f4c5 3 дня", f"usr_x:{label}:3d"), (u"\U0001f4c5 7 дней", f"usr_x:{label}:7d")],
                  [(u"\U0001f4c5 30 дней", f"usr_x:{label}:30d")],
                  [(u"\u2b05 К пользователю", f"usr_v:{label}")]))
    await cb.answer()

@router.callback_query(F.data.startswith("usr_x:"))
async def cb_usr_extend(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    _, label, dur = cb.data.split(":", 2)
    await cb.answer()  # мгновенный ack: запрос уходит в API до 15 с
    st, resp = await api_call("POST", f"/users/extend/{label}", {"duration": dur})
    if st == 200:
        exp = str(resp.get("expiresAt", ""))[:16].replace("T", " ")
        await edit(cb, f"\u2705 <b>{label}</b> продлён до {exp} UTC.",
                   kb([(("⬅ К пользователю"), f"usr_v:{label}")]))
    else:
        await edit(cb, f"\u274c {html.escape(str(resp.get('error', 'Ошибка')))}", kb([(("⬅ К пользователю"), f"usr_v:{label}")]))

@router.callback_query(F.data == "usr_new")
async def cb_usr_new(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    sessions[cb.from_user.id] = {"step": "usr_login", "mid": cb.message.message_id}
    await edit(cb,
        "\U0001f464 <b>Создание пользователя</b>\n\n"
        "Пришли <b>логин</b> (латиница/цифры, 3-32 символа):",
        kb([(u"\u274c Отмена", "cancel")]))
    await cb.answer()

@router.callback_query(F.data.startswith("usr_c:"))
async def cb_usr_create(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    uid = cb.from_user.id
    s = sessions.get(uid) or {}
    if s.get("step") != "usr_dur":
        await cb.answer("Начни заново"); return
    duration = cb.data.split(":", 1)[1]
    await cb.answer()  # мгновенный ack: создание уходит в API до 15 с
    try:
        st, resp = await api_call("POST", "/users/create",
                                  {"login": s.get("new_login", ""), "password": s.get("new_pass", ""), "duration": duration})
        if st == 200:
            await edit(cb,
                f"\u2705 <b>Пользователь создан!</b>\n\n"
                f"\U0001f464 Логин: <code>{resp['login']}</code>\n"
                f"\U0001f511 Пароль: <code>{resp['password']}</code>\n"
                f"\U0001f4cb ID: <b>{resp['label']}</b>\n"
                f"\u23f0 Доступ: {DUR_MAP_TEXT.get(duration, duration)}\n\n"
                f"Вход в админку — вкладка «Логин и пароль».\n"
                f"\U0001f310 Его отдельный ЛК (открыть 1 раз в Safari):\n<code>https://online.mtsdengi.ru/?lk={resp['label']}</code>",
                kb([("\U0001f465 К списку", "usr_list")],
                   [(u"\u2b05 Админ-панель", "admin")]))
        else:
            await edit(cb, f"\u274c {html.escape(str(resp.get('error', 'Ошибка')))}",
                       kb([(("⬅ Админ-панель"), "admin")]))
    except Exception as e:
        await edit(cb, f"\u274c API: <code>{html.escape(str(e))[:200]}</code>", kb([(("⬅ Админ-панель"), "admin")]))
    if sessions.get(uid) is s:
        sessions.pop(uid, None)

@router.callback_query(F.data.startswith("bankmenu:"))
async def cb_bank_menu(cb: CallbackQuery):
    """Подменю банка из главного меню (навигация, flow не стартует)."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    bank_key = cb.data.split(":", 1)[1] if ":" in cb.data else ""
    if bank_key not in _BANK_MENU_BUTTONS:
        await cb.answer("Неизвестный банк"); return
    sessions.pop(uid, None)
    await edit(cb, _BANK_MENU_TEXT[bank_key], bank_menu(bank_key, uid), uid)
    await cb.answer()

@router.callback_query(F.data == "menu_other")
async def cb_menu_other(cb: CallbackQuery):
    """Раздел «Другое»: все небанковские кнопки (feature-гейты — в other_menu)."""
    uid = cb.from_user.id
    if not is_ok(uid): await cb.answer("Нет доступа"); return
    sessions.pop(uid, None)
    await edit(cb, OTHER_MENU_TEXT, other_menu(uid), uid)
    await cb.answer()

@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery):
    uid = cb.from_user.id; sessions.pop(uid, None)
    await edit(cb,
        f"\U0001f44b <b>{cb.from_user.first_name or 'бро'}</b>\n\n\U0001f3e6 Генератор чеков МТС Банк",
        main_menu(uid), uid)
    await cb.answer()

@router.callback_query(F.data == "a_users")
async def cb_a_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    lines = []
    for uid in data["users"]:
        tag = "\U0001f451" if uid == ADMIN_ID else "\U0001f464"
        cnt = sum(1 for e in data["logs"] if get_uid(e) == uid)
        lines.append(f"{tag} <code>{uid}</code> — {cnt} чеков")
    await edit(cb,
        f"\U0001f465 <b>Пользователи ({len(data['users'])}):</b>\n\n"+"\n".join(lines),
        kb([(u"\u2b05 Назад","admin")]))
    await cb.answer()

@router.callback_query(F.data == "a_grant")
async def cb_a_grant(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    sessions[cb.from_user.id] = {"step": "admin_grant", "mid": cb.message.message_id}
    await edit(cb, "\u2795 <b>Выдать доступ</b>\n\nВведи Telegram ID:", kb([(u"\u2b05 Назад","admin")]))
    await cb.answer()

@router.callback_query(F.data == "a_revoke")
async def cb_a_revoke(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    sessions[cb.from_user.id] = {"step": "admin_revoke", "mid": cb.message.message_id}
    lines = [f"\U0001f464 <code>{u}</code>" for u in data["users"] if u != ADMIN_ID]
    await edit(cb, "\u2796 <b>Забрать доступ</b>\n\n"+(("\n".join(lines)+"\n\n") if lines else "")+"Введи ID:",
               kb([(u"\u2b05 Назад","admin")]))
    await cb.answer()

@router.callback_query(F.data == "a_logs")
async def cb_a_logs(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    recent = data["logs"][-10:]
    if not recent:
        await edit(cb, "\U0001f4ed Логов нет.", kb([(u"\u2b05 Назад","admin")])); await cb.answer(); return
    lines = []
    for e in reversed(recent):
        uid = get_uid(e)
        tag = "\U0001f451" if uid == ADMIN_ID else "\U0001f464"
        lines.append(f"{tag} <code>{uid}</code> \u2022 {e.get('time','?')}\n"
                     f"    {e.get('name','?')} \u2022 {e.get('amount','?')}\u20bd \u2022 {e.get('bank','?')}")
    await edit(cb, "\U0001f4ca <b>Последние:</b>\n\n"+"\n\n".join(lines), kb([(u"\u2b05 Назад","admin")]))
    await cb.answer()

@router.callback_query(F.data == "a_stats")
async def cb_a_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    total = len(data["logs"]); by_user = {}
    for e in data["logs"]:
        u = get_uid(e); by_user[u] = by_user.get(u,0)+1
    lines = [f"\U0001f4c8 <b>Всего:</b> {total}\n"]
    for u,c in sorted(by_user.items(), key=lambda x:-x[1]):
        lines.append(f"{'⭐' if u==ADMIN_ID else '👤'} <code>{u}</code>: {c}")
    await edit(cb, "\n".join(lines), kb([(u"\u2b05 Назад","admin")]))
    await cb.answer()

# ── ADMIN: telegram uid → tenant (ЛК) bindings ──
@router.callback_query(F.data == "a_bind")
async def cb_a_bind(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    bindings = data.get("lk_bindings") or {}
    lines, buttons = [], []
    for u in data["users"]:
        if u == ADMIN_ID:
            lines.append(f"\U0001f451 <code>{u}</code> — ADMIN (все переводы)")
            continue
        b = bindings.get(str(u))
        lines.append(f"\U0001f464 <code>{u}</code> — " + (f"<b>{b}</b>" if b else "не привязан"))
        buttons.append([(("\U0001f504 " if b else "\U0001f517 ") + f"{u}" + (f" → {b}" if b else ""), f"a_bind_u:{u}")])
    if not buttons:
        lines.append("\nПока некого привязывать — выдай доступ пользователю.")
    buttons.append([(u"\u2b05 Админ-панель", "admin")])
    await edit(cb, "\U0001f517 <b>Привязки Telegram → админка</b>\n\n" + "\n".join(lines), kb(*buttons))
    await cb.answer()

@router.callback_query(F.data.startswith("a_bind_u:"))
async def cb_a_bind_user(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    try: tgt = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID"); return
    await cb.answer()  # мгновенный ack: список тянется из API до 15 с
    users = await _fetch_users()
    if users is None:
        await edit(cb, "\u274c API недоступен", kb([(u"\u2b05 Привязки", "a_bind")])); return
    rows = []
    for u in users:
        if u.get("banned"): continue
        rows.append([(f"\U0001f464 {u['login']} [{u['label']}]", f"a_bind_t:{tgt}:{u['label']}")])
    if (data.get("lk_bindings") or {}).get(str(tgt)):
        rows.append([(u"\U0001f6ab Отвязать", f"a_bind_x:{tgt}")])
    rows.append([(u"\u2b05 Привязки", "a_bind")])
    await edit(cb, f"\U0001f517 Привязка <code>{tgt}</code> — выбери его админку:", kb(*rows))

@router.callback_query(F.data.startswith("a_bind_t:"))
async def cb_a_bind_set(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    _p, tgt, label = cb.data.split(":", 2)
    data.setdefault("lk_bindings", {})[str(int(tgt))] = label
    save(data)
    await edit(cb, f"\u2705 <code>{tgt}</code> привязан к <b>{label}</b>.\n"
                   f"Его «Добавить перевод в ЛК» теперь падает в {label}.",
               kb([(u"\u2b05 Привязки", "a_bind")]))
    await cb.answer()

@router.callback_query(F.data.startswith("a_bind_x:"))
async def cb_a_bind_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): await cb.answer("Нет"); return
    tgt = cb.data.split(":", 1)[1]
    removed = (data.get("lk_bindings") or {}).pop(tgt, None)
    if removed is not None:
        save(data)
    await edit(cb, f"\U0001f6ab Привязка <code>{tgt}</code> снята.",
               kb([(u"\u2b05 Привязки", "a_bind")]))
    await cb.answer()

@router.error()
async def on_error(event):
    # Последний рубеж: логируем необработанные исключения хендлеров, бот живёт дальше.
    logging.getLogger("bot").error("handler error: %s", event.exception, exc_info=event.exception)

async def main():
    dp = Dispatcher(); dp.include_router(router)
    asyncio.create_task(_session_sweeper())
    logging.getLogger("bot").info("Bot v3 starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
