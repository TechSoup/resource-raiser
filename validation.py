"""Structural acceptance checks run before residual LLM adjudication."""
from __future__ import annotations

import re
from typing import Any, Callable

from domain import Check, QueryIntent, Validation

SUPPRESSION_SENTINELS = {-999999999, -888888888, -666666666, -555555555, -333333333,
                         -222222222, -111111111}


def _walk(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk(v)
    else:
        yield value


def _sentinel(data):
    for value in _walk(data):
        try:
            if float(value) in SUPPRESSION_SENTINELS:
                return str(value)
        except (TypeError, ValueError):
            continue
    return None


def _question_unit(intent):
    q = intent.question.lower()
    if re.search(r"\b(percent|percentage|rate|share|ratio)\b|%", q):
        return "percent"
    if re.search(r"\b(per share|eps)\b", q):
        return "per-share"
    return None


def _data_unit(data):
    unit = str(data.get("unit") or data.get("units") or "").lower()
    if unit in ("%", "percent", "percentage", "pure"):
        return "percent"
    if "/share" in unit or "per share" in unit:
        return "per-share"
    return unit or None


def _question_currency(intent):
    q = intent.question.lower()
    if re.search(r"\b(usd|u\.s\. dollars?|dollars?)\b|\$", q):
        return "USD"
    if re.search(r"\b(eur|euros?)\b|€", q):
        return "EUR"
    if re.search(r"\b(gbp|pounds? sterling)\b|£", q):
        return "GBP"
    return None


def _question_grain(intent):
    q = intent.question.lower()
    for word, grain in (("counties", "county"), ("county", "county"), ("states", "state"),
                        ("state", "state"), ("cities", "city"), ("city", "city")):
        if re.search(rf"\b{word}\b", q):
            return grain
    return None


def structural(intent: QueryIntent, data: dict[str, Any], source_rule: Callable | None = None) -> Validation:
    checks = []
    if not isinstance(data, dict) or not data:
        return Validation(False, [Check("response", "fail", "empty or non-object response")],
                          "empty or non-object response")

    sentinel = _sentinel(data)
    checks.append(Check("sentinel", "fail" if sentinel else "pass",
                        f"suppressed/missing value sentinel {sentinel}" if sentinel else ""))
    if sentinel:
        return Validation(False, checks, checks[-1].reason)

    expected_unit, actual_unit = _question_unit(intent), _data_unit(data)
    if expected_unit and actual_unit:
        ok = expected_unit == actual_unit
        checks.append(Check("unit", "pass" if ok else "fail",
                            "" if ok else f"asked for {expected_unit}, got {actual_unit}"))
        if not ok:
            return Validation(False, checks, checks[-1].reason)
    elif expected_unit:
        checks.append(Check("unit", "inconclusive", "requested unit is absent from the response"))
    else:
        checks.append(Check("unit", "pass", "question does not constrain the unit"))

    expected_currency = _question_currency(intent)
    actual_currency = str(data.get("currency") or "").upper() or None
    if expected_currency and actual_currency:
        ok = expected_currency == actual_currency
        checks.append(Check("currency", "pass" if ok else "fail",
                            "" if ok else f"asked for {expected_currency}, got {actual_currency}"))
        if not ok:
            return Validation(False, checks, checks[-1].reason)
    elif expected_currency:
        checks.append(Check("currency", "inconclusive", "requested currency is absent from the response"))
    else:
        checks.append(Check("currency", "pass", "question does not constrain currency"))

    expected_grain, actual_grain = _question_grain(intent), data.get("grain")
    if expected_grain and actual_grain:
        ok = expected_grain == str(actual_grain).lower()
        checks.append(Check("grain", "pass" if ok else "fail",
                            "" if ok else f"asked for {expected_grain} grain, got {actual_grain}"))
        if not ok:
            return Validation(False, checks, checks[-1].reason)
    elif expected_grain:
        checks.append(Check("grain", "inconclusive", "requested grain is absent from the response"))
    else:
        checks.append(Check("grain", "pass", "question does not constrain grain"))

    requested = intent.period
    actual = str(data.get("period") or data.get("year") or data.get("fy") or "")
    if requested not in ("", "latest") and actual:
        want = re.sub(r"\D", "", requested)
        got = re.sub(r"\D", "", actual)
        ok = not want or not got or want == got
        checks.append(Check("period", "pass" if ok else "inconclusive",
                            "" if ok else f"requested {requested}; source returned {actual}"))
    else:
        checks.append(Check("period", "pass", "latest or unstated period imposes no exact match"))

    # A stable key is decisive when both sides expose one. Names are not treated as keys.
    requested_entity = data.get("requested_entity")
    returned_entity = data.get("entity")
    requested_entity = requested_entity if isinstance(requested_entity, dict) else {}
    returned_entity = returned_entity if isinstance(returned_entity, dict) else {}
    expected_keys = {str(v) for k, v in requested_entity.items()
                     if k in ("ein", "cik", "fips", "id") and v}
    actual_keys = {str(v) for k, v in returned_entity.items()
                   if k in ("ein", "cik", "fips", "id") and v}
    if expected_keys and actual_keys:
        ok = bool(expected_keys & actual_keys)
        checks.append(Check("entity-key", "pass" if ok else "fail",
                            "" if ok else "resolved entity key differs from returned entity"))
        if not ok:
            return Validation(False, checks, checks[-1].reason)
    elif intent.entity:
        actual_name = str(data.get("company") or data.get("organization") or
                          data.get("place") or data.get("entity_name") or
                          (data.get("entity") if isinstance(data.get("entity"), str) else "") or "").lower()
        wanted = set(re.findall(r"[a-z0-9]+", intent.entity.lower()))
        got = set(re.findall(r"[a-z0-9]+", actual_name))
        if wanted and wanted <= got:
            checks.append(Check("entity-name", "pass", "returned entity contains the requested name"))
        else:
            checks.append(Check("entity-key", "inconclusive", "named entity has no comparable canonical keys"))
    else:
        checks.append(Check("entity-key", "pass", "question does not constrain an entity"))

    # Measure equivalence is intentionally not guessed here. Near-collisions such as Form 990
    # total revenue and us-gaap Revenues are precisely why residual semantic adjudication exists.
    actual_measure = str(data.get("measure") or data.get("metric") or "").strip().lower()
    requested_measure = str(intent.measure or "").strip().lower()
    if not requested_measure:
        checks.append(Check("measure", "pass", "question does not constrain a measure"))
    elif actual_measure:
        want = set(re.findall(r"[a-z0-9]+", requested_measure)) - {"the", "of", "for", "total"}
        got = set(re.findall(r"[a-z0-9]+", actual_measure)) - {"the", "of", "for", "total"}
        if want and want <= got:
            checks.append(Check("measure", "pass", "returned measure contains the requested terms"))
        else:
            checks.append(Check("measure", "inconclusive", "measure requires semantic disambiguation"))
    else:
        checks.append(Check("measure", "inconclusive", "response does not label its measure"))

    if source_rule:
        result = source_rule(intent, data)
        if result:
            checks.extend(result)
            failed = next((c for c in result if c.status == "fail"), None)
            if failed:
                return Validation(False, checks, failed.reason)

    residual = any(c.status == "inconclusive" for c in checks)
    return Validation(True, checks, residual_semantic_check=residual)
