#!/usr/bin/env python3
"""Generate sources/sec-bq/ — SEC financial statements as a queryable BigQuery population source.

sec_quarterly_financials is a LONG/EAV table: one row per (company, measure_tag, period) fact, the
number in a single `value` column. So a leaf here carries the LONG-format `bq:` config — a
`measure_tag` filter picks the measure, `group_agg: MAX` collapses a company's many filings to one
value, and de-noising filters (form/quarters/fiscal_year/value cap) drop restatement and typo rows.
This flips company ranking/aggregate INFEASIBLE->EXACT, the SEC analogue of irs-990-bq for nonprofits.
Run from the project root."""
import os, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

D = "sources/sec-bq"
os.makedirs(D, exist_ok=True)
TBL = "bigquery-public-data.sec_quarterly_financials.quick_summary"
YEAR = 2017  # latest fiscal year with dense coverage in the public dataset
SRC = f"SEC financial statements (BigQuery sec_quarterly_financials, FY{YEAR})"

acc = {"type": "Data Source",
       "title": "SEC financial statements (BigQuery) — public-company financials, queryable (access)",
       "description": "SEC 10-K/10-Q XBRL financial facts as a queryable BigQuery table "
       "(sec_quarterly_financials) — RANK, FILTER and AGGREGATE revenue / net income / assets across "
       "ALL public companies, which the per-company EDGAR companyconcept API cannot.",
       "resource": "bigquery-public-data.sec_quarterly_financials",
       "publisher": "sec.gov / Google BigQuery public datasets",
       "trust": {"identity": "did:web:sec.gov", "identityType": "did"},
       "access": {"auth": "gcp", "operations": {"query": {"method": "BIGQUERY", "url": "", "capability": {
           "paths": ["key", "filter", "order", "enumerate", "aggregate"], "grain": "company",
           "order": {"server": True}, "population": {"complete": True},
           "requires_env": "GOOGLE_CLOUD_PROJECT"}}}},
       "entityType": "the population of SEC-reporting public companies — for ranking, filtering and "
       "counting by reported financials across all filers, not one company at a time"}
open(D + "/_access.md", "w").write(
    "---\n" + yaml.safe_dump(acc, sort_keys=False, allow_unicode=True) + "---\n\n"
    "# About\n\nThe SAME SEC XBRL financials as the per-company EDGAR `sec-edgar` source, but as a "
    "BigQuery TABLE — so it answers POPULATION questions across all public companies (which company "
    "has the highest revenue, companies above a threshold, how many report) that the per-CIK API "
    "cannot. Server-aggregate at COMPANY grain. **Active only when GOOGLE_CLOUD_PROJECT is set.**\n\n"
    "# Matching & caveats\n\n`sec_quarterly_financials.quick_summary` is a LONG/EAV fact table, so a "
    "clean population ranking needs de-noising that the leaf `filter` encodes:\n\n"
    "- **pick ONE reporting basis**: `form='10-K' AND fiscal_year=%d`, and `number_of_quarters=4` for "
    "flow measures (revenue, net income — a full-year duration) vs `number_of_quarters=0` for stock "
    "measures (assets — an instant balance).\n"
    "- **collapse restatements/amendments**: a company files the same fact several times; `group_agg: "
    "MAX` takes one value per `company_name` so the population is companies, not filings.\n"
    "- **span the measure's tag variants**: post-ASC-606, revenue is reported under several us-gaap "
    "tags; the filter lists them and MAX picks the company's headline figure.\n"
    "- **cap filing typos**: `value_max` drops the occasional $10-trillion fat-finger.\n" % YEAR)

FLOW = f"units='USD' AND form='10-K' AND number_of_quarters=4 AND fiscal_year={YEAR}"
STOCK = f"units='USD' AND form='10-K' AND number_of_quarters=0 AND fiscal_year={YEAR}"
REV_TAGS = ("'Revenues','RevenueFromContractWithCustomerExcludingAssessedTax',"
            "'RevenueFromContractWithCustomerIncludingAssessedTax'")

LEAVES = {
    "revenue": {
        "label": "Revenue", "word": "revenue",
        "filter": f"measure_tag IN ({REV_TAGS}) AND {FLOW}", "value_max": 1e12,
        "queries": ["Which company has the highest revenue?", "largest US public companies by revenue",
                    "rank public companies by revenue", "which companies have revenue over 100 billion",
                    "top companies by revenue"]},
    "net-income": {
        "label": "Net Income", "word": "net income",
        "filter": f"measure_tag='NetIncomeLoss' AND {FLOW}", "value_max": 1e12,
        "queries": ["Which company has the highest net income?", "most profitable US public companies",
                    "rank public companies by net income", "which companies earned the most profit",
                    "top companies by profit"]},
    "assets": {
        "label": "Total Assets", "word": "total assets",
        "filter": f"measure_tag='Assets' AND {STOCK}", "value_max": 1e13,
        "queries": ["Which company has the most total assets?", "largest US public companies by assets",
                    "rank public companies by total assets", "which companies have the biggest balance sheet",
                    "top companies by assets"]},
}

for slug, m in LEAVES.items():
    fm = {"type": "SEC Financials Population Field (BigQuery)",
          "title": f"{m['label']} by company — SEC financials (BigQuery)",
          "description": f"Rank, filter or count US public companies by {m['label'].lower()} "
          f"across ALL SEC filers.",
          "tags": ["sec", "edgar", "financials", "bigquery", "ranking", "aggregate", "population",
                   "company", m["word"].replace(" ", "-")],
          "source": "./_access.md",
          "bq": {"table": TBL, "entity_field": "company_name", "entity_kind": "company",
                 "value_field": "value", "group_agg": "MAX", "filter": m["filter"],
                 "value_max": m["value_max"], "unit": "USD", "source": SRC},
          "representativeQueries": m["queries"]}
    open(f"{D}/{slug}.md", "w").write(
        "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n"
        f"# Schema\n\nRanks/filters/counts US public companies by {m['label']} across the whole "
        f"population, via BigQuery (LONG/EAV — value picked by `measure_tag`, one value per company via "
        f"MAX). See [SEC financials BigQuery access](./_access.md).\n")
print(f"wrote sec-bq _access + {len(LEAVES)} company-ranking leaves")
