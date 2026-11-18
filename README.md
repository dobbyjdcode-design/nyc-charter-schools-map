# NYC Charter Schools Map

An interactive map of active charter schools in New York City. It joins the authoritative school list published by the New York State Education Department (NYSED) to point geometry from the New York State GIS Clearinghouse, and renders the result as a single static HTML page. The map is self-contained: the data is embedded directly in the page, so it needs no server, no API keys, and makes no network calls for data at runtime.

Live map: https://dobbyjdcode-design.github.io/nyc-charter-schools-map/

## What's on the map

285 charter schools across the five boroughs. Borough counts:

| Borough | Schools |
| --- | --- |
| Bronx | 96 |
| Brooklyn | 94 |
| Manhattan | 53 |
| Queens | 34 |
| Staten Island | 8 |
| Total | 285 |

Built with Leaflet 1.9.4 and OpenStreetMap tiles. It is a single static HTML file with the GeoJSON embedded as a JavaScript literal; there are no API keys and no runtime data calls.

## Data sources

| Source | URL | What it provided | Retrieved on |
| --- | --- | --- | --- |
| NYSED Charter School Directory, as of 2025-09-02 | https://www.nysed.gov/charter-schools/charter-schools-directory | Authoritative school list: 370 schools statewide, 299 in NYC | 2026-11-17 |
| NYS GIS Clearinghouse "Charter K-12" layer | https://nysgeohub.ny.gov/arcgis/rest/services/Education/NYS_Schools/FeatureServer/3 | Point geometry (latitude/longitude): 544 features statewide, 426 in NYC. Publishes no update date. | 2026-11-18 |
| NYC Open Data `3bkj-34v2` | https://data.cityofnewyork.us/api/views/3bkj-34v2.json | Evaluated and rejected (see below) | 2026-11-17 |

### Why NYC Open Data `3bkj-34v2` was rejected

The dataset `3bkj-34v2` ("NYC DOE Public School Location Information") looks like an obvious primary source, but it is not usable:

- It is a non-tabular "href" catalog entry, not a hosted table. Its metadata reports `viewType: href` with zero columns; it simply links out to an external DOE application.
- Its data dates to 2013 (`rowsUpdatedAt` = 2013-03-22). The catalog's `updatedAt` of 2024-11-26 is a platform-wide metadata refresh, not a data refresh.
- The documented row-download endpoint, `rows.csv?accessType=DOWNLOAD`, returns HTTP 400 with the message "Non-tabular datasets do not support rows requests."

Using it would have been wrong because it would have mapped 2013-era school locations as if current, and it mixes all public schools rather than isolating charters. We therefore dropped it in favor of the NYSED directory (current, charter-specific) joined to the NYS GIS layer (charter-specific geometry).

## Method

The join is a guarded two-pass join. The NYSED directory is the authoritative list of schools; the GIS layer supplies coordinates. The two share an institution identifier (`INSTIT_ID`), but the GIS `SED_CODE` is a duplicate of `INSTIT_ID`, so there is no independent second key. Names are normalized before comparison (uppercased, punctuation stripped, whitespace collapsed, and the tokens CHARTER, SCHOOL, THE, OF, AT, INC removed) and similarity is scored with `difflib.SequenceMatcher`.

- Pass 1 (ID + name guard): match on `INSTIT_ID`, accepted only when the normalized name similarity is at least 0.60.
- Pass 2 (name rescue): for anything not matched in Pass 1, score the NYSED name against all 426 GIS NYC features and accept the single best only if similarity is at least 0.85. Zero rescues qualified; nothing was guessed.
- No GIS feature is used by more than one school. When two schools claimed the same feature, the higher-ratio match won and the loser was pushed to unmatched.
- Coordinates outside the NYC bounding box (latitude 40.47-40.95, longitude -74.30 to -73.68) were rejected.
- Borough is assigned from each school's ZIP code, not the GIS `COUNTY_DESC` field (which contains errors; see Verification). The GIS county is retained in each feature as `gis_county` for transparency.
- Two records whose address, county and geometry point to three different boroughs are dropped as unplaceable (see known gaps).

Result: 299 NYSED NYC schools = 285 mapped + 14 not mapped.

## Verification

An ID-only join (match on `INSTIT_ID` with no name check) produced 8 false matches out of 294, a 2.7% error rate. The worst example: the GIS feature MINISINK CHARTER SCHOOL was joined to the NYSED school "Explore Enrich Charter School" and plotted near Albany, roughly 130 miles outside New York City. Other false matches paired "Girls Preparatory Charter School of the Bronx" with "Bronx Charter School for Excellence 6" and similar. These were detected by a name-agreement check (scoring every matched pair and listing those below 0.60 similarity) and fixed by adding the name guard described above. This is the most important thing in this README: identifier equality alone was not trustworthy, and the name guard is what makes the output correct.

