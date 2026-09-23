#!/usr/bin/env python3
"""
Skool free community -> GoHighLevel  (the Zapier replacement)

Mirrors the Zapier automation "Skool free community signup -> Close lead + Skool pipeline"
into GHL sub-account AA StrategyWorks, with none of Zapier's task cost and none of its
fragility:
  * a junk phone answer can never fail the record (the raw answer is kept in a text field,
    and only a genuinely parseable number goes into the phone field)
  * a malformed email is skipped and reported instead of silently losing the member
  * it is idempotent - rerunning never duplicates a contact or a pipeline card

Skool is read straight from the members page over plain HTTP - __NEXT_DATA__ is
server-rendered and carries the join-question answers, so no browser is needed. GHL is written through the public API with a Private Integration
token. No Zapier, no GHL premium inbound webhook.

Usage:
    skool_to_ghl.py                 # dry run over the most recent 2 pages
    skool_to_ghl.py --apply         # write to GHL
    skool_to_ghl.py --pages 55 --apply     # full backfill
"""
import json, re, sys, os, time, urllib.request, urllib.error

PIT   = os.environ["GHL_PIT"]
LOC   = "2RrjNuz0M4MTFlxpW37k"
PIPE  = "BaP4NEFnESaozLgCJio8"                      # 4. Skool Free Community
STAGE = "7c7ae6aa-7544-471b-8106-90df75ff86e6"      # New Member
CF = {"profile_url":"o8wRnHF4dKwgr9qttHkd",
      "joined":"8kUgdCXWmBaUjIdQXQMR",
      "join_answer":"LUZB9pSKPJmMCv8HiZDq",
      "phone_answer":"fpobnLVtWxDEfhmvXVF8"}
COMMUNITY = "portfolio-lab"

# Set by pull_skool() from Skool's own pageProps, so a caller can report the true
# community size without paging the whole member list.
LAST_TOTAL = None
LAST_TOTAL_PAGES = None
# True when the last pull_skool() walked all the way to Skool's own last page; False when it
# stopped early because the `pages` cap ran out. Anything that reasons about the ABSENCE of a
# member - departure detection above all - is only safe on a complete scan, because a member
# beyond the cap is indistinguishable from one who left.
LAST_SCAN_COMPLETE = False

try:
    import phonenumbers
except ImportError:
    # Previously this set phonenumbers = None and clean_phone() then returned None for
    # EVERY answer - silently discarding every phone number while reporting success.
    # Fail loudly instead.
    sys.exit("FATAL: phonenumbers is not installed, so every phone number would be "
             "silently dropped.  pip install phonenumbers")

COUNTRY_TO_ISO = {
 "united states":"US","united kingdom":"GB","canada":"CA","australia":"AU","india":"IN",
 "south africa":"ZA","nigeria":"NG","kenya":"KE","ghana":"GH","zimbabwe":"ZW","pakistan":"PK",
 "france":"FR","germany":"DE","the netherlands":"NL","netherlands":"NL","spain":"ES","italy":"IT",
 "sweden":"SE","norway":"NO","denmark":"DK","ireland":"IE","poland":"PL","romania":"RO",
 "portugal":"PT","belgium":"BE","switzerland":"CH","austria":"AT","greece":"GR","colombia":"CO",
 "brazil":"BR","mexico":"MX","argentina":"AR","chile":"CL","peru":"PE","philippines":"PH",
 "japan":"JP","china":"CN","singapore":"SG","malaysia":"MY","indonesia":"ID","israel":"IL",
 "united arab emirates":"AE","saudi arabia":"SA","egypt":"EG","turkey":"TR","jamaica":"JM",
 "trinidad and tobago":"TT","puerto rico":"PR","guatemala":"GT","belize":"BZ","new zealand":"NZ",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,24}$")

