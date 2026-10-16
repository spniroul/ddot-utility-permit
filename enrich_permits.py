"""
Adds FHWA functional class to 2026 utility occupancy permits
(DC Water, PEPCO, Washington Gas).

Output: one row per permit location, with the functional class at that
location and the highest functional class on the permit.

Run:  python enrich_permits.py   (see README.md for setup)
"""

import re
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests


# Settings

SCRIPT_FOLDER = Path(__file__).resolve().parent
OUTPUT_FOLDER = SCRIPT_FOLDER / "output"
OUTPUT_CSV = OUTPUT_FOLDER / "utility_occupancy_permits_2026_functional_class.csv"

OCCUPANCY_PERMIT_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_APPS/DDOT_TOPS/MapServer/24"
)
ROADWAY_SUBBLOCK_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
    "Transportation_WebMercator/MapServer/162"
)

START_DATE = "2026-01-01"
PERMIT_TYPES = ["Construction Staging Area", "Mobile Crane Work Zone"]

# Utility names are spelled inconsistently in TOPS
# (e.g. "DCWater/CIP", "Pepco", "Potomac Electric Power Company", "Wash Gas & Light Co.")
UTILITY_NAME_PATTERNS = {
    "DC Water": r"DC\s*WATER|D\.C\.\s*WATER|\bWASA\b|WATER\s*(AND|&)\s*SEWER",
    "PEPCO": r"PEPCO|POTOMAC\s*ELEC",
    "Washington Gas": r"WASH(INGTON|\.)?\s*GAS|\bWGL\b",
}
NAME_FIELDS = ["ApplicantCompany", "PermitteeName", "OwnerName"]

# ROADTYPE codes: 1 Street, 2 Service Road, 3 Ramp (4 = Alley, excluded)
ROAD_TYPES_TO_KEEP = ["1", "2", "3"]

FUNCTIONAL_CLASS_NAMES = {
    1: "Interstate",
    2: "Principal Arterial - Other Freeways or Expressways",
    3: "Principal Arterial - Other",
    4: "Minor Arterial",
    5: "Major Collector",
    6: "Minor Collector",
    7: "Local",
}

REVIEW_DISTANCE_METERS = 50     # flag locations farther than this from any road
ADDRESS_SEARCH_METERS = 60      # how far to look for a road matching the address
DC_METERS_CRS = "EPSG:26985"    # MD State Plane, meters
RECORDS_PER_REQUEST = 500

STREET_TYPE_ABBREVIATIONS = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "PLACE": "PL",
    "DRIVE": "DR", "BOULEVARD": "BLVD", "COURT": "CT", "TERRACE": "TER",
    "PARKWAY": "PKWY", "CIRCLE": "CIR", "LANE": "LN", "HIGHWAY": "HWY",
}


def download_layer(layer_url, where_clause, out_fields="*"):
    # The server caps each response at 2000 records, so get the full ID list
    # first and then download in batches.
    query_url = layer_url + "/query"

    id_json = requests.post(
        query_url,
        data={"where": where_clause, "returnIdsOnly": "true", "f": "json"},
        timeout=120,
    ).json()
    if "error" in id_json:
        raise RuntimeError(f"Server error for {layer_url}: {id_json['error']}")

    object_ids = sorted(id_json.get("objectIds") or [])
    print(f"    {len(object_ids)} matching records")
    if not object_ids:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    features = []
    for start in range(0, len(object_ids), RECORDS_PER_REQUEST):
        batch_ids = object_ids[start:start + RECORDS_PER_REQUEST]
        batch_json = requests.post(
            query_url,
            data={
                "objectIds": ",".join(str(i) for i in batch_ids),
                "outFields": out_fields,
                "outSR": "4326",
                "returnGeometry": "true",
                "f": "geojson",
            },
            timeout=300,
        ).json()
        if "error" in batch_json:
            raise RuntimeError(f"Server error for {layer_url}: {batch_json['error']}")
        features.extend(batch_json["features"])
        print(f"    downloaded {min(start + RECORDS_PER_REQUEST, len(object_ids))}"
              f" of {len(object_ids)}")
        time.sleep(0.2)

    return gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")


def epoch_ms_to_dc_time(column):
    # ArcGIS dates are UTC milliseconds; convert to DC local time
    utc_times = pd.to_datetime(column, unit="ms", utc=True, errors="coerce")
    return utc_times.dt.tz_convert("America/New_York").dt.tz_localize(None)


