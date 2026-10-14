#!/usr/bin/env python3
"""
Skool paid tiers -> Command Centre + Slack #4-skool-payments + GHL/Close notes.

Why this exists
---------------
Since early Oct 2026 the closers sell Deal Engine packages as custom-priced VIP tiers in the
Skool community (Derek Valance $8,750/yr, Hunter Flett $9,000/yr, ...). Skool takes that money
through ITS OWN Stripe account, so none of it reached the A&A Realty Stripe the dashboard
reads, and nobody was told when it landed. Measured 14 Oct 2026: 6 paying members, $36,749
lifetime, all invisible.

What Skool exposes (members page __NEXT_DATA__, admin session):
  ?annual=true&monthly=true&oneTime=true   every member on a paid tier, Skool's own filter
  ?t=cancelling / ?t=churned               the status tabs
  member.metadata.mbme     billing email (present even when the join question was skipped)
  member.metadata.mmbp     JSON {currency, amount (cents), recurring_interval, tier}
  member.metadata.mbsltv   lifetime value in cents   <- the payment signal
  member.metadata.msbs     Stripe subscription id (on Skool's Stripe, not ours)
  member.metadata.mbscpe   current period end, epoch SECONDS
There is no payment event feed, so a payment is detected as a RISE in lifetime value.

The dashboard owns the state
----------------------------
Every pass posts the whole paid list to POST /api?source=skool_payments. That handler diffs
it against the SKOOL PAYMENTS ledger tab and returns only what changed, already enriched
(first touch, closer, closer-form check). A fresh CI checkout, an overlapping run or a
re-sent snapshot therefore cannot announce a payment twice. Slack and the CRM notes happen
here, AFTER the ledger write, so a Slack outage loses a card (and alarms) rather than
recording a payment twice.

Usage:
  skool_payments.py                 one pass (what sync.yml runs)
  skool_payments.py --dry-run       read Skool, ask the dashboard what WOULD change; no writes
  skool_payments.py --dump          print the paid list as JSON and exit
  skool_payments.py --backfill --catch-up
                                    seed the ledger silently, then post ONE summary card
  skool_payments.py --preview U…    post the cards for the current state to a Slack user DM
  skool_payments.py --backfill --save FILE   seed silently and keep the events for later
  skool_payments.py --catch-up-from FILE     post the one catch-up summary from saved events

Env: SKOOL_COOKIE (or the on-disk copy), EVENT_INGEST_SECRET, SLACK_BOT_TOKEN,
     SKOOL_PAYMENTS_CHANNEL, GHL_PIT, CLOSE_API_KEY, DASHBOARD_API (optional)
"""
import json, os, re, sys, time, base64, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from skool_to_ghl import (_fetch_members_page, skool_cookie, SkoolUnavailable, BROWSER_UA,
                          COMMUNITY, LOC)
import notify_slack as N

DASHBOARD_API = os.environ.get("DASHBOARD_API", "https://deal-engine-eod.vercel.app/api")
INGEST_SECRET = os.environ.get("EVENT_INGEST_SECRET", "")
# #4-skool-payments. By ID, never by name (see #5-community-joins, renamed the day it was made).
CHANNEL = os.environ.get("SKOOL_PAYMENTS_CHANNEL", "")
PAID_FILTER = "&annual=true&monthly=true&oneTime=true"
MAX_PAGES = 50

# Gold: Deal Engine's brand accent, and the only colour not already a funnel.
FUNNEL_SKOOL_PAY = {"label": "Skool payment", "color": "#D4AF37", "chip": "🟨"}
FUNNEL_SKOOL_STATUS = {"label": "Skool billing", "color": "#8A8F98", "chip": "⬜"}

# Closer name/email -> Slack user, for "file your outcome" mentions. From the team roles
# memory (21 Sept 2026). An unknown closer falls back to Charlie, who runs the closers.
SLACK_IDS = {"charlie": "U0C0TPJG4TT", "mike": "U0C2CF2LT3J", "michael": "U0C2CF2LT3J", "mick": "U0C2CF2LT3J"}
FALLBACK_CLOSER = "U0C0TPJG4TT"

