import json, os, queue, sqlite3, sys, tempfile, threading, time, unittest, urllib.parse, urllib.request
from contextlib import closing
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ARD_STORE", "json")       # importing harness must not create a test database

import ard_client, connectors, grants, harness, nlweb, planner, renderers, validation
from registry import index
from domain import Attempt, Evidence, QueryIntent


with open(os.path.join(ROOT, "tests", "fixtures", "golden_cases.json")) as f:
    FIXTURES = json.load(f)


class DomainTests(unittest.TestCase):
    def test_suppression_sentinel_is_deterministic_failure(self):
        intent = QueryIntent("poverty rate", measure="poverty rate")
        verdict = validation.structural(intent, {"value": -888888888, "unit": "percent"})
        self.assertFalse(verdict.accepted)
        self.assertIn("sentinel", verdict.reason)

    def test_rejected_attempt_never_becomes_evidence(self):
        intent = QueryIntent("poverty rate", measure="poverty rate")
        attempt = Attempt("census", "sources/census/example.md")
        with self.assertRaises(connectors.Rejected):
            connectors.GENERIC.execute(intent, attempt, {"identifier": attempt.identifier},
                                       lambda: {"value": -888888888})
        self.assertEqual(attempt.outcome, "rejected")

    def test_renderer_uses_evidence_kind_not_classifier_shape(self):
        case = FIXTURES["evidence"][0]
        intent = QueryIntent(**case["intent"])
        attempt = Attempt("irs-grants", case["identifier"])
        evidence = connectors.GRANTS.execute(intent, attempt,
            {"identifier": case["identifier"], "title": "Grant overview"},
            lambda: case["data"], adjudicator=lambda *_: (True, ""))
        self.assertEqual(evidence.kind, case["expected_kind"])
        answer = renderers.render(evidence)
        self.assertEqual(answer.renderer, case["expected_renderer"])
        self.assertIn("10 grants", answer.text)

    def test_nlweb_parameters_are_bounded(self):
        req = nlweb.parse_request({"query": "x", "max_results": 100000, "min_score": -2,
                                   "mode": "unknown", "assumption_measure": "net income"})
        self.assertEqual(req["max_results"], 100)
        self.assertEqual(req["min_score"], 0)
        self.assertEqual(req["mode"], "generate")
        self.assertEqual(req["assumptions"]["attribute"], "net income")

    def test_finder_health_does_not_search(self):
        with mock.patch.object(ard_client, "_get", return_value={"ok": True}) as get:
            self.assertTrue(ard_client.health()["ok"])
            get.assert_called_once_with("/healthz")


