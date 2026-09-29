"""Download and cache weather history for districts ahead of time.

    python scripts/download_weather.py multan
    python scripts/download_weather.py "tando allahyar" skardu turbat
    python scripts/download_weather.py --province Sindh       # every district in a province

The app does this by itself the first time a district is used; this script is for
preparing a machine that will be offline later. Files land in data/cache/.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solarplan.history import available, cache_path, load_history  # noqa: E402
from solarplan.locations import all_locations, find  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("places", nargs="*", help="district or town names")
    ap.add_argument("--province", help="download every district in this province")
    args = ap.parse_args()

    locs = [find(p) for p in args.places]
    if args.province:
        locs += [loc for loc in all_locations() if loc.province.lower() == args.province.lower()]
    if not locs:
        ap.error("name at least one place, or --province")

    for loc in locs:
        if available(loc):
            print(f"{loc.district:24s} already here")
            continue
        try:
            w, elevation, source = load_history(loc)
        except OSError as e:
            print(f"{loc.district:24s} failed: {e}")
            continue
        print(f"{loc.district:24s} {w.index[0]:%Y}-{w.index[-1]:%Y} from {source}, {elevation:.0f} m, "
              f"saved as {cache_path(loc).name}")


if __name__ == "__main__":
    main()
