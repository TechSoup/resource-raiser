"""Deterministic prose for validated evidence types; unknown types explicitly fall back to an LLM."""
from __future__ import annotations

import decimal
from domain import Answer, Evidence


def _display(v):
    if isinstance(v, (float, decimal.Decimal)):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _entity(e):
    entity = e.entity or {}
    return (entity.get("label") or entity.get("name") or e.payload.get("company") or
            e.payload.get("organization") or e.payload.get("place") or "")


def _measure(e):
    text = (e.measure or e.payload.get("measure") or e.payload.get("metric") or "the value").strip()
    # A source that selected one series out of many names it in `series`. Without it the answer
    # to "Japanese yen to dollar exchange rate" reads "Exchange rate is 162.38", which is
    # indistinguishable from the euro answer and from a wrong currency.
    picked = e.payload.get("series")
    if isinstance(picked, str) and picked.strip() and picked.strip().lower() not in text.lower():
        text = f"{text} ({picked.strip()})"
    # User language should already be preferred by Evidence. This only prevents raw all-caps source
    # labels from becoming shouty answer prose when no user measure was available.
    return text.capitalize() if text.isupper() else text


def _value_with_unit(value, e, explicit=False):
    if explicit:
        return str(value)
    unit = (e.unit or "").lower()
    if unit in ("%", "percent", "percentage"):
        return f"{value}%"
    if (e.currency or "").upper() == "USD" and unit in ("usd", "currency"):
        return f"${value}"
    suffix = " ".join(dict.fromkeys(x for x in (e.currency, e.unit) if x and x.lower() not in
                                    ("percent", "percentage", "%")))
    return f"{value}{(' ' + suffix) if suffix else ''}"


def render(e: Evidence) -> Answer | None:
    d, src = e.payload, e.source
    if e.kind == "point" and e.value is not None:
        explicit = d.get("value_display")
        value = _value_with_unit(explicit or _display(e.value), e, bool(explicit))
        period = f" for {e.period}" if e.period else ""
        subject = f"{_entity(e)}’s {_measure(e)}" if _entity(e) else _measure(e)
        return Answer(f"{subject[0].upper() + subject[1:]}{period} is {value}, according to {src}.",
                      "point", e.kind)
    if e.kind == "status" and isinstance(e.value, bool):
        label = _entity(e) or "The organization"
        criterion = _measure(e)
        return Answer(f"{'Yes' if e.value else 'No'} — {label} {'meets' if e.value else 'does not meet'} the {criterion} criterion, according to {src}.",
                      "status", e.kind)
    if e.kind in ("ranking", "threshold"):
        rows = d.get("ranking") or []
        if e.kind == "threshold":
            n = d.get("matches", len(rows))
            return Answer(f"{n} entities match {d.get('threshold_display') or d.get('threshold') or 'the requested threshold'}, according to {src}.",
                          "threshold", e.kind)
        if rows:
            top = d.get("top") or rows[0]
            top_value = top.get("value_display") or _value_with_unit(_display(top.get("value")), e)
            return Answer(f"{top.get('label')} ranks first by {_measure(e)}, at {top_value}, according to {src}.",
                          "ranking", e.kind)
    if e.kind == "timeseries":
        series = d.get("series") or []
        if series:
            first, last = series[0], series[-1]
            change = f", a change of {_display(d['change'])}" if d.get("change") is not None else ""
            subject = f"{_entity(e)}’s {_measure(e)}" if _entity(e) else _measure(e)
            first_value = first.get("value_display") or _value_with_unit(_display(first.get("value")), e)
            last_value = last.get("value_display") or _value_with_unit(_display(last.get("value")), e)
            return Answer(f"{subject[0].upper() + subject[1:]} moved from {first_value} in {first.get('period')} to {last_value} in {last.get('period')}{change}, according to {src}.",
                          "timeseries", e.kind)
    if e.kind == "grants_made":
        return Answer(f"{d.get('funder')} granted {d.get('total_granted_display')} across {d.get('recipient_count')} recipients in the IRS 990 grant data.",
                      "grants-made", e.kind)
    if e.kind == "funded_by":
        return Answer(f"{d.get('recipient')} received {d.get('total_received_display')} from {d.get('funder_count')} funders in the IRS 990 grant data.",
                      "funded-by", e.kind)
    if e.kind == "geo_flow":
        return Answer(f"{d.get('total_display')} flowed from {d.get('from_state')} to {d.get('to_state')} across {d.get('grant_count')} grants in the IRS 990 grant data.",
                      "geo-flow", e.kind)
    if e.kind == "overview":
        return Answer(f"The IRS 990 grant graph contains {d.get('grant_count'):,} grants totaling {d.get('total_display')}, averaging {d.get('avg_grant_display')}, across {d.get('funder_count'):,} funders and {d.get('recipient_count'):,} recipients.",
                      "grant-overview", e.kind)
    return None


def kind_of(data):
    direction = data.get("direction")
    if direction in ("grants_made", "funded_by", "geo_flow", "overview"):
        return direction
    # A LIST of observations, not merely a truthy `series`. Treasury leaves use `series` for
    # the name of the series they selected ("Euro Zone-Euro"), so a truthiness test classified
    # a point answer as a timeseries and the timeseries renderer then indexed into a string.
    series = data.get("series")
    if isinstance(series, (list, tuple)) and series:
        return "timeseries"
    if data.get("ranking") is not None:
        return "threshold" if data.get("matches") is not None or data.get("threshold") else "ranking"
    value = data.get("value", data.get("status"))
    if isinstance(value, bool):
        return "status"
    if value is not None:
        return "point"
    return "complex"