class RendererGoldenTests(unittest.TestCase):
    def evidence(self, kind, payload, **kw):
        defaults = {"source": "Fixture source", "identifier": "sources/fixture.md",
                    "payload": payload, "kind": kind}
        defaults.update(kw)
        return Evidence(**defaults)

    def test_status_polarity_uses_boolean_not_friendly_status_label(self):
        intent = QueryIntent("Is the Sierra Club a 501(c)(3)?", operation="status",
                             entity="Sierra Club", measure="501(c)(3) status")
        attempt = Attempt("IRS BMF", "sources/nonprofit-bmf/eligibility.md",
                          entity={"name": "SIERRA CLUB"})
        evidence = connectors.GENERIC.execute(intent, attempt,
            {"identifier": attempt.identifier}, lambda: {
                "organization": "SIERRA CLUB", "is_501c3": False,
                "contributions_deductible": False, "value": "Active tax-exempt organization",
                "source": "IRS BMF"}, adjudicator=lambda *_: (True, ""))
        answer = renderers.render(evidence).text
        self.assertTrue(answer.startswith("No —"), answer)
        self.assertIn("does not meet", answer)
        self.assertNotIn("is Active", answer)

    def test_point_keeps_entity_user_measure_unit_and_number_format(self):
        e = self.evidence("point", {"company": "Apple Inc."},
            entity={"label": "Apple Inc."}, measure="total revenue", value=416161000000,
            unit="USD", currency="USD", period="FY2025")
        text = renderers.render(e).text
        for expected in ("Apple Inc.", "total revenue", "$416,161,000,000"):
            self.assertIn(expected, text)

    def test_percent_point_uses_percent_sign_and_human_measure(self):
        e = self.evidence("point", {}, entity={"label": "Detroit"}, measure="poverty rate",
                          value=16.9, unit="percent")
        text = renderers.render(e).text
        self.assertIn("Detroit’s poverty rate", text)
        self.assertIn("16.9%", text)

    def test_ranking_and_timeseries_keep_formatted_values(self):
        ranking = self.evidence("ranking", {"ranking": [{"label": "California", "value": 1234567}]},
                                measure="grant dollars", unit="USD", currency="USD")
        self.assertIn("$1,234,567", renderers.render(ranking).text)
        series = self.evidence("timeseries", {"series": [
            {"period": "FY2023", "value": 1000}, {"period": "FY2024", "value": 2500}]},
            entity={"label": "Example Org"}, measure="revenue", unit="USD", currency="USD")
        text = renderers.render(series).text
        self.assertIn("Example Org’s revenue", text)
        self.assertIn("$1,000", text)
        self.assertIn("$2,500", text)

    def test_census_connector_supplies_percent_and_numeric_value(self):
        intent = QueryIntent("What is Chicago's poverty rate?", operation="point",
                             entity="Chicago", entity_type="place", measure="poverty rate")
        hit = {"identifier": "sources/census/dp03-0128e.md", "title": "ACS poverty"}
        attempt = Attempt("census", hit["identifier"], entity={"label": "Chicago"})
        evidence = connectors.CENSUS.execute(intent, attempt, hit, lambda: {
            "place": "Chicago city, Illinois", "value": "16.9", "variable": "DP03_0128E",
            "metric": "PERCENTAGE OF FAMILIES AND PEOPLE", "source": "US Census ACS"},
            adjudicator=lambda *_: (True, ""))
        self.assertEqual(evidence.value, 16.9)
        self.assertEqual(evidence.unit, "%")
        text = renderers.render(evidence).text
        self.assertIn("Chicago’s poverty rate", text)
        self.assertIn("16.9%", text)

    def test_treasury_connector_supplies_usd_and_numeric_value(self):
        intent = QueryIntent("What is the total public debt?", operation="point",
                             measure="total public debt")
        hit = {"identifier": "sources/treasury/debt-to-penny-tot-pub-debt-out-amt.md",
               "title": "Debt to the Penny"}
        attempt = Attempt("treasury", hit["identifier"])
        evidence = connectors.TREASURY.execute(intent, attempt, hit, lambda: {
            "value": "40033256786764.37", "metric": "Debt to the Penny: Total Public Debt Outstanding",
            "source": "US Treasury FiscalData"}, adjudicator=lambda *_: (True, ""))
        self.assertEqual(evidence.value, 40033256786764.37)
        self.assertEqual(evidence.unit, "USD")
        self.assertIn("$40,033,256,786,764.37", renderers.render(evidence).text)


class GrantPathTests(unittest.TestCase):
    def test_sqlite_grant_overview_executes_offline(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "grants.sqlite")
            with closing(sqlite3.connect(path)) as c:
                c.execute("CREATE TABLE grant_edges (funder_ein TEXT, funder_name TEXT, "
                          "funder_state TEXT, recipient_ein TEXT, recipient_name TEXT, "
                          "recipient_state TEXT, amount REAL, purpose TEXT, tax_year INTEGER, form TEXT)")
                c.executemany("INSERT INTO grant_edges VALUES (?,?,?,?,?,?,?,?,?,?)", [
                    ("1", "FUNDER A", "CA", "2", "RECIPIENT A", "NY", 100, "Education", 2023, "990"),
                    ("1", "FUNDER A", "CA", "3", "RECIPIENT B", "WA", 300, "Health", 2024, "990")])
                c.commit()
            with mock.patch.multiple(grants, DB=path, URL=None, ROLLUPS=False):
                data = grants.overview()
            self.assertEqual(data["grant_count"], 2)
            self.assertEqual(data["total_display"], "$400")


