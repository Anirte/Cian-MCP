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

# These will be our "lookup tables" to quickly find an ID by a name
_okrug_to_id: Dict[str, int] = {}
_district_to_id: Dict[str, int] = {}

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

# This line calls the function to load and prepare data as soon as this file is imported.
# It ensures that our lookup tables are ready to use.
_load_and_prepare_data()

# --- END OF FILE district_utils.py ---
