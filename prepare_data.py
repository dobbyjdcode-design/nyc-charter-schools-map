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

# index.html embed markers (the GeoJSON literal lives between these)
INDEX_PATH = "index.html"
GEOJSON_START = "/* GEOJSON_START */"
GEOJSON_END = "/* GEOJSON_END */"

GIS_QUERY_URL = (
    "https://nysgeohub.ny.gov/arcgis/rest/services/"
    "Education/NYS_Schools/FeatureServer/3/query"
)
WHERE_CLAUSE = "COUNTY_DESC IN ('BRONX','KINGS','NEW YORK','QUEENS','RICHMOND')"

# COUNTY_DESC (GIS, uppercase) -> borough display name.
# NOTE: GIS COUNTY_DESC is unreliable (e.g. MINISINK, plus confirmed records
# where the county tag disagrees with ZIP, city and coordinates). It is kept
# only to compute the "old" borough for the change report and stored as
# gis_county for transparency; the authoritative borough comes from ZIP below.
BOROUGH_BY_COUNTY = {
    "BRONX": "Bronx",
    "KINGS": "Brooklyn",
    "NEW YORK": "Manhattan",
    "QUEENS": "Queens",
    "RICHMOND": "Staten Island",
}

BOROUGH_ORDER = ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island")

# NYC ZIP prefix (first 3 digits) -> borough. This is the authoritative source
# of borough for each school (Fix 1).
ZIP_PREFIX_BOROUGH = {
    "100": "Manhattan", "101": "Manhattan", "102": "Manhattan",
    "104": "Bronx",
    "103": "Staten Island",
    "112": "Brooklyn",
    "110": "Queens", "111": "Queens", "113": "Queens", "114": "Queens", "116": "Queens",
}


def borough_from_zip(zip_code):
    """Return borough from a 5-digit ZIP's 3-digit prefix, or None if unknown."""
    z = (zip_code or "").strip()
    return ZIP_PREFIX_BOROUGH.get(z[:3])


