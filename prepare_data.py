"""
prepare_data.py

Build data/schools.geojson for the NYC charter-schools map.

Data strategy (decided during recon):
  - AUTHORITATIVE SCHOOL LIST : NYSED Charter Schools Directory
        data/nys-charter-school-directory-as-of-09-02-2025.xlsx
        (one row per charter school; header is on the 2nd row)
  - GEOMETRY (point locations)  : NYS GIS "Charter K-12" feature layer
        https://nysgeohub.ny.gov/arcgis/rest/services/Education/NYS_Schools/FeatureServer/3

JOIN: INSTIT_ID alone produced 8 false matches (the GIS SED_CODE duplicates
INSTIT_ID, so there is no independent second key). We therefore use a GUARDED
TWO-PASS JOIN, per NYSED NYC row:

  PASS 1 (ID + name guard): if a GIS feature shares the INSTIT_ID AND the
      normalized name-similarity ratio >= 0.60, accept it (match_method="id").
  PASS 2 (name rescue): otherwise score the NYSED name against ALL GIS NYC
      features and accept the single best only if ratio >= 0.85
      (match_method="name").
  OTHERWISE: unmatched -> documented gap (never guess, never fall back to the
      rejected ID match).

Extra guards:
  - a GIS feature may back at most ONE NYSED school (collisions resolved by the
    higher ratio; the loser becomes unmatched);
  - after matching, any feature outside the NYC bounding box is dropped.

HARD RULES obeyed: stdlib only (urllib + json + zipfile + xml + difflib);
nothing invented or hand-edited; on a failed fetch we raise rather than
substitute another source.
"""

import json
import os
import re
import zipfile
import difflib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------
DATA_DIR = "data"
XLSX_PATH = os.path.join(DATA_DIR, "nys-charter-school-directory-as-of-09-02-2025.xlsx")
GIS_RAW_PATH = os.path.join(DATA_DIR, "nys_gis_charter_nyc_raw.geojson")
OUT_PATH = os.path.join(DATA_DIR, "schools.geojson")

GIS_QUERY_URL = (
    "https://nysgeohub.ny.gov/arcgis/rest/services/"
    "Education/NYS_Schools/FeatureServer/3/query"
)
WHERE_CLAUSE = "COUNTY_DESC IN ('BRONX','KINGS','NEW YORK','QUEENS','RICHMOND')"

# COUNTY_DESC (GIS, uppercase) -> borough display name
BOROUGH_BY_COUNTY = {
    "BRONX": "Bronx",
    "KINGS": "Brooklyn",
    "NEW YORK": "Manhattan",
    "QUEENS": "Queens",
    "RICHMOND": "Staten Island",
}

# NYC bounding box (rough) used to drop suspect coordinates after matching.
LAT_MIN, LAT_MAX = 40.47, 40.95
LON_MIN, LON_MAX = -74.30, -73.68

# Match thresholds
PASS1_MIN = 0.60   # ID match is only trusted if name agreement >= this
PASS2_MIN = 0.85   # name rescue must be at least this strong

PAGE_SIZE = 1000
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
SS_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# tokens removed during name normalization (same set as validate_join.py)
STOP_TOKENS = {"CHARTER", "SCHOOL", "THE", "OF", "AT", "INC"}

# GIS LEGAL_NAMEs we were explicitly asked to confirm are no longer mis-joined.
WATCH_NAMES = {
    "MINISINK CHARTER SCHOOL",
    "STRIVE CHARTER SCHOOL",
    "BRONX CHARTER SCHOOL FOR EXCELLENCE 6",
    "BRONX CHARTER SCHOOL FOR EXCELLENCE 7",
    "BRONX CHARTER SCHOOL FOR EXCELLENCE 8",
    "BRONX CHARTER SCHOOL FOR EXCELLENCE 9",
}


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def norm_name(s):
    """Uppercase, strip punctuation, collapse whitespace, drop stop tokens."""
    if s is None:
        s = ""
    s = s.upper()
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
    toks = [t for t in s.split() if t and t not in STOP_TOKENS]
    return " ".join(toks)


