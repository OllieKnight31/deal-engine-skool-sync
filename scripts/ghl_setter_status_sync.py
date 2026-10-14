#!/usr/bin/env python3
"""
Two-way sync: GHL "1. Setter Pipeline" stage  <->  Close lead status.

Why this exists
---------------
The setters work the Close LEAD STATUS. The speed-to-lead alarms, the dialler and the EOD
dashboard read the GHL Setter CARD. Until 26 Sept 2026 nothing carried a change from one to
the other, so both boards were internally consistent and disagreed with each other:
Junaid A "Spoke – Qualifying" in Close, "No Answer 2 (Day 1)" in GHL; Chris Lee "DQ" in
Close, "No Answer 1 (Day 1)" in GHL.

Two legs
--------
* Close -> GHL is real time: the Close webhook (lead.updated, status_id changed) hits
  de-funnel-relay /api/close-hook, which moves the card (api/_stagesync.js).
* GHL -> Close has no free real-time trigger (GHL's outbound webhook action is premium), so it
  is this script, on the CI chain. It is ALSO the backstop for the other leg: if a webhook was
  missed, the newer side still wins here on the next pass.

Newest wins
-----------
For each Setter card: GHL's `lastStageChangeAt` vs the newest Close status-change activity on
the lead. Whichever changed last is the truth and the other side is moved to match.

The guards
----------
* Compare EQUIVALENCE CLASSES, not names (GHL has seven No Answer stages, Close has one) -
  which is also what makes the two legs loop-free: an echo lands in the class it came from.
* Never move either side INTO intake. Intake is set at creation only.
* Never overwrite a closer's Close status (Signed Up, Closer Follow Up, Call Confirmed...)
  from GHL - GHL's "Call Set" is the setter's last word, Close carries everything after it.
* A Close status with no class (Cancelled Call, VSL Opt-in, "(old) ...") never moves GHL.
* Grace window: anything that changed on either side within GRACE_MINUTES is left alone so
  this never races the relay or a human mid-edit.
* A Setter card whose person is not in Close AT ALL gets a Close lead (status from the card's
  class; intake by rung). That is the "every lead is in Close" guarantee for the paths the
  relay missed. Confirmed by id before creating - a duplicate lead cannot be undone.
* Dry run by default. --apply writes. Raises on transport failure - never reads a dead pipe
  as "nothing to do".

THE CLASS TABLE BELOW IS DUPLICATED IN de-funnel-relay/api/_stagesync.js. Change both.

Usage:
    ghl_setter_status_sync.py                  # dry run, cards moved in the last 30 days
    ghl_setter_status_sync.py --apply
    ghl_setter_status_sync.py --apply --days 2 # cheap pass: only recently-touched cards
"""
import argparse, datetime, os, re, sys, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghl_http
from test_records import is_test_record
import ghl_dfy_to_close as base   # shared Close/GHL plumbing, dedupe-safe lead confirmation

SETTER_PIPE = "cDPVmjlo65NVVn1g6ePU"
GRACE_MINUTES = 5

CLASSES = {
    "intake":    (["New Lead - DFY (Call within 5 mins)", "New Lead - Mentorship (Call within 5 mins)"],
                  ["New Lead – DFY", "New Lead", "Free Community Member"]),
    "no_answer": (["No Answer 1 (Day 1)", "No Answer 2 (Day 1)", "No Answer 3 (Day 1)",
                   "No Answer 1 (Day 2)", "No Answer 2 (Day 2)", "No Answer 3 (Day 2)", "No Answer Day 3"],
                  ["No Answer – Attempting"]),
    "no_show":   (["No Show - Repeat Process"], ["No Show"]),
    "spoke":     (["Spoke – Qualifying"], ["Spoke – Qualifying"]),
    "qualified": (["Qualified – Not Booked"], ["Qualified – Not Booked"]),
    "callback":  (["Callback Booked"], ["Callback Booked"]),
    "call_set":  (["Call Set"], ["Call Booked", "Call Confirmed", "Closer Follow Up (Charlie)",
                                 "Closer Follow Up (Mike)", "Signed Up", "Signed Up - Balance Owed"]),
    "nurture":   (["Long Term – Nurture"], ["Long Term – Nurture"]),
    "lost":      (["Lead Lost / Not Interested"], ["Lead Lost / Not Interested", "DQ"]),
}
# Close statuses a GHL move may never overwrite - a closer set these after the setter's job ended.
PROTECTED_CLOSE = {"Call Confirmed", "Closer Follow Up (Charlie)", "Closer Follow Up (Mike)",
                   "Signed Up", "Signed Up - Balance Owed"}
