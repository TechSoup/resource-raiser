#!/usr/bin/env python3
"""Generic BigQuery population source — ranking / filtering / aggregation over a public-dataset table.

BigQuery public datasets are SQL, i.e. SERVER-AGGREGATE capabilities: they can order, filter and
count across a WHOLE population, which our per-entity REST sources cannot. Rather than a module per
table, each OKF leaf carries a `bq:` config naming the table, the value column, and the entity
column (plus how to turn that entity into a readable name):

    bq:
      table: bigquery-public-data.census_bureau_acs.county_2018_5yr
      field: median_income
      entity_field: geo_id
      entity_kind: fips
      name_table: bigquery-public-data.geo_us_boundaries.counties   # SQL join for names…
      name_key: geo_id
      name_field: county_name
      # …or, for EIN-keyed 990 data, resolve names afterward via ProPublica:
      # name_via: propublica

Two table SHAPES are handled by the same config:

  WIDE   one row per entity, the measure IS a column (census county_2018_5yr.median_income,
         irs_990.totrevenue). `field` names that column.

  LONG   one row per (entity, measure, period) fact, the value lives in a single `value`
         column and which measure it is comes from a filter (SEC sec_quarterly_financials:
         value in `value`, measure chosen by `measure_tag`). Declared by adding to `bq:`:
           value_field: value            # the numeric column
           group_agg: MAX                 # collapse many rows per entity -> one (MAX/SUM/…)
           filter: "measure_tag IN (…) AND form='10-K' AND …"   # picks the measure + de-noises
           value_max / value_min: 1e12    # sanity bounds (drop $10T filing typos)
         An entity may appear on many rows (quarters, restatements); `group_agg` picks one
         value per entity before ranking, so the population is companies, not filings.

CREDENTIAL-GATED: active only when GOOGLE_CLOUD_PROJECT is set (see planner.capabilities); otherwise
the source is invisible and population questions fall back to the honest refusal.
"""
import os

_OPS = {">": ">", ">=": ">=", "<": "<", "<=": "<="}


def available():
    return bool(os.getenv("GOOGLE_CLOUD_PROJECT"))


def _client():
    proj = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not proj:
        raise SystemExit("BigQuery source needs a GCP project — set GOOGLE_CLOUD_PROJECT and "
                         "application-default credentials (`gcloud auth application-default login`).")
    try:
        from google.cloud import bigquery
    except ImportError as e:
        raise SystemExit(f"BigQuery source needs `pip install google-cloud-bigquery` ({e})")
    return bigquery.Client(project=proj)


def _rows(sql):
    try:
        return [dict(r) for r in _client().query(sql).result()]
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"BigQuery query failed: {str(e)[:160]}")


def _label(cfg, row):
    if row.get("name"):
        return str(row["name"])
    eid = row.get("eid")
    if cfg.get("name_via") == "propublica":
        try:
            import nonprofit
            return nonprofit.resolve(str(eid))["name"]
        except Exception:
            return f"EIN {eid}"
    return str(eid)


def _sanity(cfg):
    """Value bounds that de-noise a raw fact table (e.g. drop $10T filing typos)."""
    val = cfg.get("value_field") or cfg["field"]
    clauses = [f"t.{val} IS NOT NULL"]
    if cfg.get("value_max") is not None:
        clauses.append(f"t.{val} < {float(cfg['value_max'])}")
    if cfg.get("value_min") is not None:
        clauses.append(f"t.{val} > {float(cfg['value_min'])}")
    return clauses