- GIS `COUNTY_DESC` defect: the GIS county field is unreliable. Five records had a `COUNTY_DESC` that disagreed with the record's own ZIP, city and coordinates (for example, MINISINK; "Achievement First Legacy" tagged KINGS but located in Saint Albans, Queens; "Democracy Prep Endurance" tagged BRONX but in East Harlem, Manhattan; "Lamad Averne/Arverne" tagged BRONX but in Brooklyn). Because of this, borough is now derived from ZIP code rather than `COUNTY_DESC`. A further 23 schools that sit far from their borough's geographic median were checked individually and confirmed correct (edge neighborhoods such as Far Rockaway, Coney Island and Co-op City, where ZIP, city and coordinates all agree).
- Minimum accepted name similarity across all kept matches: 0.625.
- Independent convergence: our count of 285 sits beside the NYC Charter School Center's publicly reported figure of roughly 286.
- Success Academy reconciliation: the NYSED directory lists 41 Success Academy charter entities in NYC; the GIS layer contains 59 Success Academy points; and Success Academy publicly reports 59 schools (https://www.successacademies.org/locations/). The GIS campus-level count matches Success Academy's own figure exactly. The difference is one of counting units: 41 counts charters (legal entities), while 59 counts schools (individual campuses and grade bands). Of the 41 charters we map 38; the other 3 have no usable geometry: "Success Academy Charter School-Bronx 5 Lower" (collided with the Bronx 5 Upper point and lost), and "Success Academy Charter School - NYC 17" and "NYC 18" (both opening 2026, dropped as unplaceable placeholder records).

### Spot-check (verified against each school's own website)

| School | Our address | Address on its website | Match? | Source URL |
| --- | --- | --- | --- | --- |
| Bridge Preparatory Charter School (Staten Island) | 715 OCEAN TERRACE-BLDG E, Staten Island 10301 | 715 Ocean Terrace, Staten Island, NY 10301 | Yes (street and ZIP match; the school site says "Building A" on the Petrides campus while our data says "Bldg E") | https://www.bridgeprepcharter.org/ |
| Brooklyn Prospect Charter School-CSD 15 (lowest name_ratio, 0.625) | 3002 FORT HAMILTON PKWY, Brooklyn 11218 | 3002 Fort Hamilton Pkwy, Brooklyn, NY 11218 | Yes (exact) | https://www.brooklynprospect.org/schools/windsor-terrace-elementary/ |
| Academy of the City Charter School (Queens) | 31-29 60TH ST, Woodside 11377 | 31-29 60th Street, Woodside, NY 11377 | Yes (exact) | https://www.academyofthecity.org/ |

All three verified clean. The lowest-similarity match (Brooklyn Prospect, 0.625) is a true match: the low score comes only from the differing labels ("CSD 15" versus "Windsor Terrace Campus"), not a wrong location.

## Assumptions and known gaps

- The NYSED directory is assumed to list active schools only. It has no closed/active status column; its `ACTIVE_DATE` column is an opening date (its values are opening-year serials), not a status flag.
- The GIS layer publishes no update date (its `editingInfo` is empty), so a school that relocated could be mapped at its former address.
- The directory is as of 2025-09-02 and is the newest NYSED publishes. This was verified against the NYSED page; no 2026-2027 edition exists.
- The 14 not-mapped NYSED NYC schools, with the reason for each:

  | School | Reason |
  | --- | --- |
  | Boys Preparatory Charter School of New York | no qualifying match |
  | Brooklyn Prospect Charter School - CSD 13.2 | no qualifying match |
  | Collegiate Academy for Mathematics and Personal Awareness Charter School | no qualifying match |
  | Explore Enrich Charter School | no qualifying match |
  | Explore Exceed Charter School | collision loser |
  | Girls Preparatory Charter School of New York | no qualifying match |
  | Girls Preparatory Charter School of the Bronx | no qualifying match |
  | Girls Preparatory Charter School of the Bronx II | no qualifying match |
  | Harbor Science and Arts Charter School | no qualifying match |
  | Harriet Tubman Charter School | no qualifying match |
  | Manhattan Charter School II | collision loser |
  | Success Academy Charter School-Bronx 5 Lower | collision loser |
  | Success Academy Charter School - NYC 17 | GIS record carries placeholder address and inconsistent geometry; no reliable location |
  | Success Academy Charter School - NYC 18 | GIS record carries placeholder address and inconsistent geometry; no reliable location |

  The last two (NYC 17 and NYC 18, both opening 2026) are dropped placeholder records: their GIS entry lists Success Academy's 95 Pine St / 10005 network office in Manhattan as the address, tags the county as Bronx, and places the coordinate in Queens, so no borough can be trusted.

- 132 GIS campus-level records were excluded because they have no corresponding NYSED charter entity (for example, separate middle-school and elementary-school points of a single charter).

## Next steps

- Add a campus-level layer as a toggle (the 132 excluded records), so networks like Success Academy can be viewed at either granularity.
- Geocode the 14 not-mapped schools from their published addresses.
- Add grades served and enrollment to the popups.
- Add free-text search by school name (borough and Success Academy filtering already exist via the clickable legend).

## Reproducing

Run:

```
python prepare_data.py
```

This fetches the GIS NYC features, parses the NYSED directory, runs the guarded two-pass join, rewrites `data/schools.geojson`, and re-embeds the GeoJSON into `index.html` between the `/* GEOJSON_START */` and `/* GEOJSON_END */` markers. It uses the Python standard library only; there are no dependencies to install.

## Time spent

~90 minutes of working time across two sittings (evening Nov 17, early morning Nov 18).

## How I used AI

An architect AI planned the steps and wrote the prompts; Cursor (Claude Opus) wrote the code. I reviewed every output and personally directed the key decisions: rejecting the stale NYC Open Data source, requiring a name-agreement guard after the false joins surfaced, refusing to let any record be geocoded by guess, and ruling that unmatched schools be documented rather than quietly dropped.
