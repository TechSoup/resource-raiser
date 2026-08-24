import json, os, queue, sqlite3, sys, tempfile, threading, time, unittest, urllib.parse, urllib.request
from contextlib import closing
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ARD_STORE", "json")       # importing harness must not create a test database

import ard_client, connectors, docpage, driver, grants, harness, nlweb, planner, renderers, validation
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
                                   "mode": "unknown", "assumption_measure": "net income",
                                   "on_ambiguity": "ask"})
        self.assertEqual(req["max_results"], 100)
        self.assertEqual(req["min_score"], 0)
        self.assertEqual(req["mode"], "generate")
        self.assertEqual(req["on_ambiguity"], "ask")
        self.assertEqual(req["assumptions"]["attribute"], "net income")

    def test_nested_follow_up_assumptions_are_accepted(self):
        req = nlweb.parse_request({"question": "Apple profit", "assumptions": {
            "measure": "net income", "concept": "us-gaap:NetIncomeLoss"}})
        self.assertEqual(req["query"], "Apple profit")
        self.assertEqual(req["on_ambiguity"], "answer")
        self.assertEqual(req["assumptions"], {
            "attribute": "net income", "concept": "us-gaap:NetIncomeLoss"})

    def test_query_string_assumptions_are_decoded_not_dropped(self):
        """A query string cannot carry a nested object, so a GET client JSON-encodes it. Dropping
        the string leaves the classifier's `interpretations` intact, and the ambiguous question is
        then answered from its FIRST interpretation instead of the caller's choice."""
        req = nlweb.parse_request({"query": ["Apple profit"], "assumptions": [json.dumps(
            {"measure": "operating income", "concept": "us-gaap:OperatingIncomeLoss"})]})
        self.assertEqual(req["assumptions"], {"attribute": "operating income",
                                              "concept": "us-gaap:OperatingIncomeLoss"})
        self.assertEqual(req["assumptions_error"], "")

    def test_unreadable_assumptions_are_reported_rather_than_ignored(self):
        """Silently discarding a binding is the failure mode worth preventing: the caller gets a
        confident answer to a question they did not ask."""
        for bad in ("{not json", '"a string"', "[1, 2]"):
            req = nlweb.parse_request({"query": ["q"], "assumptions": [bad]})
            self.assertEqual(req["assumptions"], {})
            self.assertTrue(req["assumptions_error"], bad)

    def test_finder_health_does_not_search(self):
        with mock.patch.object(ard_client, "_get", return_value={"ok": True}) as get:
            self.assertTrue(ard_client.health()["ok"])
            get.assert_called_once_with("/healthz")