# ── Origin tags ──────────────────────────────────────────────────────────────
# Skool records how each member found the group (`attrSrcComp`). Until 23 Sept 2026 that
# only ever reached GHL as text inside the contact's `source` string, so a Skool joiner
# could not be filtered by channel alongside the funnel's `src-*` tags. The ids below are
# the relay's channel ids (de-funnel-relay/api/_origin.js, CHANNEL) - one vocabulary
# across all four funnels.
SKOOL_SRC = {
    "instagram.com": "ig-organic", "l.instagram.com": "ig-organic", "ig.me": "dm",
    "manychat.com": "dm", "m.me": "dm", "messenger.com": "dm",
    "facebook.com": "fb-organic", "l.facebook.com": "fb-organic", "lm.facebook.com": "fb-organic",
    "youtube.com": "youtube", "m.youtube.com": "youtube", "youtu.be": "youtube",
    "google.com": "google", "bing.com": "google", "tiktok.com": "tiktok",
    "linkedin.com": "linkedin", "t.co": "x", "x.com": "x", "twitter.com": "x",
    "direct": "direct", "affiliate": "referral", "invite": "referral",
    "discovery_search_group_link": "skool", "discovery_group_link": "skool",
    "discovery_browse_group_link": "skool", "user_profile_page": "skool", "internal_link": "skool",
}
# Our own funnel as the Skool referrer means "they came from /apply or the old Typeform" -
# the GHL contact already carries the real source (or will), so Skool adds nothing here.
OWN_HOSTS = {"dealenginehq.com", "dealenginegroup.com", "typeform.com", "linktr.ee"}
_NOT_A_SOURCE = {"src-direct", "src-other"}


def origin_tags(source, existing):
    """Tags to ADD for a Skool join: always `entry-skool-join`, plus one `src-*` unless the
    contact already carries a real one (the funnel's answer beats Skool's referrer)."""
    existing = set(existing or [])
    tags = ["entry-skool-join"]
    if any(t.startswith("src-") and t not in _NOT_A_SOURCE for t in existing):
        return tags
    src = (source or "").strip().lower()
    if not src or src in OWN_HOSTS:
        return tags
    ch = SKOOL_SRC.get(src) or ("skool" if src.startswith("discovery") else "other")
    return tags + [f"src-{ch}"]


def set_origin_tags(cid, tags, existing):
    """Add `tags` (POST /contacts/{id}/tags is additive) and drop a stale src-direct /
    src-other once a real src-* is present. `/contacts/upsert` with a `tags` list REPLACES
    the contact's tags (probed 23 Sept 2026), which is why no upsert here sends any."""
    tags = [t for t in tags if t]
    if not cid or not tags:
        return None
    s, d = ghl("POST", f"/contacts/{cid}/tags", {"tags": tags})
    if s not in (200, 201):
        print(f"  tag write failed {cid}: {s} {str(d)[:100]}")
        return s
    if any(t.startswith("src-") for t in tags):
        stale = [t for t in (existing or []) if t in _NOT_A_SOURCE and t not in tags]
        if stale:
            ghl("DELETE", f"/contacts/{cid}/tags", {"tags": stale})
    return s


def ghl(method, path, payload=None, base="https://services.leadconnectorhq.com"):
    r = urllib.request.Request(base + path,
        data=json.dumps(payload).encode() if payload is not None else None, method=method,
        headers={"Authorization": f"Bearer {PIT}", "Version": "2021-07-28",
                 "Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/153.0.0.0 Safari/537.36")})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(r, timeout=40) as resp: return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            try: return e.code, json.loads(e.read().decode() or "{}")
            except Exception: return e.code, {}
        except Exception as e:
            if attempt == 2: raise
            time.sleep(3 * (attempt + 1))

def iso_from_location(loc):
    m = re.search(r"\(([^)]+)\)\s*$", (loc or "").strip())
    return COUNTRY_TO_ISO.get(m.group(1).strip().lower()) if m else None

