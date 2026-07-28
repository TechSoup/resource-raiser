---
name: okf-data-accessor
description: Answer a data question by reading an actionable OKF source document and querying the live source. Source-agnostic — all source specifics live in the OKF doc, never in code.
---

# OKF Data Accessor

Given (a) an actionable OKF source document and (b) a natural-language question,
fetch the real data and answer it. The same procedure works for any source; the
OKF document supplies every source-specific detail.

## Procedure

1. **Read the OKF doc.** Note its `access.operations` (in frontmatter) and the
   `# Schema`, `# Query`, and `# Examples` sections.
2. **Resolve identifiers if needed.** If the question names an entity that the
   source keys differently (e.g. a ticker vs a CIK), run the documented
   resolution operation first and map it (see the doc's `# Query`).
3. **Pick the operation and parameters** that answer the question, following the
   `# Query` guidance and `# Examples`.
4. **Call the accessor:**
   ```
   python3 accessor/okf_fetch.py <okf_doc.md> <operation> key=value ... [--extract dotted.path]
   ```
   The accessor fills the operation's URL template with your params (it applies
   any format rules declared in the template, e.g. zero-padding), performs the
   HTTP request, and prints the JSON.
5. **Extract the answer** from the JSON per the doc's `# Schema` (e.g. choose the
   `units.USD[]` item whose `frame` is `CY2023`), and report it **with
   provenance** — cite the source's `resource` / `trust.identity` (`did:web:…`).

## Rules

- Do not write source-specific code. If something can't be expressed through the
  OKF doc + this procedure, fix the OKF doc, not the accessor.
- Switching sources must require only pointing at a different OKF `.md` file.
