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

try:
    import phonenumbers
except ImportError:
    phonenumbers = None

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

def ghl(method, path, payload=None, base="https://services.leadconnectorhq.com"):
    r = urllib.request.Request(base + path,
        data=json.dumps(payload).encode() if payload is not None else None, method=method,
        headers={"Authorization": f"Bearer {PIT}", "Version": "2021-07-28",
                 "Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "curl/8.7.1"})
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

def pull_skool(pages):
    """Read the Skool members page over plain HTTP - no browser.

    The members page is server-rendered, so a cookie-authenticated GET returns the
    whole record set in __NEXT_DATA__. Only auth_token + client_id are needed (the
    AWS load-balancer cookies are not), and auth_token is valid for ~364 days, which
    is what lets this run unattended in CI instead of needing a logged-in browser.
    """
    cookie = os.environ.get("SKOOL_COOKIE")
    if not cookie:
        tok, cid = os.environ.get("SKOOL_AUTH_TOKEN"), os.environ.get("SKOOL_CLIENT_ID")
        if not tok:
            raise SystemExit("Set SKOOL_COOKIE, or SKOOL_AUTH_TOKEN + SKOOL_CLIENT_ID.")
        cookie = f"auth_token={tok}" + (f"; client_id={cid}" if cid else "")
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
    out, n = [], 1
    while n <= pages:
        req = urllib.request.Request(
            f"https://www.skool.com/{COMMUNITY}/-/members?p={n}",
            headers={"Cookie": cookie, "User-Agent": UA,
                     "Accept": "text/html,application/xhtml+xml"})
        # Skool intermittently stalls a page request. Without a retry one slow
        # response killed the entire run, and a scheduled run that dies part-way
        # leaves members in one CRM and not the other.
        html = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    html = resp.read().decode("utf-8", "replace")
                break
            except Exception as e:
                if attempt == 3:
                    raise
                wait = 3 * (attempt + 1)
                print(f"  skool page {n} attempt {attempt+1} failed ({e}); retrying in {wait}s")
                time.sleep(wait)
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        if not m:
            raise SystemExit(f"No __NEXT_DATA__ on page {n} - the Skool session has probably expired.")
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
        if n >= total: break
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
                "tags":["skool-member","skool-free-community"], "customFields":cfs}
        if ph: body["phone"] = ph
        if not apply_changes:
            print(f"  DRY {name[:24]:26} {m['email'][:32]:34} phone={ph or '-':16} src={m['source']}")
            continue
        s, d = ghl("POST", "/contacts/upsert", body)
        c = d.get("contact") or {}
        if s not in (200,201) or not c.get("id"):
            print(f"  FAIL contact {name}: {s} {str(d)[:120]}"); skipped += 1; continue
        created += 1
        if not has_card(c["id"]):
            so, od = ghl("POST", "/opportunities/", {"pipelineId":PIPE, "locationId":LOC,
                "pipelineStageId":STAGE, "name":f"{name} — Skool free community",
                "status":"open", "contactId":c["id"], "monetaryValue":0})
            if so in (200,201): carded += 1
            else: print(f"  card failed {name}: {so} {str(od)[:120]}")
    if apply_changes:
        print(f"\nAPPLIED: {created} contacts upserted, {carded} new pipeline cards, {skipped} failed.")
    else:
        print("\nDRY RUN - rerun with --apply to write to GoHighLevel.")

if __name__ == "__main__":
    main()