def get_occupancy_permits():
    print("Step 1: downloading occupancy permits...")
    permit_type_list = ", ".join(f"'{name}'" for name in PERMIT_TYPES)

    # Server compares in UTC, so pull from Dec 31 and apply the exact
    # local-time cutoff in filter_by_date
    where_clause = (
        f"EventTypeDescription IN ({permit_type_list}) "
        f"AND (ApplicationDate >= timestamp '2025-12-31 00:00:00' "
        f"OR IssueDate >= timestamp '2025-12-31 00:00:00')"
    )
    return download_layer(OCCUPANCY_PERMIT_URL, where_clause)


def filter_by_date(permits):
    print("Step 2: keeping permits applied for or issued on/after", START_DATE)

    for column in ["ApplicationDate", "IssueDate", "EffectiveDate", "ExpirationDate",
                   "LastApprovalDate", "LastUpdateDate"]:
        if column in permits.columns:
            permits[column] = epoch_ms_to_dc_time(permits[column])

    start = pd.Timestamp(START_DATE)
    keep = (permits["ApplicationDate"] >= start) | (permits["IssueDate"] >= start)
    permits = permits[keep].copy()

    print(f"    {len(permits)} permit locations remain")
    return permits


def find_utility(row):
    for field in NAME_FIELDS:
        value = row.get(field)
        if not isinstance(value, str):
            continue
        for utility, pattern in UTILITY_NAME_PATTERNS.items():
            if re.search(pattern, value, flags=re.IGNORECASE):
                return utility, field
    return None, None


def filter_to_utilities(permits):
    print("Step 3: keeping DC Water, PEPCO, and Washington Gas permits")

    results = permits.apply(find_utility, axis=1, result_type="expand")
    permits["Utility"] = results[0]
    permits["UtilityMatchedField"] = results[1]
    permits = permits[permits["Utility"].notna()].copy()

    # Print what matched so the patterns can be checked
    print("    Name spellings matched to each utility:")
    for utility, group in permits.groupby("Utility"):
        names = set()
        for field in NAME_FIELDS:
            names.update(group[field].dropna().unique())
        matched = sorted(n for n in names
                         if re.search(UTILITY_NAME_PATTERNS[utility], n, re.IGNORECASE))
        print(f"      {utility}: {matched}")

    print(f"    {len(permits)} utility permit locations, "
          f"{permits['TrackingNumber'].nunique()} unique permits")
    return permits


def remove_duplicate_locations(permits):
    is_duplicate = pd.DataFrame({
        "TrackingNumber": permits["TrackingNumber"],
        "x": permits.geometry.x.round(6),
        "y": permits.geometry.y.round(6),
    }).duplicated()
    print(f"    removed {int(is_duplicate.sum())} duplicate locations within the same permit")
    return permits[~is_duplicate].copy()


def get_roadway_subblocks():
    print("Step 4: downloading roadway subblocks (streets, service roads, ramps)...")
    road_type_list = ", ".join(f"'{code}'" for code in ROAD_TYPES_TO_KEEP)
    where_clause = f"ROADTYPE IN ({road_type_list}) AND FHWAFUNCTIONALCLASS IS NOT NULL"
    fields = "SUBBLOCKKEY,SUBBLOCKID,BLOCKKEY,ROUTEID,ROUTENAME,ROADTYPE,FHWAFUNCTIONALCLASS"
    return download_layer(ROADWAY_SUBBLOCK_URL, where_clause, fields)


def street_names_from_address(address):
    # "1112 M STREET NW" -> ["M ST NW"]
    # "K STREET NW AND 1ST STREET NW" -> ["K ST NW", "1ST ST NW"]
    # Ramp/exit descriptions return nothing.
    if not isinstance(address, str):
        return []

    names = []
    for piece in address.upper().split(" AND "):
        piece = piece.strip()
        if piece.startswith(("RAMP", "EXIT")):
            continue
        # Strip "600 - 699 BLOCK OF" or a house number, not both
        # (otherwise "BLOCK OF 9 1/2 STREET" loses the 9)
        without_block = re.sub(r"^\d+\s*-\s*\d+\s+BLOCK OF\s+", "", piece)
        if without_block != piece:
            piece = without_block
        else:
            piece = re.sub(r"^\d+[A-Z]?\s+", "", piece)
        piece = re.sub(r"\b(NORTH|SOUTH|EAST|WEST)BOUND\b", "", piece)
        for long_word, short_word in STREET_TYPE_ABBREVIATIONS.items():
            piece = re.sub(rf"\b{long_word}\b", short_word, piece)
        piece = re.sub(r"\s+", " ", piece).strip()
        if piece:
            names.append(piece)
    return names


