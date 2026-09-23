#!/usr/bin/env python3
"""
Run sync_health.py and tell Slack (#5-ops-alerts) when it is not healthy.

Runs in GitHub Actions (.github/workflows/health.yml) after every Skool sync run and on a
3-hourly cron backstop, so it does not depend on the MacBook being awake. The workflow
passes --always once a day (atomic claim ref, like the daily sync jobs) so a healthy
day still produces one "all checks passing" post: silence must never be mistaken for
health, because a dead watcher is silent too.

Why this exists: the health check was only ever run by hand. Everything it guards -
a dead Skool cookie, a broken self-dispatch chain, a once-a-day job that silently
stopped claiming its slot - fails quietly, and a quiet failure in this stack looks
exactly like a quiet week in the channel. Nobody watches the Actions tab.

Posts only on FAILURE by default, so a healthy system is silent.

Usage:
  health_alert.py                 run the check; post only if unhealthy
  health_alert.py --always        post either way
  health_alert.py --quiet         do not echo the health output
  health_alert.py --selftest      prove BOTH payloads are accepted by Slack without
                                  delivering either (scheduleMessage + cancel). An
                                  alarm that has never been fired is not an alarm.
Exit: mirrors sync_health.py (0 healthy, 1 unhealthy)
"""
import json, os, re, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import notify_slack as N


def run_health():
    r = subprocess.run([sys.executable, os.path.join(HERE, "sync_health.py")],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def build(out, returncode):
    """-> (healthy, text, blocks). Pure, so --selftest can exercise both branches."""
    fails = [l.strip() for l in out.splitlines() if l.strip().startswith("FAIL ")]
    healthy = returncode == 0 and not fails
    icon = "✅" if healthy else "🚨"
    head = ("Automation health: all checks passing" if healthy
            else f"<!channel> Automation health: {len(fails) or '?'} check(s) FAILING")
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"{icon}  Deal Engine automation health", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{head}*"}},
    ]
    if fails:
        body = "\n".join(f[:160] for f in fails[:12])[:2500]
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "```" + body + "```"}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Leads may not be reaching Close, GoHighLevel or #5-community-joins. "
                    "Run `automations/scripts/sync_health.py` for the full list."}]})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"{len(re.findall(r'PASS  ', out))} checks passed."}]})
    return healthy, head.replace("<!channel> ", ""), blocks


def _api(method, payload):
    req = urllib.request.Request("https://slack.com/api/" + method,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + N.SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def selftest():
    if not N.enabled:
        sys.exit("no SLACK_BOT_TOKEN - cannot self-test")
    cases = [
        ("healthy", 0, "  PASS  Close API   900 leads\n  PASS  GHL API   4 pipelines\n"),
        ("failing", 1, "  PASS  Close API   900 leads\n"
                       "  FAIL  Skool session cookie   RuntimeError: cookie rejected (401)\n"
                       "  FAIL  chain cadence (self-dispatch)   last run 4.2 h ago\n"),
    ]
    bad = 0
    for label, rc, out in cases:
        healthy, text, blocks = build(out, rc)
        r = _api("chat.scheduleMessage", N._identity({
            "channel": N.SLACK_ALERT_CHANNEL, "post_at": int(time.time()) + 900,
            "text": text, "blocks": blocks}))
        if not r.get("ok"):
            print(f"  REJECTED [{label}] {r.get('error')} {r.get('response_metadata','')}")
            bad += 1
            continue
        d = _api("chat.deleteScheduledMessage",
                 {"channel": N.SLACK_ALERT_CHANNEL, "scheduled_message_id": r["scheduled_message_id"]})
        print(f"  ACCEPTED [{label:7}] healthy={healthy} blocks={len(blocks)} "
              f"text={text[:48]!r} cancelled={d.get('ok')}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    rc, out = run_health()
    if "--quiet" not in sys.argv:
        print(out)
    healthy, text, blocks = build(out, rc)
    if healthy and "--always" not in sys.argv:
        sys.exit(0)
    if not N.enabled:
        print(f"ALERT (slack disabled): {text}")
        sys.exit(rc)
    res = N._api("chat.postMessage", N._identity({
        "channel": N.SLACK_ALERT_CHANNEL, "text": text, "blocks": blocks}))
    print("slack posted" if res.get("ok") else f"slack post FAILED: {res.get('error')}")
    sys.exit(rc)
