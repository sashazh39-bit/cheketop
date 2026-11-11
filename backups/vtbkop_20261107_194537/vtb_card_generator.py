# -*- coding: utf-8 -*-
"""VTB (openhtmltopdf) card-to-card receipt generator — donor-template replica.

Sibling of vtb_generator.py (SBP), NOT a modification of it: a card transfer
receipt has no SBP operation ID, no recipient phone and no message.  Instead
it carries two card masks in two different genuine formats, a commission and
a commission-inclusive total:

    Перевод на карту
    Статус                  Выполнено
    Дата операции           DD.MM.YYYY, HH:MM
    Номер карты             220024******1811      (sender, no spaces)
    Счет списания           *0298
    Имя плательщика         <ФИО.>
    Карта получателя        2204 32** **** 1187   (recipient, spaced)
    Банк получателя         Озон Банк
    Комиссия                99 ₽
    Сумма с комиссией       123 ₽
    Сумма операции          24 ₽   (13.5 pt, bottom block)

Producer/font/layout are byte-identical to the SBP receipts (openhtmltopdf,
SFProDisplay subsets, objects 1..18), so all machinery is reused from
vtb_generator: _Tpl parsing, openhtmltopdf int-dots right alignment, the
144-CID catalog font re-subset, the PDFBox trailer /ID and Java Deflater
DEFAULT level 6 via JZCompFS (never Python zlib).  Only obj5 (content) and
obj3 (Info dates) are rewritten for a fresh receipt; a byte-replica of a
known genuine receipt (created_dt + doc_id pinned) reproduces it exactly.

Commission (VTB retail tariff, vtb.ru/tarify, verified 2026-11-07):
to another bank's card 2% of the amount, minimum 99 RUB; to a VTB client
0 RUB.  All 5 genuine card receipts of 07.11.2026 (12..137 RUB to Ozon
Bank) charge the 99 RUB minimum, and amount + commission = total on each.
Rounding is half-up to whole rubles (receipts show whole rubles only).
"""
import os, re, random as _rnd
from datetime import datetime, timedelta, timezone

import vtb_generator as _v
from vtb_generator import _Tpl, _get_tpl, _assemble, format_amount

_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE = os.path.join(_DIR, "vtb_donor_card.pdf")

VTB_CARD_BANKS = [
    "Т-Банк", "Сбербанк", "Альфа-Банк", "Озон Банк",
    "Совкомбанк", "ВТБ", "Яндекс Банк", "Райффайзен Банк", "Газпромбанк",
]

# Sender cards are VTB cards; the genuine corpus sender BIN is 220024.
_SENDER_BIN = "220024"

# Exact layout widths (int dots, right edge 6860) harvested from the 5
# genuine card receipts of 07.11.2026; the SBP table in vtb_generator does
# not contain these clock times.
_CARD_DATE_WIDTH = {
    "07.11.2026, 00:45": 1989,
    "07.11.2026, 00:46": 1992,
    "07.11.2026, 00:48": 1992,
}


def _card_width_dots(tpl: _Tpl, text: str, size: float) -> int:
    """Layout width of a card-receipt value in int dots.

    Dates use the harvested table / the SBP generator's 3-box date model.
    9 pt amounts ('NNN ₽') are measured by openhtmltopdf as TWO inline
    boxes ('NNN ' and '₽', each ceil'ed separately): exact on all 5
    distinct genuine 9 pt amounts (99/111/123/147/236 ₽), where the plain
    aggregate /W model misses by 1 dot on 3 of them.  The 13.5 pt amount
    and every other value follow the aggregate /W model (exact on the
    whole genuine corpus, incl. '12 ₽' at 13.5 pt where splitting
    overshoots by 1 dot).
    """
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}", text):
        return _CARD_DATE_WIDTH.get(text) or _v._date_width_dots(tpl, text)
    if size == 9.0 and re.fullmatch(r"[\d ]+ ₽", text):
        head, _, cur = text.rpartition(" ")
        return (tpl._inline_width_dots(head + " ", size)
                + tpl._inline_width_dots(cur, size))
    return tpl.width_dots(text, size)


