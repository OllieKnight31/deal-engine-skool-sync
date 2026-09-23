#!/usr/bin/env python3
"""
Health check for the Skool -> GHL -> Close pipeline.

The failure this guards against: a Cloudflare block (or any API error) making the
sync print "nothing to mirror" and exit 0 - a dead pipe that looks healthy. Run
this any time you want to know the mirror is genuinely alive.

WHERE THE WORK RUNS (changed 20 Sept 2026)
  Five launchd agents were retired: ghl-close-sync, skool-ghl-sync, skool-reconcile,
  skool-ghl-deepscan, skool-daily-count. Their plists are still on disk but unloaded
  and disabled, because the MacBook sleeps when the lid closes and they only ever
  achieved 24-42% of their nominal cadence. The work now runs in GitHub Actions
  (OllieKnight31/deal-engine-skool-sync) and in the Vercel relay, so that is where
  this file looks for it. Only com.dealengine.skool-cookie-refresh is still local -
  it reads Chrome's cookie store off this disk, which exists nowhere else.

  The four once-a-day jobs (deep scan, hygiene, join count, weekly digest) ride the
  sync chain and record themselves as refs/daily-claim/<slot>/<UTC date>. That ref
  namespace, not run history, is what this file asks "did it run today" - see the
  comment above DAILY_SLOTS for why.

Every check must distinguish "I checked and it is fine" from "I could not check".
The second raises CannotCheck and prints FAIL. A pipe nobody could read is not a
healthy pipe, and this codebase has been bitten repeatedly by helpers that return
something falsy on error so a real failure reads as "no data".

WHERE THIS RUNS (changed 23 Sept 2026)
  In GitHub Actions - `.github/workflows/health.yml` - after every Skool sync run
  (workflow_run, ~hourly) with a 3-hourly cron as backstop, so the alarm fires whether
  or not the MacBook is open. It used to be launchd `com.dealengine.sync-health` every
  3 h, which missed two slots in a row the first morning the lid was closed; a watcher
  that sleeps with the laptop is not a watcher. The Mac copy is now a shim onto this
  file and the launchd job is unloaded.

  Off the Mac the launchd checks (cookie auto-refresh, local cadence, local stderr)
  are skipped as INFO - they only ever described Mac-resident jobs, and the cookie the
  cloud actually uses is the SKOOL_COOKIE secret, which is validated here directly.

Usage:  python3 sync_health.py       (needs GHL_PIT, CLOSE_API_KEY, SKOOL_COOKIE or a
                                      cookie file, SLACK_BOT_TOKEN; gh on PATH with a
                                      token that can read Actions in the sync repo)
Exit:   0 healthy, 1 unhealthy
"""
import os, sys, json, base64, subprocess, urllib.parse, datetime, re, glob, time, plistlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghl_http

GHL_PIT = os.environ.get("GHL_PIT", "")            # CI secret, or .secrets/sync.env on the Mac
GHL_LOC = "2RrjNuz0M4MTFlxpW37k"
CLOSE_KEY = os.environ.get("CLOSE_API_KEY", "")     # never a default: this repo is public
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = HERE                            # this file lives beside the scripts it checks
CLIENT_SECRETS = os.path.join(HERE, "..", "..", "..", ".secrets")   # Mac only
# True on the studio Mac, False on a GitHub runner. The launchd checks only make sense
# where launchd exists; everywhere else they are reported as INFO, not silently dropped.
ON_MAC = sys.platform == "darwin" and not os.environ.get("GITHUB_ACTIONS")
SLACK_CHANNEL = "C0C2WAXFH50"          # #5-community-joins
SLACK_ALERT_CHANNEL = "C0C2TKXLV6W"    # #5-ops-alerts - where health_alert.py and the sync alarms post

# ---- where the work lives now ------------------------------------------------
GH_REPO       = "OllieKnight31/deal-engine-skool-sync"
WF_SYNC       = 361221459              # "Skool sync" - the self-dispatching chain; owns EVERYTHING
WF_MAINT      = 361918789              # "Skool maintenance" - standby only, see maintenance_workflow()
WF_KEEPALIVE  = 362477287              # "Keep the schedule alive"
CHAIN_GAP_MIN = 75                     # handoff is every ~50-55 min; beyond this it is broken
RELAY_HEALTH  = "https://de-funnel-relay.vercel.app/api/health"
MALFORMED_PAGES = 6                    # Skool pages read for the live member checks

# ---- the once-a-day slots, and how we know they ran --------------------------
# All four daily jobs now ride the sync chain rather than GitHub cron, because cron kept
# dropping them - `Skool maintenance` reached total_count == 0, so the daily join count
# had literally never fired. Each slot fires on the first pass once the clock is PAST its
# time, so a job the chain missed is caught up later in the day rather than lost.
#
# That catch-up is exactly why run history is the wrong thing to interrogate. "Which run
# was alive during the 04:00 hour" stopped being the question the moment a slot could
# legitimately complete at 07:46. sync.yml answers the real question directly: before
# doing a slot's work it creates refs/daily-claim/<slot>/<UTC date>, which GitHub grants
# atomically - 201 to the first caller, 422 "Reference already exists" to the rest - and
# RELEASES again if the work fails, so a later run retries the same day. So:
#
#   ref present  -> that slot was claimed today and was not released, i.e. it ran and
#                   returned 0 (or is running right now)
#   ref absent   -> it has not successfully run today
#
# Listing that namespace is a direct, unambiguous answer, and it doubles as the audit
# trail: "did the digest go out on the 19th?" is one API call.
CLAIM_PREFIX  = "daily-claim"
CLAIM_EPOCH   = "2026-09-20"           # UTC date the claim namespace went live (commit 8433572).
                                       # Days before this were never written, so they are
                                       # unjudgeable rather than failures.
SLOT_GRACE_MIN = 90                    # catch-up allowance: the chain's own 75-minute handoff
                                       # budget plus a pass interval and pip install
LOG_BUDGET    = 8                      # most run logs any one check will download

# slot, minutes past UTC midnight, what it is, what it runs, Mondays-only
DAILY_SLOTS = [
    ("deepscan", 4 * 60 +  0, "60-page deep scan",         "reconcile_skool.py --pages 60 --fix", False),
    ("hygiene",  6 * 60 + 20, "pipeline hygiene",          "pipeline_advance.py --apply",         False),
    ("digest",   7 * 60 + 30, "daily join count",          "attribution_digest.py --days 1",      False),
    ("weekly",   8 * 60 +  0, "weekly attribution digest", "attribution_digest.py --days 7",      True),
]
SLOTS = {s[0]: s for s in DAILY_SLOTS}

