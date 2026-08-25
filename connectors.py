"""Execution boundary for generic and specialized Resource Raiser sources.

Connectors do not replace empirical backtracking. They make each empirical execution observable,
run structural/source validation, and admit only accepted responses as Evidence.
"""
from __future__ import annotations

import asyncio, decimal, inspect, json, os, re, time
from pathlib import Path
from typing import Callable

from domain import Attempt, Check, Evidence, QueryIntent
import renderers
import validation


class Rejected(Exception):
    def __init__(self, reason, attempt):
        super().__init__(reason)
        self.attempt = attempt


def _value(data):
    return data.get("value", data.get("status", data.get("value_usd", data.get("total_usd"))))


def _status_value(intent, data):
    """Select the boolean that answers the question; never substitute a friendly status label."""
    q = f"{intent.measure} {intent.question}".lower()
    choices = (("501(c)(3)", "is_501c3"), ("501c3", "is_501c3"),
               ("deduct", "contributions_deductible"), ("actively filing", "actively_filing"),
               ("eligible", "eligible_for_nonprofit_programs"), ("active", "actively_filing"))
    for needle, key in choices:
        if needle in q and isinstance(data.get(key), bool):
            return data[key]
    for key in ("status", "value", "is_501c3", "contributions_deductible",
                "eligible_for_nonprofit_programs", "actively_filing"):
        if isinstance(data.get(key), bool):
            return data[key]
    return _value(data)


def _evidence(intent, hit, data, attempt):
    # Compatibility metadata belongs on admitted evidence, even when the upstream API omits it.
    # Descriptor capabilities are declarations; returned fields win when both are present.
    try:
        import driver, planner
        fm = driver.frontmatter(hit.get("identifier") or "") or {}
        cap = planner.capabilities(hit.get("identifier") or "") or {}
    except Exception:
        fm, cap = {}, {}
    entity = data.get("entity") if isinstance(data.get("entity"), dict) else attempt.entity
    unit = data.get("unit") or data.get("units") or fm.get("unit")
    currency = data.get("currency")
    if not currency and isinstance(unit, str) and len(unit) == 3 and unit.isalpha():
        currency = unit.upper()
    kind = "status" if intent.operation == "status" else renderers.kind_of(data)
    value = _status_value(intent, data) if kind == "status" else _value(data)
    return Evidence(kind=kind, source=data.get("source") or hit.get("title") or hit.get("publisher") or "",
                    identifier=hit.get("identifier") or "", payload=data, entity=entity,
                    measure=intent.measure or data.get("measure") or data.get("metric"),
                    value=value, unit=unit,
                    currency=currency, period=str(data.get("period") or data.get("year") or "") or None,
                    grain=data.get("grain") or cap.get("grain"),
                    period_basis=data.get("period_basis") or fm.get("periodType"),
                    entity_key=data.get("entity_key") or (cap.get("key") or {}).get("kind"),
                    quantity_kind=data.get("quantity_kind") or fm.get("quantityKind"),
                    population_complete=(data.get("complete") if "complete" in data else
                                         (cap.get("population") or {}).get("complete")),
                    provenance={"source_document": hit.get("identifier"),
                                "retrieved_at": attempt.started_at},
                    transformations=list(data.get("transformations") or []),
                    warnings=list(data.get("alignment_warnings") or []))


class Connector:
    name = "generic-okf"

    def capabilities(self, identifier):
        import planner
        return planner.capabilities(identifier)

    def resolve(self, binding):
        return binding

    def source_checks(self, intent, data):
        return []

    def normalize(self, intent, hit, data):
        return data

    def validate(self, intent, data):
        return validation.structural(intent, data, self.source_checks)

    def execute(self, intent: QueryIntent, attempt: Attempt, hit: dict, executor: Callable,
                adjudicator: Callable | None = None) -> Evidence:
        started = time.monotonic()
        try:
            data = executor()
            attempt.raw = data
            data = self.normalize(intent, hit, data)
            verdict = self.validate(intent, data)
            attempt.validation = verdict
            if not verdict.accepted:
                attempt.outcome, attempt.reason = "rejected", verdict.reason
                raise Rejected(verdict.reason, attempt)
            if verdict.residual_semantic_check and adjudicator:
                ok, why = adjudicator(data, verdict)
                if not ok:
                    attempt.outcome, attempt.reason = "rejected", why
                    raise Rejected(why, attempt)
            attempt.outcome = "accepted"
            evidence = _evidence(intent, hit, data, attempt)
            _record_fixture(intent, attempt, evidence)
            return evidence
        except Rejected:
            raise
        except Exception as e:
            attempt.outcome, attempt.reason = "error", str(e)[:300]
            raise
        finally:
            attempt.duration_ms = round((time.monotonic() - started) * 1000)

    async def execute_async(self, intent: QueryIntent, attempt: Attempt, hit: dict,
                            executor: Callable, adjudicator: Callable | None = None) -> Evidence:
        """Await the I/O boundaries while retaining exactly the synchronous admission rules."""
        started = time.monotonic()
        try:
            data = executor()
            if inspect.isawaitable(data):
                data = await data
            attempt.raw = data
            data = self.normalize(intent, hit, data)
            verdict = self.validate(intent, data)
            attempt.validation = verdict
            if not verdict.accepted:
                attempt.outcome, attempt.reason = "rejected", verdict.reason
                raise Rejected(verdict.reason, attempt)
            if verdict.residual_semantic_check and adjudicator:
                decision = adjudicator(data, verdict)
                if inspect.isawaitable(decision):
                    decision = await decision
                ok, why = decision
                if not ok:
                    attempt.outcome, attempt.reason = "rejected", why
                    raise Rejected(why, attempt)
            attempt.outcome = "accepted"
            evidence = _evidence(intent, hit, data, attempt)
            _record_fixture(intent, attempt, evidence)
            return evidence
        except Rejected:
            raise
        except asyncio.CancelledError:
            attempt.outcome, attempt.reason = "error", "query cancelled"
            raise
        except Exception as exc:
            attempt.outcome, attempt.reason = "error", str(exc)[:300]
            raise
        finally:
            attempt.duration_ms = round((time.monotonic() - started) * 1000)