class HttpPipelineTests(unittest.TestCase):
    def test_point_sources_normalize_through_real_ask_endpoint(self):
        census_hit = {"identifier": "sources/census/dp03-0128e.md", "title": "ACS poverty",
                      "publisher": "census", "score": 100}
        treasury_hit = {"identifier": "sources/treasury/debt-to-penny-tot-pub-debt-out-amt.md",
                        "title": "Debt to the Penny", "publisher": "treasury", "score": 100}

        def discover(question, sites=None, assumptions=None):
            if "poverty" in question.lower():
                return ({"shape": "point", "entity": "Chicago", "type": "place",
                         "attribute": "poverty rate", "period": "latest"}, [census_hit])
            return ({"shape": "point", "entity": "", "type": "none",
                     "attribute": "national debt", "period": "latest"}, [treasury_hit])

        def fetch(state, ctx):
            if state["hit"]["publisher"] == "census":
                return {"place": "Chicago city, Illinois", "value": "16.9",
                        "variable": "DP03_0128E", "metric": "PERCENTAGE OF FAMILIES AND PEOPLE",
                        "source": "US Census ACS"}
            return {"value": "40033256786764.37",
                    "metric": "Debt to the Penny: Total Public Debt Outstanding",
                    "source": "US Treasury FiscalData"}

        servers = queue.Queue()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(harness, "TELEMETRY_PATH", os.path.join(td, "telemetry.jsonl")), \
             mock.patch.object(harness, "discover", side_effect=discover), \
             mock.patch.object(harness, "_entity_options", return_value=[{"label": "Chicago", "keys": {}}]), \
             mock.patch.object(harness, "_key_options", return_value=["fixture-key"]), \
             mock.patch.object(harness, "_fetch", side_effect=fetch), \
             mock.patch.object(harness, "_answers", return_value=(True, "")):
            thread = threading.Thread(target=harness.serve, args=(0, servers.put), daemon=True)
            thread.start()
            server = servers.get(timeout=3)
            base = f"http://127.0.0.1:{server.server_address[1]}/ask?streaming=false&query="
            try:
                answers = []
                for question in ("What is Chicago's poverty rate?", "What is the national debt?"):
                    with urllib.request.urlopen(base + urllib.parse.quote(question), timeout=5) as response:
                        body = json.load(response)
                    content = next(m["content"] for m in body["messages"] if m["message_type"] == "nlws")
                    answers.append(content)
            finally:
                server.shutdown()
                thread.join(timeout=3)
            self.assertIn("16.9%", answers[0]["answer"])
            self.assertEqual(answers[0]["evidence"]["value"], 16.9)
            self.assertEqual(answers[0]["evidence"]["unit"], "%")
            self.assertIn("$40,033,256,786,764.37", answers[1]["answer"])
            self.assertEqual(answers[1]["evidence"]["unit"], "USD")


class PlannerGoldenTests(unittest.TestCase):
    def test_recorded_refusals(self):
        for case in FIXTURES["refusals"]:
            with self.subTest(case=case["name"]):
                verdict, *_ = planner.verdict(case["shape"], case["identifier"])
                self.assertEqual(verdict, case["expected"])


class StreamingTests(unittest.TestCase):
    def test_intermediate_event_arrives_before_query_finishes(self):
        def fake_run(question, sites=None, assumptions=None):
            harness._say("status", icon="x", msg="live")
            time.sleep(0.12)
            return {"answer": "done", "candidates": [], "source": {}, "data": {},
                    "usage": {}, "discovery_usage": {}, "shape": "point", "plan": "test"}

        with mock.patch.object(harness, "run", side_effect=fake_run):
            stream = harness.run_nlweb({"query": "q", "sites": (), "conversation_id": None,
                                        "min_score": 0, "max_results": 10, "mode": "generate",
                                        "debug": False})
            self.assertEqual(next(stream)["message_type"], "begin-nlweb-response")
            started = time.monotonic()
            second = next(stream)
            elapsed = time.monotonic() - started
            self.assertEqual(second["message_type"], "intermediate_message")
            self.assertLess(elapsed, 0.08)


