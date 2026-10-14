#!/usr/bin/env python3
"""
Is every recent lead actually in Close?

Built 14 Oct 2026 after Ollie reported leads "not showing inside of Close". An audit of every
source against Close found the leads mostly WERE there, but in the wrong shape or under
another address, and that nothing had been watching:
  * the 26 Sept fix that keeps applications and statuses in step sat unpushed for 18 days,
    so 12 applicants had no card on the DFY Applications board and 21 statuses were stale
  * payers who used a different email were invisible (Jacob Talmer, $18,000, as
    jtalmer@proton.me, while Close had "Jake Talmer" on Closer Follow Up)
  * Calendly bookings made under a second address (vinny.patel207 vs vinay.patel207)

Each sync only proves its OWN writes. This asks the question from the other end: take every
person who reached a source in the last WINDOW_DAYS, and look for them in Close by email,
then by phone. Sources:
  GHL contacts      (GHL_READ_PIT - needs contacts.readonly, which the sync's GHL_PIT lacks)
  Calendly invitees (CALENDLY_PAT)
  Stripe payers     (STRIPE_KEY - the restricted read-only key)
A source whose credential is absent is reported, never silently skipped.

Usage:  python3 close_coverage.py            prints every miss, exit 1 if any
        from close_coverage import check     sync_health.py's "every lead is in Close"
"""
import os, re, sys, json, time, base64, datetime, urllib.parse, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghl_http

