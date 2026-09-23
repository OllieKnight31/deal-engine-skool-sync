#!/usr/bin/env python3
"""
Safety net: every recent Skool member should exist in Close AND in GoHighLevel.

This catches every failure mode the Zap can have - a rejected field, a malformed
email, an expired app connection, a task-limit halt, or the Zap simply being off -
without depending on Zapier reporting anything.

Usage:  reconcile_skool.py [--pages N] [--fix]
        --fix backfills whatever is missing (Close and GHL).

One thing this deliberately does NOT do is fight GoHighLevel's deduper. Two Skool
accounts can share a phone number - the same person joining twice - and GHL then
collapses both onto ONE contact, whose email is simply whichever upsert ran last.
Presence here is keyed on email, so the member who did not win the contact looked
missing on every single pass and was rewritten forever, which also meant
"MISSING FROM GHL: 0" could never happen and the number was worthless as a health
signal. Those members are now detected, reported under their own heading, and left
alone. Nothing is merged or deleted - there are two real people-records behind it.
"""
import json, re, sys, os, time, base64, datetime, urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.dirname(__file__))
from skool_to_ghl import (pull_skool, SkoolUnavailable, ghl, clean_phone, iso_from_location, EMAIL_RE,
                          origin_tags, set_origin_tags,
                          LOC, PIPE, STAGE, CF)
import notify_slack

CLOSE_KEY = os.environ["CLOSE_API_KEY"]
CLOSE_AUTH = "Basic " + base64.b64encode((CLOSE_KEY + ":").encode()).decode()
CLOSE_STATUS = "stat_vwALWTscQ3ITPN8yn0hKhGzRX96PQUbSlETnQgPH4tO"   # Free Community Member
CLOSE_PIPE_STAGE = "stat_uncVryRJNI5eQkeEEnzbAoYRAlGbLLzrxvLdvXnYn1Q"  # New Member
CF_CLOSE_SRC   = "cf_vqAaS8FL6yPi53YJF9EFS7MUk1FqNTJRDwKzr0DiXgG"
CF_CLOSE_TAGS  = "cf_4woErrH0PLElkvCs0zL7abxAhIBxe2Z5fpX69t0V8Iv"
CF_CLOSE_PHONE = "cf_EtAflDq1xeAZxVaS6pSPtSnm7Z7IbZUNlNEk5fkHu0V"
CF_CLOSE_ANS   = "cf_BWnMmpo4IVgcz0TSbbyYs2pQU6f44XLs1NYpx5G1p74"
CF_CLOSE_URL   = "cf_8xBqwEeBvCrBSREoTWoLNZAIzSSDWxTpkdFkJD7Wxih"

