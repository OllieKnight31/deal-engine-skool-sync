#!/usr/bin/env python3
"""
Move community cards out of "New Member" so the pipeline reflects reality.

Every card in both CRMs sat in "New Member" - seven stages existed and one was used,
which means the board was a list, not a pipeline. This advances two of them off real
signals, and deliberately refuses to fake the third.

  left the community   -> "Not A Fit / Left Community"
  stale and inactive   -> "Unresponsive"

Matching is done on the **Skool handle**, not the email. Skool only exposes an email for
members who answered the join questions (~18 Aug onward), so an email-based check would
report the entire Apr-Aug back catalogue as having "left". The handle is Skool's own id
and is stored on every synced record as `Skool Profile URL`, so it is the honest key.
Leads with no profile URL are skipped for departure detection - we genuinely cannot tell.

NOT IMPLEMENTED ON PURPOSE - "-> Welcome DM Sent when outreach fires". Nothing currently
messages the member (that was a deliberate decision), so there is no outreach event to
trigger it. Wiring it to anything else would just move cards on a lie.

Usage:
    pipeline_advance.py                        # report only
    pipeline_advance.py --apply
    pipeline_advance.py --stale-days 21 --apply
    pipeline_advance.py --apply --max-moves 50 # cap a first run
    pipeline_advance.py --repair-urls --apply  # stamp missing Skool Profile URLs first
"""
import json, os, re, sys, time, base64, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import skool_to_ghl
from skool_to_ghl import pull_skool, ghl, LOC, PIPE
import notify_slack

CLOSE_KEY = os.environ["CLOSE_API_KEY"]
CLOSE_AUTH = "Basic " + base64.b64encode((CLOSE_KEY + ":").encode()).decode()

CF_CLOSE_URL = "cf_8xBqwEeBvCrBSREoTWoLNZAIzSSDWxTpkdFkJD7Wxih"   # Skool Profile URL

CLOSE_NEW        = "stat_uncVryRJNI5eQkeEEnzbAoYRAlGbLLzrxvLdvXnYn1Q"
CLOSE_DM_SENT    = "stat_n3UxD2T2eDsb1ChisbCQBcFw8A4TRYW5jQcBh3c2wHh"
CLOSE_ENGAGED    = "stat_gXUW7nJWcEBuOQ4ZVVha9JIeBRqiNf4Jyto2ZaSsr5Q"
CLOSE_UNRESP     = "stat_WLhGQbiihvDDYTsyXXglU63ALsDBcp6J7My29nvjlFU"
CLOSE_LEFT       = "stat_HsmVgmUA4hRVgwwrOKwidBihomvb99Or3mT1h8NeYdR"
CLOSE_HISTORIC   = "stat_sbY9C2YfZb8UkBNlxd2J8EF8h2AzHUWzaUHoGlPZbDc"
# Only cards in these stages are eligible to move. Anything a human has already worked
# past - Qualified, Call Booked - is left strictly alone, and so is Historic.
CLOSE_MOVEABLE = {CLOSE_NEW, CLOSE_DM_SENT}

GHL_NEW      = "7c7ae6aa-7544-471b-8106-90df75ff86e6"
GHL_DM_SENT  = "dea66d46-a22e-4fe4-b7be-961e6857c509"
GHL_UNRESP   = "de540151-9bb6-457d-904b-f558f0b71927"
GHL_LEFT     = "129665c6-2876-4f00-ac90-6f1d16b33452"
GHL_MOVEABLE = {GHL_NEW, GHL_DM_SENT}

# 0 = walk to Skool's own last page. It MUST NOT be a fixed number: departure detection asks
# "is this handle absent from the community?", and a member sitting beyond a page cap is
# indistinguishable from one who left. The community grows ~43/day, so the old "70" (2,100
# members) would have started marking live members as departed within days of being written.
FULL_PAGES = int(os.environ.get("SKOOL_FULL_PAGES", "0"))