class SECConnector(Connector):
    name = "sec"

    def source_checks(self, intent, data):
        checks = []
        concept = str(data.get("concept") or "")
        checks.append(Check("sec-concept", "pass" if concept else "inconclusive",
                            "" if concept else "response does not identify the concept actually used"))
        return checks


class BigQueryConnector(Connector):
    name = "bigquery"

    def source_checks(self, intent, data):
        if data.get("ranking") is not None and data.get("complete") is False:
            return [Check("population-coverage", "inconclusive", "result is a bounded population window")]
        return [Check("population-coverage", "pass")]


class GrantsConnector(Connector):
    name = "irs-grants"

    def source_checks(self, intent, data):
        if data.get("matched_by") == "name":
            return [Check("entity-match", "inconclusive", "grant entity matched by name, not EIN")]
        return [Check("entity-match", "pass")]


def _numeric(value):
    """Provider JSON commonly encodes exact numbers as strings; normalize without losing integers."""
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value.strip()):
        return value
    number = decimal.Decimal(value.strip())
    return int(number) if number == number.to_integral_value() else float(number)


class CensusConnector(Connector):
    name = "census"

    def normalize(self, intent, hit, data):
        out = dict(data)
        out["value"] = _numeric(out.get("value"))
        # ACS profile titles explicitly identify percentage measures. `E` alone is insufficient:
        # profile estimate columns contain both counts and percentages depending on the row.
        try:
            import driver
            fm = driver.frontmatter(hit.get("identifier") or "") or {}
        except Exception:
            fm = {}
        variable = str(out.get("variable") or fm.get("variable") or "").upper()
        official_title = str(fm.get("title") or "").upper()
        if variable.endswith("PE") or official_title.startswith(("PERCENT", "PERCENTAGE")):
            out["unit"] = "%"
        return out


class TreasuryConnector(Connector):
    name = "treasury"

    def normalize(self, intent, hit, data):
        out = dict(data)
        out["value"] = _numeric(out.get("value"))
        try:
            import driver
            fm = driver.frontmatter(hit.get("identifier") or "") or {}
        except Exception:
            fm = {}
        field = str(fm.get("tfield") or "").lower()
        if field.endswith("_amt"):
            out["unit"] = out["currency"] = "USD"
        elif field.endswith(("_pct", "_percent")):
            out["unit"] = "%"
        elif field.endswith(("_cnt", "_count")):
            out["unit"] = "count"
        return out


GENERIC = Connector()
SEC = SECConnector()
BIGQUERY = BigQueryConnector()
GRANTS = GrantsConnector()
CENSUS = CensusConnector()
TREASURY = TreasuryConnector()


def for_hit(hit):
    ident = hit.get("identifier") or ""
    if "/sec-edgar/" in ident or "/sec-bq/" in ident:
        return BIGQUERY if ident.startswith("sources/sec-bq/") else SEC
    if ident.startswith("sources/irs-grants/"):
        return GRANTS
    if ident.startswith("sources/census/"):
        return CENSUS
    if ident.startswith("sources/treasury/"):
        return TREASURY
    if ident.startswith("sources/") and ident.split("/", 2)[1].endswith("-bq"):
        return BIGQUERY
    return GENERIC


def _record_fixture(intent, attempt, evidence):
    root = os.getenv("RR_RECORD_FIXTURES", "").strip()
    if not root:
        return
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    key = __import__("hashlib").sha256(
        f"{intent.question}|{evidence.identifier}".encode()).hexdigest()[:16]
    payload = {"intent": intent.to_dict(), "attempt": attempt.to_dict(include_raw=True),
               "evidence": evidence.to_dict()}
    tmp = path / f".{key}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path / f"{key}.json")