def _build_card_block(tpl: _Tpl, block, new_text: str) -> str:
    """One right-aligned BT..ET block, donor-styled, with the card width
    model above (donor calibration cancels model residue)."""
    if new_text == block["text"]:
        return block["body"]
    size = block["size"]
    e_donor = tpl.dots_x(block["rawx"])
    dots = (e_donor + _card_width_dots(tpl, block["text"], size)
            - _card_width_dots(tpl, new_text, size))
    adj = block["rawadj"] or _v._fmt_adj(tpl.tracking(block))
    items = []
    for i, ch in enumerate(new_text):
        item = _v._glyph_item(tpl.char2cid[ch])
        if i < len(new_text) - 1:
            item += adj
        items.append(item)
    return ("BT\n/F1 %s Tf\n1 0 0 1 %s %s Tm\n[%s] TJ\nET"
            % (block["rawsize"], _v.tm_x(dots), block["rawy"],
               " ".join(items)))


def commission(amount_rub, bank: str = None) -> int:
    """VTB card-transfer commission in whole rubles (see module docstring)."""
    b = (bank or "").replace("\u2011", "-").strip()
    if b == "ВТБ":
        return 0
    amt = int(amount_rub)
    # 2%, half-up to whole rubles, minimum 99.
    return max(99, (amt * 20 + 500) // 1000)


def _split_mask(value: str):
    """(first6, last4) from a full PAN, a mask, or a spaced mask."""
    s = re.sub(r"\s+", "", str(value or ""))
    if re.fullmatch(r"\d{6}\*+\d{4}", s):
        return s[:6], s[-4:]
    d = re.sub(r"\D", "", s)
    if 16 <= len(d) <= 19 or len(d) == 10:
        return d[:6], d[-4:]
    return None


def format_card_from(value: str) -> str:
    """Sender card as shown on genuine receipts: '220024******1811'.
    A bare last-4 is completed with the attested VTB sender BIN."""
    s = re.sub(r"\s+", "", str(value or ""))
    if re.fullmatch(r"\d{4}", s):
        return f"{_SENDER_BIN}******{s}"
    parts = _split_mask(value)
    if parts is None:
        raise ValueError("sender card: need a full number or dddddd******dddd mask")
    return f"{parts[0]}******{parts[1]}"


def format_card_to(value: str) -> str:
    """Recipient card as shown on genuine receipts: '2204 32** **** 1187'."""
    parts = _split_mask(value)
    if parts is None:
        raise ValueError("recipient card: need a full number or dddddd******dddd mask")
    first6, last4 = parts
    return f"{first6[:4]} {first6[4:]}** **** {last4}"


def check_text(text: str) -> list:
    """Characters not covered by the 144-char VTB catalog (delegates to the
    SBP generator's catalog check)."""
    return _v.check_text(text or "")


def _discover_card_fields(tpl: _Tpl) -> dict:
    """Map layout roles to value-block indices using label adjacency."""
    labels = {
        "Дата операции": "date", "Номер карты": "card_from",
        "Счет списания": "account", "Имя плательщика": "payer",
        "Карта получателя": "card_to", "Банк получателя": "bank",
        "Комиссия": "commission", "Сумма с комиссией": "total",
        "Сумма операции": "amount",
    }
    fields = {}
    for i, b in enumerate(tpl.blocks):
        role = labels.get(b["text"])
        if role and i + 1 < len(tpl.blocks):
            fields[role] = i + 1
    return fields


def generate(payer: str, card_from: str, card_to: str, bank: str,
             amount_rub, account: str = None, dt: datetime = None,
             commission_rub=None, template: str = None,
             meta_out: dict = None, created_dt: datetime = None,
             doc_id: str = None) -> bytes:
    """Build one 'Перевод на карту' receipt.

    card_from/card_to accept a full PAN or a dddddd******dddd mask (the
    sender may also be a bare last-4).  commission_rub pins the commission
    (byte-replica mode); by default the VTB tariff is applied and the total
    is amount + commission.  created_dt/doc_id pin /CreationDate and the
    trailer /ID exactly (byte-replica of a known genuine receipt).
    """
    amount = int(amount_rub)
    if amount <= 0:
        raise ValueError("amount must be positive")
    bank = (bank or "").replace("-", "\u2011")
    comm = commission(amount, bank) if commission_rub is None else int(commission_rub)
    total = amount + comm
    card_from_s = format_card_from(card_from)
    card_to_s = format_card_to(card_to)
    acct_digits = re.sub(r"\D", "", str(account or ""))
    account_s = f"*{acct_digits[-4:]}" if len(acct_digits) >= 4 else "*0298"
    # VTB receipts typeset hyphens as U+2011 (non-breaking hyphen), same
    # as in the SBP generator's bank/phone/message handling.
    payer = (payer or "").strip().replace("-", "\u2011")

    now_msk = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    lag_s = None
    if dt is None:
        # Same immediate-receipt clock model as the SBP generator: the
        # CreationDate stays in the displayed minute (4/5 genuine card
        # receipts; the 5th crosses into the next minute), never future.
        if now_msk.second >= 13:
            cd_sec = _rnd.randint(max(13, now_msk.second - 10), now_msk.second)
            lag = _rnd.randint(max(6, cd_sec - 51), min(25, cd_sec - 7))
            op_dt = now_msk.replace(second=cd_sec - lag + 1, microsecond=0)
            lag_s = lag - 1  # CreationDate lands on the generation instant
        else:
            prev = now_msk - timedelta(minutes=1)
            op_dt = prev.replace(second=_rnd.randint(0, 52), microsecond=0)
    elif dt.tzinfo is not None:
        op_dt = dt.astimezone(timezone(timedelta(hours=3))).replace(tzinfo=None)
    else:
        op_dt = dt
    display = op_dt.strftime("%d.%m.%Y, %H:%M")
    amount_s = format_amount(amount)
    comm_s = format_amount(comm)
    total_s = format_amount(total)

    path = template or _TEMPLATE
    if not os.path.exists(path):
        raise ValueError(f"VTB card template not found: {path}")
    tpl = _get_tpl(path)
    fields = _discover_card_fields(tpl)
    missing = [r for r in ("date", "card_from", "account", "payer", "card_to",
                           "bank", "commission", "total", "amount")
               if r not in fields]
    if missing:
        raise ValueError(f"card template layout not recognised, missing: {missing}")

    newvals = {
        "date": display, "card_from": card_from_s, "account": account_s,
        "payer": payer, "card_to": card_to_s, "bank": bank,
        "commission": comm_s, "total": total_s, "amount": amount_s,
    }
    if not tpl.covers(*newvals.values()):
        bad = check_text("".join(newvals.values()))
        raise ValueError(f"template does not cover glyphs: {''.join(sorted(set(bad)))}")

    plan = {fields[role]: text for role, text in newvals.items()}
    content = tpl.content
    repls = []
    for idx, new_text in plan.items():
        b = tpl.blocks[idx]
        repls.append((b["span"][0], b["span"][1],
                      _build_card_block(tpl, b, new_text)))
    for s, e, new_block in sorted(repls, reverse=True):
        content = content[:s] + new_block + content[e:]

    return _assemble(tpl, content, op_dt, meta_out, {
        "payer": payer, "card_from": card_from_s, "card_to": card_to_s,
        "bank": bank, "amount": amount_s, "commission": comm_s,
        "total": total_s, "display": display, "account": account_s,
        "amount_rub": amount, "commission_rub": comm, "total_rub": total,
    }, lag_s=lag_s, created_dt=created_dt, doc_id=doc_id)
