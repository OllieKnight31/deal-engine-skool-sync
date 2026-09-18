# Deal Engine — Skool sync

Keeps **Close CRM** and **GoHighLevel** in step with the Skool free community
(*BRRRR Real Estate Investing*) and tells the team in **Slack** about every new member,
on a schedule, without Zapier.

Runs on GitHub Actions. No server, no browser, no laptop.

## What it does

| Step | Script |
|---|---|
| Find Skool members missing from either CRM and create them (contact + pipeline card) | `scripts/reconcile_skool.py --pages 3 --fix` |
| Announce genuinely new members in Slack `#1-de-community-joins` | `scripts/notify_slack.py` (called by the reconciler) |
| Promote valid free-text phone answers into Close's phone field, repair mangled `+1…` numbers | `scripts/close_phone_normalise.py --apply` |
| One-way Skool → GoHighLevel push (used for backfills) | `scripts/skool_to_ghl.py --apply` |

Every write is idempotent — contacts upsert on email, and a pipeline card is only
created if the member has none. Re-running never duplicates anything.

## Why it exists

The Zapier automation mapped the Skool join-question phone answer straight into Close's
validated phone field. Any answer that was not a number — `Nc`, `Sorry`, `6`,
*"Around 8pm because I have football practice"* — made Close reject the whole lead, so the
run errored before writing anything and **the person was lost silently**. That was ~8% of signups.

It is also cheaper. The Zap costs 3 Zapier tasks per signup (~7,200/month at current volume,
on a 750-task plan). This costs nothing.

## Reading Skool without a browser

The members page is server-rendered, so a cookie-authenticated `GET` returns every member and
their join answers in `__NEXT_DATA__`. Only `auth_token` (+ `client_id`) is needed — the AWS
load-balancer cookies are not — and `auth_token` lasts ~364 days. That is what lets this run in CI.

## Secrets required

| Secret | What it is |
|---|---|
| `SKOOL_COOKIE` | `auth_token=…; client_id=…` from a logged-in Skool session |
| `CLOSE_API_KEY` | Close API key (org **DealEngine**) |
| `GHL_PIT` | GoHighLevel Private Integration token, sub-account AA StrategyWorks |
| `SLACK_BOT_TOKEN` | `xoxb-…` for the **Workspace Builder** app (`chat:write`). Absent = notifications silently skipped, sync still runs |
| `SLACK_CHANNEL` | Channel id, `C0C2WAXFH50` = `#1-de-community-joins` |

**Rotate `SKOOL_COOKIE` if the Skool password changes.** If the session expires the run fails
loudly with *"the Skool session has probably expired"* rather than silently syncing nothing.

## Local use

```bash
pip install -r requirements.txt
export SKOOL_COOKIE="auth_token=…; client_id=…"
export CLOSE_API_KEY=… GHL_PIT=…
python scripts/reconcile_skool.py --pages 2          # report only
python scripts/reconcile_skool.py --pages 2 --fix    # backfill both CRMs
```


## Slack notifications

One message per new member into `#1-de-community-joins`, carrying their email, phone,
**how they found Deal Engine** (Skool's own attribution — Instagram, YouTube, ManyChat,
Skool discovery…), their location, what they said they want, and buttons through to their
Skool profile and their record in both CRMs.

### Notified exactly once, by design

The "already told the team" marker is the Close lead field **Slack Notified At**
(`cf_qpVKHQiE9jsZAkUIOvGlN1quL7m0uFZAPFOBE19jhCy`), not a state file in this repo.

That matters because GitHub's scheduler drops and delays runs, and because the Zapier Zap may
still create a Close lead before this job sees the member. Keying off the lead itself means a
member is announced once regardless of *which* system got to them first, how many runs overlap,
or how late a run fires. It is also visible in the Close UI, so "did the team get pinged about
this person?" is answerable without reading CI logs.

### Flood guard

More than `SLACK_NOTIFY_MAX` new members in one run (default **8**) posts a single digest
instead of N messages. That is what a backfill looks like, and it should not bury the channel.

### Before enabling it on a community that is already in the CRM

Run once so the existing membership is not announced retrospectively:

```bash
python scripts/reconcile_skool.py --pages 3 --stamp-existing
```

This marks everyone currently known as already-announced and posts nothing. From then on only
genuinely new joins appear. `--no-notify` also suppresses posting for any one-off run.

## Known issue — GitHub's scheduler is not punctual

On 18 Sept the `*/10` cron produced **one run in 5 hours 40 minutes**. GitHub explicitly
de-prioritises and drops short-interval schedules, so treat the cadence as *eventual*, not
every-N-minutes. Everything here is idempotent and self-healing precisely because of that —
but if Slack alerts need to be genuinely prompt, the trigger has to move off GitHub cron.
