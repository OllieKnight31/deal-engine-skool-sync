#!/usr/bin/env python3
"""
Close CRM phone normaliser for Skool free-community leads.

Why this exists
---------------
The Skool join-question answer for "best number to reach you at" is free text.
Mapping it straight into Close's Contact Mobile Phone made Close reject the whole
lead whenever the answer was prose ("Nc", "Around 8pm...") - the Zap errored and the
lead was lost entirely. The Zap now writes the raw answer into the text custom field
"Skool Phone Answer" (cf_EtAflDq...), which can never be rejected.

This script promotes those raw answers into a real, dialable E.164 phone number, and
repairs numbers that were previously mangled (Close prepends +1 to any international
number written with a leading national trunk zero, e.g. 0697071167 -> +10697071167).

Usage:  close_phone_normalise.py [--apply]     (default is a dry run)
"""
import json, re, csv, sys, base64, urllib.request, urllib.error, urllib.parse, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

KEY = os.environ["CLOSE_API_KEY"]
AUTH = "Basic " + base64.b64encode((KEY + ":").encode()).decode()
BASE = "https://api.close.com/api/v1"
CF_PHONE_ANSWER = "cf_EtAflDq1xeAZxVaS6pSPtSnm7Z7IbZUNlNEk5fkHu0V"   # Skool Phone Answer (text)
SKOOL_CSV = os.path.join(os.path.dirname(__file__), "..", "..", "source", "skool", "members-2026-09-17.csv")

try:
    import phonenumbers
except ImportError:
    sys.exit("phonenumbers not installed. Use the scratchpad venv or: pip install phonenumbers")

COUNTRY_TO_ISO = {
    "united states":"US","united kingdom":"GB","canada":"CA","australia":"AU","india":"IN",
    "south africa":"ZA","nigeria":"NG","kenya":"KE","ghana":"GH","zimbabwe":"ZW","pakistan":"PK",
    "france":"FR","germany":"DE","the netherlands":"NL","netherlands":"NL","spain":"ES","italy":"IT",
    "sweden":"SE","norway":"NO","denmark":"DK","ireland":"IE","poland":"PL","romania":"RO",
    "portugal":"PT","belgium":"BE","switzerland":"CH","austria":"AT","greece":"GR",
    "brazil":"BR","mexico":"MX","argentina":"AR","colombia":"CO","chile":"CL","peru":"PE",
    "philippines":"PH","japan":"JP","china":"CN","singapore":"SG","malaysia":"MY","indonesia":"ID",
    "united arab emirates":"AE","saudi arabia":"SA","israel":"IL","egypt":"EG","turkey":"TR",
    "jamaica":"JM","trinidad and tobago":"TT","puerto rico":"PR","guatemala":"GT","belize":"BZ",
    "new zealand":"NZ","czechia":"CZ","czech republic":"CZ","hungary":"HU","morocco":"MA",
}

