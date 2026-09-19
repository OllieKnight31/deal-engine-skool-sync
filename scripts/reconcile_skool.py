#!/usr/bin/env python3
"""
Safety net: every recent Skool member should exist in Close AND in GoHighLevel.

This catches every failure mode the Zap can have - a rejected field, a malformed
email, an expired app connection, a task-limit halt, or the Zap simply being off -
without depending on Zapier reporting anything.

Usage:  reconcile_skool.py [--pages N] [--fix]
        --fix backfills whatever is missing (Close and GHL).
"""
import json, re, sys, os, time, base64, urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.dirname(__file__))
from skool_to_ghl import (pull_skool, ghl, clean_phone, iso_from_location, EMAIL_RE,
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
    for attempt in range(3):
        try:
            with urllib.request.urlopen(r, timeout=40) as resp: return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            try: return e.code, json.loads(e.read().decode() or "{}")
            except Exception: return e.code, {}
        except Exception:
            # Transient - a dropped connection here used to abort the whole run.
            if attempt == 2: raise
            time.sleep(3 * (attempt + 1))

def in_close(email):
    q = urllib.parse.quote(f'email:"{email}"')
    # "custom" pulls every custom field back as custom.cf_<id> keys - needed so we
    # can see whether Slack has already been told about this person.
    s, d = close("GET", f"/lead/?query={q}&_limit=1&_fields=id,display_name,custom")
    return (d.get("data") or [None])[0] if s == 200 else None

_GHL_EMAILS = None
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
            em = ((o.get("contact") or {}).get("email") or "").lower()
            if em: emails.add(em)
        if len(ops) < 100: break
        page += 1
    _GHL_EMAILS = emails
    return emails

def in_ghl(email):
    return email.lower() in ghl_pipeline_emails()

def main():
    pages = 2
    if "--pages" in sys.argv: pages = int(sys.argv[sys.argv.index("--pages")+1])
    fix = "--fix" in sys.argv
    notify = "--no-notify" not in sys.argv
    stamp_existing = "--stamp-existing" in sys.argv

    members = pull_skool(pages)
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
    close_lead = {}
    miss_close, miss_ghl = [], []
    for m in valid:
        lead = in_close(m["email"])
        close_lead[m["email"]] = lead
        if not lead: miss_close.append(m)
        if not in_ghl(m["email"]): miss_ghl.append(m)

    if stamp_existing:
        n = 0
        for m in valid:
            lead = close_lead.get(m["email"])
            if lead and not notify_slack.already_notified(lead):
                notify_slack.stamp_close(close, lead["id"]); n += 1
        print(f"\nstamped {n} existing leads as already-notified. "
              f"Slack will only post genuinely new joins from here.")
        return

    print(f"MISSING FROM CLOSE: {len(miss_close)}")
    for m in miss_close: print(f"   {(m['first'] or '')+' '+(m['last'] or ''):28} {m['email']:36} joined {m['joined'][:19] if m['joined'] else '?'}")
    print(f"\nMISSING FROM GHL  : {len(miss_ghl)}")
    for m in miss_ghl: print(f"   {(m['first'] or '')+' '+(m['last'] or ''):28} {m['email']:36} joined {m['joined'][:19] if m['joined'] else '?'}")

    if not fix:
        print("\nCheck only - rerun with --fix to backfill.")
        return
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

    ghl_contact = {}
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
        body = {"locationId":LOC,"firstName":m["first"] or "","lastName":m["last"] or "",
                "email":m["email"],"source":f"Skool community ({m['source']})" if m["source"] else "Skool community",
                "tags":tags,"customFields":cfs}
        if ph: body["phone"] = ph
        s, d = ghl("POST", "/contacts/upsert", body)
        c = (d.get("contact") or {})
        print(f"  ghl upsert {name}: {s} {c.get('id','')}")
        if c.get("id"):
            ghl_contact[m["email"]] = c["id"]
        if c.get("id") and not in_ghl(m["email"]):
            ghl("POST", "/opportunities/", {"pipelineId":PIPE,"locationId":LOC,"pipelineStageId":STAGE,
                "name":f"{name} — Skool free community","status":"open","contactId":c["id"],"monetaryValue":0})

    # ---- Tell the team ------------------------------------------------------
    # Driven off the Close stamp rather than "did we just create it", so a member
    # the Zap happened to reach first is still announced exactly once.
    if not (notify and notify_slack.enabled):
        if notify:
            print("\nslack: SLACK_BOT_TOKEN not set - notifications skipped")
        return

    pending = [m for m in valid
               if close_lead.get(m["email"]) and not notify_slack.already_notified(close_lead[m["email"]])]
    if not pending:
        print("\nslack: nothing new to announce")
        return

    if len(pending) > notify_slack.NOTIFY_MAX:
        ok = notify_slack.post_digest(pending, reason=f"{len(pending)} at once, likely a backfill")
        print(f"\nslack: digest of {len(pending)} members -> {ok}")
        if ok:
            for m in pending:
                notify_slack.stamp_close(close, close_lead[m['email']]['id'])
    else:
        for m in pending:
            if notify_slack.post_member(m, close_id=close_lead[m["email"]]["id"],
                                        ghl_contact_id=ghl_contact.get(m["email"])):
                notify_slack.stamp_close(close, close_lead[m["email"]]["id"])
                print(f"  slack announced {m['email']}")

if __name__ == "__main__":
    # Any unhandled failure is announced before it propagates. The job still exits
    # non-zero so CI goes red too - Slack is the alarm, the exit code is the record.
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            notify_slack.post_alert("Skool sync aborted", e.code, mention=True)
        raise
    except Exception:
        import traceback
        notify_slack.post_alert("Skool sync crashed", traceback.format_exc()[-2000:], mention=True)
        raise
