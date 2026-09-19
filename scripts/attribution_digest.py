#!/usr/bin/env python3
"""
Weekly Slack digest: where community members actually came from.

Skool records how every member found the group (`attrSrcComp`) and nothing in this stack
was reading it, so the single best signal about which channel is working has been sitting
unused. This aggregates the last N days and posts it, so it becomes an ad-spend decision
rather than a field in a CRM nobody opens.

Usage:
    attribution_digest.py                 # print, do not post
    attribution_digest.py --post
    attribution_digest.py --days 7 --post
"""
import os, sys, collections
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import skool_to_ghl
from skool_to_ghl import pull_skool, EMAIL_RE
import notify_slack

# Members come back newest-first, so a week of joins lives on the first few pages.
# 15 pages = 450 members, comfortable headroom at the current ~35 joins/day.
PAGES = int(os.environ.get("DIGEST_PAGES", "15"))

PAID = {"instagram.com", "facebook.com", "youtube.com", "tiktok.com"}
OWNED = {"dealenginehq.com", "dealenginegroup.com", "typeform.com", "manychat.com",
         "linktr.ee", "internal_link"}
SKOOL_ORGANIC = {"discovery_search_group_link", "discovery_group_link",
                 "discovery_browse_group_link", "user_profile_page"}


def bucket(src):
    if not src: return "unknown"
    if src in PAID: return "paid / social"
    if src in OWNED: return "owned funnel"
    if src in SKOOL_ORGANIC: return "Skool discovery"
    if src == "affiliate": return "affiliate"
    if src == "direct": return "direct"
    return "other"


def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
    post = "--post" in sys.argv
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    members = pull_skool(PAGES)
    recent = []
    for m in members:
        j = m.get("joined")
        if not j: continue
        try:
            dt = datetime.fromisoformat(j.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt >= cutoff:
            recent.append(m)

    srcs = collections.Counter(m.get("source") or "unknown" for m in recent)
    buckets = collections.Counter(bucket(m.get("source")) for m in recent)
    with_email = sum(1 for m in recent if m.get("email") and EMAIL_RE.match(m["email"]))
    engaged = sum(1 for m in recent if (m.get("points") or 0) > 0)
    n = len(recent)

    total = skool_to_ghl.LAST_TOTAL or len(members)
    print(f"community total: {total}   scanned: {len(members)}   joined in last {days}d: {n}")
    print(f"contactable (valid email): {with_email}/{n}"
          f"   already engaging: {engaged}/{n}\n")
    for k, v in buckets.most_common():
        print(f"  {k:18} {v:4}  {v*100//max(n,1):3}%")
    print()
    for k, v in srcs.most_common(15):
        print(f"    {k:34} {v}")

    if not post:
        print("\nnot posted — rerun with --post")
        return
    if not notify_slack.enabled:
        print("SLACK_BOT_TOKEN not set — cannot post")
        return

    def bar(v):
        return "█" * max(1, round(v * 20 / max(n, 1)))

    lines = [f"`{bar(v):<20}` *{v}*  ({v*100//max(n,1)}%)  {k}" for k, v in buckets.most_common()]
    detail = "\n".join(f"• {k} — {v}" for k, v in srcs.most_common(8))
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"📊  {n} new community members in {days} days", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": f"Community now *{total}* · *{with_email}* of the {n} left a usable email"
                 f" · *{engaged}* are already posting or commenting"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines) or "_no joins_"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Top sources*\n{detail}"}},
    ]
    r = notify_slack._api("chat.postMessage", notify_slack._identity({
        "channel": notify_slack.SLACK_CHANNEL,
        "text": f"{n} new community members in {days} days", "blocks": blocks}))
    print("posted" if r.get("ok") else f"post failed: {r.get('error')}")


if __name__ == "__main__":
    main()