def req(method, path, payload=None):
    r = urllib.request.Request(BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method, headers={"Authorization": AUTH, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        try: body = json.loads(e.read().decode() or "{}")
        except Exception: body = {}
        return e.code, body

def load_country_hints():
    """email -> ISO2 country.

    Prefers the live Skool read (every current member, always fresh). Falls back to a
    local Skool CSV export if one is present. Without a country hint a number written
    with a leading national zero cannot be resolved, so this is what makes the repair
    of Close's mangled "+10..." numbers possible.
    """
    hints = {}
    try:
        from skool_to_ghl import pull_skool, iso_from_location
        pages = int(os.environ.get("SKOOL_PAGES", "3"))
        for m in pull_skool(pages):
            iso = iso_from_location(m.get("location"))
            if m.get("email") and iso:
                hints[m["email"].strip().lower()] = iso
    except SystemExit:
        raise
    except Exception as e:
        print(f"  (live Skool hints unavailable: {str(e)[:80]})")
    path = os.path.abspath(SKOOL_CSV)
    if os.path.exists(path):
        for row in csv.DictReader(open(path)):
            em = (row.get("email") or "").strip().lower()
            if not em or em in hints: continue
            m2 = re.search(r"\(([^)]+)\)\s*$", (row.get("location") or "").strip())
            if not m2: continue
            iso = COUNTRY_TO_ISO.get(m2.group(1).strip().lower())
            if iso: hints[em] = iso
    return hints

def parse_phone(raw, region_hint):
    """Return E.164 or None. Tries the hint region, then US, then bare international."""
    if not raw: return None
    s = raw.strip()
    if len(re.sub(r"\D", "", s)) < 7:
        return None
    candidates = []
    if s.startswith("+"): candidates.append((s, None))
    for reg in [region_hint, "US", "GB"]:
        if reg: candidates.append((s, reg))
    for text, reg in candidates:
        try:
            n = phonenumbers.parse(text, reg)
            if phonenumbers.is_valid_number(n):
                return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            pass
    return None

def unmangle(e164, region_hint):
    """Repair Close's '+1' + leading-zero international numbers, e.g. +10697071167 (ZA)."""
    if not e164 or not e164.startswith("+10"): return None
    national = e164[2:]                      # strip '+1', keep the leading 0
    if not region_hint: return None
    try:
        n = phonenumbers.parse(national, region_hint)
        if phonenumbers.is_valid_number(n):
            return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        pass
    return None

def all_leads():
    out, skip = [], 0
    while True:
        s, d = req("GET", f"/lead/?_order_by=-date_created&_limit=100&_skip={skip}")
        if s != 200: break
        out += d.get("data", [])
        if not d.get("has_more"): break
        skip += 100
    return out

def main():
    apply_changes = "--apply" in sys.argv
    hints = load_country_hints()
    print(f"country hints loaded: {len(hints)}")
    leads = all_leads()
    print(f"leads fetched: {len(leads)}\n")

    promote, repair, stuck = [], [], []
    for l in leads:
        raw = l.get(f"custom.{CF_PHONE_ANSWER}")
        for c in l.get("contacts") or []:
            emails = [e["email"].lower() for e in c.get("emails", [])]
            hint = next((hints[e] for e in emails if e in hints), None)
            phones = c.get("phones") or []
            cur = phones[0]["phone"] if phones else None
            if not cur and raw:
                got = parse_phone(raw, hint)
                (promote if got else stuck).append((l, c, raw, got, hint))
            elif cur and (cur.startswith("+10") or len(re.sub(r"\D", "", cur)) < 9):
                fixed = unmangle(cur, hint) or (parse_phone(raw, hint) if raw else None)
                if fixed and fixed != cur:
                    repair.append((l, c, cur, fixed, hint))
                else:
                    stuck.append((l, c, cur, None, hint))

    print(f"PROMOTE (raw answer -> phone field): {len(promote)}")
    for l, c, raw, got, h in promote[:40]:
        print(f"   {l['display_name'][:26]:28} {str(raw)[:22]:24} -> {got:16} [{h or '?'}]")
    print(f"\nREPAIR (mangled -> correct):        {len(repair)}")
    for l, c, cur, fixed, h in repair[:40]:
        print(f"   {l['display_name'][:26]:28} {cur:18} -> {fixed:16} [{h or '?'}]")
    print(f"\nUNRESOLVED (needs a human):         {len(stuck)}")
    for l, c, val, _, h in stuck[:20]:
        print(f"   {l['display_name'][:26]:28} {str(val)[:40]}")

    if not apply_changes:
        print("\nDRY RUN - rerun with --apply to write these to Close.")
        return

    n = 0
    for l, c, raw, got, h in promote:
        ph = (c.get("phones") or []) + [{"type": "mobile", "phone": got}]
        s, d = req("PUT", f"/contact/{c['id']}/", {"phones": ph})
        n += 1 if s == 200 else 0
        if s != 200: print("  promote failed:", l["display_name"], s, str(d)[:120])
    m = 0
    for l, c, cur, fixed, h in repair:
        ph = [dict(p) for p in c.get("phones") or []]
        for p in ph:
            if p["phone"] == cur: p["phone"] = fixed
        s, d = req("PUT", f"/contact/{c['id']}/", {"phones": ph})
        m += 1 if s == 200 else 0
        if s != 200: print("  repair failed:", l["display_name"], s, str(d)[:120])
    print(f"\nAPPLIED: {n} promoted, {m} repaired.")

if __name__ == "__main__":
    main()