WINDOW_DAYS = 7
# The relay writes in seconds and the CI sweeps every ~15 min; anything younger than this
# is still in flight and not yet a miss.
GRACE_MIN = 45
LOC = "2RrjNuz0M4MTFlxpW37k"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
# Test traffic and the team's own addresses are never leads.
NOT_A_LEAD = re.compile(r"(@example\.com$|@dealenginegroup\.com$|@dealenginehq\.com$|"
                        r"^charlieknight\d*@|^charlietest@|^ollieknight31|^zz|test)", re.I)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _ts(s):
    return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _get(url, headers, method="GET", body=None):
    for attempt in range(4):
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"User-Agent": UA, "Accept": "application/json",
                                              "Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                return json.load(f)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt); continue
            raise RuntimeError(f"HTTP {e.code} on {url.split('?')[0]}: {e.read()[:160]!r}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 3:
                time.sleep(2 ** attempt); continue
            raise RuntimeError(f"{url.split('?')[0]} unreachable: {e}")


def _phone(p):
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else None


# ---- the people each source saw ---------------------------------------------

def ghl_people(since):
    tok = os.environ.get("GHL_READ_PIT", "")
    if not tok:
        raise RuntimeError("GHL_READ_PIT not set - cannot read GHL contacts")
    out, page = [], 1
    while True:
        d = ghl_http.request("POST", "/contacts/search", tok, {
            "locationId": LOC, "pageLimit": 100, "page": page,
            "sort": [{"field": "dateAdded", "direction": "desc"}]})
        rows = d.get("contacts")
        if rows is None:
            raise RuntimeError(f"GHL search returned no contacts list: {str(d)[:160]}")
        for c in rows:
            if _ts(c["dateAdded"]) < since:
                return out
            name = " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x)
            out.append(("GHL", name, c.get("email"), c.get("phone"), c["dateAdded"]))
        if len(rows) < 100:
            return out
        page += 1


def calendly_people(since):
    tok = os.environ.get("CALENDLY_PAT", "")
    if not tok:
        raise RuntimeError("CALENDLY_PAT not set - cannot read Calendly bookings")
    h = {"Authorization": f"Bearer {tok}"}
    org = _get("https://api.calendly.com/users/me", h)["resource"]["current_organization"]
    out = []
    # A booking made this week can be for any date ahead, so walk events from the window start on.
    url = "https://api.calendly.com/scheduled_events?" + urllib.parse.urlencode(
        {"organization": org, "count": 100, "min_start_time": since.isoformat().replace("+00:00", "Z")})
    while url:
        d = _get(url, h)
        for e in d["collection"]:
            for i in _get(e["uri"] + "/invitees?count=100", h)["collection"]:
                if _ts(i["created_at"]) >= since:
                    out.append(("Calendly", i.get("name"), i.get("email"), i.get("text_reminder_number"), i["created_at"]))
        url = d["pagination"].get("next_page")
    return out


def stripe_people(since):
    key = os.environ.get("STRIPE_KEY", "")
    if not key:
        raise RuntimeError("STRIPE_KEY not set - cannot read Stripe payers")
    h = {"Authorization": f"Bearer {key}"}
    out, after = [], None
    while True:
        q = {"limit": 100, "created[gte]": int(since.timestamp())}
        if after:
            q["starting_after"] = after
        d = _get("https://api.stripe.com/v1/charges?" + urllib.parse.urlencode(q), h)
        for c in d["data"]:
            bd = c.get("billing_details") or {}
            email = bd.get("email") or c.get("receipt_email")
            when = datetime.datetime.fromtimestamp(c["created"], datetime.timezone.utc).isoformat()
            out.append(("Stripe", bd.get("name"), email, bd.get("phone"), when))
        if not d.get("has_more"):
            return out
        after = d["data"][-1]["id"]


# ---- Close -----------------------------------------------------------------

def _close(path):
    key = os.environ.get("CLOSE_API_KEY", "")
    if not key:
        raise RuntimeError("CLOSE_API_KEY not set")
    return _get("https://api.close.com/api/v1" + path,
                {"Authorization": "Basic " + base64.b64encode((key + ":").encode()).decode()})


def in_close(email, phone):
    """Search by email, then phone. Ids from search are stable; that is all this needs."""
    for q in ([f'email:"{email}"'] if email else []) + ([f'phone:"{_phone(phone)}"'] if _phone(phone) else []):
        d = _close("/lead/?" + urllib.parse.urlencode({"query": q, "_limit": 1, "_fields": "id"}))
        if d.get("data"):
            return True
    return False


def recent_close_keys(since):
    """Emails and phones on every Close lead created since `since`, in a handful of paged
    reads - so only people outside that set cost a search each. Every Close caller in this
    business shares one rate budget, and this runs hourly."""
    keys, skip = set(), 0
    q = f'created > "{since.date().isoformat()}"'
    while True:
        d = _close("/lead/?" + urllib.parse.urlencode(
            {"query": q, "_limit": 200, "_skip": skip, "_fields": "id,contacts"}))
        for lead in d.get("data") or []:
            for c in lead.get("contacts") or []:
                keys.update(e["email"].strip().lower() for e in c.get("emails") or [])
                keys.update(k for k in (_phone(p["phone"]) for p in c.get("phones") or []) if k)
        if not d.get("has_more"):
            return keys
        skip += 200


def misses(window_days=WINDOW_DAYS):
    """(misses, counts, unreadable) - unreadable lists sources that could not be asked."""
    since = _now() - datetime.timedelta(days=window_days)
    cutoff = _now() - datetime.timedelta(minutes=GRACE_MIN)
    people, counts, unreadable = [], {}, []
    for name, fn in (("GHL", ghl_people), ("Calendly", calendly_people), ("Stripe", stripe_people)):
        try:
            rows = fn(since)
            counts[name] = len(rows)
            people += rows
        except Exception as e:
            unreadable.append(f"{name}: {str(e)[:160]}")
    known = recent_close_keys(since - datetime.timedelta(days=1))
    seen, out = set(), []
    for src, name, email, phone, when in people:
        email = (email or "").strip().lower()
        key = email or _phone(phone)
        if not key or key in seen or _ts(when) > cutoff:
            continue
        if NOT_A_LEAD.search(email) or NOT_A_LEAD.search(name or ""):
            continue
        seen.add(key)
        if email in known or _phone(phone) in known:
            continue
        if not in_close(email, phone):
            out.append((src, name, email, phone, when))
    return out, counts, unreadable


def check():
    """For sync_health.py: PASS text, or raise with every miss named."""
    out, counts, unreadable = misses()
    seen = ", ".join(f"{k} {v}" for k, v in counts.items())
    if out:
        names = "; ".join(f"{n or '?'} <{e or p}> via {s} {w[:10]}" for s, n, e, p, w in out[:12])
        more = f" (+{len(out) - 12} more)" if len(out) > 12 else ""
        raise RuntimeError(f"{len(out)} lead(s) from the last {WINDOW_DAYS} days are NOT in Close: {names}{more}")
    if unreadable:
        raise RuntimeError(f"could not read {'; '.join(unreadable)} (checked {seen or 'nothing'})")
    return f"all present ({seen}, last {WINDOW_DAYS} days)"


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else WINDOW_DAYS
    out, counts, unreadable = misses(days)
    print(f"checked {counts} over {days} days")
    for u in unreadable:
        print("  UNREADABLE", u)
    for s, n, e, p, w in out:
        print(f"  MISSING  {w[:16]}  {s:8}  {n or '?':28}  {e or '-':36}  {p or ''}")
    sys.exit(1 if out or unreadable else 0)
