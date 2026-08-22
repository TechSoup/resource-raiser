---
type: Nonprofit BMF Fact
title: Eligibility / Good Standing — IRS Business Master File (Nonprofit)
description: This measure indicates whether a US nonprofit is an active and recognized
  501(c)(3) organization in good standing with the IRS, which is essential for validating
  eligibility for nonprofit discounts and donation programs. It specifically addresses
  the compliance status of nonprofits, distinguishing it from other classifications
  or financial metrics. This measure does not assess the financial performance or
  mission impact of the organization, focusing instead on its IRS standing. The reporting
  is done per organization.
tags:
- nonprofit
- irs
- business-master-file
- eligibility
- good-standing
- validation
- 501c3
- techsoup
source: ./_access.md
bmf: eligibility
representativeQueries:
- Is this nonprofit in good standing with the IRS?
- Is this organization eligible for nonprofit discounts or donations?
- Is this a recognized 501(c)(3) with active tax-exempt status?
- Has this organization's tax exemption been revoked?
---

# Schema

Reports the IRS Business Master File exemption STATUS for a nonprofit, keyed by
EIN — the `exempt_organization_status_code` decoded to plain language, plus
whether it is a 501(c)(3) and whether contributions are deductible. This is the
composite "can this org receive a nonprofit discount/donation?" answer that a
validator such as TechSoup checks. Resolve the organization with the `search`
operation, then read its eligibility. See [Nonprofit BMF access](./_access.md).
