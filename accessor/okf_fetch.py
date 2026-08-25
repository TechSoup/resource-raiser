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
import asyncio, glob, sys, os, re, json, time, string, urllib.parse, urllib.request, urllib.error
from functools import lru_cache

import httpx
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import runtime


class PublisherRateLimitError(RuntimeError):
    pass


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


@lru_cache(maxsize=None)
def load_okf(path):
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
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


def preload_descriptors(sources_root=None):
    """Load immutable descriptor frontmatter before readiness; later fetches do no file reads."""
    root = sources_root or os.path.join(ROOT, "sources")
    paths = sorted(glob.glob(os.path.join(root, "**", "*.md"), recursive=True))
    for path in paths:
        load_okf(path)
    return len(paths)


def placeholders(op):
    fields = {fn for _, fn, _, _ in string.Formatter().parse(op["url"]) if fn}
    if op.get("body"):
        fields |= set(re.findall(r"\$(\w+)", op["body"]))
    return fields


def extract(obj, dotted):
    for part in dotted.split("."):
        obj = obj[int(part)] if part.lstrip("-").isdigit() else obj[part]
    return obj


def _request(okf_path, operation, params):
    """Resolve one immutable descriptor into an HTTP request without performing I/O."""
    fm = load_okf(okf_path)
    access = resolve_access(fm, okf_path)
    ops = access.get("operations", {})
    if operation not in ops:
        raise SystemExit(f"unknown operation '{operation}'. have: {list(ops)}")
    op = ops[operation]
    params = dict(params)
    for field in placeholders(op):
        if field not in params and field in fm:
            params[field] = str(fm[field])
    for key, value in list(params.items()):
        if isinstance(value, str) and value.startswith("env:"):
            params[key] = os.environ.get(value[4:], "")
    url_params = {key: urllib.parse.quote(str(value), safe="=&[]-:/,@")
                  for key, value in params.items()}
    url = op["url"].format(**url_params)
    headers = {**access.get("headers", {}), **op.get("headers", {})}
    body = None
    if op.get("body"):
        body = string.Template(op["body"]).safe_substitute(params).encode()
        headers.setdefault("Content-Type", "application/json")
    return op.get("method", "POST" if body else "GET").upper(), url, headers, body


def _decode(body, url, dotted=None):
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        low = body.lower()
        if "missing key" in low or "missing_key" in low or "api key" in low or "api_key" in low:
            raise SystemExit(f"CREDENTIAL_ERROR: {url[:120]} requires an API key; set it "
                             f"(e.g. CENSUS_API_KEY / DATA_GOV_API_KEY) and retry.\n{body[:200]}")
        raise SystemExit(f"non-JSON response from {url[:120]}\n{body[:300]}")
    if dotted:
        try:
            result = extract(result, dotted)
        except (KeyError, IndexError, TypeError):
            pass
    return result


def fetch(okf_path, operation, params=None, dotted=None):
    """In-process synchronous compatibility path; removes the subprocess boundary immediately."""
    method, url, headers, body = _request(okf_path, operation, params or {})
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    return _decode(_fetch_with_retry(request), url, dotted)


async def fetch_async(okf_path, operation, params=None, dotted=None, *, context, tries=4):
    """Native async descriptor fetch using the application-owned HTTPX client."""
    context.check()
    if context.http_client is None:
        raise RuntimeError("async publisher access requires QueryContext.http_client")
    method, url, headers, body = _request(okf_path, operation, params or {})
    response = None
    for attempt in range(tries):
        delay = 1.5 * (attempt + 1)
        try:
            response = await context.provider_call("publisher", lambda: context.http_client.request(
                method, url, headers=headers, content=body, timeout=min(40, context.remaining() or 40)))
        except (asyncio.CancelledError, runtime.QueryCancelled):
            raise
        except httpx.RequestError as exc:
            if attempt == tries - 1:
                raise SystemExit(f"network error for {url}: {exc}") from exc
        else:
            if response.status_code not in (429, 500, 502, 503, 504):
                break
            if attempt == tries - 1:
                break
            if response.status_code == 429:
                try:
                    delay = float(response.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    pass
                # Some APIs return the seconds until a daily quota resets (College Scorecard
                # returned 11,057). Sleeping that long merely converts a clear 429 into the
                # query's opaque deadline error. Honor short throttles; report long ones now.
                max_retry = float(os.getenv("PUBLISHER_MAX_RETRY_AFTER_SECONDS", "30"))
                remaining = context.remaining()
                if delay > max_retry or (remaining is not None and delay >= remaining):
                    break
        await context.sleep(delay)
    if response is None:
        raise SystemExit(f"network error for {url}")
    if response.status_code == 429:
        raise PublisherRateLimitError(
            f"{urllib.parse.urlparse(url).netloc} is temporarily rate limiting requests; "
            "please try again shortly")
    if response.is_error:
        raise SystemExit(f"HTTP {response.status_code} for {url}\n{response.text[:500]}")
    return _decode(response.text, url, dotted)


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

    result = fetch(okf_path, operation, params, dotted)
    print(json.dumps(result, indent=2) if not isinstance(result, str) else result)


if __name__ == "__main__":
    main(sys.argv[1:])
