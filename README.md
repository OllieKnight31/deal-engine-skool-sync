# Deal Engine — Skool sync

Keeps **Close CRM** and **GoHighLevel** in step with the Skool free community
(*BRRRR Real Estate Investing*), on a schedule, without Zapier.

Runs every 10 minutes on GitHub Actions. No server, no browser, no laptop.

## What it does

| Step | Script |
|---|---|
| Find Skool members missing from either CRM and create them (contact + pipeline card) | `scripts/reconcile_skool.py --pages 3 --fix` |
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
