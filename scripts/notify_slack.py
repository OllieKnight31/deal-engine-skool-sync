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
  SLACK_ALERT_CHANNEL  where post_alert / post_note go, default #5-ops-alerts
  SLACK_NOTIFY_MAX  above this many new members in one run, post a single
                    digest instead of flooding the channel (default 8)
"""
import json, os, urllib.request, urllib.error
from datetime import datetime, timezone

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C0C2WAXFH50")   # #5-community-joins
# Alarms go to the ops channel, not the lead feed. Until 22 Sept 2026 every "sync crashed"
# post landed in #5-community-joins between the member cards, where the people who can fix
# an automation are not the people reading joins. #5-ops-alerts exists for exactly this
# ("Failed automations, broken webhooks, integration errors") and had never received a post.
SLACK_ALERT_CHANNEL = os.environ.get("SLACK_ALERT_CHANNEL", "C0C2TKXLV6W")   # #5-ops-alerts
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


# ── Scorecard ────────────────────────────────────────────────────────────────
# Every lead event across Deal Engine posts as the same card, and the colour bar down its
# left edge says which FUNNEL it came from. The relay owns the layout
# (de-funnel-relay/api/_card.js); this is that layout in Python, because this repo cannot
# import it. Change a colour or the block order there and change it here.
#
#   🟩 #2EB67D  Application        (relay  → #5-leads)
#   🟪 #9B51E0  VSL opt-in         (relay  → #5-leads)
#   🟦 #1D9BD1  Community join     (this   → #5-community-joins)
#   🟧 #F2994A  Call booked        (relay  → #1-booked-calls)
FUNNEL_COMMUNITY = {"label": "Community join", "color": "#1D9BD1", "chip": "🟦"}


def esc(v):
    """Skool answers are free text from strangers; unescaped, '<!channel>' pings everyone."""
    return str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def scorecard(funnel, title="", name="", contact="", stats=(), quote=None, notes=(),
              links=(), footer="", heading=""):
    """Header as a top-level block, the card body in one coloured attachment.

    The split is deliberate: the colour bar only exists on attachments, and with top-level
    blocks present Slack uses `text` purely as the notification fallback rather than
    printing it above the card.
    """
    head = " · ".join(x for x in (heading or funnel["label"], title) if x)
    blocks = [{"type": "header",
               "text": {"type": "plain_text", "text": f"{funnel['chip']} {head}"[:150], "emoji": True}}]
    body = []
    if name:
        body.append({"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*{name}*" + (f"\n{contact}" if contact else "")}})
    cells = [{"type": "mrkdwn", "text": f"*{k}*\n{v}"[:2000]}
             for k, v in stats if v is not None and str(v).strip()]
    for i in range(0, len(cells), 10):          # Slack caps a section at 10 fields
        body.append({"type": "section", "fields": cells[i:i + 10]})
    if quote and str(quote[1] or "").strip():
        quoted = "\n".join(">" + line for line in str(quote[1]).strip().splitlines() if line.strip())
        body.append({"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*{quote[0]}*\n{quoted}"[:2900]}})
    notes = [n for n in notes if n]
    if notes:
        body.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(notes)}]})
    buttons = [{"type": "button", "text": {"type": "plain_text", "text": label}, "url": url}
               for label, url in links if url]
    if buttons:
        body.append({"type": "actions", "elements": buttons})
    if footer:
        body.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks, [{"color": funnel["color"], "blocks": body}]


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


# src-* tag -> label, mirroring CHANNEL in de-funnel-relay/api/_origin.js.
SRC_LABEL = {
    "meta-ads": "Meta Ads", "meta-unattrib": "Meta (unattributed)", "ig-bio": "Instagram Bio",
    "ig-organic": "Instagram", "ig-app": "Instagram (untagged link)", "fb-app": "Facebook (untagged link)",
    "fb-organic": "Facebook", "dm": "DM", "youtube-alex": "YouTube · Alex", "youtube-ana": "YouTube · Ana",
    "youtube": "YouTube", "skool": "Skool", "podcast": "Podcast", "referral": "Referral",
    "google": "Google", "tiktok": "TikTok", "email": "Email", "linkedin": "LinkedIn", "x": "X / Twitter",
    "other": "Other", "direct": "Direct / Unknown",
}


def crm_source(ghl_tags):
    """What the funnel already recorded for this person, from their src-* tag - or ''."""
    for t in ghl_tags or []:
        if t.startswith("src-") and t not in ("src-direct", "src-other"):
            return SRC_LABEL.get(t[4:], t[4:])
    return ""


def _member_card(m, close_id=None, ghl_contact_id=None, ghl_tags=None):
    """(blocks, attachments, fallback) for one new member."""
    name = f"{m.get('first') or ''} {m.get('last') or ''}".strip() or "(no name)"
    suspect = suspect_reason(m)
    handle = m.get("handle") or ""
    email = m.get("email") or ""
    # Skool's phone question is free text, so phone_raw is regularly prose
    # ("I live in our side the states"). Wrapping that in a tel: link produced an
    # unclickable, nonsense phone line. Only a genuinely parseable number becomes a
    # tel: link; anything else is shown as what it is - an answer, not a number.
    phone = m.get("phone") or ""
    phone_note = ""
    if not phone and m.get("phone_raw"):
        try:
            from skool_to_ghl import clean_phone, iso_from_location
            phone = clean_phone(m["phone_raw"], iso_from_location(m.get("location"))) or ""
        except Exception:
            phone = ""
        if not phone:
            phone_note = str(m["phone_raw"]).strip()
    pts = m.get("points") or 0

    stats = [
        ("Email", f"<mailto:{email}|{esc(email)}>" if email else ""),
        ("Phone", f"<tel:{phone}|{phone}>" if phone
                  else (f"_{esc(phone_note[:120])}_  (not a dialable number)" if phone_note else "")),
        ("Found us via", esc(_fmt_source(m.get("source")))
                         + (f"  ·  CRM says *{esc(crm_source(ghl_tags))}*"
                            if crm_source(ghl_tags) and (not m.get("source")
                                                         or m.get("source") in ("dealenginehq.com", "dealenginegroup.com", "typeform.com"))
                            else "")),
        ("Location", esc(m.get("location") or "")),
        ("Engagement", f"🔥 {pts} point{'s' if pts != 1 else ''} · level {m.get('level') or 1}" if pts else ""),
    ]

    notes = []
    if "application-complete" in (ghl_tags or []):
        notes.append("_Already applied through the funnel — their application answers are on the GHL contact._")
    elif "call-booked" in (ghl_tags or []):
        notes.append("_Already has a call booked — see the GHL contact._")
    if suspect:
        notes.append(f"⚠️  *Looks like junk* — {suspect}. Synced anyway so nothing is lost; "
                     f"tagged `skool-suspect` in GHL.")
    if pts:
        notes.append("_Already active in the community — warmer than a drive-by joiner._")

    blocks, attachments = scorecard(
        FUNNEL_COMMUNITY,
        title="⚠️ looks like junk" if suspect else "",
        name=esc(name),
        stats=stats,
        quote=("What they want", esc((m.get("why") or "").strip())),
        notes=notes,
        links=[
            ("Skool profile", f"https://www.skool.com/@{handle}" if handle else None),
            ("Open in GHL", f"https://app.gohighlevel.com/v2/location/{GHL_LOCATION}"
                            f"/contacts/detail/{ghl_contact_id}" if ghl_contact_id else None),
            ("Open in Close", f"https://app.close.com/lead/{close_id}/" if close_id else None),
        ],
        footer="4. Skool Free Community → New Member",
    )
    return blocks, attachments, f"{esc(name)} joined the BRRRR community"


def post_member(m, close_id=None, ghl_contact_id=None, ghl_tags=None):
    """Post one new member. Returns True when Slack accepted it."""
    if not enabled:
        return False
    blocks, attachments, fallback = _member_card(m, close_id, ghl_contact_id, ghl_tags)
    r = _api("chat.postMessage", _identity({"channel": SLACK_CHANNEL, "text": fallback,
                                            "blocks": blocks, "attachments": attachments,
                                            "unfurl_links": False}))
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
        lines.append(f"• *{esc(name)}* — {esc(m.get('email',''))} — via {esc(_fmt_source(m.get('source')))}")
    more = f"\n…and {len(members)-50} more" if len(members) > 50 else ""
    r = _api("chat.postMessage", _identity({
        "channel": SLACK_CHANNEL,
        "text": f"{len(members)} new community members ({reason})",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text",
             "text": f"{FUNNEL_COMMUNITY['chip']} {len(members)} new community members", "emoji": True}},
        ],
        "attachments": [{"color": FUNNEL_COMMUNITY["color"], "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": ("\n".join(lines) + more)[:2900]}},
            {"type": "context", "elements": [{"type": "mrkdwn",
             "text": f"Posted as a digest rather than {len(members)} separate messages ({reason})."}]},
        ]}],
        "unfurl_links": False}))
    if not r.get("ok"):
        print(f"  slack digest failed: {r.get('error')}")
    return bool(r.get("ok"))


def stamp_close(close_fn, lead_id):
    """Mark the Close lead as notified so no later run posts it again.

    Returns True only when Close confirmed the write. A silent failure here means the
    member gets announced again on the next pass, so the caller needs to know.
    """
    if not lead_id:
        return False
    now = datetime.now(timezone.utc).isoformat()
    status, _ = close_fn("PUT", f"/lead/{lead_id}/", {f"custom.{CF_SLACK_NOTIFIED}": now})
    return status == 200


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
    print(json.dumps(_member_card(sample)[:2], indent=2) if ok is None else f"posted: {ok}")


# The footer used to say "most likely cause: the cookie - rotate SKOOL_COOKIE" under EVERY
# alert. On 22 Sept 2026 it said that under a 403 that was Skool's WAF, and the cookie was
# fine. The footer now tells the reader how to tell the two apart, and the transient path
# passes its own hint instead.
DEFAULT_HINT = (
    "New community members are *not* reaching the CRMs or this channel until this is fixed. "
    "Read the detail before touching anything: `Skool redirected ... to /about` means the "
    "session cookie is dead - only then rotate the `SKOOL_COOKIE` repo secret "
    "(`skool_cookie_from_disk.py --push-secret` on the Mac). A `403` is Skool's CloudFront "
    "WAF refusing the runner; it clears on its own and the sync retries it every pass.")


def post_alert(title, detail="", mention=False, hint=None, header="🚨  Skool sync problem"):
    """Loud failure alarm. Nobody watches the Actions tab, so a broken sync has to
    come and find someone - otherwise it looks identical to a quiet week."""
    if not enabled:
        print(f"ALERT (slack disabled): {title} — {detail}")
        return False
    head = ("<!channel> " if mention else "") + f"*{title}*"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
    ]
    if detail:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"```{str(detail)[:2500]}```"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                                                    "text": hint or DEFAULT_HINT}]})
    r = _api("chat.postMessage", _identity({"channel": SLACK_ALERT_CHANNEL,
                                            "text": f"{header.strip()}: {title}",
                                            "blocks": blocks}))
    return bool(r.get("ok"))


def post_note(title, detail=""):
    """Quiet follow-up, no mention - e.g. "recovered" - so an alarm is never left standing
    in the channel after the thing it warned about has cleared."""
    return post_alert(title, detail, mention=False, hint="No action needed.",
                      header="✅  Skool sync recovered")