# Two Success Academy records (NYC 17 and NYC 18, both opening 2026) carry a
# placeholder network-office address (95 Pine St / 10005, Success Academy's
# Manhattan HQ), a COUNTY_DESC of BRONX, and coordinates that land in Queens --
# three different boroughs, so the point cannot be trusted. A clean, offline
# "which borough does this coordinate fall in" test would require borough
# polygons we do not have; rather than invent a fragile heuristic we exclude
# these two by INSTIT_ID, with this explanation (Fix 2).
PLACEHOLDER_INSTIT_IDS = {
    "800000093528",  # Success Academy Charter School - NYC 17
    "800000093529",  # Success Academy Charter School - NYC 18
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
    # Fix 1: borough derived from ZIP (not COUNTY_DESC).
    # Fix 2: drop the unplaceable placeholder records (PLACEHOLDER_INSTIT_IDS).
    out_features = []
    borough_changes = []       # (name, old_borough, new_borough, zip)
    unrecognized_zip = []      # (name, zip)  -> triggers STOP
    dropped_placeholder = []   # nysed_index of excluded placeholder records

    for ni, (gi, method, r) in sorted(final.items()):
        row = nysed[ni]
        p = gis[gi].get("properties") or {}
        iid = norm_key(p.get("INSTIT_ID"))

        # Fix 2: exclude unplaceable placeholder records.
        if iid in PLACEHOLDER_INSTIT_IDS:
            dropped_placeholder.append(ni)
            continue

        geom = gis[gi].get("geometry") or {}
        c = geom.get("coordinates")
        coords = [round(float(c[0]), 6), round(float(c[1]), 6)]
        addr_parts = [p.get("PHYSADDRLINE1") or "", p.get("PHYSADDRLINE2") or ""]
        address = " ".join(x.strip() for x in addr_parts if x and x.strip())
        county = (p.get("COUNTY_DESC") or "").strip()
        zip_code = (p.get("PHYSZIPCD5") or "").strip()

        # Fix 1: borough from ZIP. Stop rather than guess on an unknown prefix.
        new_borough = borough_from_zip(zip_code)
        if new_borough is None:
            unrecognized_zip.append((row["name"], zip_code))
            continue
        old_borough = BOROUGH_BY_COUNTY.get(county, county)
        if new_borough != old_borough:
            borough_changes.append((row["name"], old_borough, new_borough, zip_code))

        out_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coords},
            "properties": {
                "name": row["name"],
                "gis_name": p.get("LEGAL_NAME"),
                "address": address,
                "city": (p.get("PHYSCITY") or "").strip(),
                "zip": zip_code,
                "borough": new_borough,         # Fix 1: authoritative, from ZIP
                "gis_county": county,           # kept for transparency
                "borough_source": "zip",
                "district": row["district"],
                "year_opened": row["year_opened"],
                "authorizer": row["authorizer"],
                "instit_id": iid,
                "sed_code": norm_key(p.get("SED_CODE")),
                "match_method": method,
                "name_ratio": round(r, 3),
            },
        })

    # Fix 1 guard: never guess a borough for an unrecognized ZIP prefix.
    if unrecognized_zip:
        hr("STOP: UNRECOGNIZED ZIP PREFIX(ES) - refusing to guess a borough")
        for nm, z in unrecognized_zip:
            print("    %s | zip=%r" % (nm, z))
        raise SystemExit("Unrecognized ZIP prefix encountered; aborting.")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": out_features}, f, indent=2)

    # Not-mapped NYSED rows, with a reason. Placeholder drops are counted here.
    oob_nis = {ni for ni, _, _ in oob_drops}
    collision_loser_nis = {lo[0] for _, _, losers in collisions for lo in losers}
    placeholder_nis = set(dropped_placeholder)
    PLACEHOLDER_REASON = ("GIS record carries placeholder address and inconsistent "
                          "geometry; no reliable location")
    not_mapped = []
    for ni, row in enumerate(nysed):
        if ni in final and ni not in placeholder_nis:
            continue  # mapped and kept in output
        if ni in placeholder_nis:
            reason = PLACEHOLDER_REASON
        elif ni in oob_nis:
            reason = "dropped: out-of-bounds geometry"
        elif ni in collision_loser_nis:
            reason = "collision loser"
        else:
            reason = "no qualifying match"
        not_mapped.append((ni, row, reason))

    mapped = len(out_features)

    # =================================================================== report
    hr("FIX 1: BOROUGH DERIVED FROM ZIP (not COUNTY_DESC)")
    print("  features whose borough changed: %d" % len(borough_changes))
    for nm, ob, nb, z in sorted(borough_changes):
        print("    %s | %s -> %s | zip=%s" % (nm, ob, nb, z))

    hr("FIX 2: DROPPED PLACEHOLDER RECORDS")
    if not dropped_placeholder:
        print("  (none)")
    for ni in dropped_placeholder:
        print("    DROPPED %s | INSTIT_ID=%s" % (nysed[ni]["name"], nysed[ni]["instit_id"]))
        print("      reason: %s" % PLACEHOLDER_REASON)

    hr("REPORT")
    print("(a) total features: %d" % mapped)

    by_boro = {}
    for feat in out_features:
        by_boro[feat["properties"]["borough"]] = by_boro.get(feat["properties"]["borough"], 0) + 1
    print("\n(b) borough counts:")
    for b in BOROUGH_ORDER:
        print("    %-14s %d" % (b, by_boro.get(b, 0)))
    for b, n in by_boro.items():
        if b not in BOROUGH_ORDER:
            print("    %-14s %d  (unexpected)" % (b, n))

    sa_count = sum(1 for f in out_features
                   if "success academy" in (f["properties"]["name"] or "").lower())
    print("\n(c) Success Academy count: %d" % sa_count)

    reason_counts = {}
    for _, _, reason in not_mapped:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    print("\n(d) ARITHMETIC:")
    print("    299 NYSED NYC schools = %d mapped + %d not mapped" % (mapped, len(not_mapped)))
    for reason in sorted(reason_counts):
        print("      %d  %s" % (reason_counts[reason], reason))
    print("    (check: %d + %d = %d)" % (mapped, len(not_mapped), mapped + len(not_mapped)))
    print("    not-mapped detail:")
    for ni, row, reason in sorted(not_mapped, key=lambda t: t[1]["name"]):
        print("      - %s | INSTIT_ID=%s | %s" % (row["name"], row["instit_id"], reason))

    # (e) re-run the ZIP-vs-borough consistency check on the output.
    zip_mismatch = 0
    for feat in out_features:
        exp = borough_from_zip(feat["properties"]["zip"])
        if exp != feat["properties"]["borough"]:
            zip_mismatch += 1
    print("\n(e) ZIP-vs-borough recheck on output: %d mismatch(es)" % zip_mismatch)

    print("\n    minimum name_ratio across kept matches: %.3f"
          % (min(f["properties"]["name_ratio"] for f in out_features) if out_features else 0))
    print("    wrote %d features -> %s" % (mapped, OUT_PATH))

    # Embed the fresh GeoJSON into index.html between the marker comments.
    embed_geojson_into_index()


def embed_geojson_into_index():
    """Rewrite the text between /* GEOJSON_START */ and /* GEOJSON_END */ in
    index.html with the current data/schools.geojson, so the map ships with the
    data embedded (works over file://, no runtime fetch). Simple marker split."""
    hr("STEP 1g: EMBED GeoJSON INTO index.html")
    if not os.path.exists(INDEX_PATH):
        print("  %s not found - skipping embed" % INDEX_PATH)
        return
    with open(OUT_PATH, encoding="utf-8") as f:
        geojson_text = f.read().strip()
    with open(INDEX_PATH, encoding="utf-8") as f:
        html = f.read()
    if GEOJSON_START not in html or GEOJSON_END not in html:
        print("  markers not found in %s - skipping (nothing changed)" % INDEX_PATH)
        return

    head = html.split(GEOJSON_START, 1)[0] + GEOJSON_START
    tail = GEOJSON_END + html.split(GEOJSON_END, 1)[1]
    new_middle = "\n  const SCHOOLS = " + geojson_text + ";\n  "
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(head + new_middle + tail)

    n = len(json.loads(geojson_text).get("features", []))
    print("  embedded %d features into %s" % (n, INDEX_PATH))


if __name__ == "__main__":
    build()
