---
type: Data Source
title: US Dept. of Education — College Scorecard (access)
description: Tuition, enrollment, admissions, completion and cost data for US colleges
  and universities, from the Dept. of Education College Scorecard API. Keyed by school
  name.
resource: https://api.data.gov/ed/collegescorecard/v1/
publisher: ed.gov
trust:
  identity: did:web:ed.gov
  identityType: did
access:
  auth: key
  operations:
    school:
      method: GET
      url: https://api.data.gov/ed/collegescorecard/v1/schools?school.name={name}&fields=school.name,{fields}&per_page=1&api_key={key}
entityType: a US college or university (e.g. Stanford, MIT, Ohio State) — public, private
  nonprofit, and for-profit institutions alike
---

# About

US colleges and universities — most are nonprofits and are core users of nonprofit technology programs. Keyed by school NAME; the API does its own fuzzy matching. Fields come back as flat dotted keys (`latest.cost.tuition.out_of_state`), which each leaf pins.
