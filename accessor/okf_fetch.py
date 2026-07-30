#!/usr/bin/env python3
"""Generic OKF data accessor.

Reads an *actionable* OKF document, resolves its access operations, fills a named
operation's URL (and optional POST body) from params, performs the request, and
prints the JSON response. No source-specific code.

Operation shapes (in an OKF `access.operations` block):
  GET:   {method: GET, url: "...{param}..."}            # {param} -> URL-encoded value
  POST:  {method: POST, url: "...", body: '{"q":"$p"}'} # $p -> value via string.Template

A leaf entry with no `access` block but a `source:` cross-link inherits the
linked doc's operations; its frontmatter supplies default params.

Usage:
  okf_fetch.py <okf_doc.md> <operation> [k=v ...] [--extract a.b.0.c]
"""
import sys, os, re, json, time, string, urllib.parse, urllib.request, urllib.error
import yaml


def _fetch_with_retry(req, tries=4):
    """Fetch, retrying transient failures (dropped connections, timeouts, 429/5xx) with backoff.
    A flaky endpoint must not read as 'no data' — that would let the harness backtrack to a wrong
    source. A 4xx other than 429 is a real answer (e.g. 404 = concept not reported) and is not retried."""
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise SystemExit(f"HTTP {e.code} for {req.full_url}\n{e.read().decode('utf-8')[:500]}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == tries - 1:
                raise SystemExit(f"network error for {req.full_url}: {e}")
        time.sleep(1.5 * (attempt + 1))                       # linear backoff before the next attempt


def load_okf(path):
    text = open(path, encoding="utf-8").read()
    if not text.startswith("---"):
        raise SystemExit(f"{path}: no YAML frontmatter")
    _, fm, _b = text.split("---", 2)
    return yaml.safe_load(fm) or {}


def resolve_access(fm, okf_path):
    if fm.get("access"):
        return fm["access"]
    if fm.get("source"):
        p = os.path.normpath(os.path.join(os.path.dirname(okf_path), fm["source"]))
        return load_okf(p).get("access", {})
    raise SystemExit(f"{okf_path}: no access block and no source link")


def placeholders(op):
    fields = {fn for _, fn, _, _ in string.Formatter().parse(op["url"]) if fn}
    if op.get("body"):
        fields |= set(re.findall(r"\$(\w+)", op["body"]))
    return fields


def extract(obj, dotted):
    for part in dotted.split("."):
        obj = obj[int(part)] if part.lstrip("-").isdigit() else obj[part]
    return obj


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    okf_path, operation = argv[0], argv[1]
    params, dotted = {}, None
    i = 2
    while i < len(argv):
        if argv[i] == "--extract":
            dotted = argv[i + 1]; i += 2; continue
        k, _, v = argv[i].partition("="); params[k] = v; i += 1

    fm = load_okf(okf_path)
    access = resolve_access(fm, okf_path)
    ops = access.get("operations", {})
    if operation not in ops:
        raise SystemExit(f"unknown operation '{operation}'. have: {list(ops)}")
    op = ops[operation]

    # default-fill any placeholder (url or body) from the leaf's frontmatter
    for f in placeholders(op):
        if f not in params and f in fm:
            params[f] = str(fm[f])

    # resolve secrets referenced as "env:NAME" (keeps API keys out of the OKF docs)
    for k, v in list(params.items()):
        if isinstance(v, str) and v.startswith("env:"):
            params[k] = os.environ.get(v[4:], "")

    url_params = {k: urllib.parse.quote(str(v), safe="=&[]-:/,@") for k, v in params.items()}
    url = op["url"].format(**url_params)
    headers = {**access.get("headers", {}), **op.get("headers", {})}
    data = None
    if op.get("body"):
        data = string.Template(op["body"]).safe_substitute(params).encode()
        headers.setdefault("Content-Type", "application/json")
    method = op.get("method", "POST" if data else "GET").upper()

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    body = _fetch_with_retry(req)
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        # A non-JSON body is an API error masquerading as data (e.g. the Census API redirects a
        # keyless request to a "Missing Key" HTML page). Surface it clearly instead of a decode
        # traceback, so the harness fails loudly rather than treating it as a backtrack-able miss.
        low = body.lower()
        if "missing key" in low or "missing_key" in low or "api key" in low or "api_key" in low:
            # CREDENTIAL_ERROR: is a stable marker the driver classifies across the subprocess boundary
            # so the harness stops the search immediately instead of backtracking (~2 min) over sources
            # that can never answer without the key.
            raise SystemExit(f"CREDENTIAL_ERROR: {url[:120]} requires an API key; set it "
                             f"(e.g. CENSUS_API_KEY / DATA_GOV_API_KEY) and retry.\n{body[:200]}")
        raise SystemExit(f"non-JSON response from {url[:120]}\n{body[:300]}")
    if dotted:
        try:
            result = extract(result, dotted)
        except (KeyError, IndexError, TypeError):
            pass                                          # bad extract path -> return full result
    print(json.dumps(result, indent=2) if not isinstance(result, str) else result)


if __name__ == "__main__":
    main(sys.argv[1:])
