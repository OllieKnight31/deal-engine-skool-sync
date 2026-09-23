#!/usr/bin/env python3
"""
Make every Skool community lead present in BOTH CRMs, with a pipeline card in each.

The day-to-day sync (reconcile_skool.py) only looks at the newest few pages of Skool,
because that is all a 5-minute job needs. It therefore never touches the Apr-Aug back
catalogue that reached Close through the old Zaps and Typeform, and never had
join-question data. This closes that gap once.

Placement rule: anything created from September 2026 onward lands in "New Member" so it
joins the live triage queue. Everything older lands in "Historic (pre-Sept)" - a stage
added to both pipelines specifically so 300+ months-old leads cannot drown the queue the
setters actually work.

Idempotent: re-running creates nothing twice. Safe to interrupt and re-run.

Usage:
    backfill_all_community.py                # report only
    backfill_all_community.py --apply
    backfill_all_community.py --apply --limit 25    # try a slice first
"""
import json, os, re, sys, time, base64, urllib.request, urllib.error, urllib.parse

CLOSE_KEY = os.environ["CLOSE_API_KEY"]
CLOSE_AUTH = "Basic " + base64.b64encode((CLOSE_KEY + ":").encode()).decode()
PIT = os.environ["GHL_PIT"]
LOC = "2RrjNuz0M4MTFlxpW37k"

CLOSE_PIPE = "pipe_5IV9miCDNFveEjxw0dfagM"
CLOSE_NEW = "stat_uncVryRJNI5eQkeEEnzbAoYRAlGbLLzrxvLdvXnYn1Q"
CLOSE_HISTORIC = "stat_sbY9C2YfZb8UkBNlxd2J8EF8h2AzHUWzaUHoGlPZbDc"
CLOSE_STATUS_COMMUNITY = "Free Community Member"

GHL_PIPE = "BaP4NEFnESaozLgCJio8"
GHL_NEW = "7c7ae6aa-7544-471b-8106-90df75ff86e6"
GHL_HISTORIC = "e90efb89-4b58-46c4-b9cf-dc4f3bc0121d"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,24}$")
CUTOFF = "2026-09"          # created this month or later == still a live lead


def _req(url, headers, payload=None, method="GET"):
    r = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None,
        method=method, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode() or "{}"
            # 429 is worth waiting out; other HTTP errors are real answers.
            if e.code == 429 and attempt < 3:
                time.sleep(5 * (attempt + 1)); continue
            try: return e.code, json.loads(body)
            except Exception: return e.code, {"raw": body[:300]}
        except Exception:
            if attempt == 3: raise
            time.sleep(3 * (attempt + 1))


def close(method, path, payload=None):
    return _req("https://api.close.com/api/v1" + path,
                {"Authorization": CLOSE_AUTH, "Content-Type": "application/json"},
                payload, method)


