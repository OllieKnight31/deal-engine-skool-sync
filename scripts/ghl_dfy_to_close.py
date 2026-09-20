#!/usr/bin/env python3
"""
GHL Setter Pipeline -> Close CRM mirror (the free replacement for GHL's premium Custom Webhook).

The DealEngine Apply funnel already creates the opportunity inside GHL. This mirrors those
applications into Close so the Close-based team keeps seeing them during the transition,
with no Zapier and no GHL premium action.

  GHL "1. Setter Pipeline"                 ->  Close "DFY Applications (GHL)"
    New Lead - DFY (Call within 5 mins)    ->    New Application - DFY
    New Lead - Mentorship (Call within...) ->    New Application - Community

Idempotent: a lead is matched on email and never duplicated; an opportunity is only created
if that lead has no card in the target pipeline. Safe to run on a schedule.

Usage:
    ghl_dfy_to_close.py                    # dry run
    ghl_dfy_to_close.py --apply            # write to Close
    ghl_dfy_to_close.py --since 2026-09-18 --apply
"""
import json, os, sys, base64, argparse, urllib.request, urllib.error, urllib.parse, re, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghl_http

# No inline fallback: this repo is PUBLIC, and a missing token must fail loudly
# rather than quietly authenticating as somebody else.
GHL_PIT  = os.environ["GHL_PIT"]
GHL_LOC  = "2RrjNuz0M4MTFlxpW37k"
GHL_PIPE = "cDPVmjlo65NVVn1g6ePU"                    # 1. Setter Pipeline

CLOSE_KEY  = os.environ["CLOSE_API_KEY"]
CLOSE_AUTH = "Basic " + base64.b64encode((CLOSE_KEY + ":").encode()).decode()
CLOSE_PIPE = "pipe_7eTpG9ChAFLAj7WpvXzEnU"           # DFY Applications (GHL)

# GHL stage id -> (Close opportunity status id, Close lead status id)
CLOSE_LEAD_DFY = "stat_S46IiNjUo1o0qidptlGGqQDRtPNbtMphCxXgLX4x1lB"   # New Lead - DFY
CLOSE_LEAD_COM = "stat_vwALWTscQ3ITPN8yn0hKhGzRX96PQUbSlETnQgPH4tO"   # Free Community Member
STAGE_MAP = {
    "236e05f2-ec7e-4f78-82c6-d8bcc61c6652": ("stat_ajTunHGgv62z0NcZoff4dyqr9gYnrGBUmn2GnIsWgum", CLOSE_LEAD_DFY),  # New Lead - DFY
    "97eeab10-f9d7-4403-88e7-fa6f9233e2e6": ("stat_og0gLAL2kopHcanYFH2Uqn8t5nEmdJluD5BFC03Q0eM", CLOSE_LEAD_COM),  # New Lead - Mentorship
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def req(url, headers, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:250]}
    except Exception as e:
        return {"_err": str(e)}


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


def ghl(path):
    """Raises ghl_http.GHLError/GHLBlocked on failure - never returns empty on error."""
    return ghl_http.get(path, GHL_PIT)


class CloseError(RuntimeError):
    pass


def close(path, data=None, method=None, _raise=True):
    r = req("https://api.close.com/api/v1" + path,
            {"Authorization": CLOSE_AUTH, "Content-Type": "application/json",
             "User-Agent": ghl_http.UA},
            data, method)
    if _raise and isinstance(r, dict) and ("_http" in r or "_err" in r):
        raise CloseError(f"Close {method or 'GET'} {path} -> {str(r)[:220]}")
    return r


def pull_ghl_opps():
    out, page = [], 1
    while True:
        d = ghl(f"/opportunities/search?location_id={GHL_LOC}&pipeline_id={GHL_PIPE}"
                f"&limit=100&page={page}")
        batch = d.get("opportunities", [])
        out += batch
        if len(batch) < 100:
            return out
        page += 1
        if page > 30:
            return out


def close_email_index():
    """
    Every email already in Close -> lead id.

    Guarded: on 18 Sept a failed page returned {} and this function handed back an
    EMPTY index, so every contact looked brand new and the sync created duplicate
    leads. An empty index is now treated as a failure, never as "Close is empty".
    """
    idx, skip, pages = {}, 0, 0
    while True:
        q = urllib.parse.urlencode({"query": "*", "_limit": 100, "_skip": skip,
                                    "_fields": "id,contacts"})
        d = close("/lead/?" + q)
        if "data" not in d:
            raise CloseError(f"lead page {pages} returned no 'data' key: {str(d)[:200]}")
        for l in d["data"]:
            for c in l.get("contacts", []):
                for e in c.get("emails", []):
                    idx.setdefault(e["email"].lower().strip(), l["id"])
        pages += 1
        if not d.get("has_more"):
            break
        skip += 100
        if pages > 200:
            break

    total = close("/lead/?_limit=1&_fields=id").get("total_results", 0)
    if total and not idx:
        raise CloseError(
            f"email index came back EMPTY while Close reports {total} leads. "
            f"Refusing to continue - this would create duplicate leads.")
    return idx