class EndToEndRenderingTests(unittest.TestCase):
    """Above the connector, through run().

    The connector tests call `CENSUS.execute(...)` directly, so they prove the connector is
    correct but not that the server reaches it. A wiring change that bypassed normalization —
    evidence built from the raw fetch, a path that never calls execute() — would leave every
    connector test green while the served answer lost its unit and its formatting. These stub
    only discovery and the network, and assert the text a caller actually receives.
    """

    def _answer(self, question, identifier, title, publisher, payload, intent_extra=None):
        hit = {"identifier": identifier, "title": title, "publisher": publisher, "score": 100}
        ctx = {"entity": "Chicago", "type": "place", "attribute": "poverty rate",
               "shape": "point", "period": "latest", "sources": [publisher], "entities": [],
               "interpretations": [], "threshold": None, "quantifier": "exhaustive"}
        ctx.update(intent_extra or {})
        with mock.patch.object(harness, "discover", return_value=(ctx, [hit])), \
             mock.patch.object(harness, "_fetch", return_value=payload), \
             mock.patch.object(harness, "_entity_options",
                               return_value=[{"label": ctx["entity"], "keys": {}}]), \
             mock.patch.object(harness, "_answers", return_value=(True, "")), \
             mock.patch.object(harness.TK, "synthesize",
                               side_effect=AssertionError("synthesis must not run for a "
                                                          "deterministically renderable shape")):
            return harness.run(question)

    def test_census_point_reaches_the_renderer_with_unit_and_number(self):
        res = self._answer(
            "What is the poverty rate in Chicago?", "sources/census/dp03-0128e.md",
            "Poverty rate — US Census ACS", "census",
            {"place": "Chicago city, Illinois", "value": "16.9", "variable": "DP03_0128E",
             "metric": "PERCENTAGE OF FAMILIES AND PEOPLE", "source": "US Census ACS"})
        self.assertIn("16.9%", res["answer"])
        self.assertEqual(res["data"]["value"], 16.9)          # normalized, not the raw string
        self.assertEqual(res["data"]["unit"], "%")
        self.assertNotEqual(res["answer_renderer"], "llm-fallback")

    def test_treasury_point_reaches_the_renderer_formatted(self):
        res = self._answer(
            "What is the national debt?",
            "sources/treasury/debt-to-penny-tot-pub-debt-out-amt.md",
            "Debt to the Penny", "treasury",
            {"value": "40033256786764.37", "metric": "Debt to the Penny",
             "source": "US Treasury FiscalData"},
            intent_extra={"entity": "", "type": "none", "attribute": "national debt"})
        self.assertIn("$40,033,256,786,764.37", res["answer"])
        self.assertIsInstance(res["data"]["value"], float)
        self.assertEqual(res["data"]["unit"], "USD")


class IndexArtifactTests(unittest.TestCase):
    def test_build_publishes_one_versioned_generation(self):
        with tempfile.TemporaryDirectory() as td:
            builds, current = os.path.join(td, "builds"), os.path.join(td, "current")
            legacy_vec, legacy_meta = os.path.join(td, "vectors.npy"), os.path.join(td, "meta.json")
            def fake_embed(texts, batch=96):
                import numpy as np
                return np.ones((len(texts), 3), dtype=np.float32)
            with mock.patch.multiple(index, BUILDS=builds, CURRENT=current,
                                     LEGACY_VEC=legacy_vec, LEGACY_META=legacy_meta,
                                     CACHE_VEC=legacy_vec, CACHE_META=legacy_meta), \
                 mock.patch.object(index, "embed", side_effect=fake_embed), \
                 mock.patch.object(index.llm, "embed_model", return_value="fixture-model"), \
                 mock.patch.object(index.llm, "provider", return_value="fixture"):
                index._STORE = None
                index.build(limit=12)
                self.assertTrue(index.embed.called)
                self.assertTrue(os.path.islink(current))
                with open(os.path.join(current, "manifest.json")) as f:
                    manifest = json.load(f)
                self.assertEqual(manifest["entry_count"], 12)
                self.assertEqual(manifest["vector_dimension"], 3)
                ok, detail = index.verify()
                self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()
