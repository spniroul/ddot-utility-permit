"""
Compares three ways of getting the functional class for a permit location,
on the same random sample from the main output:

  1. Subblock download + local matching (what enrich_permits.py uses)
  2. LRS API: DDOT's getPointOnSubBlock, one request per point
  3. MAR lookup: the permit address -> MAR address point -> SUBBLOCKKEY

For 2 and 3 the SUBBLOCKKEY is looked up on the Roadway SubBlock layer to get
the functional class. Prints coverage, agreement and run time for each.

Run after enrich_permits.py:  python compare_methods.py
"""

import json
import time
from pathlib import Path

import pandas as pd
import requests

SCRIPT_FOLDER = Path(__file__).resolve().parent
INPUT_CSV = SCRIPT_FOLDER / "output" / "utility_occupancy_permits_2026_functional_class.csv"
RAW_FOLDER = SCRIPT_FOLDER / "output" / "lrs_raw_responses"
COMPARISON_CSV = SCRIPT_FOLDER / "output" / "method_comparison_sample.csv"

OCCUPANCY_PERMIT_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_APPS/DDOT_TOPS/MapServer/24"
)
ROADWAY_SUBBLOCK_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
    "Transportation_WebMercator/MapServer/162"
)
LRS_URL = (
    "https://rh.dcgis.dc.gov/dcgis/rest/services/DDOT/RoadwayBlocks/MapServer/"
    "exts/DdotLrsExtensions/getPointOnSubBlock"
)
# MAR address points published as a map service (has MAR_ID and SUBBLOCKKEY)
MAR_ADDRESS_POINTS_URL = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Location/MapServer/0"
)

SAMPLE_SIZE = 100
LRS_SEARCH_RADIUS = 50
BATCH_SIZE = 100

ROAD_TYPE_NAMES = {"1": "Street", "2": "Service Road", "3": "Ramp", "4": "Alley",
                   "5": "Driveway", "6": "Trail", "7": "Walkway", "8": "Streetcar"}


def query_attributes(layer_url, where_clause, out_fields):
    response = requests.post(layer_url + "/query", data={
        "where": where_clause, "outFields": out_fields,
        "returnGeometry": "false", "f": "json",
    }, timeout=120).json()
    if "error" in response:
        raise RuntimeError(f"Server error for {layer_url}: {response['error']}")
    return [feature["attributes"] for feature in response["features"]]


def sql_list(values):
    # Quote values for an IN (...) clause, escaping single quotes
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def in_batches(values):
    values = list(values)
    for start in range(0, len(values), BATCH_SIZE):
        yield values[start:start + BATCH_SIZE]


def find_value(data, key_name):
    # LRS response format isn't documented, so search the whole JSON
    if isinstance(data, dict):
        for key, value in data.items():
            if key.upper() == key_name.upper():
                return value
            found = find_value(value, key_name)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_value(item, key_name)
            if found is not None:
                return found
    return None


def lookup_subblocks(subblock_keys):
    # SUBBLOCKKEY -> (functional class, road type). No road type filter here,
    # so alleys show up with a blank class.
    info = {}
    keys = [k for k in set(subblock_keys) if isinstance(k, str) and k]
    for batch in in_batches(keys):
        rows = query_attributes(
            ROADWAY_SUBBLOCK_URL, f"SUBBLOCKKEY IN ({sql_list(batch)})",
            "SUBBLOCKKEY,FHWAFUNCTIONALCLASS,ROADTYPE,ROUTENAME")
        for row in rows:
            info[row["SUBBLOCKKEY"]] = row
    return info


