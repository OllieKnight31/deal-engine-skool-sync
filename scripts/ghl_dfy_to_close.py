#!/usr/bin/env python3
"""
GHL Setter Pipeline -> Close CRM mirror (the free replacement for GHL's premium Custom Webhook).

The DealEngine Apply funnel already creates the opportunity inside GHL. This mirrors those
applications into Close so the Close-based team keeps seeing them during the transition,
with no Zapier and no GHL premium action.

  GHL "1. Setter Pipeline"                 ->  Close "DFY Applications (GHL)"
    New Lead - DFY (Call within 5 mins)    ->    New Application - DFY
    New Lead - Mentorship (Call within...) ->    New Application - Community

The GHL stage is NOT enough to route this. The Setter Pipeline has only two intake stages,
and the Mentorship rung and the Community rung BOTH land on "New Lead - Mentorship", so
mapping off the stage labelled every Mentorship applicant a free community member. The rung
lives on the CONTACT - as a tag (rung-done-for-you / rung-mentorship / rung-community) written
by the apply funnel, and as the custom field "Rung". So the Close lead status is derived from
the rung, and only the card (of which there are still exactly two, mirroring GHL's two stages)
is derived from whether that rung is DFY or not.

    rung done-for-you  ->  lead "New Lead - DFY"           card "New Application - DFY"
    rung mentorship    ->  lead "New Lead"                 card "New Application - Community"
    rung community     ->  lead "Free Community Member"    card "New Application - Community"

Idempotent: a lead is matched on email and never duplicated; an opportunity is only created
if that lead has no card in the target pipeline. Safe to run on a schedule.

THIS IS NO LONGER THE ONLY WRITER. The Vercel relay (de-funnel-relay POST /api/lead)
creates the Close lead and its DFY card in the same second as the GHL contact. So this
sweep is now a backstop, not the primary path, and it has to behave like one:

  * It builds its dedupe index from GET /lead/?query=* - Close's SEARCH index, which
    lags writes by minutes. Seconds after a real-time write the index still shows
    nothing, the sweep concludes the person is new, and it creates a DUPLICATE lead.
    That is exactly what produced the duplicate Tyler Pearson lead on 19 Sept.
  * So anything younger than GRACE_MINUTES is left to the relay. A relay write that
    genuinely failed is still there to be caught on the next pass - late is recoverable,
    a duplicate lead is not.
  * And before creating anything, the candidate is confirmed with a strongly-consistent
    read by id rather than trusted from the search snapshot. If that confirmation cannot
    be obtained, the member is skipped, never created.

Usage:
    ghl_dfy_to_close.py                    # dry run
    ghl_dfy_to_close.py --apply            # write to Close
    ghl_dfy_to_close.py --since 2026-09-18 --apply
    ghl_dfy_to_close.py --grace-minutes 0 --apply   # ignore the relay hand-off window
"""
import json, os, sys, base64, argparse, datetime, urllib.request, urllib.error, urllib.parse, re, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghl_http

# No inline fallback: this repo is PUBLIC, and a missing token must fail loudly
# rather than quietly authenticating as somebody else.
GHL_PIT  = os.environ["GHL_PIT"]
GHL_LOC  = "2RrjNuz0M4MTFlxpW37k"
GHL_PIPE = "cDPVmjlo65NVVn1g6ePU"                    # 1. Setter Pipeline
GHL_PIPE_VSL = "CGQb9RPdeLZ2zV1LDM3k"                # 5. VSL Opt-ins

CLOSE_KEY  = os.environ["CLOSE_API_KEY"]
CLOSE_AUTH = "Basic " + base64.b64encode((CLOSE_KEY + ":").encode()).decode()
CLOSE_PIPE = "pipe_7eTpG9ChAFLAj7WpvXzEnU"           # DFY Applications (GHL)
CLOSE_PIPE_VSL = "pipe_4AdHWeZv4sNS3JFPHCIZXz"       # VSL Opt-ins

