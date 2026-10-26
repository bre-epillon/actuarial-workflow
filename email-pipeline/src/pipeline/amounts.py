"""Deterministic reading of amounts as written in emails, to check what agents extract.

Agents report each amount twice: `as_written` (verbatim, e.g. "EUR 12.5m") and
`value` (12500000). check_amount() re-reads the text and flags any mismatch,
so a mis-scaled or mistyped number never passes silently (CLAUDE.md rule 1).
It errs on the side of flagging: `as_written` must hold exactly one amount.
"""
from __future__ import annotations

import math
import re

MULTIPLIERS = {
    "k": 1e3, "th": 1e3, "tsd": 1e3, "thousand": 1e3, "thousands": 1e3,
    "m": 1e6, "mm": 1e6, "mn": 1e6, "mln": 1e6, "mio": 1e6, "million": 1e6, "millions": 1e6,
    "bn": 1e9, "b": 1e9, "mrd": 1e9, "billion": 1e9, "billions": 1e9,
}
SYMBOLS = {"€": "EUR", "£": "GBP", "¥": "JPY"}
ISO_CODES = {
    "AED", "ARS", "AUD", "BGN", "BRL", "CAD", "CHF", "CLP", "CNY", "COP", "CZK", "DKK", "EGP", "EUR",
    "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "ISK", "JPY", "KRW", "KZT", "MAD", "MXN", "MYR", "NGN",
    "NOK", "NZD", "PEN", "PHP", "PKR", "PLN", "QAR", "RON", "RSD", "RUB", "SAR", "SEK", "SGD", "THB",
    "TRY", "TWD", "UAH", "USD", "VND", "ZAR",
}
_NUMBER = re.compile(
    r"(?P<sign>[-−]\s*)?"
    r"(?P<num>\d{1,3}(?:[   '’]\d{3})+(?:[.,]\d+)?|\d[\d.,]*\d|\d)"  # 12 500 000 | 12,500.00 | 7
    r"(?:\s*(?P<mult>" + "|".join(sorted(MULTIPLIERS, key=len, reverse=True)) + r")(?![a-z]))?"
    r"(?P<pct>\s*%)?",
    re.IGNORECASE,
)


def _numbers(text: str, percent: bool) -> list[float]:
    """The amounts (percent=False) or percentages (percent=True) written in `text`."""
    out = []
    for m in _NUMBER.finditer(text):
        if bool(m.group("pct")) != percent:
            continue
        raw = re.sub(r"[   '’]", "", m.group("num"))
        value = _read(raw, has_multiplier=bool(m.group("mult")))
        if value is None:
            continue
        factor = MULTIPLIERS[m.group("mult").lower()] if m.group("mult") else 1.0
        out.append((-1.0 if m.group("sign") else 1.0) * value * factor)
    return out


def amount_readings(text: str) -> list[float]:
    return _numbers(text, percent=False)


def _read(raw: str, has_multiplier: bool) -> float | None:
    """One reading per number. A lone separator before exactly three digits is a
    thousands separator ('375,000', '12.500'), except before a multiplier ('1.234m')
    or after a leading zero ('0,125')."""
    dots, commas = raw.count("."), raw.count(",")
    if dots and commas:  # the last separator is the decimal one
        dec, thou = (".", ",") if raw.rfind(".") > raw.rfind(",") else (",", ".")
        raw = raw.replace(thou, "").replace(dec, ".")
    elif dots or commas:
        sep = "." if dots else ","
        if raw.count(sep) > 1:  # 12,500,000 or 12.500.000
            raw = raw.replace(sep, "")
        else:
            head, tail = raw.split(sep)
            thousands = len(tail) == 3 and head != "0" and not has_multiplier
            raw = head + tail if thousands else f"{head}.{tail}"
    try:
        return float(raw)
    except ValueError:
        return None


def currency_in(text: str) -> str | None:
    """The currency written in `text`, if unambiguous ('$' is not: USD, CAD, AUD...)."""
    for code in re.findall(r"\b[A-Z]{3}\b", text):
        if code in ISO_CODES:
            return code
    for symbol, code in SYMBOLS.items():
        if symbol in text:
            return code
    return None


def check_amount(value: float, as_written: str, currency: str | None = None) -> str | None:
    """None if `value` (and `currency`) match the verbatim text, else what is wrong."""
    readings = amount_readings(as_written)
    if not readings:
        return f"no number found in {as_written!r}"
    if len(readings) > 1:
        return f"{as_written!r} contains {len(readings)} numbers; it must be only the amount"
    if not math.isclose(value, readings[0], rel_tol=1e-9, abs_tol=0.005):
        return f"value {value:,.2f} does not match {as_written!r} (reads as {readings[0]:,.2f})"
    written = currency_in(as_written)
    if written and currency and written != currency:
        return f"currency {currency} does not match {as_written!r}"
    return None


def check_percent(percent: float, as_written: str) -> str | None:
    """None if `percent` matches the single percentage in the verbatim text ('7.5%', '7,50 %')."""
    readings = _numbers(as_written, percent=True)
    if len(readings) != 1:
        return f"{as_written!r} must contain exactly one percentage (found {len(readings)})"
    if not math.isclose(percent, readings[0], rel_tol=1e-9, abs_tol=1e-9):
        return f"{percent}% does not match {as_written!r}"
    return None


def format_amount(value: float, currency: str | None = None, signed: bool = False) -> str:
    text = f"{value:+,.0f}" if signed else f"{value:,.0f}"
    return f"{currency} {text}" if currency else text
