#!/usr/bin/env python3
"""
Shared, hardened HTTP layer for GoHighLevel calls.

Exists because of a real outage mode we hit on 18 Sept 2026:

  Cloudflare in front of services.leadconnectorhq.com rejects Python's default
  User-Agent ("Python-urllib/3.x") with HTTP 403 "Error 1010: Access denied -
  The site owner has blocked access based on your browser's signature."

The block itself was survivable. What made it dangerous was that the caller did
`response.get("opportunities", [])`, got `[]`, and cheerfully reported
"nothing to mirror" with exit code 0 — a hard failure wearing the costume of a
clean run. On a 15-minute schedule that would have quietly lost every
application until somebody noticed by hand.

So this module does three things:
  1. Always sends a real browser User-Agent.
  2. Retries 403 / 429 / 5xx with exponential backoff.
  3. Raises GHLBlocked / GHLError instead of returning something falsy, so a
     failure can never be mistaken for "no data".
"""
import json, time, urllib.request, urllib.error

BASE = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"

# A genuine desktop Chrome UA. Do NOT use "curl/x" or the urllib default —
# both are on Cloudflare's signature blocklist for this host.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

RETRY_ON = {403, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF = 2.0


class GHLError(RuntimeError):
    """Any non-2xx that survived the retries."""


class GHLBlocked(GHLError):
    """Cloudflare bot-signature block (Error 1010) — almost always the UA."""


def _is_cloudflare_block(status, body):
    return status == 403 and ("error-1010" in body or "Access denied" in body
                              or "cloudflare" in body.lower())


def request(method, path, token, payload=None, base=BASE, timeout=45):
    """Returns parsed JSON, or raises. Never returns an empty dict on failure."""
    url = path if path.startswith("http") else base + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Version": API_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:400]
            except Exception:
                pass
            last = (e.code, body)
            if _is_cloudflare_block(e.code, body) and attempt == MAX_ATTEMPTS:
                raise GHLBlocked(
                    f"Cloudflare blocked {method} {url} (HTTP 403, Error 1010) after "
                    f"{attempt} attempts. The User-Agent is almost certainly the cause. "
                    f"Body: {body[:200]}")
            if e.code not in RETRY_ON or attempt == MAX_ATTEMPTS:
                raise GHLError(f"{method} {url} -> HTTP {e.code}: {body[:200]}")
        except Exception as e:
            last = ("EXC", str(e))
            if attempt == MAX_ATTEMPTS:
                raise GHLError(f"{method} {url} -> {type(e).__name__}: {e}")
        time.sleep(BACKOFF ** attempt)
    raise GHLError(f"{method} {url} failed after {MAX_ATTEMPTS} attempts: {last}")


def get(path, token, **kw):
    return request("GET", path, token, **kw)


def post(path, token, payload, **kw):
    return request("POST", path, token, payload=payload, **kw)


def preflight(token, location_id):
    """Cheap call that proves auth + Cloudflare are both happy. Raises if not."""
    d = get(f"/opportunities/pipelines?locationId={location_id}", token)
    pipes = d.get("pipelines")
    if pipes is None:
        raise GHLError(f"preflight returned no 'pipelines' key: {str(d)[:200]}")
    return pipes