GHL_LOCATION = LOC
CHARGE_KINDS = {"new_paid", "renewal", "additional_charge"}


# ── Skool ────────────────────────────────────────────────────────────────────

def _page(cookie, n, query):
    html = _fetch_members_page(cookie, n, BROWSER_UA, query)
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise SystemExit(f"No __NEXT_DATA__ on members page {n}{query}: the page changed or the "
                         f"session was rejected without a redirect.")
    return json.loads(m.group(1)).get("props", {}).get("pageProps", {})


def _walk(cookie, query):
    """Every user on a filtered members list. Returns (users, complete)."""
    users, n = [], 1
    while n <= MAX_PAGES:
        pp = _page(cookie, n, query)
        users += pp.get("users") or []
        total_pages = pp.get("totalPages") or 1
        if n >= total_pages:
            return users, True
        n += 1
    return users, False


def _iso(v):
    if v in (None, ""):
        return ""
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            return datetime.fromtimestamp(int(v), tz=timezone.utc).isoformat()
        return str(v)
    except Exception:
        return ""


def _cents(v):
    try:
        return round(int(v) / 100, 2)
    except Exception:
        return None


def payer(u, status="active"):
    """One Skool user -> the snapshot record the dashboard expects (money in dollars)."""
    mem = u.get("member") or {}
    md = mem.get("metadata") or {}
    try:
        mmbp = json.loads(md.get("mmbp") or "{}")
    except Exception:
        mmbp = {}
    name = " ".join(x for x in (u.get("firstName"), u.get("lastName")) if x) or u.get("name") or ""
    return {
        "member_id": mem.get("id") or "", "handle": u.get("name") or "", "name": name,
        "email": (md.get("mbme") or "").strip().lower(),
        "tier": mmbp.get("tier") or "", "price": _cents(mmbp.get("amount")),
        "currency": mmbp.get("currency") or "usd", "interval": mmbp.get("recurring_interval") or "",
        "ltv": _cents(md.get("mbsltv")), "sub_id": md.get("msbs") or "",
        "period_end": _iso(md.get("mbscpe")), "joined_at": mem.get("approvedAt") or mem.get("createdAt") or "",
        "status": status, "attribution": md.get("attrSrcComp") or "", "location": md.get("requestLocation") or "",
    }


def read_paid(cookie):
    """(payers, complete). Cancelling members are read from their own tab and win on status."""
    users, complete = _walk(cookie, PAID_FILTER)
    out = {}
    for u in users:
        p = payer(u)
        if p["member_id"] and p["ltv"] is not None:
            out[p["member_id"]] = p
    canc, canc_complete = _walk(cookie, "&t=cancelling")
    for u in canc:
        p = payer(u, status="cancelling")
        if not p["member_id"]:
            continue
        if p["member_id"] in out:
            out[p["member_id"]]["status"] = "cancelling"
        elif p["ltv"] is not None:
            out[p["member_id"]] = p
    return list(out.values()), complete and canc_complete


def churned_ids(cookie, wanted):
    """Which of `wanted` sit in Skool's Churned tab. Only walked when someone is missing."""
    users, _ = _walk(cookie, "&t=churned")
    have = {(u.get("member") or {}).get("id") for u in users}
    return {m for m in wanted if m in have}


# ── Dashboard ────────────────────────────────────────────────────────────────

def post_snapshot(body):
    if not INGEST_SECRET:
        raise SystemExit("EVENT_INGEST_SECRET is not set - the dashboard would refuse the snapshot.")
    req = urllib.request.Request(DASHBOARD_API + "?source=skool_payments", data=json.dumps(body).encode(),
                                 method="POST", headers={"Authorization": "Bearer " + INGEST_SECRET,
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "deal-engine-skool-sync"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode()[:300]}"
            if e.code < 500:
                break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"dashboard refused the Skool payments snapshot: {last}")


