"""
validate_join.py  --  VALIDATION ONLY. Reads existing artifacts, changes nothing.

Inputs (all already on disk, produced by prepare_data.py):
  - data/schools.geojson                         (294 matched output features)
  - data/nys_gis_charter_nyc_raw.geojson         (426 raw GIS NYC features)
  - data/nys-charter-school-directory-as-of-09-02-2025.xlsx  (NYSED directory)

We re-derive the 294 matched PAIRS directly from schools.geojson, which stores
both the NYSED "name" and the GIS "gis_name" plus instit_id, so no re-join is
required to compare names.

Stdlib only. Nothing is written or fixed.
"""

import json
import os
import re
import zipfile
import difflib
import xml.etree.ElementTree as ET

DATA_DIR = "data"
OUT_PATH = os.path.join(DATA_DIR, "schools.geojson")
XLSX_PATH = os.path.join(DATA_DIR, "nys-charter-school-directory-as-of-09-02-2025.xlsx")
SS_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

LAT_MIN, LAT_MAX = 40.47, 40.95
LON_MIN, LON_MAX = -74.30, -73.68

# tokens removed during name normalization
STOP_TOKENS = {"CHARTER", "SCHOOL", "THE", "OF", "AT", "INC"}


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def norm_name(s):
    """Uppercase, strip punctuation, collapse whitespace, drop stop tokens."""
    if s is None:
        s = ""
    s = s.upper()
    s = re.sub(r"[^A-Z0-9\s]", " ", s)          # strip punctuation
    toks = [t for t in s.split() if t and t not in STOP_TOKENS]
    return " ".join(toks)


def read_xlsx_rows(path):
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(SS_NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(SS_NS + "t")))
    root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

    def col_index(ref):
        letters = "".join(ch for ch in ref if ch.isalpha())
        n = 0
        for ch in letters:
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    rows = []
    for row in root.iter(SS_NS + "row"):
        cells = {}
        maxc = -1
        for c in row.findall(SS_NS + "c"):
            ci = col_index(c.get("r"))
            t = c.get("t")
            v = c.find(SS_NS + "v")
            isel = c.find(SS_NS + "is")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr" and isel is not None:
                val = "".join(x.text or "" for x in isel.iter(SS_NS + "t"))
            elif v is not None:
                val = v.text
            else:
                val = ""
            cells[ci] = val
            maxc = max(maxc, ci)
        rows.append([cells.get(i, "") for i in range(maxc + 1)])
    return rows


def load_nysed_nyc():
    rows = read_xlsx_rows(XLSX_PATH)
    hdr_idx = 0
    for idx, r in enumerate(rows[:5]):
        if any((c or "").strip().lower() == "county" for c in r):
            hdr_idx = idx
            break
    header = [h.strip() for h in rows[hdr_idx]]
    col = {name: i for i, name in enumerate(header)}

    def get(row, name):
        i = col.get(name)
        if i is None or i >= len(row):
            return ""
        return (row[i] or "").strip()

    out = []
    for r in rows[hdr_idx + 1:]:
        if not any((c or "").strip() for c in r):
            continue
        if get(r, "Region") == "New York City":
            out.append({
                "name": get(r, "Charter School Name"),
                "active_date": get(r, "ACTIVE_DATE"),
                "year_opened": get(r, "Year Opened"),
                "authorizer": get(r, "Authorizer"),
                "instit_id": get(r, "Institution ID"),
            })
    return out