# --- Close ids -------------------------------------------------------------
CLOSE_LEAD_DFY = "stat_S46IiNjUo1o0qidptlGGqQDRtPNbtMphCxXgLX4x1lB"   # New Lead - DFY
CLOSE_LEAD_NEW = "stat_YhCiU5puvSNnmL9B6bxWJE8Bu4cNVevZEzQ706M2i3T"   # New Lead        (Mentorship)
CLOSE_LEAD_COM = "stat_vwALWTscQ3ITPN8yn0hKhGzRX96PQUbSlETnQgPH4tO"   # Free Community Member
CLOSE_CARD_DFY = "stat_ajTunHGgv62z0NcZoff4dyqr9gYnrGBUmn2GnIsWgum"   # New Application - DFY
CLOSE_CARD_COM = "stat_og0gLAL2kopHcanYFH2Uqn8t5nEmdJluD5BFC03Q0eM"   # New Application - Community

# The two GHL intake stages. Scope only - they no longer decide the Close status.
GHL_STAGE_DFY        = "236e05f2-ec7e-4f78-82c6-d8bcc61c6652"   # New Lead - DFY
GHL_STAGE_MENTORSHIP = "97eeab10-f9d7-4403-88e7-fa6f9233e2e6"   # New Lead - Mentorship
INTAKE_STAGES = {GHL_STAGE_DFY, GHL_STAGE_MENTORSHIP}

# rung -> (Close opportunity status id, Close lead status id)
RUNG_MAP = {
    "done-for-you": (CLOSE_CARD_DFY, CLOSE_LEAD_DFY),
    "mentorship":   (CLOSE_CARD_COM, CLOSE_LEAD_NEW),
    "community":    (CLOSE_CARD_COM, CLOSE_LEAD_COM),
}
# Used only when no rung can be read at all - the pre-rung behaviour, kept so a
# missing tag degrades to the old mapping instead of dropping the application.
STAGE_FALLBACK = {
    GHL_STAGE_DFY:        (CLOSE_CARD_DFY, CLOSE_LEAD_DFY),
    GHL_STAGE_MENTORSHIP: (CLOSE_CARD_COM, CLOSE_LEAD_COM),
}
# How long the real-time relay owns a brand-new application before this sweep will
# touch it. Close's search index lags writes by minutes, so a lead the relay created
# seconds ago is invisible to close_email_index() and looks brand new - and creating it
# again is how the duplicate Tyler Pearson lead happened on 19 Sept. Anything OLDER than
# this and still absent from Close is a genuine relay failure, which is what the sweep
# exists to repair, so the window only has to outlast the index lag.
GRACE_MINUTES = 30
RUNG_CF = "ZNFocXSONdBVwLlzrAPK"                  # contact custom field "Rung"
RUNG_TAG_RE = re.compile(r"^rung-(done-for-you|mentorship|community)$")
# The three intake statuses. A lead still sitting on one of these has not been
# worked by a human yet, so a disagreement with the rung is a genuine mislabel
# rather than normal pipeline movement.
INTAKE_LEAD_STATUSES = {CLOSE_LEAD_DFY, CLOSE_LEAD_NEW, CLOSE_LEAD_COM}
CLOSE_STATUS_LABEL = {CLOSE_LEAD_DFY: "New Lead – DFY",
                      CLOSE_LEAD_NEW: "New Lead",
                      CLOSE_LEAD_COM: "Free Community Member"}

# --- VSL opt-ins ------------------------------------------------------------
# The relay's real-time write is the ONLY path into Close for a VSL opt-in, so without
# a sweep behind it a single failed Close call loses the lead with nobody ever noticing.
# Every other leg has a backstop; this gives that one the same.
GHL_STAGE_VSL_NEW = "53c9d64b-19dd-4b09-919c-e8c86d476a50"            # New Opt-in
CLOSE_LEAD_VSL    = "stat_yIViGxAECndeeVSzKqvDES6mINHUPkapsENShFu7wqM"  # VSL Opt-in
CLOSE_CARD_VSL    = "stat_n46mciUPBhXCCdrf7MWp0V7fCfpG02xQmSDdGxPqUD1"  # New Opt-in
CF_SOURCE_SELF    = "cf_vqAaS8FL6yPi53YJF9EFS7MUk1FqNTJRDwKzr0DiXgG"    # Source (self-reported)
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


