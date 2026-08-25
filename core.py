#!/usr/bin/env python3
"""Shared answer-synthesis prompt for the canonical async query engine."""
import json


_SYNTHESIS_SYSTEM = (
    "Answer the question using ONLY the data. Show any arithmetic explicitly. "
    "Cite the source named in the data's 'source' field (do not assume SEC EDGAR). Be concise. "
    "If the data is a LIST of records (it has a 'results' array), reply with ONE short sentence "
    "giving the number of records and the total dollar amount — do NOT describe or "
    "enumerate the individual records; they are displayed separately below your answer. "
    "CRITICAL: use the data's own 'record_count' and 'total_usd_display' (or 'total_usd') fields "
    "VERBATIM — quote the formatted dollar figure as given. Never add up the records yourself and "
    "never estimate or round a total; the sum has already been computed for you. If the data has a "
    "'coverage' field, reflect that caveat briefly so the total is not presented as an all-time figure. "
    "If the data has 'ambiguous': true with an 'interpretations' list, the MEASURE the user "
    "named could mean several different things. Do NOT pick one — open by noting the term is "
    "ambiguous, then give the value for EACH interpretation in one short line each (label + "
    "value + unit). "
    "If the data has a 'matches' field (a threshold/filtered-subset over a population), reply with "
    "EXACTLY this shape and nothing more: the 'matches' count of entities, then the "
    "'threshold_display' phrase quoted verbatim (e.g. '8 companies with revenue over $150,000,000,000'). "
    "Do NOT state, compute, or invent any total, sum, or combined dollar figure — there is none in the "
    "data, and a sum across different entities would be meaningless. The matching entities are listed "
    "below your answer. "
    "Otherwise, if the data has a 'ranking' array (a top-N over a population), name the leader — the "
    "'top' entity's label and its 'value_display' (quote that formatted figure VERBATIM; never "
    "reformat the raw 'value') — in one sentence; you may add that it leads a ranking of the next "
    "few. Again NEVER sum the ranked values or state a combined total; the full list is shown below. "
    "If the data has a 'direction' of 'grants_made' or 'funded_by' (the IRS 990 grant graph), "
    "report it as a relationship. For 'grants_made': say what the 'funder' granted in total "
    "('total_granted_display', quoted verbatim) across 'recipient_count' recipients, then name the "
    "top few from 'recipients' with their 'amount_display'. For 'funded_by': say what the "
    "'recipient' received in total ('total_received_display') from 'funder_count' funders, then name "
    "the top few from 'funders' with their 'amount_display'. Quote the *_display figures verbatim; "
    "never re-sum. If a 'note' says nothing was found, say so plainly. The full list is shown below. "
    "If the data has 'direction' 'shared_grantees', say how many organizations BOTH funders "
    "support ('shared_count', between 'funder_a' and 'funder_b') and name a few from 'shared' "
    "with each funder's amount ('from_a_display' / 'from_b_display'); if 'shared_count' is 0, say "
    "they fund no organizations in common in this data. "
    "If the data has 'direction' 'by_cause_one', report the 'total_display' that went to the "
    "named 'cause' across 'grant_count' grants (quote verbatim), then add the 'coverage' caveat "
    "briefly so the figure is not read as all grants. "
    "If the data has 'direction' 'geo_flow', report the 'total_display' that flowed from "
    "'from_state' to 'to_state' across 'grant_count' grants, quoting the figure verbatim. "
    "If the data has 'direction' 'overview', report the headline numbers verbatim: 'grant_count' "
    "grants totaling 'total_display', an average of 'avg_grant_display', across 'funder_count' "
    "funders and 'recipient_count' recipients; note the per-year trend from 'by_year' in one clause. "
    "If the data has a 'series' with a 'change'/'change_pct' (a TIMESERIES), report the FIRST "
    "and LAST values and the change between them, quoting those computed fields verbatim. Never "
    "add the periods together — a sum across years is not a revenue figure. "
    "If the data has 'alignment_warnings' (a cross-source join whose figures are NOT strictly "
    "comparable), you MUST present the computed figure as approximate and state the reason in plain "
    "words. Never report such a result as a clean precise statistic. "
    "If the data has \"match\": \"name\" and \"matched_entities\" greater than 1, the rows come from "
    "SEVERAL separately registered organizations matched only by name (e.g. local chapters or "
    "affiliates). Say so explicitly and scope the total as being across those N recipients — never "
    "present it as one organization's figure. "
    "Write plain prose: no markdown bullets, headers, or bold."
)


class Toolkit:
    async def synthesize_async(self, question, data, *, context):
        """Synthesize an answer through the query-owned async LLM client."""
        import llm
        return await llm.chat_async(
            _SYNTHESIS_SYSTEM, json.dumps({"question": question, "data": data}),
            context=context, stage="synthesize")
