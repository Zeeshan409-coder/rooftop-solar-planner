import pytest

from solarplan.locations import all_locations, custom, find, tree
from solarplan.tariff import SUPPLIERS


def test_every_district_is_inside_pakistan_with_a_known_supplier():
    locs = all_locations()
    assert len(locs) > 170
    assert len({loc.id for loc in locs}) == len(locs)
    for loc in locs:
        assert 23.5 < loc.lat < 37.2 and 60.8 < loc.lon < 77.9, loc
        assert loc.supplier in SUPPLIERS, loc


def test_all_provinces_and_territories_are_covered():
    provinces = {p["province"] for p in tree()}
    assert provinces == {"Punjab", "Sindh", "Khyber Pakhtunkhwa", "Balochistan",
                         "Islamabad Capital Territory", "Gilgit-Baltistan", "Azad Kashmir"}


@pytest.mark.parametrize("name,district,supplier", [
    ("lahore", "Lahore", "LESCO"),
    ("Karachi", "Karachi South", "K-Electric"),
    ("nawabshah", "Shaheed Benazirabad", "HESCO"),     # by town
    ("sukkur", "Sukkur", "SEPCO"),
    ("abbottabad", "Abbottabad", "HAZECO"),
    ("hub", "Hub", "K-Electric"),
    ("skardu", "Skardu", "GB Power"),
    ("mirpur", "Mirpur", "AJK Power"),
    ("Dera Ismail Khan", "Dera Ismail Khan", "PESCO"),
    ("faisalbad", "Faisalabad", "FESCO"),               # typo
])
def test_find(name, district, supplier):
    loc = find(name)
    assert loc.district == district and loc.supplier == supplier


def test_unknown_place_suggests_close_names():
    with pytest.raises(KeyError, match="no district"):
        find("atlantis")


def test_custom_point_takes_the_nearest_supplier():
    loc = custom(31.40, 74.20)       # just outside Lahore
    assert loc.supplier == "LESCO"
    with pytest.raises(ValueError):
        custom(51.5, -0.1)
