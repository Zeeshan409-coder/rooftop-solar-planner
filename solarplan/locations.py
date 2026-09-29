"""Every district of Pakistan, with its main town and electricity supplier.

The table lives in data/pakistan_districts.csv (province, division, district, town,
lat, lon, supplier). Coordinates are those of the district headquarters; for solar
planning a few kilometres either way make no difference.
"""

from __future__ import annotations

import csv
import difflib
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

TABLE = Path(__file__).resolve().parent.parent / "data" / "pakistan_districts.csv"


@dataclass(frozen=True)
class Location:
    id: str
    province: str
    division: str
    district: str
    town: str
    lat: float
    lon: float
    supplier: str

    @property
    def label(self) -> str:
        if self.town.lower() in self.district.lower():
            return f"{self.district}, {self.province}"
        return f"{self.district} ({self.town}), {self.province}"

    def to_dict(self) -> dict:
        return asdict(self) | {"label": self.label}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@lru_cache(maxsize=1)
def all_locations() -> tuple[Location, ...]:
    with open(TABLE, newline="", encoding="utf-8") as f:
        return tuple(
            Location(slug(r["district"]), r["province"], r["division"], r["district"], r["town"],
                     float(r["lat"]), float(r["lon"]), r["supplier"])
            for r in csv.DictReader(f)
        )


# city names that cover more than one district
ALIASES = {"karachi": "karachi-south", "quetta": "quetta-east"}


def _index() -> dict[str, Location]:
    out = {}
    for loc in all_locations():
        out[loc.id] = loc
        out.setdefault(slug(loc.town), loc)     # "nawabshah", "mithi", "turbat"...
    for alias, target in ALIASES.items():
        out[alias] = out[target]
    return out


def find(name: str) -> Location:
    """Look a place up by district or town name, forgiving about case and spelling."""
    key = slug(name)
    index = _index()
    if key in index:
        return index[key]
    close = difflib.get_close_matches(key, index, n=3, cutoff=0.75)
    if len(close) == 1 or (close and difflib.SequenceMatcher(None, key, close[0]).ratio() > 0.9):
        return index[close[0]]
    hint = f" Did you mean {', '.join(index[c].district for c in close)}?" if close else ""
    raise KeyError(f"no district called '{name}'.{hint}")


def custom(lat: float, lon: float, name: str = "", supplier: str = "") -> Location:
    """A point that isn't a district headquarters; takes the supplier of the nearest district."""
    if not (23.5 <= lat <= 37.2 and 60.8 <= lon <= 77.9):
        raise ValueError("that point is outside Pakistan")
    nearest = min(all_locations(), key=lambda loc: (loc.lat - lat) ** 2 + (loc.lon - lon) ** 2)
    return Location(f"point-{lat:.2f}-{lon:.2f}", nearest.province, nearest.division,
                    name or f"{lat:.3f}, {lon:.3f}", name or nearest.town, lat, lon,
                    supplier or nearest.supplier)


def tree() -> list[dict]:
    """Provinces > divisions > districts, in table order, for the location picker."""
    provinces: dict[str, dict[str, list]] = {}
    for loc in all_locations():
        provinces.setdefault(loc.province, {}).setdefault(loc.division, []).append(
            {"id": loc.id, "district": loc.district, "town": loc.town, "supplier": loc.supplier,
             "lat": loc.lat, "lon": loc.lon})
    return [{"province": p, "divisions": [{"division": d, "districts": ds} for d, ds in divs.items()]}
            for p, divs in provinces.items()]