def _select(cfg, extra, order, lim, having=None):
    """One SQL SELECT over the population. `extra` = list of extra WHERE clauses.

    LONG tables (a `group_agg` in cfg) collapse many rows per entity to one aggregate value
    before ordering; WIDE tables read the value column directly. Both return (eid, name?, value)."""
    table, ent = cfg["table"], cfg["entity_field"]
    where = "WHERE " + " AND ".join(([cfg["filter"]] if cfg.get("filter") else []) + _sanity(cfg) + extra)
    if cfg.get("group_agg"):
        val = cfg.get("value_field") or cfg["field"]
        sql = (f"SELECT t.{ent} AS eid, {cfg['group_agg']}(t.{val}) AS value FROM `{table}` t "
               f"{where} GROUP BY t.{ent}")
        if having:
            sql += f" HAVING value {having}"
        return sql + f" ORDER BY value {order} LIMIT {lim}"
    field = cfg["field"]
    if cfg.get("name_table"):
        return (f"SELECT t.{ent} AS eid, n.{cfg['name_field']} AS name, t.{field} AS value "
                f"FROM `{table}` t LEFT JOIN `{cfg['name_table']}` n ON t.{ent}=n.{cfg['name_key']} "
                f"{where} ORDER BY t.{field} {order} LIMIT {lim}")
    return (f"SELECT t.{ent} AS eid, t.{field} AS value FROM `{table}` t "
            f"{where} ORDER BY t.{field} {order} LIMIT {lim}")


def rank(cfg, n=10, ascending=False, threshold=None):
    """Top/bottom-N by the field, or those past a threshold — one SQL query over the population."""
    thr = threshold if (threshold and threshold.get("value") is not None) else None
    grouped = bool(cfg.get("group_agg"))
    extra, having = [], None
    if thr:
        clause = f"{_OPS.get(thr.get('op'), '>')} {float(thr['value'])}"
        # A grouped value is only known post-aggregation, so its threshold is a HAVING.
        if grouped:
            having = clause
        else:
            extra.append(f"t.{cfg['field']} {clause}")
    order = "ASC" if ascending else "DESC"
    rows = _rows(_select(cfg, extra, order, 200 if thr else int(n), having=having))
    kind = cfg.get("entity_kind", "id")
    usd = cfg.get("unit") == "USD"
    def disp(v):  # pre-format so the synthesizer quotes a figure rather than a raw float
        return ("${:,.0f}" if usd else "{:,.0f}").format(v) if float(v).is_integer() or usd \
            else ("${:,.2f}" if usd else "{:,.2f}").format(v)
    out = [{"label": _label(cfg, r), "entity": f"{kind}/{r['eid']}",
            "value": float(r["value"]), "value_display": disp(float(r["value"]))}
           for r in rows if r.get("value") is not None]
    res = {"source": cfg.get("source") or f"BigQuery {cfg['table']}",
           "measure": cfg.get("field") or cfg.get("value_field") or "value",
           "complete": True}
    if thr:
        # Pre-format the bound so the synthesizer never mistakes the raw threshold integer for a total.
        op_word = {">": "over", ">=": "at least", "<": "under", "<=": "at most"}.get(thr.get("op"), "over")
        res.update({"threshold_display": f"{op_word} {disp(float(thr['value']))}", "matches": len(out),
                    "ranking": out[:50]})
    else:
        res.update({"ranking": out[:int(n)], "top": out[0] if out else None})
    return res


def aggregate(cfg, agg="count", where=None):
    table = cfg["table"]
    if cfg.get("group_agg"):
        # LONG: count DISTINCT entities (one company files many rows) after the measure filter.
        val = cfg.get("value_field") or cfg["field"]
        clauses = ([cfg["filter"]] if cfg.get("filter") else []) + [f"t.{val} IS NOT NULL"]
        if where:
            clauses.append(where)
        w = " WHERE " + " AND ".join(clauses)
        expr = f"COUNT(DISTINCT t.{cfg['entity_field']})" if agg == "count" else f"{agg.upper()}(t.{val})"
        sql = f"SELECT {expr} AS v FROM `{table}` t{w}"
    else:
        expr = "COUNT(*)" if agg == "count" else f"{agg.upper()}({cfg['field']})"
        sql = f"SELECT {expr} AS v FROM `{table}`" + (f" WHERE {where}" if where else "")
    return {"aggregate": agg, "value": _rows(sql)[0]["v"],
            "source": cfg.get("source") or f"BigQuery {table}"}