def clean_phone(raw, iso):
    """Only ever returns a genuinely valid E.164 number - otherwise None."""
    if not raw or not phonenumbers: return None
    if len(re.sub(r"\D", "", raw)) < 7: return None
    for reg in ([None] if raw.strip().startswith("+") else []) + [iso, "US", "GB"]:
        try:
            n = phonenumbers.parse(raw.strip(), reg)
            if phonenumbers.is_valid_number(n):
                return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
        except Exception: pass
    return None

class SkoolUnavailable(Exception):
    """Skool did not serve the members page, and the credential is NOT the reason.

    Measured 22 Sept 2026 from a GitHub runner, same cookie, minutes apart:
      cookie + browser UA        -> 200, __NEXT_DATA__ present (three times running)
      no cookie                  -> 307 to /<community>/about   <- what a DEAD SESSION looks like
      cookie + non-browser UA    -> 403 from CloudFront          <- what the WAF looks like
    So a 403 is Skool's AWS WAF / CloudFront edge refusing the request (server: CloudFront,
    x-cache: Error from cloudfront). The 06:25 and 06:28 UTC crashes that day were exactly
    that, and the alert blamed the cookie and sent a human off to rotate a secret that was
    working the whole time. A 403, 429, 5xx or a timeout is transient: retry, skip the pass,
    and only alarm when it persists. Rotating the cookie does nothing for it.
    """
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status, self.detail = status, detail


# Seconds between attempts at one page: 5+10+20 = 35s, plus at most 4 x 30s of socket
# timeout = 155s worst case per page, which leaves room for the other two pages of a pass
# inside the 300s `timeout` that sync.yml wraps every pass in. Deliberately short: a WAF
# block usually outlasts one pass anyway, so the real retry is the NEXT pass, ~3 minutes
# later, and the pass counter in reconcile_skool.py is what decides when to alarm.
# Override with SKOOL_RETRY_BACKOFF="0,0" in tests.
_BACKOFF = [int(x) for x in os.environ.get("SKOOL_RETRY_BACKOFF", "5,10,20").split(",") if x.strip() != ""]
TRANSIENT_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}


