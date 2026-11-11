"""
MTS/EXI-BANK statement generator — wrapper around the original byte-exact generator.

There is intentionally NO PyMuPDF/helv fallback: the built-in ``helv`` font
has no Cyrillic, so a fallback PDF would silently be a broken statement
(dots/garbage instead of text) while still returning HTTP 200. If the
original generator is unavailable or fails, this module raises loudly and
the HTTP layer returns an error instead of a bogus PDF.
"""

import inspect
import os
import sys
import logging

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))

# Try to import the original byte-exact generator
_original_generate = None
_original_import_error = None
try:
    # Set donor path before importing
    donor_path = os.path.join(_DIR, "exi_donor.pdf")
    if os.path.exists(donor_path):
        os.environ.setdefault("EXI_DONOR", donor_path)

    # Add pdf-service dir to path for the catalog
    if _DIR not in sys.path:
        sys.path.insert(0, _DIR)

    from exi_statement_core import generate_statement as _orig_gen
    _original_generate = _orig_gen
    logger.info("EXI-BANK original generator loaded OK")
except Exception as e:  # keep the service up; generation itself fails loudly
    _original_import_error = e
    logger.warning("EXI-BANK original generator not available: %s", e)


def _fix_account_key(acc: str) -> str:
    """Recompute the CBR check digit (position 9) for EXI-Bank BIK 044030889.

    No-op for genuine accounts (their digit already satisfies the check);
    repairs accounts assembled from a template + foreign tail digits.
    OPT-IN ONLY — see generate_mts_statement.
    """
    if not isinstance(acc, str) or len(acc) != 20 or not acc.isdigit():
        return acc
    for d in "0123456789":
        cand = acc[:8] + d + acc[9:]
        s = "889" + cand
        if sum((int(ch) * (7, 1, 3)[i % 3]) % 10 for i, ch in enumerate(s)) % 10 == 0:
            return cand
    return acc


def _coerce_bool(value) -> bool:
    """Explicit-flag coercion: only true/True/'true'/'1'/'yes' enable a flag."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def generate_mts_statement(data: dict) -> bytes:
    """Generate EXI-BANK statement PDF from request data.

    The account-number check-digit repair (`_fix_account_key`) is OPT-IN:
    pass ``fix_account_key=True`` in *data* to enable it. By default the
    account is passed through verbatim, because genuine MTS-Bank/EXI-BANK
    statements exist that do NOT satisfy the CBR check-digit rule for BIK
    044030889 (e.g. 40914810700002139661) — auto-repair would silently
    mutate such genuine accounts and break byte-exact reproduction.
    This matches the bot-side ``statement_generator.generate_statement``,
    which also defaults to ``fix_account_key=False``.

    Raises:
        RuntimeError: if the byte-exact generator is unavailable or fails.
            Never returns a PyMuPDF/helv fallback PDF.
    """
    if _original_generate is None:
        raise RuntimeError(
            "EXI-BANK original generator not available"
            + (f": {_original_import_error}" if _original_import_error else "")
            + "; refusing to emit a PyMuPDF/helv fallback statement"
        )

    ops = data.get("operations", [])
    client_name = data.get("client_name", "Клиент")
    card = data.get("card", "220383*7826")
    raw_account = data.get("account_number", "40914810500002158766")
    fix_key = _coerce_bool(data.get("fix_account_key", False))
    # The core generator applies the opt-in repair itself when supported;
    # applying it here as well would be idempotent, so delegate to the core
    # to keep bot/pdf-service behaviour in exactly one place. For an older
    # core without the keyword, apply it here (same opt-in semantics).
    account = raw_account
    balance = data.get("balance", "0.00 RUR")
    period_from = data.get("period_from", "01.11.2026")
    period_to = data.get("period_to", "01.11.2026")
    stmt_date = data.get("stmt_date", period_to)

    # Convert operations to (date, amount_text, description) tuples
    rows = []
    for op in ops:
        rows.append((
            op.get("date", ""),
            op.get("amount_text", "0 RUR"),
            op.get("description", "Операция"),
        ))

    # Calculate totals from operations
    inc = 0.0
    exp = 0.0
    for op in ops:
        amt_text = op.get("amount_text", "0")
        v_str = amt_text.replace("RUR", "").replace(" ", "").replace("\xa0", "").strip()
        try:
            v = float(v_str)
        except (ValueError, TypeError):
            continue
        if v >= 0:
            inc += v
        else:
            exp += abs(v)

    # Genuine statements print totals Java-double style ("+ 300.0 RUR"),
    # not "%g" ("+ 300 RUR") and not 2-decimals ("+ 300.50 RUR").
    total_in = f"+ {repr(float(round(inc, 2)))} RUR"
    total_out = f"- {repr(float(round(exp, 2)))} RUR"

    kwargs = dict(
        name=client_name,
        card=card,
        account=account,
        balance=balance,
        operations=rows,
        holds=(),
        stmt_date=stmt_date,
        period_from=period_from,
        period_to=period_to,
        total_in=total_in,
        total_out=total_out,
    )
    try:
        if "fix_account_key" in inspect.signature(_original_generate).parameters:
            kwargs["fix_account_key"] = fix_key
        elif fix_key:
            kwargs["account"] = _fix_account_key(raw_account)
        result = _original_generate(**kwargs)
    except Exception:
        logger.exception("Original EXI-BANK generator failed; no fallback will be emitted")
        raise

    if not isinstance(result, (bytes, bytearray)) or not bytes(result).startswith(b"%PDF"):
        raise RuntimeError("EXI-BANK generator returned a non-PDF result; refusing to pass it through")
    result = bytes(result)
    logger.info("MTS statement generated (original): %d bytes, %d ops", len(result), len(rows))
    return result