def norm_key(value):
    """Normalize a join id: str + strip whitespace + drop a trailing '.0'."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# HTTP helper (browser User-Agent; stdlib urllib only)
# ---------------------------------------------------------------------------
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Step 1a: fetch GIS NYC features (with paging)
# ---------------------------------------------------------------------------
def fetch_gis_nyc_features():
    hr("STEP 1a: FETCH GIS NYC FEATURES (NYS 'Charter K-12')")
    all_features = []
    offset = 0
    pages = 0
    while True:
        params = {
            "where": WHERE_CLAUSE,
            "outFields": "*",
            "f": "geojson",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
        }
        url = GIS_QUERY_URL + "?" + urllib.parse.urlencode(params)
        doc = json.loads(http_get(url))
        if "error" in doc:
            raise RuntimeError("GIS query returned an error: %r" % (doc["error"],))
        feats = doc.get("features", [])
        pages += 1
        exceeded = bool(doc.get("exceededTransferLimit", False))
        print("  page %d: offset=%d fetched=%d exceededTransferLimit=%s"
              % (pages, offset, len(feats), exceeded))
        all_features.extend(feats)
        if feats and (exceeded or len(feats) == PAGE_SIZE):
            offset += len(feats)
            continue
        break
    print("  TOTAL pages: %d" % pages)
    print("  TOTAL features fetched: %d" % len(all_features))

    merged = {"type": "FeatureCollection", "features": all_features}
    with open(GIS_RAW_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f)
    print("  cached raw GeoJSON -> %s" % GIS_RAW_PATH)
    return all_features


# ---------------------------------------------------------------------------
# Step 1b: parse the NYSED directory xlsx (stdlib zipfile + xml)
# ---------------------------------------------------------------------------
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


def parse_nysed_nyc():
    hr("STEP 1b: PARSE NYSED DIRECTORY (keep Region == 'New York City')")
    rows = read_xlsx_rows(XLSX_PATH)
    hdr_idx = 0
    for idx, r in enumerate(rows[:5]):
        if any((c or "").strip().lower() == "county" for c in r):
            hdr_idx = idx
            break
    header = [h.strip() for h in rows[hdr_idx]]
    col = {name: i for i, name in enumerate(header)}
    print("  header row index: %d" % hdr_idx)
    print("  columns: %s" % header)

    def get(row, name):
        i = col.get(name)
        if i is None or i >= len(row):
            return ""
        return (row[i] or "").strip()

    nyc_rows = []
    for r in rows[hdr_idx + 1:]:
        if not any((c or "").strip() for c in r):
            continue
        if get(r, "Region") == "New York City":
            nyc_rows.append({
                "name": get(r, "Charter School Name"),
                "district": get(r, "School District"),
                "county": get(r, "County"),
                "region": get(r, "Region"),
                "year_opened": get(r, "Year Opened"),
                "authorizer": get(r, "Authorizer"),
                "instit_id": get(r, "Institution ID"),
                "sed_code": get(r, "SED Code"),
            })
    print("  NYSED rows with Region == 'New York City': %d" % len(nyc_rows))
    return nyc_rows


# ---------------------------------------------------------------------------
# Build: guarded two-pass join + report
# ---------------------------------------------------------------------------
def build():
    gis = fetch_gis_nyc_features()
    nysed = parse_nysed_nyc()

    # Precompute GIS helpers.
    gis_norm = [norm_name((f.get("properties") or {}).get("LEGAL_NAME")) for f in gis]
    gis_by_instit = {}
    for i, f in enumerate(gis):
        k = norm_key((f.get("properties") or {}).get("INSTIT_ID"))
        if k:
            gis_by_instit.setdefault(k, i)  # INSTIT_ID is unique in this layer

    hr("STEP 1c: GUARDED TWO-PASS JOIN")
    print("  PASS 1 accepts an INSTIT_ID match only if name ratio >= %.2f" % PASS1_MIN)
    print("  PASS 2 rescues by name only if best ratio >= %.2f" % PASS2_MIN)

    # candidate[nysed_index] = (gis_index, method, ratio)
    candidates = {}
    for ni, row in enumerate(nysed):
        nm = norm_name(row["name"])
        ik = norm_key(row["instit_id"])

        # PASS 1: ID + name guard
        gi = gis_by_instit.get(ik)
        if gi is not None:
            r = ratio(nm, gis_norm[gi])
            if r >= PASS1_MIN:
                candidates[ni] = (gi, "id", r)
                continue  # accepted by id

        # PASS 2: name rescue against ALL GIS NYC features
        best_i, best_r = None, -1.0
        for gj in range(len(gis)):
            r = ratio(nm, gis_norm[gj])
            if r > best_r:
                best_r, best_i = r, gj
        if best_i is not None and best_r >= PASS2_MIN:
            candidates[ni] = (best_i, "name", best_r)
        # else: no candidate -> unmatched

    # Constraint: each GIS feature backs at most one NYSED school.
    claims = {}  # gis_index -> list of (nysed_index, method, ratio)
    for ni, (gi, method, r) in candidates.items():
        claims.setdefault(gi, []).append((ni, method, r))

    collisions = []          # (gis_index, winner_tuple, [loser_tuples])
    accepted = {}            # nysed_index -> (gis_index, method, ratio)
    for gi, claimants in claims.items():
        if len(claimants) == 1:
            ni, method, r = claimants[0]
            accepted[ni] = (gi, method, r)
        else:
            ordered = sorted(claimants, key=lambda t: t[2], reverse=True)
            win = ordered[0]
            accepted[win[0]] = (gi, win[1], win[2])
            collisions.append((gi, win, ordered[1:]))  # losers -> unmatched

    # Out-of-bounds drop (after matching).
    oob_drops = []           # (nysed_index, gis_index, coords)
    final = {}               # nysed_index -> (gis_index, method, ratio)
    for ni, (gi, method, r) in accepted.items():
        geom = gis[gi].get("geometry") or {}
        coords = geom.get("coordinates")
        ok = (coords and len(coords) >= 2 and coords[0] is not None
              and coords[1] is not None)
        if ok:
            lon, lat = coords[0], coords[1]
            if LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX:
                final[ni] = (gi, method, r)
            else:
                oob_drops.append((ni, gi, [lon, lat]))
        else:
            oob_drops.append((ni, gi, coords))

    # ------------------------------------------------------------------ build
    out_features = []
    for ni, (gi, method, r) in sorted(final.items()):
        row = nysed[ni]
        p = gis[gi].get("properties") or {}
        geom = gis[gi].get("geometry") or {}
        c = geom.get("coordinates")
        coords = [round(float(c[0]), 6), round(float(c[1]), 6)]
        addr_parts = [p.get("PHYSADDRLINE1") or "", p.get("PHYSADDRLINE2") or ""]
        address = " ".join(x.strip() for x in addr_parts if x and x.strip())
        county = (p.get("COUNTY_DESC") or "").strip()
        out_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coords},
            "properties": {
                "name": row["name"],
                "gis_name": p.get("LEGAL_NAME"),
                "address": address,
                "city": (p.get("PHYSCITY") or "").strip(),
                "zip": (p.get("PHYSZIPCD5") or "").strip(),
                "borough": BOROUGH_BY_COUNTY.get(county, county),
                "district": row["district"],
                "year_opened": row["year_opened"],
                "authorizer": row["authorizer"],
                "instit_id": norm_key(p.get("INSTIT_ID")),
                "sed_code": norm_key(p.get("SED_CODE")),
                "match_method": method,
                "name_ratio": round(r, 3),
            },
        })

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": out_features}, f, indent=2)

    # Unmatched NYSED rows (anything not in final), with a reason.
    oob_nis = {ni for ni, _, _ in oob_drops}
    collision_loser_nis = {lo[0] for _, _, losers in collisions for lo in losers}
    unmatched = []
    for ni, row in enumerate(nysed):
        if ni in final:
            continue
        if ni in oob_nis:
            reason = "dropped: out-of-bounds geometry"
        elif ni in collision_loser_nis:
            reason = "collision loser"
        else:
            reason = "no qualifying match"
        unmatched.append((ni, row, reason))

    # =================================================================== report
    hr("REPORT")

    n_id = sum(1 for v in final.values() if v[1] == "id")
    n_name = sum(1 for v in final.values() if v[1] == "name")
    print("(a) COUNTS")
    print("    matched via id   : %d" % n_id)
    print("    matched via name : %d" % n_name)
    print("    unmatched        : %d" % len(unmatched))

    print("\n(b) COMPLETE UNMATCHED NYSED NYC LIST (README known-gaps):")
    if not unmatched:
        print("    (none)")
    for ni, row, reason in sorted(unmatched, key=lambda t: t[1]["name"]):
        print("    - %s | INSTIT_ID=%s | %s"
              % (row["name"], row["instit_id"], reason))

    print("\n(c) PASS-2 NAME RESCUES:")
    rescues = [(ni, v) for ni, v in final.items() if v[1] == "name"]
    if not rescues:
        print("    (none)")
    for ni, (gi, method, r) in sorted(rescues, key=lambda t: -t[1][2]):
        print("    ratio=%.3f  NYSED='%s'  GIS='%s'"
              % (r, nysed[ni]["name"], (gis[gi].get("properties") or {}).get("LEGAL_NAME")))

    print("\n(d) COLLISIONS RESOLVED:")
    if not collisions:
        print("    (none)")
    for gi, win, losers in collisions:
        legal = (gis[gi].get("properties") or {}).get("LEGAL_NAME")
        wni, wmethod, wr = win
        print("    GIS '%s' (INSTIT_ID=%s)"
              % (legal, norm_key((gis[gi].get('properties') or {}).get('INSTIT_ID'))))
        print("        WINNER : '%s' (method=%s ratio=%.3f)"
              % (nysed[wni]["name"], wmethod, wr))
        for lni, lmethod, lr in losers:
            print("        loser  : '%s' (method=%s ratio=%.3f) -> unmatched"
                  % (nysed[lni]["name"], lmethod, lr))

    print("\n(e) OUT-OF-BOUNDS DROPS (after matching):")
    if not oob_drops:
        print("    (none)")
    for ni, gi, coords in oob_drops:
        legal = (gis[gi].get("properties") or {}).get("LEGAL_NAME")
        print("    NYSED='%s' <- GIS='%s' coords=%s  (outside lat %.2f-%.2f / lon %.2f-%.2f)"
              % (nysed[ni]["name"], legal, coords, LAT_MIN, LAT_MAX, LON_MIN, LON_MAX))

    print("\n(f) WATCHED FORMERLY-WRONG GIS FEATURES - current status:")
    # map gis_index -> accepted/final status
    for gi, legal in sorted((i, (gis[i].get("properties") or {}).get("LEGAL_NAME"))
                            for i in range(len(gis))
                            if (gis[i].get("properties") or {}).get("LEGAL_NAME") in WATCH_NAMES):
        # find any NYSED row that ended up using this feature
        used_by = [ni for ni, (g, m, r) in final.items() if g == gi]
        dropped = [ni for ni, g, c in oob_drops if g == gi]
        if used_by:
            ni = used_by[0]
            g, m, r = final[ni]
            print("    GIS '%s' -> MATCHED to NYSED '%s' (method=%s ratio=%.3f)"
                  % (legal, nysed[ni]["name"], m, r))
        elif dropped:
            ni = dropped[0]
            print("    GIS '%s' -> DROPPED (out-of-bounds); was tentatively NYSED '%s'"
                  % (legal, nysed[ni]["name"]))
        else:
            print("    GIS '%s' -> NOT matched to any NYSED school (correctly unused)"
                  % legal)

    print("\n(g) MINIMUM name_ratio across accepted matches:")
    if final:
        print("    %.3f" % min(v[2] for v in final.values()))
    else:
        print("    (no matches)")

    print("\n(h) FINAL COUNTS BY BOROUGH:")
    by_boro = {}
    for feat in out_features:
        b = feat["properties"]["borough"]
        by_boro[b] = by_boro.get(b, 0) + 1
    for b in ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"):
        print("    %-14s %d" % (b, by_boro.get(b, 0)))
    for b, n in by_boro.items():
        if b not in ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"):
            print("    %-14s %d  (unexpected)" % (b, n))
    print("    TOTAL FEATURES: %d" % len(out_features))

    print("\n(i) ARITHMETIC:")
    print("    299 NYSED NYC schools = %d mapped + %d unmatched"
          % (len(final), len(unmatched)))
    print("    (check: %d + %d = %d)"
          % (len(final), len(unmatched), len(final) + len(unmatched)))

    print("\n  wrote %d features -> %s" % (len(out_features), OUT_PATH))


if __name__ == "__main__":
    build()