def ghl(method, path, payload=None):
    return _req("https://services.leadconnectorhq.com" + path,
                {"Authorization": f"Bearer {PIT}", "Version": "2021-07-28",
                 "Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "curl/8.7.1"}, payload, method)


def all_community_leads():
    """Every Close lead at status 'Free Community Member', with contact details."""
    out, skip = [], 0
    q = urllib.parse.quote(f'lead_status:"{CLOSE_STATUS_COMMUNITY}"')
    while True:
        s, d = close("GET", f"/lead/?query={q}&_limit=100&_skip={skip}"
                            f"&_fields=id,display_name,contacts,date_created")
        if s != 200:
            raise SystemExit(f"Close lead search failed: {s} {d}")
        out += d["data"]
        if not d.get("has_more"): break
        skip += 100
    return out


def close_carded_lead_ids():
    """Lead ids that already hold a card in the Skool pipeline."""
    ids, skip = set(), 0
    stages = {CLOSE_NEW, CLOSE_HISTORIC,
              "stat_n3UxD2T2eDsb1ChisbCQBcFw8A4TRYW5jQcBh3c2wHh",
              "stat_gXUW7nJWcEBuOQ4ZVVha9JIeBRqiNf4Jyto2ZaSsr5Q",
              "stat_sGixdx3qkP9NQ2lh7cncapt58q8n7z95UKGm3L4vPkB",
              "stat_3qNNHVCWcz3aNQ5rxsWbGF7P5nPeeJExaeMcqxypTbB",
              "stat_WLhGQbiihvDDYTsyXXglU63ALsDBcp6J7My29nvjlFU",
              "stat_HsmVgmUA4hRVgwwrOKwidBihomvb99Or3mT1h8NeYdR"}
    while True:
        s, d = close("GET", f"/opportunity/?_limit=200&_skip={skip}&_fields=id,status_id,lead_id")
        if s != 200: break
        for o in d["data"]:
            if o["status_id"] in stages:
                ids.add(o["lead_id"])
        if not d.get("has_more"): break
        skip += 200
    return ids


def ghl_carded_emails():
    """Emails that already hold a card in the GHL community pipeline."""
    emails, page = set(), 1
    while True:
        s, d = ghl("GET", f"/opportunities/search?location_id={LOC}"
                          f"&pipeline_id={GHL_PIPE}&limit=100&page={page}")
        if s != 200: break
        ops = d.get("opportunities", [])
        for o in ops:
            e = ((o.get("contact") or {}).get("email") or "").lower().strip()
            if e: emails.add(e)
        if len(ops) < 100: break
        page += 1
    return emails


def details(lead):
    emails, phones = [], []
    for c in (lead.get("contacts") or []):
        emails += [(e.get("email") or "").lower().strip() for e in (c.get("emails") or [])]
        phones += [(p.get("phone") or "").strip() for p in (c.get("phones") or [])]
    emails = [e for e in emails if e and EMAIL_RE.match(e)]
    phones = [p for p in phones if p]
    created = (lead.get("date_created") or "")[:7]
    return (emails[0] if emails else None, phones[0] if phones else None,
            created, created >= CUTOFF)


def main():
    apply = "--apply" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

    print("reading Close leads…");        leads = all_community_leads()
    print("reading Close cards…");        close_carded = close_carded_lead_ids()
    print("reading GHL cards…");          ghl_carded = ghl_carded_emails()
    print(f"\ncommunity leads in Close : {len(leads)}")
    print(f"already carded in Close   : {len(close_carded & {l['id'] for l in leads})}")
    print(f"already carded in GHL     : {len(ghl_carded)}\n")

    need_close, need_ghl, unusable = [], [], []
    for L in leads:
        email, phone, created, is_live = details(L)
        rec = {"id": L["id"], "name": L.get("display_name") or "", "email": email,
               "phone": phone, "created": created, "live": is_live}
        if not email and not phone:
            unusable.append(rec); continue
        if L["id"] not in close_carded:
            need_close.append(rec)
        # A phone-only lead is still pushable - GHL upserts on phone too. It just cannot
        # be deduped against the carded-email set, so we lean on GHL refusing a second
        # opportunity for a contact that already has one.
        if email:
            if email not in ghl_carded:
                need_ghl.append(rec)
        else:
            need_ghl.append(rec)

    print(f"need a Close card : {len(need_close)}")
    print(f"need a GHL card   : {len(need_ghl)}")
    print(f"unusable          : {len(unusable)}")
    print(f"   of which live (Sept+) : close {sum(1 for r in need_close if r['live'])}"
          f" / ghl {sum(1 for r in need_ghl if r['live'])}")
    if not apply:
        print("\nreport only — rerun with --apply")
        return

    if limit:
        need_close, need_ghl = need_close[:limit], need_ghl[:limit]

    ok_c = fail_c = 0
    for i, r in enumerate(need_close, 1):
        stage = CLOSE_NEW if r["live"] else CLOSE_HISTORIC
        s, d = close("POST", "/opportunity/", {
            "lead_id": r["id"], "status_id": stage, "value": 0,
            "note": "Skool free community" + ("" if r["live"] else " — historic backfill")})
        if s == 200: ok_c += 1
        else:
            fail_c += 1; print(f"  close card FAILED {r['name']}: {s} {str(d)[:120]}")
        if i % 50 == 0: print(f"  …close {i}/{len(need_close)}")
    print(f"Close cards created: {ok_c}  failed: {fail_c}")

    ok_g = fail_g = dup_g = 0
    for i, r in enumerate(need_ghl, 1):
        first, _, last = (r["name"] or "").partition(" ")
        tags = ["skool-member", "skool-free-community", "entry-skool-join"] + ([] if r["live"] else ["skool-historic"])
        # No `tags` on the upsert - it REPLACES the contact's tags (probed 23 Sept 2026).
        body = {"locationId": LOC, "firstName": first or r["name"] or "Unknown",
                "lastName": last or "", "source": "Skool community (Close backfill)"}
        if r["email"]: body["email"] = r["email"]
        if r["phone"]: body["phone"] = r["phone"]
        s, d = ghl("POST", "/contacts/upsert", body)
        cid = (d.get("contact") or {}).get("id")
        if not cid:
            fail_g += 1; print(f"  ghl upsert FAILED {r['name']}: {s} {str(d)[:120]}"); continue
        ghl("POST", f"/contacts/{cid}/tags", {"tags": tags})
        stage = GHL_NEW if r["live"] else GHL_HISTORIC
        s2, d2 = ghl("POST", "/opportunities/", {
            "pipelineId": GHL_PIPE, "locationId": LOC, "pipelineStageId": stage,
            "name": f"{r['name']} — Skool free community", "status": "open",
            "contactId": cid, "monetaryValue": 0})
        if s2 in (200, 201):
            ok_g += 1
        elif "NO_DUPLICAT" in str(d2.get("code", "")) or "duplicate opportunity" in str(d2).lower():
            # GHL refuses more than one opportunity per contact, across all pipelines.
            # The contact already sits on some board, so nothing is missing - not a failure.
            dup_g += 1
        else:
            fail_g += 1; print(f"  ghl card FAILED {r['name']}: {s2} {str(d2)[:120]}")
        if i % 50 == 0: print(f"  …ghl {i}/{len(need_ghl)}")
    print(f"GHL contact+card created: {ok_g}  "
          f"already carded elsewhere: {dup_g}  failed: {fail_g}")

    if unusable:
        print(f"\nSkipped {len(unusable)} with no usable email or phone:")
        for r in unusable[:20]:
            print(f"   {r['created']}  {r['name']}  email={r['email']} phone={r['phone']}")


if __name__ == "__main__":
    main()
