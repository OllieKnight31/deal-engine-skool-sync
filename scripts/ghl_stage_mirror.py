#!/usr/bin/env python3
"""
Propagate pipeline STAGE changes from GoHighLevel into Close.

Why this exists
---------------
`ghl_dfy_to_close.py` mirrors people into Close, but it only ever *creates* a card. It never
touches the stage of a card that already exists. So the two boards agree at the moment a lead
arrives and drift apart the instant anything moves. Measured 21 Sept 2026:

    Skool Free Community          GHL   CLOSE
    New Member                    279     349
    Welcome DM Sent                65       0   <-- 65 people had visibly progressed in GHL
    Historic (pre-Sept)           293     311       and Close still showed them untouched

A GHL workflow advances New Member -> Welcome DM Sent about 4 seconds after a card is created,
so the gap grows every single day (it was 39 the previous afternoon, 65 the next morning).
Anyone reading Close would conclude nobody had ever been worked.

Direction and authority
-----------------------
GoHighLevel is the system of record for the two mirrored boards - the workflows live there and
the team works there. Propagation is strictly **one-way, GHL -> Close**. This script never
writes to GoHighLevel.

The guards matter more than the feature
---------------------------------------
* **Never clobber a human's judgement.** A Close card sitting in a stage of type `won` or `lost`
  is a decision somebody made; it is never moved. Close carries won/lost as a stage `type` and
  GHL has no equivalent, which is exactly why this asymmetry has to be handled explicitly.
* **Only ever move forward.** Stages are compared by position. If Close is *ahead* of GHL we
  leave it alone and report the disagreement rather than dragging a card backwards.
* **Grace window.** A card whose GHL stage changed within GRACE_MINUTES is left alone, so this
  never races the real-time Vercel relay or a workflow that is still mid-flight.
* **Idempotent.** A second run writes nothing.
* **Loud on failure.** This codebase has repeatedly been bitten by helpers that return something
  falsy on error, so a dead pipe reads as "nothing to do" and exits 0. Everything here raises.
* **Dry run by default.** Writes only with --apply.

Stages are matched **by name**, resolved live from both platforms rather than hardcoded, because
the two boards were built to carry identical stage names. An unmatched name is a hard error, not
a silent skip - a renamed stage must break loudly rather than quietly stop syncing.
"""

import json, os, sys, base64, argparse, datetime, urllib.request, urllib.error, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghl_http

GHL_TOKEN   = os.environ["GHL_PIT"]
LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "2RrjNuz0M4MTFlxpW37k")
CLOSE_KEY   = os.environ["CLOSE_API_KEY"]
CLOSE_AUTH  = "Basic " + base64.b64encode((CLOSE_KEY + ":").encode()).decode()

GRACE_MINUTES = 30

# The mirrored pairs. Both were built stage-for-stage on purpose.
PAIRS = [
    {"label": "Skool Free Community",
     "ghl":   "BaP4NEFnESaozLgCJio8",
     "close": "pipe_5IV9miCDNFveEjxw0dfagM"},
    {"label": "VSL Opt-ins",
     "ghl":   "CGQb9RPdeLZ2zV1LDM3k",
     "close": "pipe_4AdHWeZv4sNS3JFPHCIZXz"},
]


class MirrorError(RuntimeError):
    pass


