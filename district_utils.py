# --- START OF FILE district_utils.py ---

import json
from typing import Optional, Dict

# This dictionary helps to find a full Okrug name (like "ЮЗАО") by its abbreviation (like "юзао")
# It's needed because users type abbreviations, but the JSON file contains full names.
OKRUG_ALIASES: Dict[str, str] = {
    "цао": "ЦАО",
    "сао": "САО",
    "свао": "СВАО",
    "вао": "ВАО",
    "ювао": "ЮВАО",
    "юао": "ЮАО",
    "юзао": "ЮЗАО",
    "зао": "ЗАО",
    "сзао": "СЗАО",
    "зелао": "ЗелАО",
    "нао": "НАО (Новомосковский)",
    "тао": "ТАО (Троицкий)",
}

# Cian's internal region IDs
REGION_MOSCOW = 1
REGION_MO = 4593

# Cian's geo.value "type" field depends on the level of the location, and this
# differs between Moscow and Moscow Oblast (confirmed empirically via live API tests):
#   - Moscow: okrug AND raion both use "district"
#   - Moscow Oblast: a whole CITY uses "location", but a MICRODISTRICT inside
#     that city uses "district" (the city's own "district" type returns 0 results)
MO_GEO_TYPE_CITY = "location"
MO_GEO_TYPE_MICRODISTRICT = "district"

# These will be our "lookup tables" to quickly find an ID by a name
_okrug_to_id: Dict[str, int] = {}
_district_to_id: Dict[str, int] = {}

# Lookup tables for Moscow Oblast (Подмосковье)
_mo_city_to_id: Dict[str, int] = {}
_mo_city_has_districts: Dict[int, bool] = {}
# Cache of district trees fetched per-city, populated lazily by the parser
# (mo_city_id -> {district_name_lower: district_id})
_mo_city_districts: Dict[int, Dict[str, int]] = {}


def _load_and_prepare_data():
    """
    This is an internal function that runs only once.
    It reads the districts.json file and prepares the data for quick searching.
    """
    global _okrug_to_id, _district_to_id

    # If the data is already loaded, do nothing
    if _okrug_to_id and _district_to_id:
        return

    try:
        with open('districts.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print("ERROR: districts.json file not found!")
        return

    temp_okrug_map = {}
    temp_district_map = {}

    # Перебираем все элементы верхнего уровня
    for item_data in data:
        item_type = item_data.get("type")
        item_name_lower = item_data["name"].lower()
        item_id = item_data["id"]

        if item_type == "Okrug":
            # Сохраняем округ
            temp_okrug_map[item_name_lower] = item_id

            # Перебираем детей (районы и поселения внутри округа)
            for district_data in item_data.get("childs", []):
                district_name_lower = district_data["name"].lower()
                district_id = district_data["id"]
                temp_district_map[district_name_lower] = district_id

        # Если на верхнем уровне лежит Район или Поселение (как "Новая Москва")
        elif item_type in ("Raion", "Poselenie"):
            temp_district_map[item_name_lower] = item_id

    # Add aliases to the okrug map so "юзао" can find the ID for "ЮЗАО"
    for alias, full_name in OKRUG_ALIASES.items():
        okrug_id = temp_okrug_map.get(full_name.lower())
        if okrug_id:
            temp_okrug_map[alias] = okrug_id

    # Assign the prepared data to our global variables
    _okrug_to_id = temp_okrug_map
    _district_to_id = temp_district_map


def _load_mo_cities():
    """
    Loads Moscow Oblast (Подмосковье) cities from mo_cities.json.
    This file has a flat structure: {"status": "ok", "data": {"items": [...]}}
    Each city has: id, name, hasDistricts (whether it has internal districts).
    """
    global _mo_city_to_id, _mo_city_has_districts

    if _mo_city_to_id:
        return

    try:
        with open('mo_cities.json', 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        print("ERROR: mo_cities.json file not found!")
        return

    items = raw.get("data", {}).get("items", [])

    temp_city_map = {}
    temp_has_districts = {}

    for city in items:
        name_lower = city["name"].lower()
        city_id = city["id"]
        temp_city_map[name_lower] = city_id
        temp_has_districts[city_id] = bool(city.get("hasDistricts"))

    _mo_city_to_id = temp_city_map
    _mo_city_has_districts = temp_has_districts


def get_okrug_id(name: str) -> Optional[int]:
    """
    Finds the ID for an Okrug by its name or abbreviation.
    Example: get_okrug_id("юзао") returns 10.
    """
    # .get() is a safe way to look up a value; it returns None if the key is not found
    return _okrug_to_id.get(name.lower())


def get_district_id(name: str) -> Optional[int]:
    """
    Finds the ID for a Raion (district) by its name.
    Example: get_district_id("Коньково") returns 103.
    """
    return _district_to_id.get(name.lower())


def get_mo_city_id(name: str) -> Optional[int]:
    """
    Finds the Cian locationId for a Moscow Oblast city by its name.
    Example: get_mo_city_id("Балашиха") returns 174292.
    """
    return _mo_city_to_id.get(name.lower())


def mo_city_has_districts(city_id: int) -> bool:
    """
    Returns True if the given MO city has internal districts
    (i.e. get-districts-tree?locationId={city_id} is expected to return data).
    """
    return _mo_city_has_districts.get(city_id, False)


def register_mo_city_districts(city_id: int, districts_tree: list) -> None:
    """
    Caches a fetched district tree for a given MO city, so get_mo_district_id()
    can look districts up by name afterwards.

    `districts_tree` is the raw JSON list returned by
    https://www.cian.ru/api/geo/get-districts-tree/?locationId={city_id}
    The parser is responsible for fetching this; this module only stores
    and indexes it once fetched, mirroring how districts.json is indexed.
    """
    name_to_id = {}

    def _walk(nodes):
        for node in nodes:
            name_to_id[node["name"].lower()] = node["id"]
            children = node.get("childs") or []
            if children:
                _walk(children)

    _walk(districts_tree or [])
    _mo_city_districts[city_id] = name_to_id


def get_mo_district_id(city_id: int, district_name: str) -> Optional[int]:
    """
    Finds a district's ID within a specific MO city.
    Requires register_mo_city_districts() to have been called for this city_id first
    (the parser fetches and registers the tree on demand, then caches it here).
    """
    return _mo_city_districts.get(city_id, {}).get(district_name.lower())


# This line calls the function to load and prepare data as soon as this file is imported.
# It ensures that our lookup tables are ready to use.
_load_and_prepare_data()
_load_mo_cities()

# --- END OF FILE district_utils.py ---