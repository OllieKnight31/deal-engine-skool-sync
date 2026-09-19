#!/usr/bin/env python3
"""
Slack notification for new Skool community members.

Posts one message per genuinely new member into the team channel, with their
contact details, how they found Deal Engine, what they said they want, and
one-click links into Skool and both CRMs.

Why the state lives in Close rather than a file in this repo: GitHub's scheduler
drops and delays runs, and a repo state file would need the workflow to commit
back to itself. Stamping the Close lead's "Slack Notified At" field instead means
a member is notified exactly once no matter how often this runs, how many runs
overlap, or which system created the lead first. It is also visible to a human in
the Close UI, so "did the team get pinged about this person?" is answerable
without reading logs.

Env:
  SLACK_BOT_TOKEN   xoxb- token with chat:write (required; absent = no-op)
  SLACK_CHANNEL     channel id, default the Deal Engine community channel
  SLACK_NOTIFY_MAX  above this many new members in one run, post a single
                    digest instead of flooding the channel (default 8)
"""
import json, os, urllib.request, urllib.error
from datetime import datetime, timezone

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C0C2WAXFH50")   # #5-community-joins
# Always address the channel by ID, never by name: this channel has already been renamed once
# (#1-de-community-joins -> #5-community-joins on 18 Sept) and a name-based config would have
# broken silently. The ID is stable across renames.

# The posting app is the shared "Workspace Builder" bot, which is the wrong name to see beside
# a lead alert. chat:write.customize lets each message carry its own identity without needing a
# second Slack app.
POST_AS = os.environ.get("SLACK_POST_AS", "Deal Engine")
POST_ICON = os.environ.get("SLACK_POST_ICON", ":house:")
# The community takes ~43 joins/day, so a 4-hour scheduler gap accumulates ~7 members.
# A threshold of 8 would therefore digest ordinary traffic, when the ask was a message
# per member. 25 only trips on a genuine backfill.
NOTIFY_MAX = int(os.environ.get("SLACK_NOTIFY_MAX", "25"))

CF_SLACK_NOTIFIED = "cf_qpVKHQiE9jsZAkUIOvGlN1quL7m0uFZAPFOBE19jhCy"
GHL_LOCATION = "2RrjNuz0M4MTFlxpW37k"

enabled = bool(SLACK_TOKEN)


def _identity(payload):
    """Stamp every outgoing message with the Deal Engine identity."""
    payload.setdefault("username", POST_AS)
    payload.setdefault("icon_emoji", POST_ICON)
    return payload


def _api(method, payload):
    req = urllib.request.Request(
        "https://slack.com/api/" + method,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"http {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Skool shows a "Spam risk score" only while a join request is still PENDING; once a
# member is approved it is gone from the page data, and this community's pending queue
# is normally empty. So this is our own conservative check, not Skool's score - it only
# fires on things that are near-certainly wrong, because a flag that cries wolf gets
# ignored and then the real ones get ignored too.
TYPO_DOMAINS = {
    "gnail.com", "gmial.com", "gmai.com", "gmaill.com", "gmail.cm", "gmail.con",
    "hotmial.com", "hotmai.com", "hotmail.con", "yahooo.com", "yaho.com", "yahoo.con",
    "outlok.com", "outloo.com", "iclould.com", "iclod.com", "icloud.con", "aol.con",
}
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "yopmail.com", "trashmail.com", "sharklasers.com",
    "throwawaymail.com", "getnada.com", "maildrop.cc", "dispostable.com",
}


def suspect_reason(m):
    """Return a short reason string when a member looks like junk, else None."""
    email = (m.get("email") or "").strip().lower()
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1]
    if domain in TYPO_DOMAINS:
        return f"mistyped email domain ({domain})"
    if domain in DISPOSABLE_DOMAINS:
        return f"disposable email domain ({domain})"
    name = f"{m.get('first') or ''} {m.get('last') or ''}".lower()
    if "http://" in name or "https://" in name or ".com" in name:
        return "name contains a URL"
    return None


def _fmt_source(src):
    """Turn Skool's raw attribution token into something a human reads."""
    if not src:
        return "unknown"
    pretty = {
        "discovery_search_group_link": "Skool discovery (search)",
        "discovery_group_link": "Skool discovery",
        "discovery_browse_group_link": "Skool discovery (browse)",
        "user_profile_page": "a Skool profile",
        "direct": "direct",
        "affiliate": "affiliate",
        "invite": "invite link",
        "internal_link": "internal link",
    }
    return pretty.get(src, src)


