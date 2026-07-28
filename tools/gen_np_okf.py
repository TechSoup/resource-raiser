#!/usr/bin/env python3
"""Generate per-field OKF leaf entries for IRS Form 990 nonprofit financials.

The 990 has a bounded, curated set of headline numeric fields (unlike the ~8.5k
XBRL taxonomy), so we hand-map the meaningful ones. One OKF leaf per field, keyed
by EIN at query time (the organization is a parameter). Each leaf cross-links to
nonprofit-990/_access.md for the query mechanics.
"""
import os, yaml

OUT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "sources", "nonprofit-990"))

# All numeric IRS Form 990 fields exposed by ProPublica -> (human label, definition)
FIELDS = {
    "totrevenue":        ("Total Revenue", "Total revenue for the year."),
    "totfuncexpns":      ("Total Functional Expenses", "Total expenses for the year."),
    "totassetsend":      ("Total Assets (End of Year)", "Total assets at the end of the year."),
    "totliabend":        ("Total Liabilities (End of Year)", "Total liabilities at the end of the year."),
    "totnetassetend":    ("Net Assets (End of Year)", "Net assets or fund balances at the end of the year."),
    "totcntrbgfts":      ("Total Contributions and Grants", "Contributions, gifts, and grants received."),
    "totprgmrevnue":     ("Program Service Revenue", "Total program service revenue."),
    "invstmntinc":       ("Investment Income", "Investment income."),
    "compnsatncurrofcr": ("Officer Compensation", "Compensation of current officers, directors, trustees, key employees."),
    "othrsalwages":      ("Salaries and Wages", "Other salaries and wages paid to employees."),
    "payrolltx":         ("Payroll Taxes", "Payroll taxes paid."),
    "profndraising":     ("Professional Fundraising Fees", "Fees paid for professional fundraising services."),
    "grsincfndrsng":     ("Fundraising Gross Income", "Gross income from fundraising events."),
    "lessdirfndrsng":    ("Direct Fundraising Expenses", "Less: direct expenses of fundraising events."),
    "netincfndrsng":     ("Net Fundraising Income", "Net income or loss from fundraising events."),
    "grsincgaming":      ("Gaming Gross Income", "Gross income from gaming."),
    "lessdirgaming":     ("Direct Gaming Expenses", "Less: direct expenses of gaming."),
    "netincgaming":      ("Net Gaming Income", "Net income or loss from gaming."),
    "grsalesinvent":     ("Gross Sales of Inventory", "Gross sales of inventory, less returns."),
    "lesscstofgoods":    ("Cost of Goods Sold", "Less: cost of goods sold."),
    "netincsales":       ("Net Income from Sales", "Net income or loss from sales of inventory."),
    "netrntlinc":        ("Net Rental Income", "Net rental income."),
    "grsrntsreal":       ("Gross Rents — Real Property", "Gross rents from real property."),
    "grsrntsprsnl":      ("Gross Rents — Personal Property", "Gross rents from personal property."),
    "rntlexpnsreal":     ("Rental Expenses — Real Property", "Rental expenses for real property."),
    "rntlexpnsprsnl":    ("Rental Expenses — Personal Property", "Rental expenses for personal property."),
    "rntlincreal":       ("Rental Income — Real Property", "Net rental income from real property."),
    "rntlincprsnl":      ("Rental Income — Personal Property", "Net rental income from personal property."),
    "royaltsinc":        ("Royalties Income", "Royalties income."),
    "netgnls":           ("Net Gain/Loss on Asset Sales", "Net gain or loss from sales of assets."),
    "gnlsecur":          ("Gain/Loss — Securities", "Gain or loss from sales of securities."),
    "gnlsothr":          ("Gain/Loss — Other Assets", "Gain or loss from sales of other assets."),
    "grsalesecur":       ("Gross Sales — Securities", "Gross sales price of securities sold."),
    "grsalesothr":       ("Gross Sales — Other Assets", "Gross sales price of other assets sold."),
    "cstbasisecur":      ("Cost Basis — Securities", "Cost or other basis of securities sold."),
    "cstbasisothr":      ("Cost Basis — Other Assets", "Cost or other basis of other assets sold."),
    "grsincmembers":     ("Income from Members", "Gross income from members or shareholders."),
    "grsincother":       ("Other Gross Income", "Other gross income."),
    "miscrevtot11e":     ("Miscellaneous Revenue", "Total miscellaneous revenue."),
    "initiationfees":    ("Initiation Fees", "Initiation fees and capital contributions."),
    "grsrcptspublicuse": ("Gross Receipts — Public Use of Facilities", "Gross receipts from public use of club facilities."),
    "retainedearnend":   ("Retained Earnings (End of Year)", "Retained earnings or fund balance at end of year."),
    "secrdmrtgsend":     ("Secured Mortgages (End of Year)", "Secured mortgages and notes payable at end of year."),
    "unsecurednotesend": ("Unsecured Notes (End of Year)", "Unsecured notes and loans payable at end of year."),
    "txexmptbndsend":    ("Tax-Exempt Bond Liabilities (End of Year)", "Tax-exempt bond liabilities at end of year."),
    "txexmptbndsproceeds": ("Tax-Exempt Bond Proceeds", "Proceeds of tax-exempt bonds."),
    # Public-support-test fields (Schedule A, 170(b)(1)(A) and 509(a)(2)):
    "gftgrntsrcvd170":   ("Gifts/Grants Received (170)", "Gifts, grants, and contributions received (170(b)(1)(A) support test)."),
    "grsrcptsrelated170": ("Gross Receipts — Related (170)", "Gross receipts from related activities (170 support test)."),
    "txrevnuelevied170": ("Tax Revenue Levied (170)", "Tax revenue levied for the organization (170 support test)."),
    "srvcsval170":       ("Govt Services Value (170)", "Value of services/facilities furnished by a government unit (170)."),
    "grsinc170":         ("Gross Income (170)", "Gross income (170 support test)."),
    "totgftgrntrcvd509": ("Gifts/Grants Received (509)", "Gifts, grants, and contributions received (509(a)(2) support test)."),
    "grsrcptsadmissn509": ("Gross Receipts — Admissions (509)", "Gross receipts from admissions, merchandise, services (509(a)(2))."),
    "txrevnuelevied509": ("Tax Revenue Levied (509)", "Tax revenue levied for the organization (509(a)(2))."),
    "srvcsval509":       ("Govt Services Value (509)", "Value of services/facilities furnished by a government unit (509(a)(2))."),
    "subtotsuppinc509":  ("Subtotal Support Income (509)", "Subtotal of support income (509(a)(2))."),
    "totsupp509":        ("Total Support (509)", "Total support (509(a)(2) support test)."),
}


def main():
    os.makedirs(OUT, exist_ok=True)
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import repr_queries

    def write_leaf(field, queries):                           # called per field as its queries land
        label, desc = FIELDS[field]
        fm = {
            "type": "Nonprofit 990 Field",
            "title": f"{label} — IRS Form 990 (Nonprofit)",
            "description": desc,
            "tags": ["nonprofit", "irs", "form-990", "charity", "ein"] + label.lower().split()[:3],
            "source": "./_access.md",
            "field": field,
            "representativeQueries": queries,
        }
        body = (f"# Schema\n\nReports the IRS Form 990 field `{field}` ({label}) per nonprofit, by "
                f"fiscal year, keyed by EIN. Resolve the organization with the `search` operation, "
                f"then read this field from the chosen year's filing. See "
                f"[Nonprofit 990 access](./_access.md).\n")
        with open(os.path.join(OUT, field + ".md"), "w", encoding="utf-8") as f:
            f.write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body)

    repr_queries.for_items([(field, label, desc) for field, (label, desc) in FIELDS.items()],
                           "IRS Form 990 nonprofit financial field", on_ready=write_leaf)
    print(f"wrote {len(FIELDS)} nonprofit-990 field entries to {OUT}")


if __name__ == "__main__":
    main()
