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
| Mirror the GHL Setter Pipeline (DFY / Mentorship applications) into Close | `scripts/ghl_dfy_to_close.py --apply` |
| Full 60-page sweep of the whole community, once a day | `scripts/reconcile_skool.py --pages 60 --fix` |

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
| `SLACK_CHANNEL` | Channel id, `C0C2WAXFH50` = `#5-community-joins` (member cards, daily count, digests) |
| `SLACK_ALERT_CHANNEL` | Optional. Where crash / unavailable / recovered alerts go. Default `C0C2TKXLV6W` = `#5-ops-alerts` |

**Rotate `SKOOL_COOKIE` if the Skool password changes.** If the session expires the run fails
loudly with *"Skool redirected page N to …/about: the session cookie is no longer accepted"*
rather than silently syncing nothing.

**A `403` is NOT the cookie.** Measured 22 Sept 2026 from a GitHub runner with the very secret
the sync uses: a dead or missing cookie is answered with a **307 redirect to `/about`**, never a
403. A 403 comes from CloudFront itself (`server: CloudFront`, `x-cache: Error from cloudfront`):
Skool's AWS WAF refusing the runner, and the same cookie got 200 from the same runner minutes
later. The sync now treats 403 / 429 / 5xx / timeouts as transient (`SkoolUnavailable`): it
retries the page (5 s, 10 s, 20 s), then skips the pass with exit 75 and lets the next pass
retry. Slack is only told after **3 consecutive skipped passes** (~10 min), the alert says it is
Skool's side and nothing needs rotating, and a "recovered" note follows when Skool answers again.
Before rotating anything, run the **Skool probe** workflow (Actions → *Skool probe* → Run): it
shows which of the two shapes you actually have.

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

## Cadence — a self-dispatching chain, not a cron

GitHub's `schedule` trigger is best-effort and openly de-prioritised. Measured on this repo
over 19–20 Sept the hourly `7 * * * *` cron fired with gaps of **246, 288, 201, 172, 181 and
162 minutes**, and nothing at all between 01:00Z and 05:50Z. Each run only covers ~55 minutes
of internal polling, so genuine coverage was **~28% of the clock**.

So the cadence no longer depends on cron. Each `Skool sync` run polls for ~55 minutes and then
**dispatches its own successor** (`gh workflow run sync.yml`), the same pattern that gives
`OllieKnight31/speed-to-lead-pinger` an unbroken chain. `concurrency: skool-sync` with
`cancel-in-progress: false` guarantees exactly one live writer however many triggers pile up.
The `*/15` cron is a **restarter only**, for a chain broken by a cancel or a GitHub-side kill.

Cancelling a run stops the chain deliberately — the hand-off is skipped on cancel, never on
failure. To restart it by hand: `gh workflow run sync.yml -R OllieKnight31/deal-engine-skool-sync`.

`keepalive.yml` makes an empty commit twice a month, because GitHub disables scheduled
workflows on a repo with 60 days of no commits — and API self-dispatch does not count as
activity, so the restarter would otherwise switch itself off unnoticed.

### What each pass does

| Cadence | Work |
|---|---|
| every pass (~5 min) | `reconcile_skool.py --pages 3 --fix`, `close_phone_normalise.py --apply` |
| every 3rd pass (~15 min) | `ghl_dfy_to_close.py --apply` |
| every 6th pass (~30 min) | the reconcile widens to `--pages 8` |
| once/day 04:00 UTC | the reconcile widens to `--pages 60` — the full-community deep scan |
| once/day 06:20 UTC | `pipeline_advance.py` — pipeline hygiene |
| once/day 07:30 UTC | `attribution_digest.py --days 1 --post` — the daily join count |
| Mondays 08:00 UTC | `attribution_digest.py --days 7 --post` — the weekly attribution digest |

### The once-a-day jobs, and why the guard is a git ref

All three used to hang off cron and all three were being dropped. `Skool maintenance` had
**`total_count == 0`** — both of the daily join count's slots (19 Sept 07:30 UTC, 20 Sept
06:20 UTC) were silently dropped, so the number that says the funnel is alive was never
produced, while the launchd agent that used to produce it had already been retired.

A run lasts ~55 minutes, so two consecutive runs can both be alive inside the same hour — an
in-run flag alone would post the join count to Slack twice. So the guard is an **atomic
server-side claim**: the run creates `refs/daily-claim/<slot>/<UTC date>`, and GitHub gives
the first caller 201 and every other caller 422 *Reference already exists*. It is race-free,
survives restarts and overlapping runs, is neither a tag nor a branch, and doubles as an
audit trail:

```bash
gh api repos/OllieKnight31/deal-engine-skool-sync/git/matching-refs/daily-claim
```

Each job fires once the clock is **past** its slot rather than exactly on it, so a job is
caught up rather than lost if the chain was down at the appointed minute. If a job fails, its
claim is **released** so a later run retries the same day, and the run still ends red.

To run one on demand, dispatch `Skool sync` with `force_hygiene` / `force_digest` / `deep`.
Forcing skips the clock but still honours the claim, so it cannot double-post either.

The deep scan lives **inside** the loop rather than in its own workflow on purpose: the loop is
the single serialised writer, and Close's search index lags writes by minutes, so a deep scan
running concurrently with a 5-minute pass would read "not in Close" for a lead the other pass
had just created and duplicate it.

## Replaces these Mac launchd agents

These ran on Ollie's MacBook and only achieved 24–42% of their expected runs, because the
machine sleeps when the lid closes. The 04:30 deep scan had **never once succeeded** — it fired
during Deep Idle, DarkWoke for ~45s with no DNS, and died on `socket.gaierror`.

| launchd agent | Was | Now |
|---|---|---|
| `com.dealengine.ghl-close-sync` | every 15 min | every 3rd loop pass |
| `com.dealengine.skool-ghl-sync` | every 30 min | covered by the reconcile (which writes GHL too) |
| `com.dealengine.skool-reconcile` | every 2 h | every 6th loop pass, `--pages 8` |
| `com.dealengine.skool-ghl-deepscan` | nightly 04:30, never succeeded | daily 04:00 UTC inside the loop |
| `com.dealengine.skool-daily-count` | daily 08:30 | daily 07:30 UTC inside the loop |

`com.dealengine.skool-cookie-refresh` is **not** replaced — it reads Chrome's cookie store on
disk, which only exists on the Mac. It refreshes the local `.secrets/skool_cookie` file; the
`SKOOL_COOKIE` repo secret is separate and lasts ~364 days.