# Retired launchd jobs and the CI job that took the work over. A plist sitting on disk
# unloaded is not a failure - but it is exactly what makes people believe a job is still
# covered, so every one of them is named out loud with its new home.
MOVED_TO = {
    "ghl-close-sync":     "GH Actions 'Skool sync' -> ghl_dfy_to_close.py, every 5th pass (~15 min)",
    "skool-ghl-sync":     "GH Actions 'Skool sync' -> reconcile pass, every ~3 min",
    "skool-reconcile":    "GH Actions 'Skool sync' -> reconcile --pages 8, every 10th pass (~30 min)",
    "skool-ghl-deepscan": "GH Actions 'Skool sync' -> deepscan slot, 04:00 UTC claim",
    "skool-daily-count":  "GH Actions 'Skool sync' -> digest slot, 07:30 UTC claim",
}


class CannotCheck(RuntimeError):
    """The question could not be asked at all - no gh, no network, unreadable logs.

    Never a PASS. "I could not look" and "I looked and it is fine" are different
    answers, and collapsing them is how a dead pipe reads as healthy.
    """


def _slack_token():
    """One owner for the bot token: the file the Slack app wrote it to."""
    tok = os.environ.get("SLACK_BOT_TOKEN", "")
    if tok:
        return tok
    env = os.path.expanduser("~/.claude/channels/slack/.env")
    if os.path.exists(env):
        for line in open(env):
            if line.startswith("SLACK_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


fails = []


def check(label, fn):
    try:
        msg = fn()
        print(f"  PASS  {label:34} {msg}")
    # SystemExit is caught on purpose. Under Homebrew python3 the last check imports
    # skool_to_ghl, which sys.exit()s when phonenumbers is missing - that killed the
    # whole script before it printed any verdict at all, which reads as a pass to
    # anyone skimming. A dead check must say FAIL, not say nothing.
    except (Exception, SystemExit) as e:
        print(f"  FAIL  {label:34} {type(e).__name__}: {str(e)[:400]}")
        fails.append(label)


def info(label, msg):
    """Neither PASS nor FAIL. For state that is correct but would otherwise be invisible."""
    print(f"  INFO  {label:34} {msg}")


# ---- launchd introspection ---------------------------------------------------
# "is it loaded" is not "is it working", and it is certainly not "is it running as
# often as it is supposed to". These read what launchd actually recorded.

class NotLoaded(RuntimeError):
    """The plist exists on disk but launchd is not running the job."""


def job_status(label):
    """runs / last exit code straight out of launchd, not out of a log file."""
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise NotLoaded(f"{label} is not loaded in launchd")
    def field(name):
        m = re.search(rf"^\s*{re.escape(name)} = (.+?)\s*$", r.stdout, re.M)
        return m.group(1) if m else None
    def num(name):
        v = field(name)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {"runs": num("runs") or 0, "last_exit": num("last exit code"),
            "last_exit_raw": field("last exit code")}


def _schedule_seconds(p):
    """Nominal seconds between runs, from the plist itself."""
    if isinstance(p.get("StartInterval"), int):
        return p["StartInterval"]
    sci = p.get("StartCalendarInterval")
    if sci:
        entries = sci if isinstance(sci, list) else [sci]
        per = max(1, len(entries))
        if any("Weekday" in e for e in entries):
            return 7 * 86400 // per
        if any("Day" in e for e in entries):
            return 31 * 86400 // per
        return 86400 // per
    return None


def _human(sec):
    if sec % 86400 == 0 and sec >= 86400:
        return f"{sec // 86400}d"
    if sec % 3600 == 0 and sec >= 3600:
        return f"{sec // 3600}h"
    return f"{sec // 60}m"


def local_plists():
    """(label, short name, parsed plist) for every com.dealengine.* agent on disk."""
    out = []
    for path in sorted(glob.glob(os.path.expanduser("~/Library/LaunchAgents/com.dealengine.*.plist"))):
        label = os.path.basename(path)[:-len(".plist")]
        try:
            with open(path, "rb") as fh:
                p = plistlib.load(fh)
        except Exception as e:
            raise CannotCheck(f"unreadable plist {label}: {e}")
        out.append((label, label.split(".")[-1], p, path))
    return out


# ---- GitHub Actions ----------------------------------------------------------
# The five retired agents all became jobs inside these workflows, so "is the sync
# running" is now a question about GitHub, not about launchd.

_gh_bin = None


def gh_bin():
    """Resolve gh once, loudly.

    launchd and a bare PATH do not see /opt/homebrew/bin, and a missing binary must
    read as "I could not check the sync", never as "the sync has nothing to report".
    """
    global _gh_bin
    if _gh_bin:
        return _gh_bin
    for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        if os.path.exists(p):
            _gh_bin = p
            return p
    from shutil import which
    p = which("gh")
    if not p:
        raise CannotCheck("gh CLI not found - cannot read GitHub Actions state")
    _gh_bin = p
    return p


def gh_api(path, want=dict):
    """One authenticated GET. Anything other than parsed JSON of the expected shape is
    CannotCheck - never an empty default that a caller would read as "nothing wrong"."""
    r = subprocess.run([gh_bin(), "api", "-H", "Accept: application/vnd.github+json", path],
                       capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise CannotCheck(f"gh api {path}: {(r.stderr or r.stdout).strip()[:200]}")
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise CannotCheck(f"gh api {path} returned non-JSON: {r.stdout[:160]}")
    if not isinstance(d, want):
        raise CannotCheck(f"gh api {path} returned {type(d).__name__}, "
                          f"expected {want.__name__}")
    return d


_wf_cache, _runs_cache = {}, {}


def workflow(wid):
    if wid not in _wf_cache:
        w = gh_api(f"repos/{GH_REPO}/actions/workflows/{wid}")
        if "state" not in w or "name" not in w:
            raise CannotCheck(f"workflow {wid} payload has no state/name: {str(w)[:160]}")
        _wf_cache[wid] = w
    return _wf_cache[wid]


def workflow_runs(wid, n=20):
    """Runs newest-first. An empty list is a real answer ("it has never run") and is
    returned as such; a missing key is not, and raises."""
    key = (wid, n)
    if key not in _runs_cache:
        d = gh_api(f"repos/{GH_REPO}/actions/workflows/{wid}/runs?per_page={n}")
        if "workflow_runs" not in d or not isinstance(d["workflow_runs"], list):
            raise CannotCheck(f"no workflow_runs list for workflow {wid}")
        _runs_cache[key] = sorted(d["workflow_runs"],
                                  key=lambda r: r.get("run_started_at") or "", reverse=True)
    return _runs_cache[key]


def _utc(ts):
    try:
        return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        raise CannotCheck(f"unparseable GitHub timestamp {ts!r}")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _age_min(ts):
    return (_now() - _utc(ts)).total_seconds() / 60.0


def _ago(ts):
    m = _age_min(ts)
    return f"{m:.0f} min" if m < 180 else (f"{m/60:.1f} h" if m < 2880 else f"{m/1440:.1f} d")


def sync_workflow_enabled():
    """GitHub silently disables a repo's schedules after 60 days with no commits, and a
    disabled workflow answers every other question with a stale 'last run succeeded'."""
    w = workflow(WF_SYNC)
    if w["state"] != "active":
        raise RuntimeError(f"'{w['name']}' state is '{w['state']}' - GitHub is NOT running it"
                           + (" (the 60-day no-commit auto-disable fired)"
                              if "inactivity" in w["state"] else ""))
    return f"'{w['name']}' active in {GH_REPO}"


def sync_last_run():
    """The newest run that has actually FINISHED.

    An in-flight run has conclusion = null, and reading null as "not a failure" is how
    a red chain reads as green. The run in flight is reported, never used as evidence.
    """
    runs = workflow_runs(WF_SYNC, 20)
    if not runs:
        raise RuntimeError("the Skool sync workflow has never run")
    done = [r for r in runs if r.get("status") == "completed"]
    flight = [r for r in runs if r.get("status") != "completed"]
    tail = f", #{flight[0]['run_number']} in flight" if flight else ""
    if not done:
        raise RuntimeError(f"not one completed run in the last {len(runs)}{tail}")
    r = done[0]
    if r.get("conclusion") != "success":
        raise RuntimeError(f"run #{r['run_number']} ended '{r.get('conclusion')}' "
                           f"{_ago(r['run_started_at'])} ago - {r.get('html_url')}")
    return f"#{r['run_number']} success, started {_ago(r['run_started_at'])} ago{tail}"


def sync_chain_alive():
    """Time since the last run STARTED - the only number that shows the chain is intact.

    Each run polls ~55 minutes and then dispatches its own successor, so starts land
    every ~50-55 min. The `*/15` cron in sync.yml is only a restarter and cannot be
    relied on: measured on this repo, GitHub dropped that schedule for 162-288 minutes
    at a time. A gap past CHAIN_GAP_MIN means the handoff is broken, not merely late.
    """
    runs = workflow_runs(WF_SYNC, 20)
    if not runs:
        raise RuntimeError("the Skool sync workflow has never run")
    r = runs[0]
    mins = _age_min(r["run_started_at"])
    if mins > CHAIN_GAP_MIN:
        raise RuntimeError(
            f"last start was run #{r['run_number']} {mins:.0f} min ago (budget "
            f"{CHAIN_GAP_MIN} min) - the self-dispatch handoff is broken and only the "
            f"*/15 cron restarter can recover it, which GitHub drops for hours at a time")
    return (f"last start {mins:.0f} min ago (#{r['run_number']}, {r.get('status')}), "
            f"budget {CHAIN_GAP_MIN} min")


def run_log(run_id):
    """Text of one run's logs. None when the run is still in flight.

    `gh run view --log` exits 0 and prints "logs will be available when it is complete"
    for a running job. Treating that sentence as a log body - and therefore as "no deep
    scan found" - is precisely the dead-pipe-reads-healthy trap, so it is detected.
    """
    r = subprocess.run([gh_bin(), "run", "view", str(run_id), "--repo", GH_REPO, "--log"],
                       capture_output=True, text=True, timeout=240)
    blob = (r.stdout or "") + (r.stderr or "")
    if "still in progress" in blob or "logs will be available" in blob:
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        raise CannotCheck(f"run {run_id} logs unreadable: {blob.strip()[:160]}")
    return r.stdout


# The run log contains the workflow's own SOURCE as well as its output - GitHub echoes
# every shell line of a `run:` step. sync.yml's completion line is literally
# `echo "$slot completed at $(date -u +%H:%M:%SZ)"`, so a plain substring test matches
# every run of this workflow whether or not the slot ever fired. These match real output
# only: the slot name expanded, a real clock stamp, the runner's own ##[error] rendering.
def _slot_ok_re(slot):
    return re.compile(rf"\b{re.escape(slot)} completed at\s+(\d{{2}}:\d{{2}}:\d{{2}}Z)")


def _slot_fail_re(slot):
    return re.compile(rf"##\[error\]{re.escape(slot)} FAILED")


def _slot_unguarded_re(slot):
    return re.compile(rf"##\[error\]\[{re.escape(slot)}\] NOT RUN")


# Runs from before commit 8433572 spelled the deep scan's completion differently. Kept so
# a scan that genuinely completed on the old wording is not called a failure.
LEGACY_OK = {"deepscan": re.compile(r"deep scan completed at\s+(\d{2}:\d{2}:\d{2}Z)")}

_claims_cache = {}


def daily_claims():
    """{UTC date -> set(slot)} straight out of the refs/daily-claim namespace.

    An empty list from GitHub is a real answer ("nothing claimed"), and is returned as
    one. Anything else - a failed call, a non-list body, a ref that does not parse - is
    CannotCheck. That separation is the whole point: the agent that owns sync.yml hit
    precisely this bug in the workflow, where a bare success/failure made a BROKEN claim
    API look identical to "already done", which would have silently disabled all three
    daily jobs the moment the token changed. The monitor must not repeat the mistake it
    is meant to catch.
    """
    if "d" not in _claims_cache:
        refs = gh_api(f"repos/{GH_REPO}/git/matching-refs/{CLAIM_PREFIX}", list)
        out = {}
        pat = re.compile(rf"^refs/{re.escape(CLAIM_PREFIX)}/([^/]+)/(\d{{4}}-\d{{2}}-\d{{2}})$")
        for r in refs:
            if not isinstance(r, dict) or "ref" not in r:
                raise CannotCheck(f"claim listing has an entry with no ref: {str(r)[:120]}")
            m = pat.match(r["ref"])
            if m:
                out.setdefault(m.group(2), set()).add(m.group(1))
        _claims_cache["d"] = out
    return _claims_cache["d"]


def _slot_day(slot, mondays_only):
    """The UTC date this slot should most recently have run, or None if it never has yet.

    For a daily slot that is today once its time has passed, else yesterday. For the
    Monday-only slot it is the most recent Monday whose time has passed. Days before
    CLAIM_EPOCH are not returned: nothing ever wrote a claim then, so judging them would
    be inventing a failure out of missing history rather than out of missing work.
    """
    now = _now()
    now_min = now.hour * 60 + now.minute
    at = SLOTS[slot][1]
    d = now.date()
    if mondays_only:
        back = (d.weekday() - 0) % 7                  # 0 = Monday
        if back == 0 and now_min < at:
            back = 7
        d = d - datetime.timedelta(days=back)
    elif now_min < at:
        d = d - datetime.timedelta(days=1)
    iso = d.isoformat()
    return iso if iso >= CLAIM_EPOCH else None


def _log_proof(slot, since):
    """Second chance before calling a slot failed: does a run log actually show it
    finishing? Only walked when the claim is MISSING, so it costs nothing on a healthy
    system, and it is not leniency - it demands the same completion line, just from the
    run's own output instead of from the ref.

    Returns (stamp, run_number) on proof, None if the logs are readable and show none,
    and raises CannotCheck if they could not be read at all.
    """
    ok, bad, unguarded = _slot_ok_re(slot), _slot_fail_re(slot), _slot_unguarded_re(slot)
    legacy = LEGACY_OK.get(slot)
    runs = [r for r in workflow_runs(WF_SYNC, 30)
            if (_utc(r["updated_at"]) if r.get("status") == "completed" and r.get("updated_at")
                else _now()) >= since]
    read, problems = 0, []
    for r in runs[:LOG_BUDGET]:
        if r.get("status") != "completed":
            continue                                  # in flight: no log to read yet, and
                                                      # asking for one is a wasted round trip
        txt = run_log(r["id"])                        # raises CannotCheck if unreadable
        if txt is None:
            continue                                  # raced to in-flight between calls
        read += 1
        m = ok.search(txt) or (legacy.search(txt) if legacy else None)
        if m:
            return m.group(1), r["run_number"]
        if unguarded.search(txt):
            problems.append(f"#{r['run_number']} refused to run it unguarded "
                            f"(the once-a-day claim API was broken)")
        elif bad.search(txt):
            problems.append(f"#{r['run_number']} ran it and it FAILED")
    if read == 0 and runs:
        raise CannotCheck(f"{len(runs)} run(s) since the slot but none had readable logs - "
                          f"{slot} could not be verified either way")
    if problems:
        raise RuntimeError("; ".join(problems[:3]))
    return None


def slot_check(slot):
    """Did this once-a-day job actually run on the day it was last due?

    Asks the claim namespace, not run history. sync.yml takes the claim before doing the
    work and RELEASES it again if the work fails, so a surviving ref means the job ran
    and returned 0 (or is running right now); an absent one means it has not succeeded
    today and a later run will retry it.

    The deep scan is why this check exists at all. On the Mac it had runs = 1, last exit
    code = 1 - it had never once completed - and every earlier version of this file said
    PASS because the plist was loaded. Loaded is not completed, and neither is scheduled.
    """
    _slot, at, desc, cmd, mondays = SLOTS[slot]
    hhmm = f"{at // 60:02d}:{at % 60:02d}"
    day = _slot_day(slot, mondays)
    claims = daily_claims()                           # raises CannotCheck if unreadable
    if day is None:
        return (f"no {'Monday' if mondays else 'day'} has come due since the claim namespace "
                f"went live ({CLAIM_EPOCH}) - nothing to judge yet, and nothing missed")
    if day in claims and slot in claims[day]:
        return f"claimed {day} ({hhmm} UTC slot, {desc})"

    # No claim. Work out whether it is merely not due yet, or genuinely missing.
    due = datetime.datetime.combine(datetime.date.fromisoformat(day),
                                    datetime.time(at // 60, at % 60),
                                    tzinfo=datetime.timezone.utc)
    late = (_now() - due).total_seconds() / 60.0
    if late < SLOT_GRACE_MIN:
        return (f"{hhmm} UTC slot for {day} came due {late:.0f} min ago; the chain catches it "
                f"up on its next pass (grace {SLOT_GRACE_MIN} min)")

    proof = _log_proof(slot, due)                     # raises on a run that failed/refused
    if proof:
        stamp, num = proof
        return (f"{desc} completed at {stamp} in run #{num}, but holds no claim ref for "
                f"{day} - it ran before the claim namespace existed, or the ref was removed "
                f"after the fact")
    raise RuntimeError(
        f"{desc} has NOT run for {day}: no refs/{CLAIM_PREFIX}/{slot}/{day}, and no run log "
        f"shows it completing. Due at {hhmm} UTC, now {late:.0f} min late (grace "
        f"{SLOT_GRACE_MIN} min). It runs `{cmd}` on the first chain pass past the slot, so "
        f"either the chain has been down since then or the claim is stuck - list it with: "
        f"gh api repos/{GH_REPO}/git/matching-refs/{CLAIM_PREFIX}")


MAINT_OK_CONCLUSIONS = ("success", "cancelled", "skipped", None)


def maintenance_workflow():
    """Standby only. Deliberately NOT judged on freshness.

    This used to demand a successful run within 30 h, which was right when the daily
    work lived here. It does not any more: hygiene, the join count and the Monday digest
    all ride the sync chain now, and maintenance.yml sits in the same `skool-sync`
    concurrency group, so the always-live chain will normally supersede a queued
    maintenance run. Demanding freshness from a workflow that is designed to be
    superseded would paint this report red while the work was being done perfectly - and
    a monitor that cries wolf gets ignored, which is worse than no monitor.

    Whether the daily work happened is answered by the claim-ref checks above. All this
    asks is the question it can still honestly answer: is the standby broken? A
    'cancelled' conclusion is the designed outcome of concurrency supersession, so only
    a real failure counts.
    """
    w = workflow(WF_MAINT)
    if w["state"] != "active":
        raise RuntimeError(f"'{w['name']}' state is '{w['state']}' - the belt-and-braces "
                           f"cron is gone; the chain is then the only thing running the "
                           f"daily jobs")
    runs = workflow_runs(WF_MAINT, 10)
    done = [r for r in runs if r.get("status") == "completed"]
    if not done:
        return (f"'{w['name']}' active, standby only ({len(runs)} run(s), none completed yet) "
                f"- the daily work rides the chain, see the slot checks above")
    r = done[0]
    if r.get("conclusion") not in MAINT_OK_CONCLUSIONS:
        raise RuntimeError(f"standby run #{r['run_number']} ended '{r.get('conclusion')}' "
                           f"{_ago(r['run_started_at'])} ago - the fallback is broken, so a "
                           f"chain outage would have no backstop: {r.get('html_url')}")
    return (f"'{w['name']}' active, standby only; last run #{r['run_number']} "
            f"'{r.get('conclusion')}' {_ago(r['run_started_at'])} ago")


def keepalive_workflow():
    """The backstop under every schedule in the repo.

    GitHub disables scheduled workflows on a repo with no commit for 60 days. The sync
    chain dispatches itself through the API, which does NOT count as repo activity, so
    without an empty commit twice a month the `*/15` restarter would switch itself off -
    and the day the chain broke there would be nothing left to restart it.
    """
    w = workflow(WF_KEEPALIVE)
    if w["state"] != "active":
        raise RuntimeError(f"'{w['name']}' state is '{w['state']}' - nothing is keeping the "
                           f"restarter cron alive")
    repo = gh_api(f"repos/{GH_REPO}")
    if "pushed_at" not in repo:
        raise CannotCheck("repo payload has no pushed_at - cannot measure the 60-day clock")
    quiet_d = _age_min(repo["pushed_at"]) / 1440.0
    runs = workflow_runs(WF_KEEPALIVE, 5)
    if runs:
        r = runs[0]
        last = f"last empty commit {_ago(r['run_started_at'])} ago"
        if r.get("status") == "completed" and r.get("conclusion") != "success":
            raise RuntimeError(f"run #{r['run_number']} ended '{r.get('conclusion')}' - the empty "
                               f"commit is not landing, so the 60-day clock is running "
                               f"({quiet_d:.0f} d quiet)")
    else:
        last = (f"not due yet (cron 03:17 UTC on the 1st/15th; workflow created "
                f"{w.get('created_at','?')[:10]})")
    if quiet_d > 45:
        raise RuntimeError(f"repo has had no commit for {quiet_d:.0f} d and GitHub disables "
                           f"schedules at 60 d - {last}")
    return f"active, repo last commit {quiet_d:.1f} d ago (60 d = auto-disable); {last}"


# ---- Vercel relay ------------------------------------------------------------

def relay_health():
    """Both intake routes are dual-written to Close in real time by de-funnel-relay.

    If closeApiKey is false the relay still returns 200, still accepts the lead and
    still posts to Slack - it just never reaches Close. That is a dead pipe wearing the
    costume of a healthy one, which is the exact failure class this file exists to
    catch, so every config boolean must be true, not merely present.
    """
    r = subprocess.run(["curl", "-s", "--max-time", "25", "-w", "\n%{http_code}", RELAY_HEALTH],
                       capture_output=True, text=True, timeout=45)
    parts = (r.stdout or "").rsplit("\n", 1)
    if r.returncode != 0 or len(parts) != 2:
        raise CannotCheck(f"relay unreachable: {((r.stderr or r.stdout) or 'no output').strip()[:160]}")
    body, code = parts[0], parts[1].strip()
    if code != "200":
        raise RuntimeError(f"HTTP {code} from /api/health: {body[:160]}")
    try:
        d = json.loads(body)
    except ValueError:
        raise CannotCheck(f"non-JSON from /api/health: {body[:160]}")
    if d.get("ok") is not True:
        raise RuntimeError(f"relay reports ok={d.get('ok')!r}: {body[:200]}")
    cfg = d.get("config")
    if not isinstance(cfg, dict) or not cfg:
        raise CannotCheck(f"no config map in the health payload: {body[:160]}")
    if "closeApiKey" not in cfg:
        raise RuntimeError("health payload no longer reports closeApiKey - the real-time "
                           "Close mirror cannot be proved configured")
    off = sorted(k for k, v in cfg.items() if v is not True)
    if off:
        lead = "Close mirroring is OFF - " if "closeApiKey" in off else ""
        raise RuntimeError(f"{lead}config false: {', '.join(off)} (leads would be accepted "
                           f"and silently dropped)")
    return f"200 ok, {len(cfg)}/{len(cfg)} config flags true: {', '.join(sorted(cfg))}"


# ---- CRM APIs ----------------------------------------------------------------

def ghl_reachable():
    pipes = ghl_http.preflight(GHL_PIT, GHL_LOC)
    return f"{len(pipes)} pipelines"


def close_reachable():
    auth = "Basic " + base64.b64encode((CLOSE_KEY + ":").encode()).decode()
    out = subprocess.run(["curl", "-s", "-H", f"Authorization: {auth}",
                          "https://api.close.com/api/v1/lead/?_limit=1&_fields=id"],
                         capture_output=True, text=True).stdout
    d = json.loads(out)
    n = d.get("total_results")
    if not n:
        raise RuntimeError(f"no total_results: {out[:120]}")
    return f"{n} leads"


# ---- still local to this Mac -------------------------------------------------

def _cookie_string():
    """The cookie this process would sync with: the CI secret first, else the Mac file."""
    c = os.environ.get("SKOOL_COOKIE", "").strip()
    if c:
        return c, "SKOOL_COOKIE (the CI secret)"
    path = os.environ.get("SKOOL_COOKIE_FILE") or os.path.join(CLIENT_SECRETS, "skool_cookie")
    if os.path.exists(path):
        return open(path).read().strip(), "cookie file on this Mac"
    raise CannotCheck("no SKOOL_COOKIE and no cookie file - nothing could sync")


def _jwt_expiry(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        exp = json.loads(base64.urlsafe_b64decode(p))["exp"]
        return datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
    except Exception:
        return None


def skool_cookie_live():
    """The credential the sync ACTUALLY uses: proves it reads members, and fails at 30 days
    to expiry so there is a month to refresh it (skool_cookie_from_disk.py --push-secret,
    on the Mac). The old version checked the Mac's file, which is not what CI runs on."""
    cookie, src = _cookie_string()
    tok = dict(p.split("=", 1) for p in cookie.split("; ") if "=" in p).get("auth_token", "")
    ms = skool_members(MALFORMED_PAGES)               # raises if the read is broken
    exp = _jwt_expiry(tok)
    if not exp:
        raise RuntimeError(f"{len(ms)} members read but the auth_token is not a JWT - expiry unknown")
    days = (exp - _now()).days
    if days < 30:
        raise RuntimeError(f"session expires {exp.date()} - only {days} days left; refresh it now "
                           f"(Mac: automations/scripts/skool_cookie_from_disk.py --push-secret)")
    return f"{len(ms)} members read | JWT expires {exp.date()} ({days} days left) | {src}"


def cookie_refresher_scheduled():
    """The one agent that genuinely cannot move to CI: it reads Chrome's cookie store
    off this disk, and CI has no Chrome and no disk."""
    st = job_status("com.dealengine.skool-cookie-refresh")   # raises NotLoaded
    if st["last_exit"] not in (0, None):
        raise RuntimeError(f"{st['runs']} run(s), last exit code {st['last_exit']}")
    return f"loaded, daily, {st['runs']} run(s), last exit {st['last_exit_raw']}"


def phonenumbers_present():
    """Checked in THIS interpreter, which is the one the sync scripts run under here."""
    r = subprocess.run([sys.executable, "-c", "import phonenumbers; print('ok')"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"phonenumbers missing for {sys.executable} - phones would be dropped")
    return f"importable by {os.path.basename(sys.executable)} ({sys.version.split()[0]})"


def local_no_fatal():
    """stderr for the jobs this Mac still owns, judged against the MOST RECENT run.

    The old version failed on any byte ever written to a .err file, so a single Close
    read timeout at 03:03 kept this report red through every clean run afterwards, and
    a monitor that cries wolf gets ignored. An error only counts if nothing has
    succeeded since it was written: stdout newer than stderr means at least one good
    run has happened on top of it, which makes it history, not a live fault.

    Retired jobs are deliberately out of scope - their errors belong to GitHub Actions
    now, and the CI run checks above are what judge them.
    """
    checked, stale, live = 0, [], []
    for label, short, p, _path in local_plists():
        try:
            job_status(label)
        except NotLoaded:
            continue                                  # retired; the CI checks cover its work
        err, out = p.get("StandardErrorPath"), p.get("StandardOutPath")
        if not err or not os.path.exists(err):
            continue
        checked += 1
        if os.path.getsize(err) == 0:
            continue
        lines = [l for l in open(err, errors="replace").read().strip().splitlines() if l.strip()]
        tail = lines[-1][:160] if lines else "(whitespace only)"
        e_at = os.path.getmtime(err)
        o_at = os.path.getmtime(out) if out and os.path.exists(out) else 0
        if o_at > e_at + 1:
            stale.append(f"{short} (error {_human(int(time.time()-e_at))} old, superseded by a "
                         f"clean run {_human(int(time.time()-o_at))} ago)")
        else:
            live.append(f"{short}: {tail}")
    if live:
        raise RuntimeError(f"{len(live)} live error(s) with no successful run since: "
                           + "; ".join(live))
    if checked == 0:
        raise CannotCheck("no loaded local job declares a stderr path - nothing could be judged")
    note = ("; " + str(len(stale)) + " stale, already superseded: " + ", ".join(stale)) if stale else ""
    return f"{checked} loaded local job(s) clean{note}"


# ---- live Skool reads --------------------------------------------------------
# One pull, shared. Cached only on success: a remembered empty list is exactly the
# "no data reads as healthy" trap this file exists to prevent.

_members_cache = {}


def _load_repo():
    sys.path.insert(0, os.path.abspath(REPO))
    os.environ.setdefault("CLOSE_API_KEY", CLOSE_KEY)
    os.environ.setdefault("GHL_PIT", GHL_PIT)
    os.environ.setdefault("SKOOL_COOKIE_FILE", os.path.join(CLIENT_SECRETS, "skool_cookie"))


def skool_members(pages):
    if pages in _members_cache:
        return _members_cache[pages]
    _load_repo()
    from skool_to_ghl import pull_skool
    ms = pull_skool(pages)
    if not ms:
        raise RuntimeError(f"Skool returned 0 members over {pages} page(s) - the members read "
                           f"is broken, and an empty list must not read as 'nothing wrong'")
    _members_cache[pages] = ms
    return ms


def malformed_emails():
    """Skool members whose join-question email is unusable, read LIVE from Skool.

    It used to grep ~/Library/Logs/dealengine/skool-ghl-sync.log with re.search, which
    returns the FIRST match in the file - the oldest reading ever taken, not the current
    one - and that log is frozen now anyway, because skool_to_ghl.py is not part of the
    CI loop. Counting against Skool itself cannot go stale.
    """
    ms = skool_members(MALFORMED_PAGES)
    from skool_to_ghl import EMAIL_RE
    bad = [m["email"] for m in ms if m.get("email") and not EMAIL_RE.match(m["email"])]
    none = sum(1 for m in ms if not m.get("email"))
    if bad:
        return (f"{len(bad)} need manual fix of {len(ms)} read: {bad[:4]}"
                + (f" (+{none} with no email at all)" if none else ""))
    return f"0 of {len(ms)} read (pages {MALFORMED_PAGES}); {none} with no email at all"


def slack_can_post():
    """Auth AND channel membership. A token that authenticates but is not in the channel
    fails only at post time, which is exactly when nobody is watching."""
    tok = _slack_token()
    if not tok:
        raise RuntimeError("no SLACK_BOT_TOKEN on disk - nothing would be announced")
    import urllib.request
    def api(method, **q):
        url = "https://slack.com/api/" + method + ("?" + urllib.parse.urlencode(q) if q else "")
        r = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
        return json.load(urllib.request.urlopen(r, timeout=20))
    a = api("auth.test")
    if not a.get("ok"):
        raise RuntimeError(f"auth.test: {a.get('error')}")
    names = []
    # Both channels: the lead feed AND the ops channel the alarms go to. Being in one and
    # not the other is the worst shape - joins post fine while every alarm is rejected.
    for cid in (SLACK_CHANNEL, SLACK_ALERT_CHANNEL):
        c = api("conversations.info", channel=cid)
        if not c.get("ok"):
            raise RuntimeError(f"conversations.info {cid}: {c.get('error')}")
        ch = c["channel"]
        if not ch.get("is_member"):
            raise RuntimeError(f"bot is not in #{ch.get('name')} - posts would be rejected")
        names.append("#" + ch.get("name"))
    return f"{a.get('team')} -> {' + '.join(names)}"


def every_lead_announced():
    """The end-to-end question: is any recent Skool member sitting in Close without
    having been announced? Checked against live Close data, not against a log line."""
    ms = skool_members(MALFORMED_PAGES)[:60]
    import reconcile_skool as R, notify_slack as N
    from skool_to_ghl import EMAIL_RE
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
    unreadable = 0
    stale, aged = [], 0
    for m in ms:
        if not (m["email"] and EMAIL_RE.match(m["email"])):
            continue
        j = m.get("joined")
        try:
            dt = datetime.datetime.fromisoformat((j or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if dt > cutoff:
            continue                      # too new; the next scan has not run yet
        aged += 1
        lead = R.in_close(m["email"])
        # reconcile_skool now returns a SEARCH_FAILED sentinel rather than None when the
        # Close search itself fails, precisely so a failure is not read as "not in Close".
        # It is an opaque object(), so subscripting it below raised
        # TypeError: 'object' object is not subscriptable and took the whole check down.
        if lead is getattr(R, "SEARCH_FAILED", object()):
            unreadable += 1
            continue
        if not lead:
            continue
        # Close's search index lags writes, so the notified stamp must be read back by
        # id (strongly consistent) or this check invents unannounced members.
        fresh = R.lead_by_id(lead["id"]) or lead
        if not N.already_notified(fresh):
            stale.append(m["email"])
    if stale:
        raise RuntimeError(f"{len(stale)} member(s) in Close but never announced: {stale[:4]}")
    if aged == 0:
        raise CannotCheck("not one of the 60 newest members is older than 3 h - nothing was "
                          "actually testable, so this is not a pass")
    if unreadable:
        raise CannotCheck(f"{unreadable} of {aged} Close searches failed - cannot say whether "
                          f"those members were announced")
    return f"0 unannounced of {aged} older than 3 h (60 newest checked)"


# ---- cadence -----------------------------------------------------------------

# One overnight with the lid shut is normal; ~18h without a run is not.
LAPTOP_STALE_SEC = 18 * 3600


def cadence_verdict(name, nominal, runs, elapsed, age):
    """None when the job is keeping to its schedule, else why it is not.

    Two independent ways to be wrong:
      late  - nothing has run recently (a 15-minute job that last ran 12 hours ago)
      slow  - it does run, but far less often than the schedule says (the 24-42%
              band that every previous check happily reported as PASS)
    """
    if age is not None and elapsed > nominal and age > nominal * 2 + 300:
        return f"{name} last ran {_human(int(age))} ago (every {_human(nominal)})"
    expected = int(elapsed // nominal)
    # These are launchd jobs on a LAPTOP. StartInterval does not catch up windows missed
    # while the machine slept - it fires once on wake - so the percentage below measures how
    # much of the day the lid was open, not whether the job works. It read 13% (2 of 15) for
    # a job that had run at 01:05 and correctly posted its alarm. Failing on that trains
    # everyone to ignore a red health check, which is the opposite of the point.
    #
    # The honest signal for a local job is the `late` arm above: has it run RECENTLY. That
    # still catches a job that is genuinely dead. The percentage is reported, not enforced.
    if expected >= 5 and runs / float(expected) < 0.7:
        return None if age is not None and age < LAPTOP_STALE_SEC else (
            f"{name} last ran {_human(int(age))} ago (every {_human(nominal)}) and the Mac "
            f"has not been awake enough to catch up")
    # launchd's run counter resets whenever the job is reloaded, while the anchor below is
    # older - so a job that demonstrably ran (fresh log) can report 0 runs across many
    # windows and fail for a reason that has nothing to do with the job. If the counter says
    # "never" but the log says otherwise, the counter is the thing that is wrong; fall back
    # to the log-age arm above, which already catches a genuinely dead job.
    if runs == 0 and age is not None and age < nominal * 2 + 300:
        return None
    # Five windows, not three. launchd skips StartInterval firings while the Mac is asleep
    # and runs once on wake, so a lid-closed night makes any local job look 60-70%. At three
    # windows a single skip is a FAIL (2/3 = 67%), which would fire the 3-hourly Slack alarm
    # on ordinary laptop behaviour and train everyone to ignore it. The 24-42% band this
    # check exists to catch shows up at any window size.
    return None


def run_frequency():
    """Is every launchd job that is STILL LOADED running as often as its plist says?

    "loaded, last exit 0" is not a cadence. Four jobs were covering 24-42% of their
    nominal schedule while all 19 checks reported PASS, because nothing ever compared
    runs against elapsed time.

    An unloaded plist is not a cadence failure - there is no cadence - but it is not
    silently ignored either. If its work has a known new home it is reported as INFO
    below; if it does not, the work has simply stopped and that IS a failure.
    """
    now = time.time()
    late, slow, moved, orphan, ok = [], [], [], [], 0
    for label, short, p, path in local_plists():
        nominal = _schedule_seconds(p)
        try:
            st = job_status(label)
        except NotLoaded:
            (moved if short in MOVED_TO else orphan).append(short)
            continue
        if not nominal:
            continue                                  # on-demand job, no cadence to hold it to
        logs = [p.get("StandardOutPath"), p.get("StandardErrorPath")]
        stamps = [os.path.getmtime(f) for f in logs if f and os.path.exists(f)]
        births = [getattr(os.stat(f), "st_birthtime", os.path.getctime(f))
                  for f in logs if f and os.path.exists(f)]
        # Anchor on the later of "plist last written" (a rewrite reloads the job and
        # resets launchd's run counter) and "log first written".
        anchor = max([os.path.getmtime(path)] + ([min(births)] if births else []))
        elapsed = max(0.0, now - anchor)
        age = (now - max(stamps)) if stamps else None
        bad = cadence_verdict(short, nominal, st["runs"], elapsed, age)
        if bad:
            (late if "last ran" in bad else slow).append(bad)
        else:
            ok += 1

    note = f"; {len(moved)} retired plist(s) on disk, see INFO below" if moved else ""
    if orphan:
        raise RuntimeError(f"{len(orphan)} plist(s) unloaded with no known replacement - that "
                           f"work has simply stopped: {', '.join(orphan)}{note}")
    problems = late + slow
    if problems:
        raise RuntimeError(f"{len(problems)} job(s) off schedule, {ok} on schedule: "
                           + "; ".join(problems) + note)
    return f"{ok} loaded job(s) at their nominal cadence{note}"


def report_retired():
    """Name every retired plist and where its work actually runs now.

    Informational on purpose. A plist sitting on disk is what makes people believe a
    job is still covered, so it is never silently skipped - but it is not a failure
    either, because the work moved rather than stopped.
    """
    try:
        for label, short, _p, _path in local_plists():
            if short not in MOVED_TO:
                continue
            try:
                job_status(label)
            except NotLoaded:
                info(short, f"plist on disk, unloaded -> {MOVED_TO[short]}")
    except Exception as e:
        print(f"  FAIL  {'retired job inventory':34} {type(e).__name__}: {str(e)[:200]}")
        fails.append("retired job inventory")


RELAY = os.environ.get("RELAY_BASE", "https://de-funnel-relay.vercel.app")


def _relay_status(path, method="POST", body="{}"):
    args = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", method,
            "-H", "content-type: application/json", RELAY + path]
    if method == "POST":
        args[-1:] = ["-d", body, RELAY + path]
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def close_webhook_live():
    """The Close -> Slack no-show pipe. Before 21 Sept the only subscription pointed at
    hooks.zapier.com and had been PAUSED with permission_revoked since 15 Sept, which is
    indistinguishable from 'nobody no-showed' unless something checks."""
    auth = "Basic " + base64.b64encode((CLOSE_KEY + ":").encode()).decode()
    out = subprocess.run(["curl", "-s", "-H", f"Authorization: {auth}",
                          "https://api.close.com/api/v1/webhook/"],
                         capture_output=True, text=True).stdout
    try:
        body = json.loads(out)
    except Exception:
        raise CannotCheck(f"Close /webhook/ returned unparseable output: {out[:120]}")
    # A failed read is NOT an absence. `.get("data", [])` on {"error": "Unauthorized"} is []
    # and used to be reported as "Close has NO webhook subscription - no-show alerts are
    # dead", which is a page-someone-at-3am alarm raised by a transient 401. Observed for
    # real on 23 Sept: both CRM calls 401'd for one run and were fine seconds later.
    if not isinstance(body, dict) or "data" not in body:
        raise CannotCheck(f"could not read Close webhooks: {str(body)[:120]}")
    subs = body["data"]
    if not subs:
        raise RuntimeError("Close has NO webhook subscription - no-show alerts are dead")
    ours = [w for w in subs if "de-funnel-relay" in (w.get("url") or "")]
    if not ours:
        raise RuntimeError(f"{len(subs)} subscription(s) but none point at the relay: "
                           + ", ".join((w.get('url') or '')[:40] for w in subs))
    bad = [w for w in ours if w.get("status") != "active"]
    if bad:
        raise RuntimeError(f"relay subscription is {bad[0].get('status')} "
                           f"({bad[0].get('pause_reason')})")
    zap = [w for w in subs if "zapier" in (w.get('url') or '')]
    extra = f"; {len(zap)} stale Zapier subscription(s) still present" if zap else ""
    delivered = sum((w.get("counters") or {}).get("first_success", 0) for w in ours)
    return f"active, {int(delivered)} delivered{extra}"


def stripe_webhook_armed():
    """401 on an unsigned POST is the proof STRIPE_WEBHOOK_SECRET is actually bound. A 200
    means the endpoint is live but accepting forged payments from anyone who knows the URL."""
    code = _relay_status("/api/stripe")
    if code == "401":
        return "signature enforced (unsigned POST -> 401)"
    if code == "200":
        raise RuntimeError("endpoint is UNAUTHENTICATED - STRIPE_WEBHOOK_SECRET not bound")
    raise RuntimeError(f"unexpected status {code}")


def close_hook_armed():
    code = _relay_status("/api/close-hook")
    if code == "401":
        return "signature enforced (unsigned POST -> 401)"
    if code == "200":
        raise RuntimeError("endpoint is UNAUTHENTICATED - CLOSE_WEBHOOK_SECRET not bound")
    raise RuntimeError(f"unexpected status {code}")


def calendly_webhook_state():
    """Reported, not silently skipped. The page hook covers in-browser bookings; what is
    missing is the server-side net for e-mail-link bookings, reschedules and cancellations."""
    token = os.environ.get("CALENDLY_PAT", "").strip()
    if not token:
        pat = os.path.join(CLIENT_SECRETS, "calendly_pat")
        if not os.path.exists(pat):
            raise RuntimeError("no CALENDLY_PAT secret and no PAT on disk - /api/calendly can never fire")
        token = open(pat).read().strip()
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
    me = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                         "-H", f"User-Agent: {ua}", "https://api.calendly.com/users/me"],
                        capture_output=True, text=True).stdout
    try:
        org = json.loads(me)["resource"]["current_organization"]
    except Exception:
        raise RuntimeError(f"PAT rejected: {me[:120]}")
    q = urllib.parse.quote(org)
    subs = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {token}",
                           "-H", f"User-Agent: {ua}",
                           f"https://api.calendly.com/webhook_subscriptions?organization={q}"
                           f"&scope=organization&count=20"],
                          capture_output=True, text=True).stdout
    n = len(json.loads(subs).get("collection", [])) if subs.strip().startswith("{") else 0
    if n:
        return f"{n} subscription(s) on org {org.rsplit('/', 1)[-1][:8]}"
    raise RuntimeError("PAT valid but 0 subscriptions - Calendly free plan blocks the "
                       "webhook API (403 'upgrade to Standard'), and this org has 0 events. "
                       "Booked calls rely on the page hook only.")


def stripe_enrichment_state():
    """Reads the relay's `optional.stripeSecretKey` flag - outside `config` on purpose, so
    relay_health() does not fail on it. See de-funnel-relay/STRIPE-SETUP.md."""
    out = subprocess.run(["curl", "-s", "--max-time", "25", RELAY_HEALTH],
                         capture_output=True, text=True, timeout=45).stdout
    d = json.loads(out)
    opt = d.get("optional") or {}
    if opt.get("stripeSecretKey") is True:
        return "set - invoice/PI dedupe exact, cards carry product + card + receipt"
    return ("NOT SET - payments still post; invoice failures thread under the PI card; "
            "create a restricted read-only key in a normal browser (STRIPE-SETUP.md)")


print("DealEngine automation health\n")
print(" CRM APIs")
check("GHL API (Cloudflare + auth)", ghl_reachable)
check("Close API", close_reachable)
print("\n Skool -> CRM sync (GitHub Actions)")
check("sync workflow enabled", sync_workflow_enabled)
check("last sync run", sync_last_run)
check("chain cadence (self-dispatch)", sync_chain_alive)
check("keepalive (cron anti-disable)", keepalive_workflow)
check("maintenance workflow (standby)", maintenance_workflow)
print("\n Once-a-day jobs (atomic claim refs)")
check("60-page deep scan  04:00", lambda: slot_check("deepscan"))
check("pipeline hygiene   06:20", lambda: slot_check("hygiene"))
check("daily join count   07:30", lambda: slot_check("digest"))
check("weekly digest  Mon 08:00", lambda: slot_check("weekly"))
print("\n Real-time intake (Vercel relay)")
check("relay /api/health", relay_health)
check("Close no-show webhook", close_webhook_live)
check("/api/close-hook signature", close_hook_armed)
check("/api/stripe signature", stripe_webhook_armed)
# INFO, not a check. Without STRIPE_SECRET_KEY the relay still posts every payment and
# failure; cards are thinner and an invoice failure may double up as a thread reply.
# Creating the key is a hands-on-keyboard step (Stripe's email-link verification fails
# under automation), so failing every run here would only train people to mute the alarm.
try:
    info("Stripe read-only key", stripe_enrichment_state())
except Exception as _e:
    info("Stripe read-only key", f"UNKNOWN - {str(_e)[:200]}")
# INFO, not a check. It is a genuine gap, but a KNOWN one that is blocked on a Calendly
# plan upgrade and on finding the org that owns the live booking link. Failing every run
# would make the 3-hourly Slack alarm cry wolf until someone muted it, and then the real
# failures would be muted too. Revisit the moment a paid-org PAT exists.
try:
    info("Calendly webhook", calendly_webhook_state())
except Exception as _e:
    info("Calendly webhook", f"NOT WIRED - {str(_e)[:200]}")
print("\n Skool credential (the one this process syncs with)")
check("Skool session cookie", skool_cookie_live)
check("phonenumbers dependency", phonenumbers_present)
print("\n Skool -> Slack")
check("malformed Skool emails", malformed_emails)
check("Slack auth + channel", slack_can_post)
check("every lead announced", every_lead_announced)
if ON_MAC:
    print("\n Still local to this Mac")
    check("cookie auto-refresh", cookie_refresher_scheduled)
    check("no fatal errors", local_no_fatal)
    print("\n Cadence")
    check("run frequency vs schedule", run_frequency)
    report_retired()
else:
    print("\n Mac-only jobs")
    info("launchd checks", "skipped - not on the Mac. The cookie auto-refresh is Mac-only and "
                           "optional: the secret validated above is what the cloud syncs with, "
                           "and it fails this report a month before it expires.")

print()
if fails:
    print(f"UNHEALTHY - {len(fails)} check(s) failed: {', '.join(fails)}")
    sys.exit(1)
print("HEALTHY - mirror is alive and writing.")