# ── Slack ────────────────────────────────────────────────────────────────────

def money(v):
    if v is None or v == "":
        return ""
    v = float(v)
    return f"${v:,.2f}" if v % 1 else f"${v:,.0f}"


def _date(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%-d %b %Y")
    except Exception:
        return ""


def closer_slack_id(closer):
    key = (closer or "").strip().lower().split(" ")[0].split("@")[0]
    return SLACK_IDS.get(key, FALLBACK_CLOSER)


def card(e):
    """Blocks + attachments for one ledger event."""
    kind = e["kind"]
    plan = f"{e.get('tier_label') or 'Skool'} {money(e.get('price'))}/{e.get('interval') or '?'}"
    is_money = kind in CHARGE_KINDS
    rc = e.get("reconcile") or {}
    if is_money:
        title = f"{money(e['amount'])} · {e['label']}"
        funnel = FUNNEL_SKOOL_PAY
    else:
        title = e["label"]
        funnel = FUNNEL_SKOOL_STATUS
    renew_label = "Ends" if e.get("status") in ("cancelling", "churned", "left_paid") else "Renews"
    stats = [
        ("Amount", money(e["amount"]) if is_money else None),
        ("Paid to date (Skool)", money(e.get("ltv"))),
        ("Plan", plan),
        ("Was", e.get("previous") or None),
        (renew_label, _date(e.get("period_end") or "")),
        ("First touch", e.get("first_touch")),
        ("Closer", e.get("closer") or "Unknown"),
        ("Joined Skool", _date(e.get("joined_at") or "")),
    ]
    st = rc.get("status")
    notes = []
    if st == "match":
        notes.append(f"✅ {rc.get('text')}")
    elif st in ("mismatch", "unknown"):
        notes.append(f"⚠️ {rc.get('text')}")
    elif st == "no_outcome":
        notes.append(f"⚠️ {rc.get('text')}")
    if e.get("via_skool"):
        notes.append("Came into the process through the Skool community.")
    if not e.get("ghl_contact_id"):
        notes.append("Not in GHL yet, so no CRM note was written.")
    links = [
        ("Skool profile", f"https://www.skool.com/@{e['handle']}?g={COMMUNITY}" if e.get("handle") else ""),
        ("GHL contact", f"https://app.gohighlevel.com/v2/location/{GHL_LOCATION}/contacts/detail/{e['ghl_contact_id']}" if e.get("ghl_contact_id") else ""),
        ("Close lead", f"https://app.close.com/lead/{e['close_lead_id']}/" if e.get("close_lead_id") else ""),
    ]
    blocks, atts = N.scorecard(funnel, title=title, name=N.esc(e.get("name") or e.get("email")),
                               contact=N.esc(e.get("email") or ""), stats=stats, notes=[N.esc(x) for x in notes],
                               links=links, footer=f"Paid via Skool · {N.esc(e.get('sub_id') or 'no subscription id')}")
    # A mention inside an attachment does not reliably notify, so the ask sits at the top level.
    if is_money and kind == "new_paid" and st in ("no_outcome", "mismatch", "unknown"):
        who = closer_slack_id(e.get("closer"))
        ask = ("please file the closer outcome for this sale" if st == "no_outcome"
               else "the closer form does not match what Skool collected, please check it")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"<@{who}> {ask}."}})
    fallback = f"Skool payment: {e.get('name')} {title}"
    return {"text": fallback, "blocks": blocks, "attachments": atts}


def post_card(e, channel):
    msg = N._identity({"channel": channel, **card(e)})
    r = N._api("chat.postMessage", msg)
    if not r.get("ok"):
        raise RuntimeError(f"Slack refused the card for {e.get('email')}: {r.get('error')}")
    return r.get("ts")