def name_matches(route_name, address_names):
    # Prefix match too, since some addresses drop the quadrant
    # ("NORTH CAPITOL ST" vs ROUTENAME "NORTH CAPITOL ST BN")
    if not isinstance(route_name, str):
        return False
    route_name = route_name.upper()
    return any(route_name == name or route_name.startswith(name + " ")
               for name in address_names)


def add_functional_class(permits, subblocks):
    # Nearest road is right ~97% of the time. The misses are corner permits
    # that sit slightly closer to the cross street (43 Rhode Island Ave NW
    # was matching U St). So: prefer a nearby road whose name matches the
    # address, and only fall back to plain nearest when there isn't one.
    print("Step 5: matching each permit location to its roadway")

    permits_m = permits.to_crs(DC_METERS_CRS)
    subblocks_m = subblocks.to_crs(DC_METERS_CRS)[
        ["SUBBLOCKKEY", "ROUTENAME", "FHWAFUNCTIONALCLASS", "geometry"]]

    # Fallback: nearest road. On exact ties keep the higher class.
    nearest = gpd.sjoin_nearest(
        permits_m[["OBJECTID", "geometry"]], subblocks_m,
        how="left", distance_col="DistanceToRoadway_m",
    )
    nearest = nearest.sort_values(["OBJECTID", "FHWAFUNCTIONALCLASS"])
    nearest = nearest.drop_duplicates(subset="OBJECTID", keep="first")
    nearest["MatchMethod"] = "Nearest roadway"

    # Roads within the search distance whose name matches the address
    search_areas = permits_m[["OBJECTID", "WorkLocationFullAddress", "geometry"]].copy()
    search_areas["geometry"] = search_areas.geometry.buffer(ADDRESS_SEARCH_METERS)
    candidates = gpd.sjoin(search_areas, subblocks_m, how="inner", predicate="intersects")

    candidates["AddressStreets"] = candidates["WorkLocationFullAddress"].apply(
        street_names_from_address)
    is_match = [name_matches(route, names)
                for route, names in zip(candidates["ROUTENAME"], candidates["AddressStreets"])]
    candidates = candidates[is_match].copy()

    point_by_id = permits_m.set_index("OBJECTID").geometry
    points = gpd.GeoSeries(point_by_id.loc[candidates["OBJECTID"]].values, crs=DC_METERS_CRS)
    lines = gpd.GeoSeries(subblocks_m.geometry.loc[candidates["index_right"]].values,
                          crs=DC_METERS_CRS)
    candidates["DistanceToRoadway_m"] = points.distance(lines).values

    # Single street: closest match. Intersection address: the work is on both
    # streets, so take the higher class.
    candidates["IsIntersection"] = candidates["AddressStreets"].apply(len) > 1
    candidates["SortClass"] = candidates["FHWAFUNCTIONALCLASS"].where(
        candidates["IsIntersection"], 0)
    candidates = candidates.sort_values(
        ["OBJECTID", "SortClass", "DistanceToRoadway_m", "FHWAFUNCTIONALCLASS"])
    by_address = candidates.drop_duplicates(subset="OBJECTID", keep="first").copy()
    by_address["MatchMethod"] = "Address street match"

    columns = ["OBJECTID", "SUBBLOCKKEY", "ROUTENAME", "FHWAFUNCTIONALCLASS",
               "DistanceToRoadway_m", "MatchMethod"]
    nearest_only = nearest[~nearest["OBJECTID"].isin(by_address["OBJECTID"])]
    best_match = pd.concat([by_address[columns], nearest_only[columns]])

    # Keep the plain-nearest answer for comparison
    nearest_by_id = nearest.set_index("OBJECTID")
    best_match["NearestRoadwayClassCode"] = (
        best_match["OBJECTID"].map(nearest_by_id["FHWAFUNCTIONALCLASS"]).astype("Int64"))

    changed = (by_address["OBJECTID"].map(nearest_by_id["ROUTENAME"])
               != by_address["ROUTENAME"]).sum()
    print(f"    {len(by_address)} locations matched by address street name, "
          f"{len(nearest_only)} by nearest roadway")
    print(f"    {changed} locations moved off a cross street onto their address street")

    joined = permits_m.merge(best_match, on="OBJECTID", how="left")
    joined["FunctionalClassCode"] = joined["FHWAFUNCTIONALCLASS"].astype("Int64")
    joined["FunctionalClassName"] = joined["FunctionalClassCode"].map(FUNCTIONAL_CLASS_NAMES)
    joined["DistanceToRoadway_m"] = joined["DistanceToRoadway_m"].round(1)
    joined["NeedsReview"] = joined["DistanceToRoadway_m"] > REVIEW_DISTANCE_METERS

    print(f"    {int(joined['NeedsReview'].sum())} locations are more than "
          f"{REVIEW_DISTANCE_METERS} m from a roadway (NeedsReview)")
    return joined.to_crs("EPSG:4326")