def main():
    schools = json.load(open(OUT_PATH, encoding="utf-8"))["features"]

    # ------------------------------------------------------------------ (1)
    hr("1. NAME AGREEMENT across matched pairs (false-match detector)")
    scored = []
    for feat in schools:
        p = feat["properties"]
        nysed = p.get("name") or ""
        gis = p.get("gis_name") or ""
        a, b = norm_name(nysed), norm_name(gis)
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        coords = feat["geometry"]["coordinates"]
        scored.append((ratio, nysed, gis, p.get("instit_id"),
                       p.get("borough"), coords))

    lo = sum(1 for s in scored if s[0] < 0.6)
    mid = sum(1 for s in scored if 0.6 <= s[0] <= 0.8)
    hi = sum(1 for s in scored if s[0] > 0.8)
    print("  total matched pairs : %d" % len(scored))
    print("  ratio  < 0.6        : %d" % lo)
    print("  ratio  0.6 - 0.8    : %d" % mid)
    print("  ratio  > 0.8        : %d" % hi)
    print("\n  --- ALL pairs with ratio < 0.6 (ascending, NOT truncated) ---")
    low = sorted([s for s in scored if s[0] < 0.6], key=lambda x: x[0])
    if not low:
        print("    (none)")
    for ratio, nysed, gis, iid, boro, coords in low:
        print("    ratio=%.3f" % ratio)
        print("      NYSED : %s" % nysed)
        print("      GIS   : %s" % gis)
        print("      INSTIT_ID=%s  borough=%s  coords=%s" % (iid, boro, coords))

    # ------------------------------------------------------------------ (2)
    hr("2. COORDINATE SANITY on 294 output features")
    oob = []
    coord_groups = {}
    for feat in schools:
        p = feat["properties"]
        lon, lat = feat["geometry"]["coordinates"]
        if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
            oob.append((p.get("name"), p.get("gis_name"), [lon, lat],
                        p.get("borough")))
        coord_groups.setdefault((lon, lat), []).append(p.get("name"))

    print("  OUT-OF-BOUNDS features: %d" % len(oob))
    for name, gis, coords, boro in oob:
        print("    %s | GIS=%s | coords=%s | borough=%s"
              % (name, gis, coords, boro))

    dup_groups = {k: v for k, v in coord_groups.items() if len(v) > 1}
    print("\n  DUPLICATE-coordinate groups: %d" % len(dup_groups))
    shown = 0
    for coord, names in sorted(dup_groups.items(), key=lambda kv: -len(kv[1])):
        print("    coord=%s  (%d schools):" % (list(coord), len(names)))
        for nm in names:
            print("       - %s" % nm)
        shown += 1
        if shown == 5:
            break

    # ------------------------------------------------------------------ (3)
    hr("3. NYSED LIST HYGIENE on 299 NYC rows")
    nysed = load_nysed_nyc()
    print("  NYSED NYC rows loaded: %d" % len(nysed))

    def tally(key):
        d = {}
        for r in nysed:
            v = r.get(key, "")
            d[v] = d.get(v, 0) + 1
        return d

    print("\n  --- Year Opened (distinct values : count) ---")
    yo = tally("year_opened")
    for v, n in sorted(yo.items(), key=lambda kv: (kv[0] == "", kv[0])):
        flag = ""
        if v == "" or v is None:
            flag = "   <-- BLANK"
        else:
            try:
                if int(float(v)) > 2026:
                    flag = "   <-- LATER THAN 2026"
            except ValueError:
                flag = "   <-- non-numeric"
        print("    %-8s : %d%s" % (repr(v), n, flag))

    print("\n  --- ACTIVE_DATE (distinct values : count) ---")
    ad = tally("active_date")
    for v, n in sorted(ad.items(), key=lambda kv: -kv[1]):
        print("    %-12s : %d" % (repr(v), n))
    # crude interpretation aid: is it a single constant, a date-like serial, etc.
    nonblank = [v for v in ad if (v or "").strip()]
    blank = ad.get("", 0)
    print("  distinct non-blank ACTIVE_DATE values: %d ; blank rows: %d"
          % (len(nonblank), blank))

    print("\n  --- Authorizer (distinct values : count) ---")
    au = tally("authorizer")
    for v, n in sorted(au.items(), key=lambda kv: -kv[1]):
        print("    %-40s : %d" % (repr(v), n))

    # ------------------------------------------------------------------ (4)
    hr("4. NETWORK CHECK: SUCCESS ACADEMY in matched NYSED names")
    sa = [feat["properties"]["name"] for feat in schools
          if "SUCCESS ACADEMY" in (feat["properties"].get("name") or "").upper()]
    print("  matched features whose NYSED name contains 'SUCCESS ACADEMY': %d"
          % len(sa))
    for nm in sa[:5]:
        print("    - %s" % nm)

    # ------------------------------------------------------------------ (5)
    hr("5. VERDICT: records believed wrong (reported, NOT fixed)")
    verdicts = []
    # (a) out-of-bounds coordinates
    for name, gis, coords, boro in oob:
        verdicts.append("BAD GEOMETRY: '%s' joined to GIS '%s' at %s "
                        "(outside NYC; placed in %s)" % (name, gis, coords, boro))
    # (b) low name-agreement pairs
    for ratio, nysed_n, gis_n, iid, boro, coords in low:
        verdicts.append("LOW NAME MATCH (ratio=%.3f): NYSED '%s' vs GIS '%s' "
                        "(INSTIT_ID=%s)" % (ratio, nysed_n, gis_n, iid))
    if not verdicts:
        print("  (no records flagged)")
    for line in verdicts:
        print("  - %s" % line)


if __name__ == "__main__":
    main()