def post_catch_up(events, channel):
    """One summary for the payers who were already paying before this went live."""
    money_rows = [e for e in events if e["kind"] in CHARGE_KINDS]
    total = sum(e["amount"] for e in money_rows)
    lines = []
    for e in sorted(money_rows, key=lambda x: -x["amount"]):
        rc = (e.get("reconcile") or {})
        flag = "✅" if rc.get("status") == "match" else "⚠️"
        lines.append(f"{flag} *{N.esc(e['name'])}*: {money(e['ltv'])} paid, {e.get('tier_label')} "
                     f"{money(e['price'])}/{e['interval']}. First touch: {N.esc(e.get('first_touch') or '?')}. "
                     f"{N.esc(rc.get('text') or '')}")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🟨 Skool payments: catch-up", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"From now on every Skool payment posts here as it happens. These {len(money_rows)} members were "
            f"already paying through Skool before tracking started: *{money(total)}* collected in total, "
            f"now on the Command Centre."}},
    ]
    for i in range(0, len(lines), 8):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines[i:i + 8])[:2900]}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
        "⚠️ = the closer outcome form disagrees with Skool or was never filed. Skool's figure is the money truth."}]})
    r = N._api("chat.postMessage", N._identity({"channel": channel, "text": "Skool payments catch-up", "blocks": blocks}))
    if not r.get("ok"):
        raise RuntimeError(f"Slack refused the catch-up post: {r.get('error')}")
    return r.get("ts")


# ── CRMs (tag + note; never a stage move) ────────────────────────────────────