def _member_blocks(m, close_id=None, ghl_contact_id=None):
    name = f"{m.get('first') or ''} {m.get('last') or ''}".strip() or "(no name)"
    suspect = suspect_reason(m)
    handle = m.get("handle") or ""
    email = m.get("email") or ""
    phone = m.get("phone") or m.get("phone_raw") or ""
    why = (m.get("why") or "").strip()
    loc = m.get("location") or ""

    facts = []
    if email:
        facts.append(f"*Email*  <mailto:{email}|{email}>")
    if phone:
        facts.append(f"*Phone*  <tel:{phone}|{phone}>")
    facts.append(f"*Found us via*  {_fmt_source(m.get('source'))}")
    if loc:
        facts.append(f"*Location*  {loc}")
    pts = m.get("points") or 0
    if pts:
        facts.append(f"*Engagement*  🔥 {pts} point{'s' if pts != 1 else ''} in the community "
                     f"(level {m.get('level') or 1}) — warmer than a drive-by joiner")

    icon = "⚠️" if suspect else "🟢"
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"{icon}  {name} joined the community", "emoji": True}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "\n".join(facts)}},
    ]
    if suspect:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"⚠️  *Looks like junk* — {suspect}. Synced anyway so nothing is lost; "
                    f"tagged `skool-suspect` in GHL."}]})

    if why:
        quoted = "\n".join("> " + line for line in why.splitlines() if line.strip())
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"*What they want*\n{quoted}"}})

    buttons = []
    if handle:
        buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Skool profile"},
                        "url": f"https://www.skool.com/@{handle}"})
    if ghl_contact_id:
        buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Open in GHL"},
                        "url": f"https://app.gohighlevel.com/v2/location/{GHL_LOCATION}"
                               f"/contacts/detail/{ghl_contact_id}"})
    if close_id:
        buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Open in Close"},
                        "url": f"https://app.close.com/lead/{close_id}/"})
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})

    return blocks, f"{name} joined the BRRRR community"


def post_member(m, close_id=None, ghl_contact_id=None):
    """Post one new member. Returns True when Slack accepted it."""
    if not enabled:
        return False
    blocks, fallback = _member_blocks(m, close_id, ghl_contact_id)
    r = _api("chat.postMessage", _identity({"channel": SLACK_CHANNEL, "text": fallback,
                                            "blocks": blocks, "unfurl_links": False}))
    if not r.get("ok"):
        print(f"  slack post failed for {m.get('email')}: {r.get('error')}")
    return bool(r.get("ok"))


def post_digest(members, reason="backfill"):
    """One summary message instead of N individual ones."""
    if not enabled or not members:
        return False
    lines = []
    for m in members[:50]:
        name = f"{m.get('first') or ''} {m.get('last') or ''}".strip() or "(no name)"
        lines.append(f"• *{name}* — {m.get('email','')} — via {_fmt_source(m.get('source'))}")
    more = f"\n…and {len(members)-50} more" if len(members) > 50 else ""
    r = _api("chat.postMessage", _identity({
        "channel": SLACK_CHANNEL,
        "text": f"{len(members)} new community members ({reason})",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text",
             "text": f"🟢  {len(members)} new community members", "emoji": True}},
            {"type": "context", "elements": [{"type": "mrkdwn",
             "text": f"Posted as a digest rather than {len(members)} separate messages ({reason})."}]},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines) + more}},
        ],
        "unfurl_links": False}))
    if not r.get("ok"):
        print(f"  slack digest failed: {r.get('error')}")
    return bool(r.get("ok"))


def stamp_close(close_fn, lead_id):
    """Mark the Close lead as notified so no later run posts it again."""
    if not lead_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    close_fn("PUT", f"/lead/{lead_id}/", {f"custom.{CF_SLACK_NOTIFIED}": now})


def already_notified(lead):
    return bool((lead or {}).get(f"custom.{CF_SLACK_NOTIFIED}"))


if __name__ == "__main__":
    # Smoke test: post a clearly-marked sample so the format can be eyeballed.
    import sys
    sample = {"first": "Sample", "last": "Member (test)", "email": "sample@example.com",
              "phone": "+15551234567", "handle": "sample-member",
              "source": "instagram.com", "location": "austin (united states)",
              "why": "I want to buy my first BRRRR property but I don't know where to start."}
    if "--selftest" in sys.argv:
        cases = [
            ({"first":"Carter","last":"Glowacki","email":"carter.glowacki28@gnail.com"}, "typo"),
            ({"first":"Real","last":"Person","email":"teddybear57718@gmail.com"}, None),
            ({"first":"Sean","last":"Mackenzie","email":"seanmack575@gmail.com"}, None),
            ({"first":"Temp","last":"User","email":"x@mailinator.com"}, "disposable"),
            ({"first":"buy","last":"cheap.com","email":"a@gmail.com"}, "URL"),
        ]
        for c, expect in cases:
            got = suspect_reason(c)
            flag = "FLAG" if got else "ok  "
            print(f"  {flag} {c['email']:36} -> {got}")
        raise SystemExit
    ok = post_member(sample, close_id=None, ghl_contact_id=None) if "--post" in sys.argv else None
    print(json.dumps(_member_blocks(sample)[0], indent=2) if ok is None else f"posted: {ok}")


def post_alert(title, detail="", mention=False):
    """Loud failure alarm. Nobody watches the Actions tab, so a broken sync has to
    come and find someone - otherwise it looks identical to a quiet week."""
    if not enabled:
        print(f"ALERT (slack disabled): {title} — {detail}")
        return False
    head = ("<!channel> " if mention else "") + f"*{title}*"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🚨  Skool sync problem", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
    ]
    if detail:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"```{str(detail)[:2500]}```"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "New community members are *not* reaching the CRMs or this channel until this is fixed. "
                "Most likely cause: the Skool session cookie was invalidated by a password change — "
                "rotate the `SKOOL_COOKIE` repo secret."}]})
    r = _api("chat.postMessage", _identity({"channel": SLACK_CHANNEL,
                                            "text": f"Skool sync problem: {title}",
                                            "blocks": blocks}))
    return bool(r.get("ok"))
