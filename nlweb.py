#!/usr/bin/env python3
"""The NLWeb protocol, spoken over the Resource Raiser engine.

This is the ONLY query contract the server offers. The engine's own shape — a synthesized answer
plus the table it came from — maps onto NLWeb's message stream without inventing anything:

    begin-nlweb-response   the query started
    intermediate_message   the play-by-play (discovery, plan, resolve, fetch, acceptance check)
    result                 the ARD candidate tables, as NLWeb Items
    nlws                   {"@type": "GeneratedAnswer", "answer": …, "items": […]}
    complete / end-nlweb-response

An OKF **source** is an NLWeb **site** — both name "the corpus a result came from" — so `?site=`
filters discovery to those sources, exactly as `sites=` does upstream. An OKF **table** is an
NLWeb **Item**: its `schema_object` is the descriptor's own frontmatter, which is the closest
thing this system has to structured data about a result, because it is literally that.

Request (GET or POST /ask): query, site, mode, max_results, min_score, streaming, sse_format,
conversation_id, on_ambiguity, assumptions, debug. Streaming is the default; `streaming=false`
returns one JSON document of the same messages, which is what upstream does. `assumptions` is a
JSON object either way: a GET client sends it encoded, since a query string has no nested objects.
"""
import json, time, uuid

# The message vocabulary. There are no others.
BEGIN = "begin-nlweb-response"
END = "end-nlweb-response"
CANDIDATE = "candidate"
RESULT = "result"
NLWS = "nlws"
INTERMEDIATE = "intermediate_message"
COMPLETE = "complete"
ERROR = "error"

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",          # a buffering proxy would destroy streaming
}

TIER_THRESHOLDS = ((80, "strong"), (50, "moderate"), (20, "weak"))


def tier_for(score):
    for threshold, tier in TIER_THRESHOLDS:
        if (score or 0) >= threshold:
            return tier
    return "none"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "+00:00"


class Stream:
    """Builds the message sequence for one query, numbering it as it goes.

    `sequence` is monotonic per query so a client can order messages and notice a gap without
    reading their contents — which also makes a captured stream readable after the fact.
    """

    def __init__(self, conversation_id=None):
        self.query_id = str(uuid.uuid4())
        self.conversation_id = conversation_id
        self.n = 0

    def message(self, message_type, content="", sender="assistant"):
        self.n += 1
        m = {
            "message_id": str(uuid.uuid4()),
            "message_type": message_type,
            "sender_type": sender,
            "timestamp": _now(),
            "query_id": self.query_id,
            "sequence": self.n,
            "content": content,
        }
        if self.conversation_id:
            m["conversation_id"] = self.conversation_id
        return m


def encode(msg, named=False):
    """One SSE frame. A newline inside the JSON would split the frame, so assert rather than
    trust — json.dumps escapes them, but the invariant is what matters."""
    payload = json.dumps(msg, separators=(",", ":"))
    assert "\n" not in payload, "message JSON must be single-line"
    if named:
        return f"event: {msg['message_type']}\nid: {msg['message_id']}\ndata: {payload}\n\n".encode()
    return f"data: {payload}\n\n".encode()


def item(hit, site=None):
    """An ARD candidate table as an NLWeb Item.

    `url` points at the table's ARD entry, so a client that follows it gets the descriptor rather
    than a dead identifier. `schema_object` carries the OKF frontmatter — the structured record the
    engine actually reasons over.
    """
    ident = hit.get("identifier", "")
    score = hit.get("score")
    score = int(score) if isinstance(score, (int, float)) else 0
    return {
        "@type": "Item",
        "url": f"ard?id={ident}",
        "name": hit.get("title") or ident,
        "site": site or hit.get("publisher") or "",
        "score": score,
        "tier": tier_for(score),
        "description": hit.get("description", "") or "",
        "schema_object": hit.get("schema_object") or {},
    }


def parse_request(params):
    """Flat HTTP params -> the request fields. Accepts `?a=1&a=2` lists and JSON scalars alike,
    because a query string and a JSON body disagree about that."""
    def one(key, default=None):
        v = params.get(key, default)
        if isinstance(v, (list, tuple)):
            return v[0] if v else default
        return v

    def as_int(key, fallback):
        raw = one(key)
        try:
            return int(raw) if raw not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    raw_site = one("site") or ""
    sites = ()
    if isinstance(raw_site, str) and raw_site.strip():
        if raw_site.strip().lower() != "all":          # "all" means no filter, matching NLWeb
            sites = tuple(s.strip() for s in raw_site.split(",") if s.strip())

    streaming = one("streaming", True)
    if isinstance(streaming, str):
        streaming = streaming.strip().lower() not in ("false", "0", "no")
    mode = str(one("mode") or "generate").strip().lower()
    if mode not in ("generate", "list"):
        mode = "generate"
    on_ambiguity = str(one("on_ambiguity") or "answer").strip().lower()
    if on_ambiguity not in ("answer", "ask", "all"):
        on_ambiguity = "answer"
    assumptions = {}
    assumptions_error = ""
    nested = one("assumptions")
    if isinstance(nested, str) and nested.strip():
        # A query string cannot carry a nested object, so a GET client sends it JSON-encoded. Decode
        # before the dict check below, or the binding is dropped and an ambiguous question silently
        # falls back to its FIRST interpretation: the caller resolves a clarification by choosing
        # "gross profit" and is told, with full confidence, Apple's net income.
        try:
            nested = json.loads(nested)
        except (TypeError, ValueError):
            nested, assumptions_error = None, "'assumptions' is not valid JSON"
        else:
            if not isinstance(nested, dict):
                nested, assumptions_error = None, "'assumptions' must be a JSON object"
    if isinstance(nested, dict):
        aliases = {"measure": "attribute", "operation": "shape"}
        for key in ("entity", "entity_qid", "type", "measure", "attribute", "period",
                    "operation", "shape", "concept"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                assumptions[aliases.get(key, key)] = value.strip()
    # entity_qid pins the RECORD a caller chose from an entity clarification. Without it here
    # the browser's payload is silently reduced to the label, the follow-up re-searches that
    # name, finds the same records and asks again - an infinite loop that no in-process test
    # sees, because those call run(assumptions=...) directly and never cross this parser.
    for param, field in (("assumption_entity", "entity"),
                         ("assumption_entity_qid", "entity_qid"),
                         ("assumption_type", "type"),
                         ("assumption_measure", "attribute"), ("assumption_period", "period"),
                         ("assumption_operation", "shape"),
                         ("assumption_concept", "concept")):
        value = one(param)
        if isinstance(value, str) and value.strip():
            assumptions[field] = value.strip()

    return {
        "query": (one("query") or one("question") or "").strip(),
        "sites": sites,
        "mode": mode,
        "max_results": max(1, min(as_int("max_results", 10), 100)),
        "min_score": max(0, min(as_int("min_score", 0), 100)),
        "streaming": bool(streaming),
        "named_events": str(one("sse_format") or "").strip().lower() == "named",
        "conversation_id": one("conversation_id"),
        "debug": str(one("debug") or "").strip().lower() in ("1", "true", "yes"),
        "on_ambiguity": on_ambiguity,
        "assumptions": assumptions,
        "assumptions_error": assumptions_error,
    }
