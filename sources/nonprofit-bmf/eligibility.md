---
type: Nonprofit BMF Fact
title: Eligibility / Good Standing — IRS Business Master File (Nonprofit)
description: Whether a US nonprofit is an active, recognized 501(c)(3) in good standing with the IRS — the validation check for nonprofit discount/donation eligibility (e.g. TechSoup, software-donation programs).
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