def run_lrs(sample):
    print(f"LRS API: {len(sample)} requests, one per point...")
    RAW_FOLDER.mkdir(parents=True, exist_ok=True)
    start = time.time()
    keys = []
    for _, row in sample.iterrows():
        params = {
            "x": row["Longitude"], "y": row["Latitude"], "inSR": 4326, "outSR": 4326,
            "searchRadius": LRS_SEARCH_RADIUS, "returnNearestMatchOnly": "true",
            "includeSectionEvents": "true", "f": "json",
        }
        try:
            data = requests.get(LRS_URL, params=params, timeout=60).json()
        except Exception as error:
            data = {"script_error": str(error)}
        (RAW_FOLDER / f"objectid_{row['OBJECTID']}.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        keys.append(find_value(data, "SUBBLOCKKEY"))
    seconds = time.time() - start
    print(f"    done in {seconds:.0f} s")
    return keys, seconds


def run_mar(sample):
    print("MAR lookup: permit address -> MAR address point -> SUBBLOCKKEY...")
    start = time.time()

    # The permit layer stores WorkLocationAddressID; try it as the MAR_ID first
    address_ids = {}
    for batch in in_batches(sample["OBJECTID"]):
        rows = query_attributes(
            OCCUPANCY_PERMIT_URL, f"OBJECTID IN ({', '.join(map(str, batch))})",
            "OBJECTID,WorkLocationAddressID")
        for row in rows:
            address_ids[row["OBJECTID"]] = row["WorkLocationAddressID"]

    by_mar_id = {}
    ids = [i for i in set(address_ids.values()) if i]
    for batch in in_batches(ids):
        rows = query_attributes(
            MAR_ADDRESS_POINTS_URL, f"MAR_ID IN ({', '.join(map(str, batch))})",
            "MAR_ID,SUBBLOCKKEY")
        for row in rows:
            by_mar_id[row["MAR_ID"]] = row["SUBBLOCKKEY"]

    # Fall back to matching the address text
    by_address = {}
    addresses = sample["WorkLocationFullAddress"].dropna().str.upper().unique()
    for batch in in_batches(addresses):
        rows = query_attributes(
            MAR_ADDRESS_POINTS_URL, f"UPPER(ADDRESS) IN ({sql_list(batch)})",
            "ADDRESS,SUBBLOCKKEY")
        for row in rows:
            by_address[row["ADDRESS"].upper()] = row["SUBBLOCKKEY"]

    keys, how = [], []
    for _, row in sample.iterrows():
        mar_id = address_ids.get(row["OBJECTID"])
        address = str(row["WorkLocationFullAddress"]).upper()
        if mar_id in by_mar_id:
            keys.append(by_mar_id[mar_id])
            how.append("MAR_ID")
        elif address in by_address:
            keys.append(by_address[address])
            how.append("Address text")
        else:
            keys.append(None)
            how.append("No MAR match")
    seconds = time.time() - start
    print(f"    done in {seconds:.0f} s")
    return keys, how, seconds


def summarize(comparison, method, seconds):
    has_class = comparison[f"{method}Class"].notna()
    agrees = comparison[f"{method}Class"] == comparison["ScriptClass"]
    print(f"  {method:6} returned a functional class for {has_class.sum():>3} of {len(comparison)}"
          f" | agrees with script on {agrees[has_class].sum():>3} of {has_class.sum():>3}"
          f" | {seconds:.0f} s")


def main():
    table = pd.read_csv(INPUT_CSV)
    sample = table.sample(min(SAMPLE_SIZE, len(table)), random_state=1).reset_index(drop=True)

    lrs_keys, lrs_seconds = run_lrs(sample)
    mar_keys, mar_how, mar_seconds = run_mar(sample)

    print("Looking up functional class for the returned subblocks...")
    info = lookup_subblocks(lrs_keys + mar_keys)

    comparison = pd.DataFrame({
        "OBJECTID": sample["OBJECTID"],
        "WorkLocationFullAddress": sample["WorkLocationFullAddress"],
        "ScriptRoute": sample["ROUTENAME"],
        "ScriptMatchMethod": sample["MatchMethod"],
        "ScriptClass": sample["FunctionalClassCode"],
        "LrsSubBlockKey": lrs_keys,
        "LrsRoute": [info.get(k, {}).get("ROUTENAME") for k in lrs_keys],
        "LrsRoadType": [ROAD_TYPE_NAMES.get(str(info.get(k, {}).get("ROADTYPE")))
                        for k in lrs_keys],
        "LrsClass": [info.get(k, {}).get("FHWAFUNCTIONALCLASS") for k in lrs_keys],
        "MarMatchedBy": mar_how,
        "MarSubBlockKey": mar_keys,
        "MarRoute": [info.get(k, {}).get("ROUTENAME") for k in mar_keys],
        "MarClass": [info.get(k, {}).get("FHWAFUNCTIONALCLASS") for k in mar_keys],
    })
    comparison.to_csv(COMPARISON_CSV, index=False)

    print("\nResults on the same sample")
    print("--------------------------")
    summarize(comparison, "Lrs", lrs_seconds)
    summarize(comparison, "Mar", mar_seconds)
    print(f"\nLRS matched an alley for {(comparison['LrsRoadType'] == 'Alley').sum()} points")
    print("MAR match source:")
    print(comparison["MarMatchedBy"].value_counts().to_string())
    print("\nAddresses MAR couldn't resolve (examples):")
    print(comparison.loc[comparison["MarMatchedBy"] == "No MAR match",
                         "WorkLocationFullAddress"].head(8).to_string(index=False))
    print(f"\nSaved {COMPARISON_CSV}")


if __name__ == "__main__":
    main()