def _ghl(method, path, payload=None):
    tok = os.environ.get("GHL_PIT", "")
    req = urllib.request.Request("https://services.leadconnectorhq.com" + path, method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Version": "2021-07-28",
                                          "Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def _close(method, path, payload=None):
    key = os.environ.get("CLOSE_API_KEY", "")
    auth = "Basic " + base64.b64encode((key + ":").encode()).decode()
    req = urllib.request.Request("https://api.close.com/api/v1" + path, method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def note_text(e):
    rc = e.get("reconcile") or {}
    lines = [f"Skool billing: {e['label']}" + (f", {money(e['amount'])}" if e["kind"] in CHARGE_KINDS else ""),
             f"Plan: {e.get('tier_label')} {money(e.get('price'))}/{e.get('interval')}",
             f"Paid to date on Skool: {money(e.get('ltv'))}",
             f"Status: {e.get('status')}" + (f", renews/ends {_date(e.get('period_end') or '')}" if e.get('period_end') else ""),
             f"First touch: {e.get('first_touch')}",
             f"Closer form check: {rc.get('text') or 'n/a'}",
             f"Skool subscription: {e.get('sub_id') or 'n/a'}"]
    return "\n".join(lines)


def write_crms(e):
    """GHL: additive tags (never /contacts/upsert, which REPLACES the tag list) + a note.
    Close: a note on the lead. Returns a short report; failures are reported, never raised."""
    rep = []
    if os.environ.get("GHL_PIT") and e.get("ghl_contact_id"):
        cid = e["ghl_contact_id"]
        if e["kind"] in CHARGE_KINDS:
            tags = ["skool-paid", f"skool-tier-{e.get('tier') or 'unknown'}"]
            s, _ = _ghl("POST", f"/contacts/{cid}/tags", {"tags": tags})
            rep.append(f"ghl tags {s}")
        elif e["kind"] in ("churned", "left_paid"):
            s, _ = _ghl("POST", f"/contacts/{cid}/tags", {"tags": ["skool-paid-ended"]})
            rep.append(f"ghl tags {s}")
        s, _ = _ghl("POST", f"/contacts/{cid}/notes", {"body": note_text(e)})
        rep.append(f"ghl note {s}")
    lead = e.get("close_lead_id")
    if os.environ.get("CLOSE_API_KEY") and not lead and e.get("email"):
        q = urllib.parse.quote(f'email:"{e["email"]}"')
        s, d = _close("GET", f"/lead/?query={q}&_limit=1&_fields=id")
        lead = ((d.get("data") or [{}])[0] or {}).get("id") if s == 200 else None
        e["close_lead_id"] = lead or ""
    if os.environ.get("CLOSE_API_KEY") and lead:
        s, _ = _close("POST", "/activity/note/", {"lead_id": lead, "note": note_text(e)})
        rep.append(f"close note {s}")
    return ", ".join(rep) or "no CRM record"


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    backfill = "--backfill" in args
    preview = args[args.index("--preview") + 1] if "--preview" in args else None
    if "--catch-up-from" in args:
        saved = json.load(open(args[args.index("--catch-up-from") + 1]))
        if not CHANNEL or not N.enabled:
            raise SystemExit("SKOOL_PAYMENTS_CHANNEL and SLACK_BOT_TOKEN are both needed to post the catch-up.")
        print("catch-up posted:", post_catch_up(saved, CHANNEL))
        return 0
    try:
        cookie = skool_cookie()
        payers, complete = read_paid(cookie)
    except SkoolUnavailable as ex:
        # Same contract as reconcile_skool.py: the WAF, not us. The next pass retries.
        print(f"Skool unavailable, skipping the payments check this pass: {ex}")
        return 75
    print(f"skool paid tiers: {len(payers)} payer(s), list complete={complete}")
    if "--dump" in args:
        print(json.dumps(payers, indent=1))
        return 0

    resp = post_snapshot({"payers": payers, "complete": complete, "backfill": backfill, "dry_run": dry or bool(preview)})
    events = resp.get("events") or []
    missing = resp.get("missing") or []
    if missing and not dry and not preview:
        # Read the paid list a second time before believing an absence: one short page is all
        # it takes to look like a cancellation (absence-logic-needs-complete-coverage).
        again, complete2 = read_paid(cookie)
        still = [m for m in missing if m not in {p["member_id"] for p in again}]
        if complete2 and still:
            churned = churned_ids(cookie, still)
            deps = [{"member_id": m, "status": "churned" if m in churned else "left_paid"} for m in still]
            r2 = post_snapshot({"departures": deps})
            events += r2.get("events") or []
    print(f"dashboard: {len(events)} change(s){' (dry run)' if dry or preview else ''}; missing={missing}")
    for e in events:
        print(f"  {e['kind']:<18} {money(e['amount']):>10}  {e.get('name')} <{e.get('email')}>  "
              f"first touch: {e.get('first_touch')}  |  {(e.get('reconcile') or {}).get('text')}")

    if "--save" in args and events:
        with open(args[args.index("--save") + 1], "w") as fh:
            json.dump(events, fh, indent=1)
    if not events or dry:
        return 0
    channel = preview or CHANNEL
    if backfill and "--catch-up" not in args:
        for e in events:
            print(f"  crm {e.get('name')}: {write_crms(e)}")
        return 0
    if not channel or not N.enabled:
        N.post_alert("Skool payments recorded but NOT announced",
                     f"{len(events)} change(s) written to the SKOOL PAYMENTS tab, but SKOOL_PAYMENTS_CHANNEL "
                     f"or SLACK_BOT_TOKEN is not set.", hint="Set the secret in deal-engine-skool-sync.")
        return 1
    if preview:
        for e in events:
            post_card(e, channel)
        print(f"previewed {len(events)} card(s) to {channel}")
        return 0
    if backfill:
        if "--catch-up" in args:
            print("catch-up posted:", post_catch_up(events, channel))
        for e in events:
            print(f"  crm {e.get('name')}: {write_crms(e)}")
        return 0
    failed = 0
    for e in events:
        try:
            ts = post_card(e, channel)
            print(f"  posted {e['kind']} for {e.get('name')} ts={ts}")
        except Exception as ex:
            failed += 1
            N.post_alert("Skool payment recorded but the Slack card failed",
                         f"{e.get('name')} <{e.get('email')}> {e['kind']} {money(e['amount'])}: {ex}",
                         hint="The payment IS on the dashboard (SKOOL PAYMENTS tab). Only the announcement was lost.")
        try:
            print(f"  crm {e.get('name')}: {write_crms(e)}")
        except Exception as ex:
            print(f"  crm write failed for {e.get('email')}: {ex}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