class DocPageTests(unittest.TestCase):
    """The served page is rendered from the Markdown file, so these guard the renderer, not prose."""

    MD = ("# Title\n\nIntro **bold** and `code` text.\n\n"
          "## Section one\n\n> a quote\n\n```text\na -> b\n```\n\n"
          "| Shape | Path |\n|---|---|\n| Point | Keyed |\n\n"
          "- first\n- second\n\n## Section two\n\n1. one\n2. two\n")

    def test_blocks_render_without_leaking_markdown(self):
        body, toc = docpage.render(self.MD)
        for tag in ("<h1", "<h2", "<blockquote>", "<pre", "<table>", "<ul>", "<ol>",
                    "<strong>bold</strong>", "<code>code</code>"):
            self.assertIn(tag, body, tag)
        self.assertNotIn("**", body)
        self.assertNotIn("|---|", body)
        self.assertEqual([a for a, _ in toc], ["section-one", "section-two"])

    def test_code_spans_are_not_reformatted_and_html_is_escaped(self):
        body, _ = docpage.render("Pass `**kwargs` to <script>alert(1)</script> & co.\n")
        self.assertIn("<code>**kwargs</code>", body)      # not turned into <strong>
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&amp;", body)

    def test_every_fence_closes_and_nothing_is_dropped(self):
        """A fence parser that miscounts silently swallows the rest of the document."""
        path = os.path.join(docpage.ROOT, "LIFE_OF_A_QUERY.md")
        if not os.path.exists(path):                      # not shipped in every checkout
            self.skipTest("LIFE_OF_A_QUERY.md is not present")
        with open(path, encoding="utf-8") as fh:
            md = fh.read()
        body, toc = docpage.render(md)
        self.assertEqual(body.count("<pre"), md.count("\n```") // 2)
        self.assertEqual(body.count("<h2 "), md.count("\n## "))
        self.assertEqual(len(toc), md.count("\n## "))
        self.assertIn("compact thesis", body.lower())     # the LAST section survived the parse

    def test_absent_document_is_a_clean_miss_not_an_exception(self):
        self.assertIsNone(docpage.markdown_page("NO_SUCH_DOCUMENT.md", "x"))


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

    def test_post_ask_can_return_a_clarification_request(self):
        seen = {}

        def fake_run(question, sites=None, assumptions=None, on_ambiguity="answer"):
            seen.update(question=question, on_ambiguity=on_ambiguity)
            return {"question": question, "status": "needs_clarification", "answer": None,
                    "shape": "point", "plan": "ask caller", "usage": {}, "discovery_usage": {},
                    "intent": {"operation": "point", "measure": "profit"}, "attempts": [],
                    "evidence": {"kind": "alternatives"}, "answer_renderer": None,
                    "source": {}, "candidates": [], "data": {"ambiguous": True},
                    "clarification": {"question": "Which profit measure?", "attribute": "profit",
                                      "options": [{"id": "net-income", "label": "Net income",
                                                   "value": 97, "unit": "USD", "period": "FY2023",
                                                   "assumptions": {"measure": "net income"}}]}}

        servers = queue.Queue()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(harness, "TELEMETRY_PATH", os.path.join(td, "telemetry.jsonl")), \
             mock.patch.object(harness, "run", side_effect=fake_run):
            thread = threading.Thread(target=harness.serve, args=(0, servers.put), daemon=True)
            thread.start()
            server = servers.get(timeout=3)
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/ask",
                data=json.dumps({"query": "Apple profit", "streaming": False,
                                 "on_ambiguity": "ask"}).encode(),
                headers={"content-type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.load(response)
            finally:
                server.shutdown()
                thread.join(timeout=3)
        content = next(m["content"] for m in body["messages"] if m["message_type"] == nlweb.NLWS)
        self.assertEqual(seen["on_ambiguity"], "ask")
        self.assertEqual(content["@type"], "ClarificationRequest")
        self.assertEqual(content["options"][0]["value"], 97)


class PlannerGoldenTests(unittest.TestCase):
    def test_recorded_refusals(self):
        for case in FIXTURES["refusals"]:
            with self.subTest(case=case["name"]):
                verdict, *_ = planner.verdict(case["shape"], case["identifier"])
                self.assertEqual(verdict, case["expected"])


class StreamingTests(unittest.TestCase):
    def test_intermediate_event_arrives_before_query_finishes(self):
        def fake_run(question, sites=None, assumptions=None, on_ambiguity="answer"):
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


class AmbiguityTests(unittest.TestCase):
    HIT = {"identifier": "sources/sec-edgar/net-income-loss.md", "title": "Net income",
           "publisher": "sec-edgar", "score": 92}
    CTX = {"shape": "point", "entity": "Apple", "type": "company", "attribute": "profit",
           "period": "FY2023", "entities": [], "interpretations": ["net income", "operating income"],
           "threshold": None, "quantifier": "exhaustive"}
    OPTIONS = {"ambiguous": True, "attribute": "profit", "entity": "Apple",
               "interpretations": [
                   {"interpretation": "net income", "label": "Net income", "value": 97000000000.0,
                    "unit": "USD", "period": "FY2023", "source": "SEC EDGAR",
                    "concept": "us-gaap:NetIncomeLoss"},
                   {"interpretation": "operating income", "label": "Operating income",
                    "value": 114000000000.0, "unit": "USD", "period": "FY2023",
                    "source": "SEC EDGAR", "concept": "us-gaap:OperatingIncomeLoss"}],
               "source": "SEC EDGAR"}

    def result(self, mode):
        with mock.patch.object(harness, "discover", return_value=(dict(self.CTX), [dict(self.HIT)])), \
             mock.patch.object(harness, "_run_ambiguous", return_value=dict(self.OPTIONS)):
            return harness.run("What was Apple's profit in 2023?", on_ambiguity=mode)

    def test_ask_returns_fetched_clarification_not_an_answer(self):
        result = self.result("ask")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIsNone(result["answer"])
        options = result["clarification"]["options"]
        self.assertEqual([o["value"] for o in options], [97000000000, 114000000000])
        self.assertTrue(all(type(o["value"]) is int for o in options))
        self.assertEqual(options[0]["assumptions"]["concept"], "us-gaap:NetIncomeLoss")

    def test_all_returns_each_interpretation_and_answer_is_default(self):
        all_result = self.result("all")
        self.assertEqual(all_result["answer_renderer"], "alternatives")
        self.assertEqual(len(all_result["data"]["interpretations"]), 2)
        answer_result = self.result("answer")
        self.assertEqual(answer_result["status"], "answered")
        self.assertIn("$97,000,000,000", answer_result["answer"])
        self.assertEqual(len(answer_result["data"]["interpretations"]), 2)

    def test_clarification_uses_nlws_content_type(self):
        result = self.result("ask")
        with mock.patch.object(harness, "run", return_value=result):
            messages = list(harness.run_nlweb({"query": "q", "sites": (), "conversation_id": "c1",
                                               "min_score": 0, "max_results": 10, "mode": "generate",
                                               "debug": False, "on_ambiguity": "ask"}))
        content = next(m["content"] for m in messages if m["message_type"] == nlweb.NLWS)
        self.assertEqual(content["@type"], "ClarificationRequest")
        self.assertEqual(content["status"], "needs_clarification")
        self.assertEqual(content["options"][1]["value"], 114000000000)
        self.assertIs(type(content["options"][1]["value"]), int)

    def test_measure_assumption_clears_classifier_ambiguity(self):
        classified = {**self.CTX, "sources": ["sec-edgar"]}
        with mock.patch.object(harness.TK, "llm", return_value=json.dumps(classified)), \
             mock.patch.object(ard_client, "search_many", return_value=[]) as search_many:
            ctx, _ = harness.discover("Apple profit", assumptions={"attribute": "net income"})
        self.assertEqual(ctx["attribute"], "net income")
        self.assertEqual(ctx["interpretations"], [])
        search_many.assert_called_once_with(["net income", "Apple profit"], k=12,
                                            sources=["sec-edgar"], rerank_query="net income")

    def test_choosing_a_later_option_binds_that_option(self):
        """The full clarification round trip, resolved the way a GET client resolves it: echo the
        option's own `assumptions` back. Binding the SECOND option is the case that matters — the
        ambiguity branch answers from options[0], so dropping the binding still returns a plausible
        number, and the caller who chose operating income is told net income instead."""
        option = self.result("ask")["clarification"]["options"][1]
        self.assertEqual(option["assumptions"]["measure"], "operating income")

        req = nlweb.parse_request({"query": ["What was Apple's profit in 2023?"],
                                   "assumptions": [json.dumps(option["assumptions"])]})
        self.assertEqual(req["assumptions"]["attribute"], "operating income")

        classified = {**self.CTX, "sources": ["sec-edgar"]}
        with mock.patch.object(harness.TK, "llm", return_value=json.dumps(classified)), \
             mock.patch.object(ard_client, "search_many", return_value=[]):
            ctx, _ = harness.discover("What was Apple's profit in 2023?",
                                      assumptions=req["assumptions"])
        self.assertEqual(ctx["attribute"], "operating income")
        self.assertEqual(ctx["concept"], "us-gaap:OperatingIncomeLoss")
        # Cleared, so the ambiguity branch does not re-run and re-select its own first option.
        self.assertEqual(ctx["interpretations"], [])

    def test_sec_resolution_only_marks_materially_different_values(self):
        reported = [(0, {"concept": "us-gaap:Revenue", "metric": "Revenue", "value": 383,
                         "unit": "USD", "period": "FY2023", "period_end": "2023-09-30"}),
                    (1, {"concept": "us-gaap:DeferredRevenueRecognized",
                         "metric": "Deferred revenue recognized", "value": 8.2, "unit": "USD",
                         "period": "FY2023", "period_end": "2023-09-30"})]
        decision = json.dumps({"i": 0, "dominant": False, "alternatives": [0, 1],
                               "why": "the wording is broad"})
        with mock.patch.object(driver, "ask_llm", return_value=decision):
            chosen = driver._pick_by_data("revenue", reported, log=False)
        self.assertEqual(len(chosen["_ambiguity"]["options"]), 2)
        same = [(rank, {**row, "value": 383}) for rank, row in reported]
        with mock.patch.object(driver, "ask_llm", return_value=decision):
            chosen = driver._pick_by_data("revenue", same, log=False)
        self.assertNotIn("_ambiguity", chosen)

    def test_broad_revenue_cannot_silently_choose_a_specialized_sibling(self):
        reported = [(0, {"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                         "metric": "Revenue from customer contracts", "value": 383285000000,
                         "unit": "USD", "period": "FY2023", "period_end": "2023-09-30"}),
                    (1, {"concept": "us-gaap:ContractWithCustomerLiabilityRevenueRecognized",
                         "metric": "Deferred revenue recognized", "value": 8200000000,
                         "unit": "USD", "period": "FY2023", "period_end": "2023-09-30"})]
        wrong = json.dumps({"i": 1, "dominant": True, "alternatives": []})
        with mock.patch.object(driver, "ask_llm", return_value=wrong):
            chosen = driver._pick_by_data("revenue", reported, log=False)
        self.assertEqual(chosen["concept"],
                         "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")
        self.assertEqual([o["value"] for o in chosen["_ambiguity"]["options"]],
                         [383285000000, 8200000000])

    def test_sec_follow_up_fetches_the_chosen_concept_without_reselection(self):
        concept = "FixtureChosenRevenue"
        hit = {"identifier": "sources/sec-edgar/fixture-chosen-revenue.md"}
        fm = {"concept": concept, "title": "Chosen revenue — SEC EDGAR",
              "unit": "currency", "periodType": "duration", "resource": "SEC fixture"}
        filing = {"entityName": "Apple Inc.", "units": {"USD": [{
            "form": "10-K", "start": "2022-09-25", "end": "2023-09-30", "val": 383285000000}]}}
        with mock.patch.object(driver, "_concept_meta", return_value=hit), \
             mock.patch.object(driver, "frontmatter", return_value=fm), \
             mock.patch.object(driver, "_sec_concept", return_value=filing), \
             mock.patch.object(ard_client, "search") as search:
            result = driver.fetch_metric("revenue", cik="320193", period="FY2023",
                                         concept=f"us-gaap:{concept}", log=False)
        search.assert_not_called()
        self.assertEqual(result["concept"], f"us-gaap:{concept}")
        self.assertEqual(result["value"], 383285000000)


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
    def test_reranker_is_low_reasoning_and_output_bounded(self):
        with mock.patch.dict(os.environ, {"ARD_RERANK_MAX_TOKENS": "321",
                                          "ARD_RERANK_REASONING_EFFORT": "low"}), \
             mock.patch.object(driver, "ask_llm", return_value='{"ranked":[]}') as ask:
            index._rerank("revenue", [{"title": "Revenue"}], 1)
        self.assertEqual(ask.call_args.kwargs["max_tokens"], 321)
        self.assertEqual(ask.call_args.kwargs["reasoning_effort"], "low")

    def test_multi_query_search_embeds_once_and_unions_by_best_similarity(self):
        import numpy as np
        vectors = np.asarray([[1.0, 0.0], [0.0, 1.0], [.7, .7]], dtype=np.float32)
        metadata = [{"identifier": f"sources/sec-edgar/fixture-{i}.md", "title": f"Fixture {i}"}
                    for i in range(3)]
        query_vectors = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        with mock.patch.object(index, "_store", return_value=(vectors, metadata)), \
             mock.patch.object(index, "embed", return_value=query_vectors) as embed:
            results = index.search_many(["revenue", "Apple revenue"], k=2,
                                        sources=["sec-edgar"], rerank=False)
        embed.assert_called_once_with(["revenue", "Apple revenue"])
        self.assertEqual({result["identifier"] for result in results}, {
            "sources/sec-edgar/fixture-0.md", "sources/sec-edgar/fixture-1.md"})

    def test_embedding_only_search_honors_k_beyond_rerank_prefilter(self):
        import numpy as np
        vectors = np.asarray([[1.0, i / 1000] for i in range(30)], dtype=np.float32)
        metadata = [{"identifier": f"sources/sec-edgar/fixture-{i}.md", "title": f"Fixture {i}"}
                    for i in range(30)]
        with mock.patch.object(index, "_store", return_value=(vectors, metadata)), \
             mock.patch.object(index, "embed", return_value=np.asarray([[1.0, 0.0]], dtype=np.float32)):
            results = index.search("revenue", k=25, sources=["sec-edgar"], rerank=False)
        self.assertEqual(len(results), 25)

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


class SearchOrderTests(unittest.TestCase):
    """A verdict about a table must not be re-asked once per entity, key and period."""

    STEPS = [("hit", lambda s: ["A", "B", "C"]),
             ("entity", lambda s: ["e1", "e2", "e3"]),
             ("period", lambda s: ["p1", "p2"])]

    def test_rejecting_a_table_skips_its_whole_subtree(self):
        seen = []

        def goal(state):
            seen.append((state["hit"], state["entity"], state["period"]))
            if state["hit"] != "C":
                raise harness.Prune("hit", "this table measures something else")
            return state

        result = harness._solve(self.STEPS, goal, {})
        # One attempt per table, not one per table x entity x period.
        self.assertEqual(len(seen), 3)
        self.assertEqual(result["hit"], "C")

    def test_an_ordinary_dead_end_still_tries_the_next_key(self):
        """A table with no data for one key may have data for the next; only Prune skips."""
        seen = []

        def goal(state):
            seen.append((state["hit"], state["entity"], state["period"]))
            if (state["hit"], state["entity"]) != ("B", "e2"):
                raise harness.Backtrack("no data for this key")
            return state

        result = harness._solve(self.STEPS, goal, {})
        self.assertEqual((result["hit"], result["entity"]), ("B", "e2"))
        self.assertGreater(len(seen), 3)

    def test_a_prune_is_consumed_only_by_the_choice_it_names(self):
        seen = []

        def goal(state):
            seen.append(state["entity"])
            if state["entity"] != "e3":
                raise harness.Prune("entity", "entity cannot answer this")
            return state

        harness._solve(self.STEPS, goal, {})
        self.assertEqual(seen, ["e1", "e2", "e3"])


class EntityTypeGateTests(unittest.TestCase):
    """The reported leak: a question about the city of Detroit fetched for nonprofits.

    The fixture holds the REAL Wikidata claims and P31 class labels for every candidate the
    resolver returns for the mention "Detroit", captured from the live API. An identifier-only
    rule passes University of Detroit Mercy, which carries no EIN at all, so this is the case
    that proves the production bug rather than the helper.
    """

    with open(os.path.join(ROOT, "tests", "fixtures", "detroit_candidates.json")) as f:
        DETROIT = json.load(f)

    def _keep(self, thint):
        return {d["label"] for d in self.DETROIT.values()
                if harness._type_compatible(d["keys"], thint, d["classes"])}

    def test_only_the_city_answers_a_place_question(self):
        self.assertEqual(self._keep("place"), {"Detroit"})

    def test_the_reported_leaks_are_both_dropped(self):
        for qid in ("Q1320232", "Q1201549"):          # U of Detroit Mercy, Detroit Institute of Arts
            d = self.DETROIT[qid]
            self.assertFalse(harness._type_compatible(d["keys"], "place", d["classes"]),
                             f"{d['label']} must not answer a question about a place")

    def test_an_entity_with_no_identifiers_is_still_judged(self):
        """University of Detroit Mercy has no EIN; absence of identifiers cannot mean 'allow'."""
        self.assertEqual(self.DETROIT["Q1320232"]["keys"], {})
        self.assertFalse(harness._type_compatible({}, "place", ["university"]))

    def test_sports_teams_and_creative_works_are_dropped(self):
        for qid in ("Q1642809", "Q194116", "Q271880", "Q141104587"):
            d = self.DETROIT[qid]
            self.assertFalse(harness._type_compatible(d["keys"], "place", d["classes"]))

    def test_no_evidence_at_all_still_passes(self):
        """Absence of both identifiers and classes is not disqualifying."""
        self.assertTrue(harness._type_compatible({}, "place", []))

    def test_identifiers_of_the_requested_kind_accept_immediately(self):
        self.assertTrue(harness._type_compatible({"fips_place": "2622000"}, "place", []))
        self.assertTrue(harness._type_compatible({"cik": "320193"}, "company", []))

    def test_no_type_hint_never_rejects(self):
        self.assertTrue(harness._type_compatible({"ein": "1"}, None, ["university"]))


class EligibilityGateTests(unittest.TestCase):
    """Candidates whose declared subject cannot answer the question cost no fetch."""

    HITS = [{"identifier": "sources/census/poverty.md", "title": "Poverty rate"},
            {"identifier": "sources/census/broadband.md", "title": "Broadband subscriptions"}]

    def test_ineligible_candidates_are_dropped(self):
        with mock.patch.object(harness.TK, "llm", return_value=json.dumps({"eligible": [1]})):
            kept, dropped = harness._eligible("broadband in Detroit", self.HITS)
        self.assertEqual([h["identifier"] for h in kept], ["sources/census/broadband.md"])
        self.assertEqual(dropped, 1)

    def test_a_broken_judge_fails_open(self):
        """A judge that cannot answer must not make the engine refuse answerable questions."""
        with mock.patch.object(harness.TK, "llm", side_effect=RuntimeError("provider down")):
            kept, dropped = harness._eligible("broadband in Detroit", self.HITS)
        self.assertEqual(kept, self.HITS)
        self.assertIsNone(dropped)

    def test_a_malformed_verdict_fails_open(self):
        with mock.patch.object(harness.TK, "llm", return_value=json.dumps({"eligible": "all"})):
            kept, _ = harness._eligible("broadband in Detroit", self.HITS)
        self.assertEqual(kept, self.HITS)

    def test_judging_everything_ineligible_is_allowed(self):
        """An empty result is the honest outcome when nothing measures what was asked."""
        with mock.patch.object(harness.TK, "llm", return_value=json.dumps({"eligible": []})):
            kept, dropped = harness._eligible("broadband in Detroit", self.HITS)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 2)


if __name__ == "__main__":
    unittest.main()