def close(method, path, payload=None):
    r = urllib.request.Request("https://api.close.com/api/v1" + path,
        data=json.dumps(payload).encode() if payload is not None else None, method=method,
        headers={"Authorization": CLOSE_AUTH, "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode() or "{}"
            if e.code == 429 and attempt < 3:
                time.sleep(5 * (attempt + 1)); continue
            try: return e.code, json.loads(body)
            except Exception: return e.code, {"raw": body[:300]}
        except Exception:
            if attempt == 3: raise
            time.sleep(3 * (attempt + 1))


def handle_from_url(u):
    m = re.search(r"skool\.com/@([A-Za-z0-9\-_.]+)", u or "")
    return m.group(1).lower() if m else None


def skool_snapshot():
    """Every current member: handles, plus engagement by handle and by email."""
    members = pull_skool(FULL_PAGES)
    handles, by_email = set(), {}
    for m in members:
        h = (m.get("handle") or "").lower()
        if h: handles.add(h)
        e = (m.get("email") or "").lower().strip()
        if e: by_email[e] = m
    return members, handles, by_email


def close_community_cards():
    """Open cards in the Close Skool pipeline, with their lead's profile URL."""
    out, skip = [], 0
    stages = CLOSE_MOVEABLE
    while True:
        s, d = close("GET", f"/opportunity/?_limit=200&_skip={skip}"
                            f"&_fields=id,status_id,lead_id,date_created")
        if s != 200: break
        for o in d["data"]:
            if o["status_id"] in stages:
                out.append(o)
        if not d.get("has_more"): break
        skip += 200
    # Pull the leads' profile URL + email in bulk-ish (one call per lead, cached by id).
    leads = {}
    for o in out:
        lid = o["lead_id"]
        if lid in leads: continue
        s, d = close("GET", f"/lead/{lid}/?_fields=id,display_name,contacts,custom")
        leads[lid] = d if s == 200 else {}
    return out, leads


def ghl_cards_by_email():
    cards, page = {}, 1
    while True:
        s, d = ghl("GET", f"/opportunities/search?location_id={LOC}"
                          f"&pipeline_id={PIPE}&limit=100&page={page}")
        if s != 200: break
        ops = d.get("opportunities", [])
        for o in ops:
            e = ((o.get("contact") or {}).get("email") or "").lower().strip()
            if e and o.get("pipelineStageId") in GHL_MOVEABLE:
                cards[e] = o
        if len(ops) < 100: break
        page += 1
    return cards


def move_close(opp_id, status_id):
    return close("PUT", f"/opportunity/{opp_id}/", {"status_id": status_id})


def move_ghl(opp_id, stage_id):
    return ghl("PUT", f"/opportunities/{opp_id}", {"pipelineId": PIPE,
                                                   "pipelineStageId": stage_id})


def _norm_name(v):
    return re.sub(r"[^a-z]", "", (v or "").lower())


def repair_profile_urls(leads_by_id, by_email, apply, members=None):
    """Stamp `Skool Profile URL` onto Close leads that lack it.

    Departure detection keys on the Skool handle, which lives in that field - but leads the
    old Zap created never had it mapped, so 232 of 275 cards were unmatchable and the whole
    feature only worked for 16% of the board. Matching email -> handle against the live
    member list fills it in.
    """
    # Name index, used only as a fallback. Skool's recorded email is sometimes malformed
    # (one member is stored as "…@gmail.com07"), so an email-only match leaves real members
    # permanently unevaluable for departure.
    by_name = {}
    for mm in (members or []):
        k = _norm_name((mm.get("first") or "") + (mm.get("last") or ""))
        if len(k) >= 6:
            by_name.setdefault(k, []).append(mm)
    claimed = {handle_from_url(l.get(f"custom.{CF_CLOSE_URL}") or "")
               for l in leads_by_id.values()}
    claimed.discard(None)

    fixed = matched = by_nm = 0
    for lid, lead in leads_by_id.items():
        if lead.get(f"custom.{CF_CLOSE_URL}"):
            continue
        emails = []
        for c in (lead.get("contacts") or []):
            emails += [(e.get("email") or "").lower().strip() for e in (c.get("emails") or [])]
        m = next((by_email[e] for e in emails if e in by_email), None)
        if not m:
            # Fallback: exactly one Skool member with this name, and nobody else already
            # holds that handle. Both guards matter - a loose name match would hand two
            # different people the same Skool profile.
            cands = by_name.get(_norm_name(lead.get("display_name")), [])
            cands = [c for c in cands if c.get("handle") and c["handle"].lower() not in claimed]
            if len(cands) == 1:
                m = cands[0]; by_nm += 1
                claimed.add(m["handle"].lower())
        if not m or not m.get("handle"):
            continue
        matched += 1
        if apply:
            s, _ = close("PUT", f"/lead/{lid}/",
                         {f"custom.{CF_CLOSE_URL}": f"https://www.skool.com/@{m['handle']}"})
            if s == 200: fixed += 1
    print(f"profile URLs: {matched} matchable from the live member list "
          f"({by_nm} of them by name, because Skool's stored email was unusable), "
          f"{fixed} written")
    return fixed


def main():
    apply = "--apply" in sys.argv
    stale_days = int(sys.argv[sys.argv.index("--stale-days") + 1]) if "--stale-days" in sys.argv else 21
    max_moves = int(sys.argv[sys.argv.index("--max-moves") + 1]) if "--max-moves" in sys.argv else None

    print("pulling the full Skool member list "
          f"({'every page' if not FULL_PAGES else f'up to {FULL_PAGES} pages'})…")
    members, handles, by_email = skool_snapshot()
    total = skool_to_ghl.LAST_TOTAL
    pages_seen = skool_to_ghl.LAST_TOTAL_PAGES
    print(f"  current members: {len(members)}  distinct handles: {len(handles)}  "
          f"(Skool reports {total} across {pages_seen} pages)")

    # Three independent ways the scan can be too short to reason about absence. Any of them
    # and we move NOTHING: a missed sweep costs a day, mass-marking live members as
    # "Not A Fit / Left Community" in both CRMs costs a manual cleanup of up to 150 records.
    problems = []
    if len(members) < 100:
        problems.append(f"suspiciously short member list ({len(members)})")
    if not skool_to_ghl.LAST_SCAN_COMPLETE:
        problems.append(f"scan stopped at the page cap, did not reach Skool's last page "
                        f"(cap {FULL_PAGES or 'none'})")
    if total and len(members) < total * 0.97:
        problems.append(f"only saw {len(members)} of {total} members "
                        f"({len(members)*100//max(total,1)}%)")
    if problems:
        msg = "; ".join(problems)
        print(f"REFUSING TO MOVE ANY CARD - {msg}")
        notify_slack.post_alert(
            "Pipeline hygiene skipped - incomplete member scan", msg + 
            "\n\nNo cards were moved. Departure detection needs the whole member list, "
            "because a member beyond the scan looks identical to one who left.")
        raise SystemExit(1)

    print("reading Close cards…")
    cards, leads = close_community_cards()
    print(f"  moveable Close cards: {len(cards)}")
    print("reading GHL cards…")
    ghl_cards = ghl_cards_by_email()
    print(f"  moveable GHL cards: {len(ghl_cards)}\n")

    if "--repair-urls" in sys.argv:
        repair_profile_urls(leads, by_email, apply, members)
        if not apply:
            print("\nreport only — rerun with --apply to write them")
        return

    now = datetime.now(timezone.utc)
    left, stale, unknown_leads = [], [], []
    for o in cards:
        lead = leads.get(o["lead_id"]) or {}
        url = lead.get(f"custom.{CF_CLOSE_URL}") or ""
        h = handle_from_url(url)
        emails = []
        for c in (lead.get("contacts") or []):
            emails += [(e.get("email") or "").lower().strip() for e in (c.get("emails") or [])]
        email = next((e for e in emails if e), None)
        rec = {"opp": o["id"], "name": lead.get("display_name"), "email": email,
               "handle": h, "created": (o.get("date_created") or "")[:10]}
        if not h:
            unknown_leads.append(rec)   # no profile URL - cannot tell if they left
        elif h not in handles:
            left.append(rec); continue
        # still a member: is the card stale and the member inactive?
        m = by_email.get(email) if email else None
        pts = (m or {}).get("points") or 0
        # Age off the SKOOL JOIN DATE, not the card's creation date. A backfilled card is
        # created today for someone who joined in April - measuring the card would reset
        # everyone's clock to zero every time we repair the pipeline.
        stamp = (m or {}).get("joined") or o.get("date_created") or ""
        try:
            age = (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).days
        except Exception:
            age = 0
        if age >= stale_days and pts == 0:
            rec["age"] = age
            stale.append(rec)

    print(f"LEFT THE COMMUNITY        : {len(left)}")
    for r in left[:15]:
        print(f"   {r['name']} ({r['handle']})")
    if len(left) > 15: print(f"   …and {len(left)-15} more")
    print(f"\nSTALE >= {stale_days}d AND 0 points : {len(stale)}")
    for r in stale[:15]:
        print(f"   {r['name']:32} joined {r['age']}d ago, 0 points")
    if len(stale) > 15: print(f"   …and {len(stale)-15} more")
    print(f"\nno profile URL, skipped   : {len(unknown_leads)}")
    for r in unknown_leads:
        print(f"   {str(r['name'])[:30]:32} {str(r['email'] or '-')[:34]:36} "
              f"cannot be checked for departure")
    if unknown_leads:
        print("   ^ not in Skool by email or name. Either they already left, or their Close\n"
              "     record is not really a Skool member. Needs a human eye, not a guess.")

    if not apply:
        print("\nreport only — rerun with --apply")
        return

    plan = [(r, CLOSE_LEFT, GHL_LEFT) for r in left] + [(r, CLOSE_UNRESP, GHL_UNRESP) for r in stale]
    if max_moves:
        plan = plan[:max_moves]
    ok = fail = 0
    for r, cstage, gstage in plan:
        s, _ = move_close(r["opp"], cstage)
        if s == 200: ok += 1
        else: fail += 1; print(f"  close move FAILED {r['name']}: {s}")
        g = ghl_cards.get(r["email"]) if r["email"] else None
        if g:
            s2, d2 = move_ghl(g["id"], gstage)
            if s2 not in (200, 201):
                print(f"  ghl move FAILED {r['name']}: {s2} {str(d2)[:100]}")
    print(f"\nClose cards moved: {ok}  failed: {fail}")


if __name__ == "__main__":
    main()
