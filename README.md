# Utility Occupancy Permits – Functional Class Enrichment

This adds the FHWA functional class to 2026 Public Space Occupancy Permits for DC Water, PEPCO and Washington Gas. The output is a CSV with one row per permit location. Each row has the functional class at that location and the highest functional class on the permit.

The code is also on GitHub: https://github.com/spniroul/ddot-utility-permit

## How to run

You need Python 3.10 or newer on Windows.

**1. Get the code.** Either unzip the submitted folder, or get it from GitHub:

- With git:
  ```bat
  git clone https://github.com/spniroul/ddot-utility-permit.git
  cd ddot-utility-permit
  ```
- Without git: on the GitHub page, click **Code → Download ZIP**, unzip it, and open Command Prompt in the unzipped folder.

**2. Set up and run.** From that folder:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python enrich_permits.py
```

It takes a few minutes, mostly to download the road network. The result is saved to `output\utility_occupancy_permits_2026_functional_class.csv`. The `output` folder already holds the results from my run, so they can be checked without running anything; running the script overwrites them with fresh data. Paths are built relative to the script, so it doesn't matter where the folder lives.

After that, you can optionally run:

```bat
python compare_methods.py
```

This takes 100 random locations from the output and runs them through the two methods suggested in the instructions, the LRS API and the MAR address lookup, then compares them with the main script. Results go to `output\method_comparison_sample.csv`.

If you want to change the date, permit types, utility names or distance thresholds, they're all at the top of `enrich_permits.py`.

## What the script does

1. Downloads Construction Staging Area and Mobile Crane Work Zone permits from the TOPS occupancy layer. The service only returns 2,000 records per request, so the script gets the full list of IDs first and downloads in batches.
2. Keeps permits that were applied for or issued on or after Jan 1, 2026. The dates come back in UTC, so they're converted to Eastern time before filtering. Otherwise a permit filed late on Dec 31 would count as 2026.
3. Keeps only the three utilities. Their names aren't consistent in the data (`DCWater/CIP`, `DCWater/OPS`, `Pepco`, `Potomac Electric Power Company`, `Wash Gas & Light Co.`), so the script matches name patterns and prints every spelling it matched.
4. Removes duplicate points within the same permit. There were two.
5. Downloads the roadway subblocks for streets, service roads and ramps. Alleys are left out because they don't have a functional class.
6. Matches each permit location to a road and takes that road's functional class.
7. Takes the highest functional class on each permit. That's the lowest code, since 1 is Interstate and 7 is Local.

## How I matched permits to roads

The instructions suggested either the LRS API or a MAR address lookup. I tried both, and ended up downloading the subblocks and doing the matching locally with geopandas.

- **MAR** only works when the permit has a real street address. On a sample of 100 locations it resolved 13. Most staging permits are block ranges like "600 - 699 BLOCK OF 3RD STREET SW" or intersections, and those aren't MAR addresses.
- **The LRS** worked, but it's one web request per location (about 5,000 here), and it searches alleys too.
- **Downloading the subblocks** is one download, covers every location, and lets me control which road types count.

My first version just took the nearest road. To check it, I compared the matched road name with the street in the permit address, and they agreed about 97% of the time. Most of the misses were permits near a corner, where the cross street was a few meters closer. For example, 43 Rhode Island Ave NW was matching U St NW, which is Local, instead of Rhode Island Ave, which is a Principal Arterial. That error understates the work-hour restriction, so I added an address check:

- Read the street name from the address. If a road with that name is within 60 m, use it.
- If the address is an intersection ("K STREET NW AND 1ST STREET NW"), use the higher class of the two streets, since the work touches both.
- If there's no usable street name (ramp or exit descriptions, "ROOSEVELT BRIDGE"), fall back to the nearest road.

After the change, 99.4% of locations match their address street. The functional class changed for 63 locations (1.3%), mostly from Local up to an arterial or collector. The `MatchMethod` column shows which rule was used, and `NearestRoadwayClassCode` keeps the old nearest-road answer for comparison.

### Comparing the three methods

`compare_methods.py` runs the LRS API and the MAR lookup on the same 100 random locations. For both methods it takes the SUBBLOCKKEY they return and looks up its functional class on the subblock layer. For MAR it first tries the permit's `WorkLocationAddressID` as a MAR ID, then falls back to the address text.

- **LRS:** returned a road for all 100. 98 of those had a functional class, and all 98 agreed with my script. The other 2 landed on alleys, which have no class.
- **MAR:** resolved 13 of 100, and all 13 agreed with my script. The other 87 were block ranges (86) or an intersection (1). `WorkLocationAddressID` never matched a MAR ID, so all 13 came from the address text.
- **Where LRS and MAR disagreed:** there were three locations (1731 Rhode Island Ave NE, 1836 Ingleside Ter NW, 747 Alabama Ave SE). MAR agreed with my script every time. The LRS picked an alley for the first two and Wheeler Rd for the third. These are the corner cases the address check was added for.

So the LRS gives the same answers but needs a request per location and can land on alleys, and MAR can't handle most of these permits. The subblock approach matched both where they worked and covered every location.

## Results

- 4,981 locations on 3,129 permits: DC Water 1,568, Washington Gas 911, PEPCO 650.
- About half the locations are on Local streets.
- 53% of PEPCO permits touch a Minor Arterial or bigger road, compared with 27% for DC Water and 28% for Washington Gas. PEPCO will run into work-hour restrictions more often.
- 21 locations are more than 50 m from any road and are flagged `NeedsReview`. They still get the nearest road's class.

## Output columns

- **Permit:** `TrackingNumber` (one permit, which can have several locations), `PermitNumber`, `OBJECTID` (one location), `EventTypeDescription`, `StatusDescription`, `Utility`, `UtilityMatchedField`, `ApplicantCompany`, `PermitteeName`, `OwnerName`, and the application, issue, effective and expiration dates in Eastern time
- **Location:** `WorkLocationFullAddress`, `WorkLocationWard`, `Longitude`, `Latitude`, `LocationsOnPermit`
- **Road match:** `SUBBLOCKKEY`, `ROUTENAME`, `MatchMethod`, `NearestRoadwayClassCode`, `DistanceToRoadway_m`, `NeedsReview`
- **Functional class:** `FunctionalClassCode` and `FunctionalClassName` for the location, `HighestFunctionalClassCode` and `HighestFunctionalClassName` for the permit

Class codes: 1 Interstate, 2 Freeway/Expressway, 3 Principal Arterial, 4 Minor Arterial, 5 Major Collector, 6 Minor Collector, 7 Local.

## Assumptions and notes

- I read "permit applications or issued permits on or after Jan 1, 2026" as applied **or** issued.
- I kept every status, including Rejected, Withdrawn and Revoked, because the request covers applications. `StatusDescription` can be used to filter them out.
- When two roads are equally good matches, the higher class wins. For work-hour rules, that's the safer mistake.
- "Service lanes" are treated as the Service Road road type, the closest value in the data.
- The service doesn't seem to keep all of 2026. Very few applications are from before June, and most are June through October. The date filter is right; the older records just aren't in the layer.
- None of the three utilities had Mobile Crane Work Zone permits, so every row is a Construction Staging Area. The filter includes both types.
- `FROMSTREET` and `TOSTREET` on the subblock layer only say "Block Only", and `MultiAddresses` on the permit layer is empty, so I left them out.

## Files

- `enrich_permits.py`: main script
- `compare_methods.py`: optional comparison with the LRS API and MAR lookup
- `requirements.txt`
- `.gitignore`
- `output/`: results from my run (the main CSV and `method_comparison_sample.csv`); the scripts recreate them

## Use of AI

I used Claude to help write and debug the code and draft this README.