def pull_ghl_opps(pipeline_id=GHL_PIPE):
    out, page = [], 1
    while True:
        d = ghl(f"/opportunities/search?location_id={GHL_LOC}&pipeline_id={pipeline_id}"
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


def lead_has_card(lead_id, close_pipeline):
    """Dedupe is PER-PIPELINE, never global. A VSL opt-in who later joins the community
    is two real events on two boards and legitimately holds a card on each, so a card in
    some other pipeline is never a reason to skip."""
    d = close(f"/lead/{lead_id}/?_fields=id,opportunities", _raise=False)
    if isinstance(d, dict) and isinstance(d.get("opportunities"), list):
        return any(o.get("pipeline_id") == close_pipeline for o in d["opportunities"])
    # Reading the lead by id is strongly consistent and is the answer we want; the
    # opportunity search is the fallback only if that shape is ever unavailable.
    d = close(f"/opportunity/?lead_id={lead_id}")
    return any(o.get("pipeline_id") == close_pipeline for o in d.get("data", []))


class ConfirmFailed(RuntimeError):
    """Could not establish, with certainty, whether this person is already in Close."""


def read_lead_by_id(lead_id):
    """Strongly consistent. Returns the lead, None if it genuinely does not exist,
    or raises ConfirmFailed if Close could not answer."""
    d = close(f"/lead/{lead_id}/?_fields=id,display_name,status_id,status_label", _raise=False)
    if isinstance(d, dict) and d.get("id"):
        return d
    if isinstance(d, dict) and d.get("_http") == 404:
        return None
    raise ConfirmFailed(f"read of {lead_id} returned {str(d)[:120]}")


def confirm_existing_lead(email, indexed_id):
    """The last word on "is this person already in Close?", before anything is created.

    The bulk index is a SEARCH snapshot taken at the top of the run, so it is stale in
    both directions. A hit is re-read by id; a miss is re-checked with a targeted search
    now (minutes fresher than the snapshot) and that hit is then re-read by id too.
    Anything less than a definite "no such lead" raises, and the caller skips - a late
    mirror is recoverable on the next pass, a duplicate lead is not.
    """
    if indexed_id:
        lead = read_lead_by_id(indexed_id)
        if lead:
            return lead
    q = urllib.parse.quote(f'email:"{email}"')
    d = close(f"/lead/?query={q}&_limit=1&_fields=id", _raise=False)
    if not isinstance(d, dict) or "_http" in d or "_err" in d or "data" not in d:
        raise ConfirmFailed(f"search for {email} returned {str(d)[:120]}")
    hit = (d.get("data") or [None])[0]
    return read_lead_by_id(hit["id"]) if hit else None


# The Private Integration token has opportunities.* but not contacts.readonly, so
# /contacts/<id> answers 401. Probe once, then stop asking - the tag carried on the
# opportunity search result is the cheap path and covers every funnel-created lead.
_contact_read_ok = None


def rung_from_tags(tags):
    for t in tags or []:
        m = RUNG_TAG_RE.match((t or "").strip().lower())
        if m:
            return m.group(1)
    return None


def rung_from_custom_field(contact_id):
    """Second source of truth: the contact custom field "Rung" (Done-for-you /
    Mentorship / Community). Needs contacts.readonly; silently unavailable without it."""
    global _contact_read_ok
    if not contact_id or _contact_read_ok is False:
        return None
    try:
        d = ghl(f"/contacts/{contact_id}")
        _contact_read_ok = True
    except ghl_http.GHLError:
        _contact_read_ok = False
        return None
    for f in (d.get("contact") or {}).get("customFields", []) or []:
        if f.get("id") == RUNG_CF:
            v = str(f.get("value") or "").strip().lower().replace(" ", "-")
            return v if v in RUNG_MAP else None
    return None


def resolve_rung(opp):
    """(rung, source). rung is None when nothing on the contact says which rung this is."""
    c = opp.get("contact") or {}
    tags = list(c.get("tags") or [])
    for rel in opp.get("relations") or []:
        tags += list(rel.get("tags") or [])
    r = rung_from_tags(tags)
    if r:
        return r, "tag"
    r = rung_from_custom_field(c.get("id") or opp.get("contactId"))
    if r:
        return r, "custom field"
    return None, None


def route_dfy(o):
    """(card status, lead status, label, rung_missing) for an application."""
    rung, src = resolve_rung(o)
    if rung:
        card, lead = RUNG_MAP[rung]
        return card, lead, f"{rung}/{src}", False
    # Never drop the application - fall back to the old stage mapping, but say so
    # loudly, because that fallback is exactly what mislabelled Mentorship applicants
    # as free community members.
    card, lead = STAGE_FALLBACK[o["pipelineStageId"]]
    return card, lead, "?/NO RUNG - stage fallback", True


def route_vsl(o):
    """One stage, one status - the VSL board has no rung to read."""
    return CLOSE_CARD_VSL, CLOSE_LEAD_VSL, "vsl-optin", False


SWEEPS = [
    {"label": "DFY applications   GHL 1. Setter Pipeline -> Close DFY Applications (GHL)",
     "ghl_pipeline": GHL_PIPE, "stages": INTAKE_STAGES,
     "close_pipeline": CLOSE_PIPE, "route": route_dfy, "rung_aware": True,
     "custom": {}, "description": "Mirrored from GHL Setter Pipeline"},
    {"label": "VSL opt-ins        GHL 5. VSL Opt-ins -> Close VSL Opt-ins",
     "ghl_pipeline": GHL_PIPE_VSL, "stages": {GHL_STAGE_VSL_NEW},
     "close_pipeline": CLOSE_PIPE_VSL, "route": route_vsl, "rung_aware": False,
     "custom": {CF_SOURCE_SELF: "VSL opt-in"}, "description": "Mirrored from GHL VSL Opt-ins"},
]


def sweep(cfg, idx, a, totals):
    """Mirror one GHL pipeline into one Close pipeline. idx is shared across sweeps
    because the Close lead is shared - only the CARD is per-pipeline."""
    print(f"\n{cfg['label']}")
    try:
        opps = pull_ghl_opps(cfg["ghl_pipeline"])
    except ghl_http.GHLError as e:
        print(f"FATAL: could not read GHL opportunities.\n{e}", file=sys.stderr)
        sys.exit(2)
    scoped = [o for o in opps
              if (o.get("createdAt") or "")[:10] >= a.since
              and o.get("pipelineStageId") in cfg["stages"]]

    # Hand-off window: the relay writes these to Close in real time, and Close's search
    # index has not caught up yet, so the sweep must not second-guess it.
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(minutes=a.grace_minutes))
    live, too_new = [], []
    for o in scoped:
        try:
            created = datetime.datetime.fromisoformat(
                (o.get("createdAt") or "").replace("Z", "+00:00"))
        except ValueError:
            live.append(o)               # unreadable timestamp: treat as old, not as new
            continue
        (too_new if created > cutoff else live).append(o)

    print(f"  opportunities: {len(opps)}   in scope (since {a.since}, mapped stage): "
          f"{len(scoped)}   older than the {a.grace_minutes} min relay window: {len(live)}")
    for o in too_new:
        c = o.get("contact") or {}
        print(f"  RELAY {(c.get('name') or o.get('name') or '')[:28]:28} "
              f"{(c.get('email') or '?'):34} created {o.get('createdAt','')[:19]} "
              f"- inside the {a.grace_minutes} min window, left to the real-time relay")
    if not live:
        print("  nothing to mirror.")
        return

    for o in live:
        c = o.get("contact") or {}
        email = (c.get("email") or "").strip().lower()
        name = (c.get("name") or "").strip() or (o.get("name") or "").strip()
        phone = (c.get("phone") or "").strip()

        opp_status, want_lead_status, route_label, rung_missing = cfg["route"](o)
        rung = route_label.split("/")[0] if cfg["rung_aware"] and not rung_missing else None
        if rung_missing:
            totals["no_rung"] += 1
            print(f"  NORUNG no rung tag or custom field on {name!r} ({email or '?'}) "
                  f"- falling back to the stage mapping")

        if not EMAIL_RE.match(email):
            print(f"  SKIP  no usable email  {name!r}  ({email!r})")
            totals["skipped"] += 1
            continue

        # Never create off the back of the search snapshot alone. Confirm by id first;
        # an inconclusive answer means skip, because a duplicate lead cannot be undone
        # by the next pass but a late mirror can.
        try:
            confirmed = confirm_existing_lead(email, idx.get(email))
        except ConfirmFailed as e:
            print(f"  SKIP  could not confirm {email} in Close ({e}) - "
                  f"not creating, will retry next pass")
            totals["skipped"] += 1
            continue
        lead_id = confirmed["id"] if confirmed else None
        if lead_id:
            idx[email] = lead_id
        elif idx.get(email):
            # The snapshot pointed at a lead that no longer exists (merged or deleted).
            idx.pop(email, None)

        action = []
        if not lead_id:
            if a.apply:
                contact = {"name": name, "emails": [{"type": "other", "email": email}]}
                if phone:
                    contact["phones"] = [{"type": "mobile", "phone": phone}]
                payload = {
                    "name": name, "status_id": want_lead_status, "contacts": [contact],
                    "description": f"{cfg['description']} · {o.get('name','')}"}
                for cf_id, cf_val in cfg["custom"].items():
                    payload[f"custom.{cf_id}"] = cf_val
                r = close("/lead/", payload)
                if not r.get("id"):
                    print(f"  FAIL  lead {name}: {str(r)[:120]}")
                    totals["failed"] += 1
                    continue
                lead_id = r["id"]
                idx[email] = lead_id
            totals["made_lead"] += 1
            action.append("create lead")
        elif rung:
            # An existing lead is never restatused here - a human moving it through the
            # pipeline must win. But a lead still parked on one of the three INTAKE
            # statuses has not been worked yet, so a disagreement with the rung is a
            # real mislabel and is reported rather than left invisible. The status comes
            # from the confirming read by id, so it is never a stale search value.
            cur_id, cur_label = confirmed.get("status_id"), confirmed.get("status_label")
            if cur_id in INTAKE_LEAD_STATUSES and cur_id != want_lead_status:
                totals["mislabelled"] += 1
                print(f"  MISLABEL {name[:24]:24} {email:34} rung={rung:12} "
                      f"is {cur_label!r}, should be {CLOSE_STATUS_LABEL.get(want_lead_status, want_lead_status)!r}"
                      f"  ({lead_id})")
        # Check the real state in both modes, so a dry run tells the truth. In a dry run
        # a lead that does not exist yet definitionally has no card, so say so rather
        # than under-reporting the work by one card per new person.
        needs_card = (not lead_has_card(lead_id, cfg["close_pipeline"])) if lead_id else True
        if needs_card:
            if a.apply:
                r = close("/opportunity/", {"lead_id": lead_id,
                                            "status_id": opp_status, "value": 0})
                if not r.get("id"):
                    print(f"  FAIL  card {name}: {str(r)[:120]}")
                    totals["failed"] += 1
                    continue
            totals["made_card"] += 1
            action.append("create card")
        if not action:
            action.append("already mirrored")
        print(f"  {'APPLY' if a.apply else 'DRY  '} {name[:28]:28} {email:34} "
              f"[{route_label}] -> {', '.join(action)}")
        if a.apply:
            time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--since", default="2026-09-18",
                    help="only mirror GHL opportunities created on/after this date")
    ap.add_argument("--grace-minutes", type=int, default=GRACE_MINUTES,
                    help="leave applications younger than this to the real-time relay "
                         f"(default {GRACE_MINUTES}; 0 disables the hand-off window)")
    ap.add_argument("--only", choices=["dfy", "vsl"],
                    help="run just one of the two sweeps")
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
        idx = close_email_index()
    except CloseError as e:
        print(f"FATAL: could not build the Close email index - NOT a clean run.\n{e}",
              file=sys.stderr)
        sys.exit(3)
    print(f"emails already in Close: {len(idx)}")

    totals = dict(made_lead=0, made_card=0, skipped=0, failed=0, no_rung=0, mislabelled=0)
    wanted = {"dfy": [SWEEPS[0]], "vsl": [SWEEPS[1]]}.get(a.only, SWEEPS)
    for cfg in wanted:
        sweep(cfg, idx, a, totals)

    print(f"\nleads {'created' if a.apply else 'to create'}: {totals['made_lead']}   "
          f"cards {'created' if a.apply else 'to create'}: {totals['made_card']}   "
          f"skipped: {totals['skipped']}   failed: {totals['failed']}")
    if totals["no_rung"]:
        print(f"no rung readable on {totals['no_rung']} application(s) - those used the stage "
              f"fallback, which cannot tell Mentorship from Community")
    if totals["mislabelled"]:
        print(f"MISLABELLED: {totals['mislabelled']} existing lead(s) sit on an intake status "
              f"that disagrees with the contact's rung (listed above, not auto-changed)")
    if not a.apply:
        print("DRY RUN - re-run with --apply to write.")


if __name__ == "__main__":
    main()