# Moves refused even when the other side is newer (same table as BLOCKED in _stagesync.js):
#   call_set -> pre-booking: a booking is a fact; a later "Spoke" dropdown is a setter logging
#     the conversation (Lamont Graggs, 26 Sept). Only no_show / lost / nurture leave Call Set.
#   lost -> no_answer: a dialler retry is not a decision to reopen (Amug Mug, 21 Sept).
BLOCKED = {"call_set": {"no_answer", "spoke", "qualified", "callback"}, "lost": {"no_answer"}}
RUNG_INTAKE = {"done-for-you": "New Lead – DFY", "mentorship": "New Lead", "community": "Free Community Member"}


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[‐-―-]", "-", str(s or "").lower())).strip()


GHL_CLASS = {norm(n): c for c, (g, _) in CLASSES.items() for n in g}
CLOSE_CLASS = {norm(n): c for c, (_, cl) in CLASSES.items() for n in cl}


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def last_close_status_change(lead_id):
    """Newest status-change activity on the lead. Activity reads are by lead id, so they do
    not go through the lagging search index."""
    d = base.close(f"/activity/status_change/lead/?lead_id={lead_id}&_limit=1&_fields=date_created,new_status_label")
    rows = d.get("data") or []
    return parse_ts(rows[0]["date_created"]) if rows else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=int, default=30,
                    help="only cards created or moved in the last N days (default 30)")
    a = ap.parse_args()

    ghl_http.preflight(base.GHL_PIT, base.GHL_LOC)

    # Stage ids <-> names, live. A class stage that no longer exists is a hard error.
    pipes = base.ghl(f"/opportunities/pipelines?locationId={base.GHL_LOC}")["pipelines"]
    pipe = next((p for p in pipes if p["id"] == SETTER_PIPE), None)
    if not pipe:
        raise SystemExit(f"FATAL: GHL Setter pipeline {SETTER_PIPE} not found")
    stage_name = {s["id"]: s["name"] for s in pipe["stages"]}
    stage_id = {norm(s["name"]): s["id"] for s in pipe["stages"]}
    for c, (g, _) in CLASSES.items():
        for n in g:
            if norm(n) not in stage_id:
                raise SystemExit(f"FATAL: GHL Setter stage {n!r} is gone - renamed? Fix CLASSES in both files.")

    statuses = base.close("/status/lead/")["data"]
    status_id = {norm(s["label"]): s["id"] for s in statuses}
    for c, (_, cl) in CLASSES.items():
        for n in cl:
            if norm(n) not in status_id:
                raise SystemExit(f"FATAL: Close lead status {n!r} is gone - renamed? Fix CLASSES in both files.")

    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now - datetime.timedelta(days=a.days)
    grace = now - datetime.timedelta(minutes=GRACE_MINUTES)

    cards = base.pull_ghl_opps(SETTER_PIPE)
    scoped = [o for o in cards
              if max(filter(None, [parse_ts(o.get("lastStageChangeAt")), parse_ts(o.get("createdAt")),
                                   parse_ts(o.get("updatedAt"))]), default=now) >= horizon]
    print(f"Setter cards: {len(cards)}   in scope (last {a.days}d): {len(scoped)}")

    t = dict(ok=0, to_close=0, to_ghl=0, created=0, protected=0, skipped=0, failed=0)
    for o in scoped:
        c = o.get("contact") or {}
        email = (c.get("email") or "").strip().lower()
        name = (c.get("name") or o.get("name") or "")[:26]
        gname = stage_name.get(o.get("pipelineStageId"), "?")
        gcls = GHL_CLASS.get(norm(gname))
        g_at = parse_ts(o.get("lastStageChangeAt")) or parse_ts(o.get("createdAt"))
        tag = f"{name:26} {email[:32]:32}"
        if not base.EMAIL_RE.match(email):
            print(f"  SKIP   {tag} no usable e-mail"); t["skipped"] += 1; continue
        if is_test_record(c.get("name") or o.get("name"), email):
            print(f"  TEST   {tag} test record - not synced"); t["skipped"] += 1; continue
        if g_at and g_at > grace:
            print(f"  WAIT   {tag} GHL moved {int((now - g_at).total_seconds())}s ago - inside grace"); t["skipped"] += 1; continue

        try:
            lead = base.confirm_existing_lead(email, None)
        except base.ConfirmFailed as e:
            print(f"  SKIP   {tag} could not confirm in Close ({e})"); t["skipped"] += 1; continue

        if not lead:
            # Not in Close at all. Created ones are older than the relay's own window.
            created = parse_ts(o.get("createdAt"))
            if created and created > now - datetime.timedelta(minutes=base.GRACE_MINUTES):
                print(f"  WAIT   {tag} not in Close yet - still inside the relay's window"); t["skipped"] += 1; continue
            if gcls == "intake" or not gcls:
                tags = [x.lower() for x in (c.get("tags") or [])]
                rung = next((r for r in RUNG_INTAKE if f"rung-{r}" in tags), "mentorship")
                label = RUNG_INTAKE[rung]
            else:
                label = CLASSES[gcls][1][0]
            print(f"  {'CREATE' if a.apply else 'DRY+  '} {tag} not in Close -> new lead at {label!r} (GHL {gname!r})")
            if a.apply:
                contact = {"name": c.get("name") or email, "emails": [{"type": "office", "email": email}]}
                if c.get("phone"):
                    contact["phones"] = [{"type": "mobile", "phone": c["phone"]}]
                r = base.close("/lead/", {"name": c.get("name") or email, "status_id": status_id[norm(label)],
                                          "contacts": [contact],
                                          "description": f"Backfilled from GHL Setter Pipeline ({o.get('name', '')}) - "
                                                         f"the real-time Close write never landed."}, _raise=False)
                if not r.get("id"):
                    print(f"  FAIL   {tag} lead create: {str(r)[:120]}"); t["failed"] += 1; continue
            t["created"] += 1
            continue

        clabel = lead.get("status_label") or ""
        ccls = CLOSE_CLASS.get(norm(clabel))
        if gcls == ccls:
            t["ok"] += 1; continue

        c_at = last_close_status_change(lead["id"]) or parse_ts(lead.get("date_created"))
        if c_at and c_at > grace:
            print(f"  WAIT   {tag} Close changed moments ago - inside grace"); t["skipped"] += 1; continue

        ghl_newer = (g_at or horizon) > (c_at or horizon)
        if ghl_newer:
            if not gcls or gcls == "intake":
                t["skipped"] += 1; continue
            if clabel in PROTECTED_CLOSE:
                print(f"  KEEP   {tag} Close {clabel!r} is a closer's status - GHL {gname!r} not pushed")
                t["protected"] += 1; continue
            if gcls in BLOCKED.get(ccls, ()):
                print(f"  KEEP   {tag} Close {clabel!r} ({ccls}) is not moved to {gcls} by GHL {gname!r}")
                t["protected"] += 1; continue
            target = CLASSES[gcls][1][0]
            print(f"  {'->CLOSE' if a.apply else 'DRY>CL'} {tag} Close {clabel!r} -> {target!r}   (GHL {gname!r} is newer)")
            if a.apply:
                r = base.close(f"/lead/{lead['id']}/", {"status_id": status_id[norm(target)]}, method="PUT", _raise=False)
                if "_http" in r or "_err" in r:
                    print(f"  FAIL   {tag} {str(r)[:120]}"); t["failed"] += 1; continue
            t["to_close"] += 1
        else:
            if not ccls or ccls == "intake":
                t["skipped"] += 1; continue
            if ccls in BLOCKED.get(gcls, ()):
                print(f"  KEEP   {tag} GHL {gname!r} ({gcls}) is not moved to {ccls} by Close {clabel!r}")
                t["protected"] += 1; continue
            target = CLASSES[ccls][0][0]
            print(f"  {'->GHL ' if a.apply else 'DRY>GH'} {tag} GHL {gname!r} -> {target!r}   (Close {clabel!r} is newer)")
            if a.apply:
                try:
                    ghl_http.request("PUT", f"/opportunities/{o['id']}", base.GHL_PIT,
                                     {"pipelineStageId": stage_id[norm(target)]})
                except ghl_http.GHLError as e:
                    print(f"  FAIL   {tag} {str(e)[:120]}"); t["failed"] += 1; continue
            t["to_ghl"] += 1
        if a.apply:
            time.sleep(0.2)

    print(f"\nin sync: {t['ok']}   GHL->Close: {t['to_close']}   Close->GHL: {t['to_ghl']}   "
          f"Close leads created: {t['created']}   closer-protected: {t['protected']}   "
          f"skipped: {t['skipped']}   failed: {t['failed']}")
    if not a.apply:
        print("DRY RUN - re-run with --apply to write.")
    if t["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