def add_highest_class_per_permit(permits):
    print("Step 6: finding the highest functional class for each permit")

    # min, because 1 (Interstate) is the highest class
    by_permit = permits.groupby("TrackingNumber")
    permits = permits.join(
        by_permit["FunctionalClassCode"].min().rename("HighestFunctionalClassCode"),
        on="TrackingNumber")
    permits = permits.join(
        by_permit["OBJECTID"].count().rename("LocationsOnPermit"),
        on="TrackingNumber")
    permits["HighestFunctionalClassName"] = (
        permits["HighestFunctionalClassCode"].map(FUNCTIONAL_CLASS_NAMES))
    return permits


def write_csv(permits):
    print("Step 7: writing CSV")

    permits["Longitude"] = permits.geometry.x.round(6)
    permits["Latitude"] = permits.geometry.y.round(6)

    output_columns = [
        "TrackingNumber", "PermitNumber", "OBJECTID", "EventTypeDescription",
        "StatusDescription", "Utility", "UtilityMatchedField",
        "ApplicantCompany", "PermitteeName", "OwnerName",
        "ApplicationDate", "IssueDate", "EffectiveDate", "ExpirationDate",
        "WorkLocationFullAddress", "WorkLocationWard",
        "Longitude", "Latitude", "LocationsOnPermit",
        "SUBBLOCKKEY", "ROUTENAME", "MatchMethod", "NearestRoadwayClassCode",
        "DistanceToRoadway_m", "NeedsReview",
        "FunctionalClassCode", "FunctionalClassName",
        "HighestFunctionalClassCode", "HighestFunctionalClassName",
    ]
    output_columns = [c for c in output_columns if c in permits.columns]

    table = pd.DataFrame(permits[output_columns])
    table = table.sort_values(["TrackingNumber", "FunctionalClassCode", "OBJECTID"])

    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens it without garbled characters
    table.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"    wrote {len(table)} rows to {OUTPUT_CSV}")
    return table


def print_summary(table):
    print("\nSummary")
    print("-------")
    print(f"{len(table)} locations on {table['TrackingNumber'].nunique()} permits")
    print("\nPermit types:")
    print(table["EventTypeDescription"].value_counts().to_string())
    print("\nHow locations were matched:")
    print(table["MatchMethod"].value_counts().to_string())

    print("\nLocations by functional class:")
    for code, count in table["FunctionalClassCode"].value_counts().sort_index().items():
        print(f"  {code} {FUNCTIONAL_CLASS_NAMES.get(code, 'Unknown'):52} {count:>6}")

    per_permit = table.drop_duplicates("TrackingNumber")
    print("\nPermits by utility and highest functional class (columns = class code):")
    print(pd.crosstab(per_permit["Utility"], per_permit["HighestFunctionalClassCode"],
                      margins=True, margins_name="Total").to_string())

    print("\nShare of permits whose highest class is Minor Arterial or above (codes 1-4):")
    share = (per_permit["HighestFunctionalClassCode"] <= 4).groupby(per_permit["Utility"]).mean()
    for utility, value in share.items():
        print(f"  {utility:16} {value:.0%}")


def main():
    permits = get_occupancy_permits()
    if permits.empty:
        sys.exit("No permits returned. Check the URL and filters.")

    permits = filter_by_date(permits)
    permits = filter_to_utilities(permits)
    if permits.empty:
        sys.exit("No utility permits found. Check UTILITY_NAME_PATTERNS.")
    permits = remove_duplicate_locations(permits)

    subblocks = get_roadway_subblocks()
    permits = add_functional_class(permits, subblocks)
    permits = add_highest_class_per_permit(permits)
    table = write_csv(permits)
    print_summary(table)


if __name__ == "__main__":
    main()