# ── Close HTTP ────────────────────────────────────────────────────────────────
def close(path, data=None, method=None, tries=4):
    """Raises on any non-2xx. Never returns something falsy that could read as 'no data'."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                "https://api.close.com/api/v1" + path,
                data=json.dumps(data).encode() if data is not None else None,
                method=method or ("POST" if data is not None else "GET"),
                headers={"Authorization": CLOSE_AUTH,
                         "Content-Type": "application/json",
                         "User-Agent": "curl/8.7.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise MirrorError(f"Close {method or 'GET'} {path} -> {e.code} {e.read().decode()[:300]}")
        except Exception as e:                      # transient: retry, then raise
            last = e
            time.sleep(2 * (attempt + 1))
    raise MirrorError(f"Close {method or 'GET'} {path} failed after {tries}: {last}")


def ghl(path):
    return ghl_http.get(path, GHL_TOKEN)


# ── Stage maps ────────────────────────────────────────────────────────────────
def stage_maps(pair):
    """name -> (ghl_stage_id, position) and name -> (close_status_id, position, type).

    Matched by name, live from both platforms. A name present on one side but not the other is
    fatal: it means the boards have been edited apart and silently syncing the rest would hide
    that."""
    gp = ghl(f"/opportunities/pipelines?locationId={LOCATION_ID}")["pipelines"]
    gp = next((p for p in gp if p["id"] == pair["ghl"]), None)
    if not gp:
        raise MirrorError(f"GHL pipeline {pair['ghl']} not found")

    cp = next((p for p in close("/pipeline/")["data"] if p["id"] == pair["close"]), None)
    if not cp:
        raise MirrorError(f"Close pipeline {pair['close']} not found")

    g = {s["name"].strip(): (s["id"], s["position"])
         for s in sorted(gp["stages"], key=lambda s: s["position"])}
    c = {s["label"].strip(): (s["id"], i, s.get("type", "active"))
         for i, s in enumerate(cp["statuses"])}

    only_ghl   = sorted(set(g) - set(c))
    only_close = sorted(set(c) - set(g))
    if only_ghl or only_close:
        raise MirrorError(
            f"{pair['label']}: stage names have drifted apart - "
            f"only in GHL {only_ghl}, only in Close {only_close}. "
            f"Fix the boards or update the mapping; refusing to half-sync.")
    return g, c


# ── Pulls ─────────────────────────────────────────────────────────────────────
def ghl_cards(pipeline_id, stage_by_id):
    """Every GHL card: email -> (stage_name, lastStageChangeAt)."""
    out, after = {}, None
    while True:
        u = (f"/opportunities/search?location_id={LOCATION_ID}"
             f"&pipeline_id={pipeline_id}&limit=100")
        if after:
            u += f"&startAfter={after[0]}&startAfterId={after[1]}"
        d = ghl(u)
        rows = d.get("opportunities")
        if rows is None:
            raise MirrorError("GHL opportunity search returned no 'opportunities' key")
        if not rows:
            break
        for o in rows:
            em = ((o.get("contact") or {}).get("email") or "").strip().lower()
            if not em:
                continue
            name = stage_by_id.get(o.get("pipelineStageId"))
            if not name:
                raise MirrorError(f"GHL card {o['id']} sits in unknown stage {o.get('pipelineStageId')}")
            out[em] = (name, o.get("lastStageChangeAt") or o.get("createdAt") or "")
        meta = d.get("meta") or {}
        if not meta.get("startAfterId") or len(rows) < 100:
            break
        after = (meta["startAfter"], meta["startAfterId"])
    return out


def close_index(close_pipeline):
    """email -> (opportunity_id, status_id, status_label). Built by paging, not by search."""
    leads, skip = {}, 0
    total = close("/lead/?_limit=1")["total_results"]
    while True:
        d = close(f"/lead/?_limit=100&_skip={skip}&_fields=id,contacts")
        for L in d["data"]:
            for c in L.get("contacts", []):
                for e in c.get("emails", []):
                    em = (e.get("email") or "").strip().lower()
                    if em:
                        leads.setdefault(em, L["id"])
        if not d.get("has_more"):
            break
        skip += 100
    if total and not leads:
        raise MirrorError(f"Close reports {total} leads but the email index came back empty")

    cards, skip = {}, 0
    while True:
        d = close(f"/opportunity/?_limit=100&_skip={skip}"
                  f"&_fields=id,lead_id,pipeline_id,status_id,status_label")
        for o in d["data"]:
            if o.get("pipeline_id") == close_pipeline:
                cards[o["lead_id"]] = (o["id"], o["status_id"], o["status_label"])
        if not d.get("has_more"):
            break
        skip += 100

    return {em: cards[lid] for em, lid in leads.items() if lid in cards}


# ── Sweep ─────────────────────────────────────────────────────────────────────
def sweep(pair, apply_writes, grace_minutes):
    print(f"\n=== {pair['label']} ===")
    g_by_name, c_by_name = stage_maps(pair)
    g_by_id = {sid: name for name, (sid, _) in g_by_name.items()}

    ghl_side   = ghl_cards(pair["ghl"], g_by_id)
    close_side = close_index(pair["close"])
    print(f"  GHL cards with an email: {len(ghl_side)} | Close cards matched by email: {len(close_side)}")

    now = datetime.datetime.now(datetime.timezone.utc)
    moves, held, ahead, terminal, fresh = [], 0, [], [], 0

    for em, (g_stage, changed_at) in sorted(ghl_side.items()):
        card = close_side.get(em)
        if not card:
            continue                                  # creation is ghl_dfy_to_close.py's job
        opp_id, c_status_id, c_label = card
        if c_label.strip() not in c_by_name:
            raise MirrorError(f"Close card {opp_id} in unmapped stage {c_label!r}")

        g_pos = g_by_name[g_stage][1]
        c_sid, c_pos, c_type = c_by_name[c_label.strip()]

        if g_pos == c_pos:
            held += 1
            continue
        if c_type in ("won", "lost"):
            terminal.append((em, c_label, g_stage))    # a human decided; never override
            continue
        if g_pos < c_pos:
            ahead.append((em, c_label, g_stage))       # Close further along; never go backwards
            continue

        if changed_at:
            try:
                t = datetime.datetime.fromisoformat(changed_at.replace("Z", "+00:00"))
                if (now - t).total_seconds() < grace_minutes * 60:
                    fresh += 1                         # leave it to the relay / in-flight workflow
                    continue
            except ValueError:
                pass

        moves.append((em, opp_id, c_label, g_stage, c_by_name[g_stage][0]))

    print(f"  already in step: {held} | terminal in Close (left alone): {len(terminal)} | "
          f"Close ahead of GHL: {len(ahead)} | inside {grace_minutes}m grace: {fresh}")
    if ahead:
        print("  -- Close is AHEAD of GHL on these (not touched, worth a look):")
        for em, c, g in ahead[:25]:
            print(f"       {em:<44} close={c!r} ghl={g!r}")
        if len(ahead) > 25:
            print(f"       ... and {len(ahead)-25} more")
    if terminal:
        print("  -- terminal in Close, GHL disagrees (not touched):")
        for em, c, g in terminal[:25]:
            print(f"       {em:<44} close={c!r} ghl={g!r}")
        if len(terminal) > 25:
            print(f"       ... and {len(terminal)-25} more")

    if not moves:
        print("  nothing to advance.")
        return 0, 0

    print(f"  {len(moves)} card(s) to advance:")
    for em, opp_id, c_label, g_stage, _ in moves[:40]:
        print(f"    {'APPLY' if apply_writes else 'DRY  '} {em:<44} {c_label!r} -> {g_stage!r}")
    if len(moves) > 40:
        print(f"    ... and {len(moves)-40} more")

    if not apply_writes:
        return len(moves), 0

    written = 0
    for em, opp_id, c_label, g_stage, target in moves:
        close(f"/opportunity/{opp_id}/", {"status_id": target}, method="PUT")
        # Verify by id - Close's SEARCH index lags writes by minutes and must never be
        # trusted to confirm something we just wrote.
        back = close(f"/opportunity/{opp_id}/?_fields=id,status_id,status_label")
        if back.get("status_id") != target:
            raise MirrorError(f"{opp_id}: wrote {g_stage!r} but reads back {back.get('status_label')!r}")
        written += 1
    print(f"  advanced {written} card(s), each verified by id.")
    return len(moves), written


def main():
    ap = argparse.ArgumentParser(description="Mirror GHL pipeline stages into Close (one-way).")
    ap.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    ap.add_argument("--grace-minutes", type=int, default=GRACE_MINUTES,
                    help="leave cards whose GHL stage changed this recently")
    a = ap.parse_args()

    ghl_http.preflight(GHL_TOKEN, LOCATION_ID)
    print(f"GHL -> Close stage mirror  ({'APPLY' if a.apply else 'dry run'}, "
          f"grace {a.grace_minutes}m)")

    total_pending = total_written = 0
    for pair in PAIRS:
        p, w = sweep(pair, a.apply, a.grace_minutes)
        total_pending += p
        total_written += w

    print(f"\npending: {total_pending}  written: {total_written}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MirrorError, ghl_http.GHLError) as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(2)