def _fetch_members_page(cookie, n, ua):
    """One members page as HTML.

    Raises SkoolUnavailable for a transient upstream failure (see the class) and SystemExit
    when the session itself is rejected - the one case where SKOOL_COOKIE needs rotating.
    """
    req = urllib.request.Request(
        f"https://www.skool.com/{COMMUNITY}/-/members?p={n}",
        headers={"Cookie": cookie, "User-Agent": ua,
                 "Accept": "text/html,application/xhtml+xml"})
    last = None
    for attempt in range(len(_BACKOFF) + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                final = resp.geturl()
                html = resp.read().decode("utf-8", "replace")
            # A dead cookie is a REDIRECT (307 -> /about), never an error status. urllib
            # follows it, so the tell is landing anywhere other than the members page.
            if "/-/members" not in final:
                raise SystemExit(
                    f"Skool redirected page {n} to {final}: the session cookie is no longer "
                    f"accepted. Rotate SKOOL_COOKIE - on the Mac run "
                    f"automations/scripts/skool_cookie_from_disk.py --push-secret.")
            return html
        except urllib.error.HTTPError as e:
            edge = ", ".join(f"{h}={e.headers.get(h)}" for h in ("server", "x-cache")
                             if e.headers is not None and e.headers.get(h))
            last = SkoolUnavailable(e.code, f"page {n} ({edge or 'no edge headers'})")
            if e.code not in TRANSIENT_STATUSES:
                raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = SkoolUnavailable(None, f"page {n}: {type(e).__name__}: {str(e)[:120]}")
        if attempt < len(_BACKOFF):
            wait = _BACKOFF[attempt]
            print(f"  skool page {n} attempt {attempt + 1} failed ({last}); retrying in {wait}s")
            time.sleep(wait)
    raise last


def pull_skool(pages=0):
    """Read the Skool members page over plain HTTP - no browser.

    `pages` is a CAP, not a target: the walk always stops at Skool's own `totalPages`.
    Pass 0 (the default) for "however many there are" - the community grows ~43/day, so any
    hardcoded number silently stops covering the list. Check LAST_SCAN_COMPLETE afterwards.

    The members page is server-rendered, so a cookie-authenticated GET returns the
    whole record set in __NEXT_DATA__. Only auth_token + client_id are needed (the
    AWS load-balancer cookies are not), and auth_token is valid for ~364 days, which
    is what lets this run unattended in CI instead of needing a logged-in browser.
    """
    cookie = os.environ.get("SKOOL_COOKIE")
    if not cookie:
        tok, cid = os.environ.get("SKOOL_AUTH_TOKEN"), os.environ.get("SKOOL_CLIENT_ID")
        if tok:
            cookie = f"auth_token={tok}" + (f"; client_id={cid}" if cid else "")
    if not cookie:
        # Fall back to the on-disk credential so this can run unattended from launchd,
        # where there is no shell profile to export secrets from.
        # Refresh it with:  automations/scripts/skool_cookie_from_disk.py
        secret = os.environ.get("SKOOL_COOKIE_FILE") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".secrets", "skool_cookie")
        if os.path.exists(secret):
            cookie = open(secret).read().strip()
    if not cookie:
        raise SystemExit("No Skool cookie. Set SKOOL_COOKIE / SKOOL_AUTH_TOKEN, or run "
                         "automations/scripts/skool_cookie_from_disk.py")
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
    global LAST_SCAN_COMPLETE
    LAST_SCAN_COMPLETE = False
    # 0/None means "walk to the end"; the loop still terminates on totalPages.
    cap = pages if (pages and pages > 0) else 10 ** 9
    out, n = [], 1
    while n <= cap:
        html = _fetch_members_page(cookie, n, UA)
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        if not m:
            raise SystemExit(f"No __NEXT_DATA__ on page {n} - Skool served the members URL without "
                             f"member data (session rejected without a redirect, or the page changed).")
        pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        for u in pp.get("users", []):
            mem = u.get("member") or {}
            md  = mem.get("metadata") or {}
            # Engagement signal lives on the USER's metadata, not the member's:
            # spData = {"pts": points, "lv": level, "role": n}. A member with points has
            # actually posted/commented/liked, which is a far warmer lead than a drive-by
            # joiner - and nothing in this stack was using it.
            umd = u.get("metadata") or {}
            try:
                sp = json.loads(umd.get("spData") or "{}")
            except Exception:
                sp = {}
            # lastOffline is a NANOSECOND epoch; seconds is what everything else wants.
            lo = umd.get("lastOffline")
            last_seen = int(lo) // 1_000_000_000 if isinstance(lo, (int, float)) and lo else None
            rec = {"handle":u.get("name"), "first":u.get("firstName"), "last":u.get("lastName"),
                   "joined":mem.get("createdAt"), "source":md.get("attrSrcComp"),
                   "location":md.get("requestLocation"), "email":None, "phone_raw":None, "why":None,
                   "points":sp.get("pts") or 0, "level":sp.get("lv") or 1, "last_seen":last_seen}
            sv = md.get("survey")
            if sv:
                try:
                    sd = json.loads(sv) if isinstance(sv, str) else sv
                    for q in sd.get("survey", []):
                        t  = (q.get("type") or "")
                        qq = (q.get("question") or "").lower()
                        a  = q.get("answer")
                        if t == "email" or "email" in qq:       rec["email"] = (a or "").strip()
                        elif "number" in qq or "phone" in qq:   rec["phone_raw"] = (a or "").strip()
                        else:                                   rec["why"] = a
                except Exception:
                    pass
            out.append(rec)
        global LAST_TOTAL, LAST_TOTAL_PAGES
        LAST_TOTAL = pp.get("total", LAST_TOTAL)
        LAST_TOTAL_PAGES = pp.get("totalPages", LAST_TOTAL_PAGES)
        total = pp.get("totalPages", n)
        if n >= total:
            LAST_SCAN_COMPLETE = True
            break
        n += 1
    return out

