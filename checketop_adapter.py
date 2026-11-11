# -*- coding: utf-8 -*-
"""
Адаптер чекетоп → mts-bot (menu-level merge, вариант А).

Подключает сервисы бота «чекетоп» (Альфа / ВТБ / Т-Банк) к aiogram-оболочке
mts_bot.py. Код чекетопа лежит в ./checketop_lib/ (плоские импорты внутри него),
поэтому добавляем его в sys.path один раз здесь.

Правило merge: дубликаты решает чекетоп (ВТБ СБП — отсюда, а не vtb_generator).
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "checketop_lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

_cache: dict[str, object] = {}


def _mod(name: str):
    """Ленивый импорт модулей чекетопа (как vtbgen()/ozongen() в mts_bot)."""
    if name not in _cache:
        import importlib

        _cache[name] = importlib.import_module(name)
    return _cache[name]


def alfa_scratch():
    return _mod("alfa_scratch_service")


def tbank_scratch():
    return _mod("tbank_scratch_service")


def vtb_scratch():
    return _mod("vtb_scratch_service")


# ── Альфа ────────────────────────────────────────────────────────────

def gen_alfa_sbp(*, payer: str, recipient: str, amount: int | str,
                 bank: str = "Т-Банк", phone: str = "",
                 datetime_str: str = "", operation_id: str = "",
                 account: str = "", message: str = "",
                 formed_at: str = "") -> bytes:
    """Чек Альфа СБП с нуля (alfa_scratch_service.generate_alfa_scratch).

    Пустые поля добиваются правдоподобными дефолтами, как в боте чекетоп:
    комиссия 0, дата/время — сейчас (мск), телефон и счёт — случайные.
    """
    import random
    from datetime import datetime, timedelta, timezone

    svc = alfa_scratch()
    if not datetime_str:
        now = datetime.now(timezone(timedelta(hours=3)))
        datetime_str = now.strftime("%d.%m.%Y %H:%M:%S") + " мск"
    values = {
        "payer": payer,
        "recipient": recipient,
        "amount": str(amount),
        "commission": "0",
        "bank": bank,
        "phone": phone or f"8 9{random.randint(10, 99)} "
                          f"{random.randint(100, 999)}-{random.randint(10, 99)}-{random.randint(10, 99)}",
        "datetime": datetime_str,
        "operation_id": operation_id,
        "account": account or "40817" + "".join(random.choice("0123456789") for _ in range(15)),
        "message": message or "Перевод денежных средств",
    }
    return svc.generate_alfa_scratch("sbp", values, formed_at=formed_at or None)


def gen_alfa_card(*, values: dict, formed_at: str = "") -> bytes:
    svc = alfa_scratch()
    return svc.generate_alfa_scratch("card", values, formed_at=formed_at or None)


def gen_alfa_transgran(*, values: dict, formed_at: str = "") -> bytes:
    svc = alfa_scratch()
    return svc.generate_alfa_scratch("transgran", values, formed_at=formed_at or None)


# ── ВТБ (замена vtb_generator из mts-bot) ────────────────────────────

def gen_vtb_sbp(*, payer: str, recipient: str, amount: int,
                bank: str = "Т-Банк", phone: str | None = None,
                account: str | None = None, date_str: str = "now",
                message: str | None = None) -> bytes:
    svc = vtb_scratch()
    return svc.generate_vtb_sbp(
        payer=payer, recipient=recipient, amount=amount, bank=bank,
        phone=phone, account=account, date_str=date_str, message=message,
    )


# ── Т-Банк ───────────────────────────────────────────────────────────

def gen_tbank_sbp(*, values: dict) -> bytes:
    svc = tbank_scratch()
    # Нормализация суммы как в боте чекетоп: _format_amount_str + хвостовой
    # пробел — иначе жирное «Итого» рендерится обрезанным.
    try:
        from tbank_check_service import _format_amount_str
        for key in ("amount_bold", "amount_small"):
            raw = str(values.get(key, "")).replace("\u20bd", "").replace("₽", "").strip()
            if raw:
                num = float(raw.replace("\u2009", "").replace("\u00a0", "").replace(" ", "").replace(",", "."))
                values = dict(values)
                values[key] = _format_amount_str(num) + " "
    except Exception:
        pass
    # КРИТИЧНО: пиннуем tbank_sbp_verified_05052026_enriched.pdf.
    # 1) Только *_enriched доноры содержат полный набор цифр F2 (TinkoffSans-
    #    Medium) — иначе «Итого» теряет цифры визуально («1 00 ₽»).
    # 2) Координаты SBP_FIELDS жёстко привязаны к раскладке ЭТОГО донора:
    #    у доноров других дат лейаут другой, и поля молча не заменяются
    #    (проверено прогоном по всему пулу 11.11.2026).
    donor = _LIB / "TBANK" / "tbank_sbp_verified_05052026_enriched.pdf"
    if not donor.exists():
        donor = None  # fallback на штатный пикер сервиса
    return svc.generate_from_scratch(values, receipt_type="sbp",
                                     template_path=donor)


def tbank_fields(receipt_type: str = "sbp") -> list[dict]:
    """Список полей чека Т-Банка (для формы в боте)."""
    svc = tbank_scratch()
    return svc.get_fields_for_scratch(receipt_type)


def alfa_fields(receipt_type: str = "sbp") -> list[dict]:
    """Список полей чека Альфы: берём из RECEIPT_TYPES сервиса."""
    svc = alfa_scratch()
    rtype = svc.RECEIPT_TYPES[receipt_type]
    return list(rtype.get("left", [])) + list(rtype.get("right", []))
