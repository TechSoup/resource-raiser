#!/usr/bin/env python3
"""Regenerate the BigQuery-backed sources: convert irs-990-bq leaves to the generic `bq:` config
dict, and create the census-acs-bq county-ranking source. Run from the project root."""
import os, glob, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

# --- irs-990-bq leaves -> config-dict format ---
for p in glob.glob("sources/irs-990-bq/*.md"):
    if p.endswith("_access.md"):
        continue
    raw = open(p).read()
    fm = yaml.safe_load(raw.split("---")[1])
    field = fm["bq"] if isinstance(fm["bq"], str) else fm["bq"]["field"]
    fm["bq"] = {"table": "bigquery-public-data.irs_990.irs_990_2017", "field": field,
                "entity_field": "ein", "entity_kind": "ein", "name_via": "propublica",
                "unit": "USD",  # every 990 financial field is a dollar amount
                "source": "IRS Form 990 (BigQuery bigquery-public-data.irs_990, FY2017)"}
    body = raw.split("---\n\n", 1)[1]
    open(p, "w").write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body)
print("irs-990-bq leaves -> config-dict format")

# --- census-acs-bq: county rankings via BigQuery ---
D = "sources/census-acs-bq"
os.makedirs(D, exist_ok=True)
acc = {"type": "Data Source", "title": "US Census ACS (BigQuery) — county statistics, queryable (access)",
       "description": "American Community Survey county-level statistics as a queryable BigQuery table "
       "(census_bureau_acs) — RANK, FILTER and AGGREGATE across all ~3,200 US counties, which the "
       "per-place Census API cannot.",
       "resource": "bigquery-public-data.census_bureau_acs",
       "publisher": "census.gov / Google BigQuery public datasets",
       "trust": {"identity": "did:web:census.gov", "identityType": "did"},
       "access": {"auth": "gcp", "operations": {"query": {"method": "BIGQUERY", "url": "", "capability": {
           "paths": ["key", "filter", "order", "enumerate", "aggregate"], "grain": "county",
           "order": {"server": True}, "population": {"complete": True},
           "requires_env": "GOOGLE_CLOUD_PROJECT"}}}},
       "entityType": "the population of US counties — for ranking, filtering and counting community "
       "statistics across all counties, not one at a time"}
open(D + "/_access.md", "w").write(
    "---\n" + yaml.safe_dump(acc, sort_keys=False, allow_unicode=True) + "---\n\n"
    "# About\n\nThe SAME ACS demographics as `census`, but as a BigQuery TABLE — so it answers POPULATION "
    "questions across all US counties (which county has the highest X, counties above a threshold, "
    "correlations) that the per-place API cannot. Server-aggregate at COUNTY grain; county names via a "
    "join to `geo_us_boundaries.counties`. **Active only when GOOGLE_CLOUD_PROJECT is set.**\n")

NAME = {"table": "bigquery-public-data.geo_us_boundaries.counties", "key": "geo_id", "field": "county_name"}
TBL = "bigquery-public-data.census_bureau_acs.county_2018_5yr"
SRC = "US Census ACS county 5-yr (BigQuery census_bureau_acs)"
# label, query-word, unit (USD dollar amounts get a $-formatted display; age/index do not)
MEAS = {
    "median_income": ("Median Household Income", "median household income", "USD"),
    "income_per_capita": ("Per Capita Income", "per capita income", "USD"),
    "median_rent": ("Median Gross Rent", "median rent", "USD"),
    "owner_occupied_housing_units_median_value": ("Median Home Value", "median home value", "USD"),
    "median_age": ("Median Age", "median age", None),
    "gini_index": ("Income Inequality (Gini Index)", "income inequality", None),
}
for field, (label, word, unit) in MEAS.items():
    fm = {"type": "Census ACS Population Field (BigQuery)",
          "title": f"{label} by county — US Census ACS (BigQuery)",
          "description": f"Rank, filter or aggregate US COUNTIES by {label.lower()} ({field}).",
          "tags": ["census", "acs", "county", "bigquery", "ranking", "aggregate", "population",
                   word.replace(" ", "-")],
          "source": "./_access.md",
          "bq": {"table": TBL, "field": field, "entity_field": "geo_id", "entity_kind": "fips",
                 "name_table": NAME["table"], "name_key": NAME["key"], "name_field": NAME["field"],
                 **({"unit": unit} if unit else {}), "source": SRC},
          "representativeQueries": [f"Which county has the highest {word}?",
                                    f"which counties have the lowest {word}",
                                    f"rank US counties by {word}", f"which counties have {word} above",
                                    f"top counties by {word}"]}
    slug = field.replace("_", "-")[:40]
    open(f"{D}/{slug}.md", "w").write(
        "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n"
        f"# Schema\n\nRanks/filters/aggregates US counties by `{field}` ({label}) via BigQuery. "
        f"See [Census ACS BigQuery access](./_access.md).\n")
print(f"wrote census-acs-bq _access + {len(MEAS)} county-ranking leaves")
