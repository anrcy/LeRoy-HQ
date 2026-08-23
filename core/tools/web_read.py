#!/usr/bin/env python3
"""
Free page reader - Jina Reader backend for the `web_read` capability.

Wraps https://r.jina.ai/<URL>, which returns any public page as clean markdown.
This is tier 2 of the web_read chain: use it when WebFetch fails, is blocked, or
returns an SPA shell instead of content.

AUTH REALITY CHECK (verified 2026-08-12): Agent-Reach documents this endpoint as
zero-config. It is not, from this machine. Anonymous requests are rejected with
HTTP 401 "blocked from performing anonymous queries due to bad network reputation
(AS7018)" - i.e. the ISP's ASN is denylisted for anonymous use, not the endpoint
being retired. A free Jina key (no card) restores it:

    setx JINA_API_KEY "jina_..."     # from https://jina.ai/reader

Without the key this backend reports UNAVAILABLE and the caller falls to the next
link in the chain. It never silently returns nothing.

Pattern taken from Agent-Reach (MIT). No Agent-Reach code or package is installed.

Usage:
    python tools/web_read.py https://example.com
    python tools/web_read.py https://example.com --max-chars 4000
    python tools/web_read.py https://example.com --json

LinkedIn note: PUBLIC pages only. Fetching login-walled LinkedIn pages violates
their terms, so this tool refuses known login-walled paths outright rather than
leaving that judgement to the caller.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

JINA_PREFIX = "https://r.jina.ai/"
USER_AGENT = "Mozilla/5.0 (compatible; leroy-web-read/1.0)"

# Paths that only render for a logged-in session. Fetching these would mean
# scraping behind an auth wall, which this tool will not do.
LINKEDIN_WALLED = ("/feed", "/mynetwork", "/messaging", "/notifications",
                   "/jobs/application", "/sales/", "/recruiter", "/talent")


def check_allowed(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme: {parsed.scheme or '(none)'}"
    host = (parsed.hostname or "").lower()
    if host.endswith("linkedin.com"):
        path = parsed.path.lower()
        if any(path.startswith(w) for w in LINKEDIN_WALLED):
            return ("refusing login-walled LinkedIn path - public pages only "
                    "(fetching these would mean scraping behind an auth wall)")
    return None


def fetch(url, timeout=45):
    target = JINA_PREFIX + url
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/plain, text/markdown, */*",
    }
    key = os.environ.get("JINA_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(target, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main():
    p = argparse.ArgumentParser(description="Read a web page as markdown, no API key")
    p.add_argument("url")
    p.add_argument("--max-chars", type=int, default=0, help="Truncate output (0 = no limit)")
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    refusal = check_allowed(args.url)
    if refusal:
        print(f"[XX] {refusal}", file=sys.stderr)
        return 2

    try:
        body = fetch(args.url, args.timeout)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            has_key = bool(os.environ.get("JINA_API_KEY", "").strip())
            print("[XX] Jina Reader UNAVAILABLE: anonymous access is blocked from this "
                  "network (ISP ASN denylisted).", file=sys.stderr)
            print("     FIX: free key, no card -> https://jina.ai/reader  then "
                  'setx JINA_API_KEY "jina_..."' if not has_key
                  else "     JINA_API_KEY is set but was rejected - the key may be invalid or expired.",
                  file=sys.stderr)
            print("     Falling back: use the next backend in the web_read chain "
                  "(python tools/fleet_chain.py web_read).", file=sys.stderr)
            return 3
        print(f"[XX] Jina Reader returned HTTP {e.code} for {args.url}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[XX] fetch failed: {e}", file=sys.stderr)
        return 1

    truncated = False
    if args.max_chars and len(body) > args.max_chars:
        body = body[:args.max_chars]
        truncated = True

    if args.json:
        print(json.dumps({
            "url": args.url,
            "backend": "jina",
            "chars": len(body),
            "truncated": truncated,
            "content": body,
        }, indent=2))
    else:
        print(body)
        if truncated:
            print(f"\n[...truncated at {args.max_chars} chars]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