def close(method, path, payload=None):
    r = urllib.request.Request("https://api.close.com/api/v1" + path,
        data=json.dumps(payload).encode() if payload is not None else None, method=method,
        headers={"Authorization": CLOSE_AUTH, "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(r, timeout=40) as resp: return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode() or "{}"
            # 429 and 5xx are "ask again", not answers. Returning them to the caller is how a
            # rate-limited SEARCH got read as "this lead does not exist" and duplicated it.
            if e.code == 429 or e.code >= 500:
                if attempt < 3:
                    wait = int(e.headers.get("Retry-After") or 0) or 5 * (attempt + 1)
                    time.sleep(wait); continue
            try: return e.code, json.loads(body)
            except Exception: return e.code, {}
        except Exception:
            # Transient - a dropped connection here used to abort the whole run.
            if attempt == 3: raise
            time.sleep(3 * (attempt + 1))

def lead_by_id(lead_id):
    """Immediately-consistent read. Close's SEARCH index lags writes by minutes, so the
    Slack-notified stamp written on one pass is still absent from search results on the
    next - which announced 26 members twice on 19 Sept. Fetching the lead by id is
    strongly consistent, so it is the only safe way to answer "have we told the team?".
    """
    s, d = close("GET", f"/lead/{lead_id}/?_fields=id,display_name,custom")
    return d if s == 200 else None


# Returned when the search itself failed. It is NOT the same as "no such lead": treating a
# failed lookup as an absence is what creates duplicate leads, and a duplicate lead means a
# second Slack alert for a person the team has already seen (Joshua Raj, 19 and 21 Sept).
SEARCH_FAILED = object()


def in_close(email):
    q = urllib.parse.quote(f'email:"{email}"')
    # "custom" pulls every custom field back as custom.cf_<id> keys - needed so we
    # can see whether Slack has already been told about this person.
    s, d = close("GET", f"/lead/?query={q}&_limit=1&_fields=id,display_name,custom")
    if s != 200:
        print(f"  close search FAILED for {email}: HTTP {s} - skipping rather than guessing")
        return SEARCH_FAILED
    return (d.get("data") or [None])[0]

# ---- collapsed-contact state -------------------------------------------------
# GitHub Actions starts from a clean checkout every run, so this file is an
# optimisation and a record, never the source of truth. The authoritative signal is
# rebuilt live from the pipeline on every run (see collapsed_onto), so a missing
# state file changes nothing except the wording of the first report.
STATE_FILE = os.environ.get("SKOOL_COLLISION_STATE") or os.path.join(
    os.path.expanduser("~"), ".local", "state", "dealengine", "skool_ghl_collisions.json")


def load_collisions():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh).get("collisions", {})
    except Exception:
        return {}


def save_collisions(collisions):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as fh:
            json.dump({"collisions": collisions}, fh, indent=1, sort_keys=True)
    except Exception as e:
        # Never fail a sync over a cache file.
        print(f"  note: could not write {STATE_FILE} ({e}) - collisions are still "
              f"detected live, just not remembered between runs")


# ---- just-created-in-Close cache ---------------------------------------------
# Close's SEARCH index lags writes by minutes, and in_close() is a search. So a lead
# created on one pass can still be absent from search on the NEXT pass, and the
# reconciler would create it a second time. That is how duplicate community leads got
# into Close before (Tyler Pearson, Tyler Williams - both found and deleted by hand).
#
# At the old ~6-minute pass interval this was already a live risk; it is what made
# shortening the interval unsafe. Remembering the lead id for a short window closes it:
# a cached candidate is re-read with GET /lead/{id}, which is strongly consistent, so
# the answer no longer depends on the index at all.
#
# Same storage semantics as the collision cache: shared by every pass of one CI run,
# gone on the next checkout - which is fine, because the window it guards is minutes.
CREATED_FILE = os.environ.get("SKOOL_CLOSE_CREATED") or os.path.join(
    os.path.expanduser("~"), ".local", "state", "dealengine", "skool_close_created.json")
CREATED_TTL_MIN = int(os.environ.get("CLOSE_INDEX_LAG_MIN", "30"))


def load_created():
    """email -> lead id, for leads created recently enough that search may not show them."""
    try:
        with open(CREATED_FILE) as fh:
            raw = json.load(fh).get("created", {})
    except Exception:
        return {}
    cutoff = time.time() - CREATED_TTL_MIN * 60
    return {e: v for e, v in raw.items() if (v or {}).get("at", 0) > cutoff}


def save_created(created):
    try:
        os.makedirs(os.path.dirname(CREATED_FILE), exist_ok=True)
        with open(CREATED_FILE, "w") as fh:
            json.dump({"created": created}, fh, indent=1, sort_keys=True)
    except Exception as e:
        print(f"  note: could not write {CREATED_FILE} ({e}) - a lead created in the last "
              f"{CREATED_TTL_MIN} min could be created twice if search has not caught up")


_GHL_EMAILS = None
_GHL_CONTACT_BY_EMAIL = {}
_GHL_EMAIL_BY_CONTACT = {}
_GHL_CONTACT_BY_PHONE = {}
def ghl_pipeline_emails():
    """Emails already carded in the GHL Skool pipeline.

    Read via the opportunity search on purpose: it returns the linked contact's
    email and only needs opportunities.readonly, which the Private Integration
    token has. Reading /contacts/ would need contacts.readonly, which it does not.
    """
    global _GHL_EMAILS
    if _GHL_EMAILS is not None: return _GHL_EMAILS
    emails, page = set(), 1
    while True:
        s, d = ghl("GET", f"/opportunities/search?location_id={LOC}&pipeline_id={PIPE}&limit=100&page={page}")
        if s != 200: break
        ops = d.get("opportunities", [])
        for o in ops:
            c = o.get("contact") or {}
            em = (c.get("email") or "").lower()
            cid = c.get("id")
            # The phone on the carded contact is what GHL actually deduped on, so it is
            # also the durable evidence that a second Skool account collapsed onto it.
            # It costs nothing here and needs no state file to survive a fresh checkout.
            ph = (c.get("phone") or "").strip()
            if cid and ph: _GHL_CONTACT_BY_PHONE.setdefault(ph, cid)
            if cid and em: _GHL_EMAIL_BY_CONTACT[cid] = em
            if em:
                emails.add(em)
                # Captured from the same response, at no extra cost, so the Slack post for
                # a member who was ALREADY in GHL still carries an "Open in GHL" button.
                # Previously only members created during that very run got one, which is
                # the minority case once the 30-minute GHL pass has already run.
                if cid: _GHL_CONTACT_BY_EMAIL[em] = cid
        if len(ops) < 100: break
        page += 1
    _GHL_EMAILS = emails
    return emails

def in_ghl(email):
    return email.lower() in ghl_pipeline_emails()


def collapsed_onto(email, phone):
    """(contact_id, owner_email) when GHL has already folded this member into an
    existing carded contact - otherwise None.

    Recognised by the phone number, because that is what GHL dedupes on: the member's
    email is absent from the pipeline, but their number is already on a card belonging
    to a DIFFERENT email. Creating them again is impossible (GHL returns the same
    contact and refuses a second open card in the same pipeline), so the only useful
    thing to do is recognise it, report it, and leave both records alone.
    """
    ghl_pipeline_emails()
    email = (email or "").lower()
    if not phone or not email or email in _GHL_EMAILS:
        return None
    cid = _GHL_CONTACT_BY_PHONE.get(phone)
    if not cid:
        return None
    owner = _GHL_EMAIL_BY_CONTACT.get(cid, "")
    return (cid, owner) if owner and owner != email else None


def ghl_contact_id(email, collisions=None):
    ghl_pipeline_emails()
    cid = _GHL_CONTACT_BY_EMAIL.get((email or "").lower())
    if cid:
        return cid
    # A collapsed member has no contact of their own; link to the one they landed on
    # so the Slack card still opens something real.
    rec = (collisions or {}).get((email or "").lower())
    return rec.get("contact_id") if rec else None

# Consecutive passes on which Skool itself was unavailable (a 403 from its WAF, a 5xx, a
# timeout). Kept in a file because every pass is its own process. The checkout persists for
# the life of one CI run, so a fresh run starts the count again - which is the right shape:
# a block that outlasts a run re-alarms about hourly instead of every three minutes.
STATE_DIR = os.environ.get("SKOOL_STATE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".state")
UNAVAILABLE_FILE = os.path.join(STATE_DIR, "skool_unavailable_passes")
UNAVAILABLE_ALERT_AFTER = int(os.environ.get("SKOOL_UNAVAILABLE_ALERT_AFTER", "3"))


def _unavailable_count():
    try:
        return int(open(UNAVAILABLE_FILE).read().strip() or 0)
    except Exception:
        return 0


def _unavailable_set(n):
    os.makedirs(STATE_DIR, exist_ok=True)
    if n:
        with open(UNAVAILABLE_FILE, "w") as fh:
            fh.write(str(n))
    elif os.path.exists(UNAVAILABLE_FILE):
        os.unlink(UNAVAILABLE_FILE)


def main():
    pages = 2
    if "--pages" in sys.argv: pages = int(sys.argv[sys.argv.index("--pages")+1])
    fix = "--fix" in sys.argv
    notify = "--no-notify" not in sys.argv
    stamp_existing = "--stamp-existing" in sys.argv

    members = pull_skool(pages)
    # Skool answered. If earlier passes had to be skipped, close the loop in Slack so the
    # alarm is never left standing - and only if an alarm actually went out.
    prior = _unavailable_count()
    if prior:
        _unavailable_set(0)
        if prior >= UNAVAILABLE_ALERT_AFTER:
            notify_slack.post_note("Skool is answering again",
                                   f"{prior} passes were skipped while it was not. This pass "
                                   f"syncs the backlog - nothing was lost.")
    valid = [m for m in members if m["email"] and EMAIL_RE.match(m["email"])]
    print(f"skool members checked: {len(members)} | with valid email: {len(valid)}\n")

    # Zero members from a community with ~1,800 of them means the read is broken, not that
    # nobody joined. Left unchecked this fails *silently* - a green run that syncs nothing.
    if not members:
        notify_slack.post_alert("Skool returned zero members",
                                f"Pulled {pages} page(s) and got nothing back.", mention=True)
        raise SystemExit("Skool returned no members - aborting rather than reporting success")

    # Keep the Close lead we found for each member. It carries the "Slack Notified At"
    # stamp, which is what stops the team being pinged twice about the same person.
    collisions = load_collisions()
    created = load_created()
    close_lead = {}
    miss_close, miss_ghl, collapsed = [], [], []
    recovered = 0
    search_failures = 0
    for m in valid:
        lead = in_close(m["email"])
        if lead is SEARCH_FAILED:
            # Do not create, do not announce, do not mark missing. The next pass is ~3
            # minutes away and every write here is idempotent, so losing one pass costs
            # nothing; guessing costs a duplicate lead and a duplicate Slack alert.
            search_failures += 1
            continue
        if not lead:
            # Search says missing. Before believing it, check whether WE created this lead
            # within the index-lag window and re-read it by id.
            cached = created.get(m["email"].lower())
            if cached and cached.get("lead_id"):
                lead = lead_by_id(cached["lead_id"])
                if lead:
                    recovered += 1
        close_lead[m["email"]] = lead
        if not lead: miss_close.append(m)
        if in_ghl(m["email"]):
            # They have their own contact after all - drop any stale collision record.
            collisions.pop(m["email"].lower(), None)
            continue
        hit = collapsed_onto(m["email"], clean_phone(m["phone_raw"], iso_from_location(m["location"])))
        if not hit and m["email"].lower() in collisions:
            rec = collisions[m["email"].lower()]
            hit = (rec.get("contact_id"), rec.get("owner_email", ""))
        if hit:
            m["_collapsed_onto"], m["_collapsed_owner"] = hit
            collisions.setdefault(m["email"].lower(), {})
            collisions[m["email"].lower()].update({
                "contact_id": hit[0], "owner_email": hit[1],
                "name": f"{m['first'] or ''} {m['last'] or ''}".strip(),
                "last_seen": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
            collapsed.append(m)
        else:
            miss_ghl.append(m)

    if search_failures:
        print(f"\nclose search failed for {search_failures} member(s) - they were skipped this "
              f"pass and will be picked up on the next one")

    if stamp_existing:
        n = 0
        for m in valid:
            lead = close_lead.get(m["email"])
            if lead and not notify_slack.already_notified(lead):
                notify_slack.stamp_close(close, lead["id"]); n += 1
        print(f"\nstamped {n} existing leads as already-notified. "
              f"Slack will only post genuinely new joins from here.")
        return

    if recovered:
        print(f"note: {recovered} lead(s) were absent from Close SEARCH but confirmed by id "
              f"- created within the last {CREATED_TTL_MIN} min, not duplicated\n")
    print(f"MISSING FROM CLOSE: {len(miss_close)}")
    for m in miss_close: print(f"   {(m['first'] or '')+' '+(m['last'] or ''):28} {m['email']:36} joined {m['joined'][:19] if m['joined'] else '?'}")
    print(f"\nMISSING FROM GHL  : {len(miss_ghl)}")
    for m in miss_ghl: print(f"   {(m['first'] or '')+' '+(m['last'] or ''):28} {m['email']:36} joined {m['joined'][:19] if m['joined'] else '?'}")

    # Reported separately and on purpose. These are NOT missing - GHL holds them, folded
    # into somebody else's contact - and they are NOT healthy either, because there are
    # two real people-records behind each one. Counting them as missing made the number
    # above permanently non-zero; hiding them would lose duplicates nobody can see.
    print(f"\nCOLLAPSED ONTO AN EXISTING GHL CONTACT: {len(collapsed)}")
    for m in collapsed:
        print(f"   {(m['first'] or '')+' '+(m['last'] or ''):28} {m['email']:36} "
              f"-> contact {m['_collapsed_onto']} now held by {m['_collapsed_owner']}")
    if collapsed:
        print("   (GHL deduped these on a shared phone number. Not rewritten, not merged, "
              "not deleted - resolve by hand in GHL if they are genuinely one person.)")

    if not fix:
        # Report-only writes nothing at all, not even the collision cache. Detection is
        # rebuilt live from the pipeline each run, so nothing is lost by that.
        print("\nCheck only - rerun with --fix to backfill.")
        return
    save_collisions(collisions)
    for m in miss_close:
        iso = iso_from_location(m["location"]); ph = clean_phone(m["phone_raw"], iso)
        name = f"{m['first'] or ''} {m['last'] or ''}".strip()
        contact = {"name": name, "emails":[{"type":"other","email":m["email"]}]}
        if ph: contact["phones"] = [{"type":"mobile","phone":ph}]
        body = {"name":name, "status_id":CLOSE_STATUS, "contacts":[contact],
                f"custom.{CF_CLOSE_SRC}":"Skool community", f"custom.{CF_CLOSE_TAGS}":["Skool Member"],
                f"custom.{CF_CLOSE_PHONE}": m["phone_raw"] or "", f"custom.{CF_CLOSE_ANS}": m["why"] or "",
                f"custom.{CF_CLOSE_URL}": f"https://www.skool.com/@{m['handle']}"}
        s, d = close("POST", "/lead/", body)
        print(f"  close create {name}: {s} {d.get('id','')}")
        if s == 200 and d.get("id"):
            close("POST", "/opportunity/", {"lead_id":d["id"], "status_id":CLOSE_PIPE_STAGE, "value":0,
                                            "note":"Skool free community signup (reconciler)"})
            # Newly created, so it carries no notified stamp - Slack will pick it up below.
            close_lead[m["email"]] = {"id": d["id"]}
            created[m["email"].lower()] = {"lead_id": d["id"], "at": time.time()}
            save_created(created)

    ghl_contact = {}
    ghl_tags = {}         # email -> the contact's tags after this run's write (for the card)
    claimed = {}          # contact id -> the email that legitimately owns it this run
    for m in miss_ghl:
        iso = iso_from_location(m["location"]); ph = clean_phone(m["phone_raw"], iso)
        name = f"{m['first'] or ''} {m['last'] or ''}".strip()
        cfs = [{"id":CF["profile_url"],"value":f"https://www.skool.com/@{m['handle']}"},
               {"id":CF["join_answer"],"value":m["why"] or ""},
               {"id":CF["phone_answer"],"value":m["phone_raw"] or ""}]
        if m["joined"]: cfs.append({"id":CF["joined"],"value":m["joined"][:10]})
        tags = ["skool-member", "skool-free-community"]
        if notify_slack.suspect_reason(m):
            # Still created - a heuristic must never be the reason a real lead vanishes.
            tags.append("skool-suspect")
        # No `tags` on the upsert: it REPLACES the list (probed 23 Sept 2026) and was
        # wiping `application-complete` and the funnel's `src-*` off anyone who applied
        # before joining. They are added afterwards, additively, with the origin.
        body = {"locationId":LOC,"firstName":m["first"] or "","lastName":m["last"] or "",
                "email":m["email"],"source":f"Skool community ({m['source']})" if m["source"] else "Skool community",
                "customFields":cfs}
        if ph: body["phone"] = ph
        s, d = ghl("POST", "/contacts/upsert", body)
        c = (d.get("contact") or {})
        print(f"  ghl upsert {name}: {s} {c.get('id','')}")
        if not c.get("id"):
            continue
        ghl_contact[m["email"]] = c["id"]
        before = list(c.get("tags") or [])
        added = tags + origin_tags(m["source"], before)
        set_origin_tags(c["id"], added, before)
        ghl_tags[m["email"]] = sorted(set(before) | set(added))

        # Second line of defence, for a collapse the phone index could not see (a first
        # collision, or one GHL deduped on something other than the number): the upsert
        # handed back a contact that already belongs to a different email. Record it and
        # stop - carding it would only hit OPPORTUNITY_NO_DUPLICATE, and the next run
        # would do the whole thing over again, forever.
        mine = m["email"].lower()
        owner = ""
        for candidate in ((c.get("email") or "").lower(),
                          _GHL_EMAIL_BY_CONTACT.get(c["id"], ""),
                          claimed.get(c["id"], "")):
            if candidate and candidate != mine:
                owner = candidate
                break
        if owner:
            print(f"    collapsed onto existing contact {c['id']} (held by {owner}) "
                  f"- recorded, not carded, and skipped from here on")
            collisions[mine] = {
                "contact_id": c["id"], "owner_email": owner, "name": name,
                "last_seen": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
            m["_collapsed_onto"], m["_collapsed_owner"] = c["id"], owner
            collapsed.append(m)
            save_collisions(collisions)
            continue
        claimed[c["id"]] = mine

        if not in_ghl(m["email"]):
            ghl("POST", "/opportunities/", {"pipelineId":PIPE,"locationId":LOC,"pipelineStageId":STAGE,
                "name":f"{name} — Skool free community","status":"open","contactId":c["id"],"monetaryValue":0})

    print(f"\ncollapsed onto an existing GHL contact, total: {len(collapsed)} "
          f"(recorded in {STATE_FILE}; never rewritten again)")

    # ---- Tell the team ------------------------------------------------------
    # Driven off the Close stamp rather than "did we just create it", so a member
    # the Zap happened to reach first is still announced exactly once.
    if not (notify and notify_slack.enabled):
        if notify:
            print("\nslack: SLACK_BOT_TOKEN not set - notifications skipped")
        return

    candidates = [m for m in valid
                  if close_lead.get(m["email"]) and not notify_slack.already_notified(close_lead[m["email"]])]
    # Re-check every candidate against a consistent read before announcing. Only candidates
    # need it, so this costs one extra GET per genuinely-new member rather than per member.
    pending = []
    for m in candidates:
        lead_id = close_lead[m["email"]]["id"]
        fresh = lead_by_id(lead_id)
        if fresh is None:
            print(f"  could not re-read {m['email']} - skipping rather than risk a duplicate")
            continue
        if notify_slack.already_notified(fresh):
            continue          # stale search result; the team already knows
        pending.append(m)
    skipped = len(candidates) - len(pending)
    if skipped:
        print(f"slack: {skipped} candidate(s) were already announced (stale search index)")
    if not pending:
        print("\nslack: nothing new to announce")
        return

    if len(pending) > notify_slack.NOTIFY_MAX:
        ok = notify_slack.post_digest(pending, reason=f"{len(pending)} at once, likely a backfill")
        print(f"\nslack: digest of {len(pending)} members -> {ok}")
        if ok:
            bad = [m["email"] for m in pending
                   if not notify_slack.stamp_close(close, close_lead[m["email"]]["id"])]
            if bad:
                print(f"  WARNING: {len(bad)} stamp(s) failed, these will re-announce: {bad[:5]}")
    else:
        for m in pending:
            if notify_slack.post_member(m, close_id=close_lead[m["email"]]["id"],
                                        ghl_contact_id=ghl_contact.get(m["email"])
                                                       or ghl_contact_id(m["email"], collisions),
                                        ghl_tags=ghl_tags.get(m["email"])):
                if not notify_slack.stamp_close(close, close_lead[m["email"]]["id"]):
                    print(f"  WARNING: stamp failed for {m['email']} - it will re-announce")
                print(f"  slack announced {m['email']}")

def run():
    # Any unhandled failure is announced before it propagates. The job still exits
    # non-zero so CI goes red too - Slack is the alarm, the exit code is the record.
    try:
        main()
    except SkoolUnavailable as e:
        # Skool refused or failed to answer, and the cookie is NOT the reason (see the class
        # docstring in skool_to_ghl.py). Skip this pass: the next one retries and every write
        # is idempotent, so joins are delayed, not lost. Alarm only once it persists, and say
        # what it actually is - the crash handler below used to blame the cookie for a 403
        # and sent a human off to rotate a secret that was working the whole time (22 Sept).
        n = _unavailable_count() + 1
        _unavailable_set(n)
        print(f"SKOOL UNAVAILABLE - pass skipped ({e}); consecutive passes: {n}")
        if n == UNAVAILABLE_ALERT_AFTER or (n > UNAVAILABLE_ALERT_AFTER and n % 20 == 0):
            notify_slack.post_alert(
                f"Skool has not answered for {n} passes in a row", str(e), mention=True,
                hint=("This is on Skool's side - its CloudFront WAF refusing the runner, or an "
                      "outage - *not* the cookie. A dead cookie shows as a redirect to /about, "
                      "never a 403. Nothing to rotate. Joins are delayed, not lost: every pass "
                      "retries and backfills as soon as Skool answers again."))
        sys.exit(75)   # EX_TEMPFAIL - a red pass, not a crash
    except SystemExit as e:
        if e.code not in (0, None):
            notify_slack.post_alert("Skool sync aborted", e.code, mention=True)
        raise
    except Exception:
        import traceback
        notify_slack.post_alert("Skool sync crashed", traceback.format_exc()[-2000:], mention=True)
        raise


if __name__ == "__main__":
    run()