def lead_has_card(lead_id):
    d = close(f"/opportunity/?lead_id={lead_id}")
    return any(o.get("pipeline_id") == CLOSE_PIPE for o in d.get("data", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--since", default="2026-09-18",
                    help="only mirror GHL opportunities created on/after this date")
    a = ap.parse_args()

    # Prove GHL is reachable and not Cloudflare-blocked BEFORE interpreting an
    # empty result as "no applications". A silent 403 previously looked identical
    # to a clean run.
    try:
        ghl_http.preflight(GHL_PIT, GHL_LOC)
    except ghl_http.GHLBlocked as e:
        print(f"FATAL: GHL is Cloudflare-blocked - NOT a clean run.\n{e}", file=sys.stderr)
        sys.exit(2)
    except ghl_http.GHLError as e:
        print(f"FATAL: GHL unreachable - NOT a clean run.\n{e}", file=sys.stderr)
        sys.exit(2)

    try:
        opps = pull_ghl_opps()
    except ghl_http.GHLError as e:
        print(f"FATAL: could not read GHL opportunities.\n{e}", file=sys.stderr)
        sys.exit(2)
    live = [o for o in opps
            if (o.get("createdAt") or "")[:10] >= a.since
            and o.get("pipelineStageId") in STAGE_MAP]
    print(f"GHL Setter Pipeline opportunities: {len(opps)}   "
          f"in scope (since {a.since}, mapped stage): {len(live)}")
    if not live:
        print("nothing to mirror.")
        return

    try:
        idx = close_email_index()
    except CloseError as e:
        print(f"FATAL: could not build the Close email index - NOT a clean run.\n{e}",
              file=sys.stderr)
        sys.exit(3)
    print(f"emails already in Close: {len(idx)}\n")

    made_lead = made_card = skipped = failed = 0
    for o in live:
        c = o.get("contact") or {}
        email = (c.get("email") or "").strip().lower()
        name = (c.get("name") or "").strip() or (o.get("name") or "").strip()
        phone = (c.get("phone") or "").strip()
        opp_status, lead_status = STAGE_MAP[o["pipelineStageId"]]

        if not EMAIL_RE.match(email):
            print(f"  SKIP  no usable email  {name!r}  ({email!r})")
            skipped += 1
            continue

        lead_id = idx.get(email)
        action = []
        if not lead_id:
            if a.apply:
                contact = {"name": name, "emails": [{"type": "other", "email": email}]}
                if phone:
                    contact["phones"] = [{"type": "mobile", "phone": phone}]
                r = close("/lead/", {
                    "name": name, "status_id": lead_status, "contacts": [contact],
                    "description": f"Mirrored from GHL Setter Pipeline · {o.get('name','')}"})
                if not r.get("id"):
                    print(f"  FAIL  lead {name}: {str(r)[:120]}")
                    failed += 1
                    continue
                lead_id = r["id"]
                idx[email] = lead_id
            made_lead += 1
            action.append("create lead")
        # check the real state in both modes, so a dry run tells the truth
        needs_card = bool(lead_id) and not lead_has_card(lead_id)
        if needs_card:
            if a.apply:
                r = close("/opportunity/", {"lead_id": lead_id,
                                            "status_id": opp_status, "value": 0})
                if not r.get("id"):
                    print(f"  FAIL  card {name}: {str(r)[:120]}")
                    failed += 1
                    continue
            made_card += 1
            action.append("create card")
        if not action:
            action.append("already mirrored")
        print(f"  {'APPLY' if a.apply else 'DRY  '} {name[:28]:28} {email:34} -> {', '.join(action)}")
        if a.apply:
            time.sleep(0.2)

    print(f"\nleads {'created' if a.apply else 'to create'}: {made_lead}   "
          f"cards {'created' if a.apply else 'to create'}: {made_card}   "
          f"skipped: {skipped}   failed: {failed}")
    if not a.apply:
        print("DRY RUN - re-run with --apply to write.")


if __name__ == "__main__":
    main()