def has_card(contact_id):
    s, d = ghl("GET", f"/opportunities/search?location_id={LOC}&contact_id={contact_id}&limit=50")
    if s != 200: return False
    return any(o.get("pipelineId") == PIPE for o in d.get("opportunities", []))

def main():
    apply_changes = "--apply" in sys.argv
    pages = 2
    if "--pages" in sys.argv:
        pages = int(sys.argv[sys.argv.index("--pages") + 1])
    members = pull_skool(pages)
    withmail = [m for m in members if m["email"] and EMAIL_RE.match(m["email"])]
    badmail  = [m for m in members if m["email"] and not EMAIL_RE.match(m["email"])]
    print(f"skool members read : {len(members)} (pages {pages})")
    print(f"  with valid email : {len(withmail)}")
    print(f"  malformed email  : {len(badmail)}  {[m['email'] for m in badmail][:5]}")
    print(f"  no email at all  : {len(members)-len(withmail)-len(badmail)}\n")

    created = carded = skipped = 0
    already = card_failed = 0
    for m in withmail:
        iso  = iso_from_location(m["location"])
        ph   = clean_phone(m["phone_raw"], iso)
        name = f"{m['first'] or ''} {m['last'] or ''}".strip()
        cfs  = [{"id":CF["profile_url"],  "value":f"https://www.skool.com/@{m['handle']}"},
                {"id":CF["join_answer"],  "value":m["why"] or ""},
                {"id":CF["phone_answer"], "value":m["phone_raw"] or ""}]
        if m["joined"]: cfs.append({"id":CF["joined"], "value":m["joined"][:10]})
        body = {"locationId":LOC, "firstName":m["first"] or "", "lastName":m["last"] or "",
                "email":m["email"], "source": f"Skool community ({m['source']})" if m["source"] else "Skool community",
                "customFields":cfs}
        if ph: body["phone"] = ph
        if not apply_changes:
            print(f"  DRY {name[:24]:26} {m['email'][:32]:34} phone={ph or '-':16} src={m['source']}")
            continue
        s, d = ghl("POST", "/contacts/upsert", body)
        c = d.get("contact") or {}
        if s not in (200,201) or not c.get("id"):
            print(f"  FAIL contact {name}: {s} {str(d)[:120]}"); skipped += 1; continue
        created += 1
        # Tags AFTER the upsert, additively - see set_origin_tags.
        set_origin_tags(c["id"], ["skool-member", "skool-free-community"]
                        + origin_tags(m["source"], c.get("tags")), c.get("tags"))
        if not has_card(c["id"]):
            so, od = ghl("POST", "/opportunities/", {"pipelineId":PIPE, "locationId":LOC,
                "pipelineStageId":STAGE, "name":f"{name} — Skool free community",
                "status":"open", "contactId":c["id"], "monetaryValue":0})
            if so in (200,201):
                carded += 1
            elif (od or {}).get("code") == "OPPORTUNITY_NO_DUPLICATE":
                # Not a failure: GHL allows one open opportunity per contact per pipeline,
                # so this means the contact is already carded here - usually two Skool
                # members collapsing onto one GHL contact via a shared phone number.
                already += 1
            else:
                # Counted, not just printed. A card failure used to be printed and then
                # reported as "0 failed" in the summary - a real loss dressed as success.
                card_failed += 1
                print(f"  CARD FAILED {name}: {so} {str(od)[:120]}")
    if apply_changes:
        print(f"\nAPPLIED: {created} contacts upserted, {carded} new pipeline cards, "
              f"{already} already carded, {skipped + card_failed} failed.")
        if skipped or card_failed:
            # Non-zero exit so launchd/CI and the health check see a bad run as bad.
            sys.exit(f"{skipped} contact failure(s), {card_failed} card failure(s) - see above")
    else:
        print("\nDRY RUN - rerun with --apply to write to GoHighLevel.")

if __name__ == "__main__":
    main()
