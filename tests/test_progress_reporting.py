"""User-visible progress reports the decisions and crosswalk work, not just activity."""
import json
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ard_client
import harness
import llm
from query_context import QueryContext
import resolver


def events(context):
    return [context.progress.get_nowait() for _ in range(context.progress.qsize())]


class ProgressReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_reports_entity_property_plan_and_ard_summary_in_order(self):
        context = QueryContext(usage_ledger=llm.Ledger(),
                               discovery_ledger=ard_client.DiscoveryUsage())
        classification = {
            "entity": "Detroit", "canonical_entity": "Detroit, Michigan",
            "entity_status": "resolved", "entity_candidates": [], "entities": [],
            "type": "place", "attribute": "median household income", "period": "latest",
            "periods": [], "sources": ["census"], "shape": "point", "threshold": None,
            "quantifier": "exhaustive", "interpretations": [],
        }
        hits = [{"identifier": "sources/census/income.md", "title": "Median income",
                 "publisher": "census", "score": 97}]
        with mock.patch.object(llm, "chat_async", mock.AsyncMock(
                return_value=json.dumps(classification))), \
             mock.patch.object(ard_client, "search_many_async", mock.AsyncMock(return_value=hits)):
            await harness.discover_async("Median income in Detroit", context=context)
        progress = events(context)
        self.assertEqual([event["kind"] for event in progress], [
            "status", "entity_detected", "property_identified", "plan", "status", "candidates"])
        self.assertEqual(progress[1]["canonical"], "Detroit, Michigan")
        self.assertEqual(progress[2]["attribute"], "median household income")
        self.assertEqual(progress[3]["shape"], "point")
        self.assertEqual(progress[5]["count"], 1)
        self.assertEqual(progress[5]["items"][0]["title"], "Median income")

    async def test_identifier_mapping_reports_search_candidates_and_selected_crosswalk(self):
        context = QueryContext()
        query = {"question": "How much NIH funding does Stanford get?", "entity": "Stanford",
                 "canonical_entity": "Stanford University", "entity_status": "resolved",
                 "entity_qid": "", "type": "educational organization"}
        candidates = [{"id": "Q41506", "label": "Stanford University",
                       "description": "private university in California"}]
        with mock.patch.object(resolver, "search_async", mock.AsyncMock(return_value=candidates)), \
             mock.patch.object(llm, "chat_async", mock.AsyncMock(return_value='{"indices":[0]}')), \
             mock.patch.object(resolver, "claims_async", mock.AsyncMock(
                 return_value=("Stanford University", {"ipeds": "243744"}))):
            mapped = await harness._link_entity_async(query, context=context)
            again = await harness._link_entity_async(query, context=context)
        self.assertEqual(mapped, again)
        self.assertEqual(mapped[0]["qid"], "Q41506")
        progress = events(context)
        self.assertEqual([event.get("phase") for event in progress],
                         ["searching", "candidates", "mapped"])
        self.assertEqual(progress[-1]["key_types"], ["ipeds"])

    def test_nlweb_progress_text_contains_all_requested_sections(self):
        messages = [
            harness._nlweb_text({"kind": "entity_detected", "entity": "Stanford",
                                 "canonical": "Stanford University", "type": "university",
                                 "status": "resolved"}),
            harness._nlweb_text({"kind": "property_identified", "attribute": "NIH funding",
                                 "period": "latest", "shape": "point"}),
            harness._nlweb_text({"kind": "plan", "shape": "point", "period": "latest",
                                 "sources": ["nih-reporter"]}),
            harness._nlweb_text({"kind": "candidates", "count": 1, "items": [{
                "title": "NIH research grants", "score": 99, "publisher": "nih-reporter"}]}),
            harness._nlweb_text({"kind": "entity_mapping", "phase": "mapped",
                                 "label": "Stanford University", "qid": "Q41506",
                                 "key_types": ["ipeds"]}),
        ]
        for heading, message in zip(("Entity detection", "Property identification", "Initial plan",
                                     "ARD summary", "Entity identifier mapping"), messages):
            self.assertIn(heading, message)
        self.assertIn("1 source family", messages[2])
        self.assertIn("1 candidate table", messages[3])
        self.assertNotIn("identifiers", messages[4])


if __name__ == "__main__":
    unittest.main()
