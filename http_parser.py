# --- START OF FILE http_parser.py ---

"""
Парсер ЦИАН через API
"""
import json
import time
import datetime
import re
import cloudscraper
# We still use our helper for district IDs
from district_utils import (
    get_okrug_id, get_district_id,
    get_mo_city_id, mo_city_has_districts,
    get_mo_district_id, register_mo_city_districts,
    REGION_MOSCOW, REGION_MO,
    MO_GEO_TYPE_CITY, MO_GEO_TYPE_MICRODISTRICT,
)

PROPERTY_FLAT = "flat"
PROPERTY_HOUSE = "house"
# Cian object_type for houses and cottages in the suburban (загородная) category
HOUSE_OBJECT_TYPE = 1

import logging
logger = logging.getLogger(__name__)

class CianHttpParser:
    def __init__(self):
        # We don't need all the fancy headers for the API, but a good User-Agent is still key.
        self.scraper = cloudscraper.create_scraper()
        self.scraper.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 YaBrowser/24.1.0.0 Safari/537.36'
        })
        # The URL for Cian's internal API
        self.api_url = "https://api.cian.ru/search-offers/v2/search-offers-desktop/"
        # Mobile search returns the card-level fields of houses (gas, water, sewerage, electricity,
        # bathhouse, garage, repair...) that the desktop search does not have.
        self.api_url_house = "https://api.cian.ru/search-offers/v1/search-offers-mobile-site/"
        self.geo_tree_url = "https://www.cian.ru/api/geo/get-districts-tree/"
        # Search of regions/cities by name (works for the whole of Russia)
        self.geo_search_url = "https://www.cian.ru/cian-api/site/v1/search-regions-cities/"
        self._geo_search_cache = {}
        self._geo_warmed_up = False

    def get_mo_city_districts(self, city_id: int, region_id: int = REGION_MO):
        """
        Fetches and registers the district/microdistrict tree for a city
        (Moscow Oblast by default, any other Russian city if region_id is passed).
        Returns the raw list (also cached into district_utils for name->id lookups
        via get_mo_district_id()).

        Example: get_mo_city_districts(174292) fetches all microdistricts of Балашиха
        (Кучино, Салтыковка, etc.) and registers them so get_mo_district_id(174292, "Кучино")
        works afterwards.
        """
        try:
            warmup_url = f"https://www.cian.ru/cat.php?deal_type=sale&offer_type=flat&region={region_id}"
            self.scraper.get(warmup_url, timeout=10)

            headers = {
                "Accept": "application/json",
                "Referer": warmup_url,
                "Host": "www.cian.ru",
            }
            response = self.scraper.get(
                self.geo_tree_url,
                params={"locationId": city_id},
                headers=headers,
                timeout=15,
            )
            if response.status_code != 200:
                logging.info(f"   MO city districts fetch failed for city {city_id}: status {response.status_code}")
                return []

            tree = response.json()
            register_mo_city_districts(city_id, tree)
            return tree
        except Exception as e:
            logging.info(f"   Error fetching MO city districts for {city_id}: {e}")
            return []

    def _search_regions_cities(self, text: str):
        """
        Searches Cian regions and cities by name (any region of Russia).
        Returns the raw `items` list; successful answers are cached per query.
        Each item has: id, name, fullName, displayName, parentId (None for a region),
        hasDistricts.
        """
        key = text.strip().lower()
        if key in self._geo_search_cache:
            return self._geo_search_cache[key]

        try:
            if not self._geo_warmed_up:
                self.scraper.get("https://www.cian.ru/", timeout=10)
                self._geo_warmed_up = True

            response = self.scraper.get(
                self.geo_search_url,
                params={"text": text.strip()},
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if response.status_code != 200:
                logging.info(f"   Geo search for '{text}' failed: status {response.status_code}")
                return []

            items = response.json().get("data", {}).get("items", [])
            self._geo_search_cache[key] = items
            return items
        except Exception as e:
            logging.info(f"   Error searching region/city '{text}': {e}")
            return []

    def resolve_place(self, city=None, region=None, city_district=None):
        """
        Resolves a place anywhere in Russia by name.

        - region only        -> whole region (subject of the Federation), no geo filter
        - city               -> whole city, geo {"type": "location"}
        - city + city_district -> district/microdistrict of that city, geo {"type": "district"}
        `region` is also used to tell apart cities with the same name
        (e.g. Железногорск in Kursk and Krasnoyarsk regions).

        Returns (geo_value, region_id, error_message). On error the first two are None.
        """
        subject = None
        if region:
            region_l = region.strip().lower()
            subjects = [i for i in self._search_regions_cities(region) if not i.get("parentId")]
            found = [
                i for i in subjects
                if region_l in (i["name"].lower(), i["fullName"].lower(), i.get("displayName", "").lower())
            ] or [i for i in subjects if region_l in i["fullName"].lower()]

            if not found:
                return None, None, f"Region '{region}' not found. Please check the spelling."
            if len(found) > 1:
                names = ", ".join(i["fullName"] for i in found)
                return None, None, f"Region '{region}' is ambiguous: {names}. Please be more specific."
            subject = found[0]

            if not city:
                return None, subject["id"], None

        city_l = city.strip().lower()
        matches = [
            i for i in self._search_regions_cities(city)
            if city_l in (i["name"].lower(), i.get("displayName", "").lower())
        ]
        if subject:
            matches = [i for i in matches if i.get("parentId") == subject["id"] or i["id"] == subject["id"]]

        if not matches:
            hint = f" in region '{region}'" if region else ""
            return None, None, f"City '{city}' not found{hint}. Please check the spelling."
        if len(matches) > 1:
            names = ", ".join(f"{i.get('displayName') or i['name']} (id {i['id']})" for i in matches)
            return None, None, f"City '{city}' is ambiguous: {names}. Specify 'region' to choose."

        place = matches[0]
        region_id = place.get("parentId") or place["id"]

        if city_district:
            district_id = get_mo_district_id(place["id"], city_district)
            if district_id is None and place.get("hasDistricts"):
                self.get_mo_city_districts(place["id"], region_id)
                district_id = get_mo_district_id(place["id"], city_district)
            if not district_id:
                return None, None, f"District '{city_district}' not found in city '{place['name']}'."
            return [{"type": MO_GEO_TYPE_MICRODISTRICT, "id": district_id}], region_id, None

        return [{"type": MO_GEO_TYPE_CITY, "id": place["id"]}], region_id, None

    def _resolve_geo_value(self, okrug, district, mo_city, mo_district, city=None, region=None, city_district=None):
        """
        Builds the `geo.value` list for jsonQuery based on which location params were given.

        Other regions of Russia (confirmed empirically, same scheme as Moscow Oblast):
            - city                             -> {"type": "location", "id": <city_id>}
            - city + city_district             -> {"type": "district", "id": <district_id>}
            - region only                      -> no geo, only region=<subject id>
            region_id is the id of the region (subject) the city belongs to.

        Moscow:
            - district (Raion/Poselenie name) -> {"type": "district", "id": ...}
            - okrug (Administrative Okrug)     -> {"type": "district", "id": ...}
        Moscow Oblast (confirmed empirically, see project notes):
            - mo_city alone                    -> {"type": "location", "id": <city_id>}
            - mo_district (microdistrict)       -> {"type": "district", "id": <microdistrict_id>}
              (passing both city+district together does NOT work as an AND filter;
               only the microdistrict id alone correctly narrows results)

        Returns (geo_value_list, region_id) or (None, None) if nothing could be resolved.
        """
        # --- Other regions of Russia ---
        if city or region:
            geo_value, region_id, error = self.resolve_place(city, region, city_district)
            if error:
                raise ValueError(error)
            return geo_value, region_id

        # --- Moscow Oblast ---
        if mo_city:
            city_id = get_mo_city_id(mo_city)
            if not city_id:
                return None, None

            if mo_district:
                # Make sure we have the microdistrict tree for this city cached
                district_id = get_mo_district_id(city_id, mo_district)
                if district_id is None and mo_city_has_districts(city_id):
                    self.get_mo_city_districts(city_id)
                    district_id = get_mo_district_id(city_id, mo_district)

                if district_id:
                    return [{"type": MO_GEO_TYPE_MICRODISTRICT, "id": district_id}], REGION_MO
                # Fall back to searching the whole city if the microdistrict wasn't found
                logging.info(f"   MO microdistrict '{mo_district}' not found in city {mo_city}, falling back to city-wide search")

            return [{"type": MO_GEO_TYPE_CITY, "id": city_id}], REGION_MO

        # --- Moscow ---
        location_id = None
        if district:
            location_id = get_district_id(district)
        elif okrug:
            location_id = get_okrug_id(okrug)

        if location_id:
            return [{"type": "district", "id": location_id}], REGION_MOSCOW

        return None, None

    def _get_json_payload(self, page, rooms, min_price, max_price, min_floor,
                           okrug, district, mo_city=None, mo_district=None,
                           city=None, region=None, city_district=None,
                           property_type=PROPERTY_FLAT, house_filters=None):
        """
        This function creates the special "request body" (payload) in the format
        that the Cian API understands.

        For houses (property_type="house") the query type is `suburbansale` with
        object_type 1 (houses and cottages); rooms and floor filters do not apply,
        `house_filters` carries the area filters instead (see _house_filters()).
        """
        geo_value, region_id = self._resolve_geo_value(
            okrug, district, mo_city, mo_district, city, region, city_district
        )

        json_data = {
            "jsonQuery": {
                "_type": "flatsale",
                "region": {"type": "terms", "value": [region_id or REGION_MOSCOW]},
                "room": {"type": "terms", "value": [rooms]},
                "floor": {"type": "range", "value": {"gte": min_floor}},
                "engine_version": {"type": "term", "value": 2},
                "page": {"type": "term", "value": page},
                "limit": {"type": "term", "value": 28}
            }
        }
        # Цена необязательна: для домов границы могут быть не заданы
        price_filter = self._range_filter(min_price, max_price)
        if price_filter:
            json_data["jsonQuery"]["price"] = price_filter

        if property_type == PROPERTY_HOUSE:
            json_query = json_data["jsonQuery"]
            json_query["_type"] = "suburbansale"
            json_query["object_type"] = {"type": "terms", "value": [HOUSE_OBJECT_TYPE]}
            del json_query["room"]
            del json_query["floor"]
            json_query.update(house_filters or {})

        if geo_value:
            json_data['jsonQuery']['geo'] = {
                "type": "geo",
                "value": geo_value
            }
            logging.info(f"   Page {page}: Searching with geo {geo_value} (region {region_id}) via API")

        return json_data

    def _add_desktop_geo(self, offers, payload):
        """
        The mobile search has no districts in `geo` (and no fullUrl). The desktop search returns
        the same offers for the same query, so take `geo` and `fullUrl` from it, matched by offer id.
        If the desktop request fails the offers stay as they are (district = whole city).
        """
        try:
            response = self.scraper.post(self.api_url, json=payload, timeout=20)
            if response.status_code != 200:
                logging.info(f"   Desktop geo request returned {response.status_code}, districts will be city-level")
                return
            twins = {
                str(o.get('id')): o
                for o in (response.json().get('data', {}).get('offersSerialized') or [])
            }
        except Exception as e:
            logging.info(f"   Desktop geo request failed: {e}")
            return

        for offer in offers:
            twin = twins.get(str(offer.get('id')))
            if not twin:
                continue
            offer['geo'] = twin.get('geo') or offer.get('geo')
            if not offer.get('fullUrl'):
                offer['fullUrl'] = twin.get('fullUrl')

    @staticmethod
    def _range_filter(low=None, high=None):
        """{"type": "range", ...} with only the bounds that were given, or None if none."""
        bounds = {}
        if low is not None:
            bounds["gte"] = low
        if high is not None:
            bounds["lte"] = high
        return {"type": "range", "value": bounds} if bounds else None

    @classmethod
    def _house_filters(cls, min_area=None, max_area=None, min_land_area=None, max_land_area=None,
                       min_floors=None, gas=None, electricity=None, water=None):
        """
        Filters for houses: house area in m2 (`total_area`), land plot area in sotkas (`site`),
        number of floors of the house (`floorn`) and communications (`gas`, `electricity`,
        `has_water`; sent only when True). Only the bounds that were given are sent.
        """
        candidates = (
            ("total_area", cls._range_filter(min_area, max_area)),
            ("site", cls._range_filter(min_land_area, max_land_area)),
            ("floorn", cls._range_filter(min_floors)),
            ("gas", {"type": "term", "value": True} if gas else None),
            ("electricity", {"type": "term", "value": True} if electricity else None),
            ("has_water", {"type": "term", "value": True} if water else None),
        )
        return {name: value for name, value in candidates if value}

    def search_flats(self, rooms=1, max_price=12000000, min_price=5000000, min_floor=2, pages=10,
                      okrug=None, district=None, mo_city=None, mo_district=None,
                      city=None, region=None, city_district=None,
                      property_type=PROPERTY_FLAT, min_area=None, max_area=None,
                      min_land_area=None, max_land_area=None, min_floors=None,
                      gas=None, electricity=None, water=None):
        """
        Searches apartments (property_type="flat") or houses (property_type="house").
        Despite the name it returns houses too, in the same dict format.
        """
        all_flats = []
        is_house = property_type == PROPERTY_HOUSE
        house_filters = self._house_filters(
            min_area, max_area, min_land_area, max_land_area, min_floors, gas, electricity, water
        )

        for page in range(1, pages + 1):
            # 1. Create the JSON payload for the current page
            payload = self._get_json_payload(
                page, rooms, min_price, max_price, min_floor,
                okrug, district, mo_city, mo_district,
                city, region, city_district,
                property_type, house_filters
            )

            try:
                # 2. Send a POST request to the API with our payload
                used_mobile = is_house
                response = self.scraper.post(self.api_url_house if is_house else self.api_url,
                                             json=payload, timeout=20)
                if is_house and response.status_code not in (200, 429):
                    # Запасной вариант: обычный поиск (без коммуникаций, но с остальными данными)
                    logging.info(f"   Page {page}: mobile search returned {response.status_code}, falling back to desktop")
                    response = self.scraper.post(self.api_url, json=payload, timeout=20)
                    used_mobile = False

                if response.status_code == 429:
                    logging.info("   ⚠️  API rate limit hit (429). Stopping search.")
                    break
                if response.status_code != 200:
                    logging.info(f"   ⚠️ Page {page}: Received status code {response.status_code}")
                    continue

                # 3. Get the JSON data from the response
                data = response.json()

                # Extract the offers from the JSON response
                offers = data.get('data', {}).get('offersSerialized')

                if not offers:
                    logging.info(f"   Page {page}: No offers found in API response. Stopping search.")
                    break

                if used_mobile:
                    self._add_desktop_geo(offers, payload)

                # 4. Parse each offer from the JSON
                for offer_data in offers:
                    flat = self._parse_offer_json(offer_data)
                    if flat:
                        all_flats.append(flat)

                logging.info(f"   Page {page}: Found {len(offers)} offers via API. Total flats so far: {len(all_flats)}")
                time.sleep(1.5) # Be respectful to the API and wait a bit

            except Exception as e:
                logging.info(f"   Error on page {page} during API request: {e}")
                continue

        return all_flats

    def get_flat_by_url(self, url: str):
        """
        Loads one apartment or house directly from a CIAN sale URL
        (/sale/flat/<id>/ or /sale/suburban/<id>/).
        """
        category, offer_id = self._extract_offer_id_from_url(url)
        page_url = f"https://www.cian.ru/sale/{category}/{offer_id}/"
        query_type = "suburbansale" if category == "suburban" else "flatsale"

        try:
            page_response = self.scraper.get(page_url, timeout=10)
            if page_response.status_code == 200:
                flat = self._extract_offer_json_from_page(page_response.text, offer_id)
                if flat:
                    parsed_flat = self._parse_offer_json(flat)
                    if parsed_flat:
                        parsed_flat["url"] = page_url
                        return parsed_flat
        except Exception as e:
            logging.info(f"   Page warmup failed for offer {offer_id}: {e}")

        headers = {
            "Accept": "application/json",
            "Referer": page_url,
            "Host": "api.cian.ru"
        }

        id_filters = [
            ("offer_id", {"type": "terms", "value": [int(offer_id)]}),
            ("cian_id", {"type": "terms", "value": [int(offer_id)]}),
            ("cianId", {"type": "terms", "value": [int(offer_id)]}),
            ("offerId", {"type": "terms", "value": [int(offer_id)]}),
            ("id", {"type": "terms", "value": [int(offer_id)]}),
        ]

        for filter_name, filter_value in id_filters:
            payload = {
                "jsonQuery": {
                    "_type": query_type,
                    "region": {"type": "terms", "value": [1]},
                    "engine_version": {"type": "term", "value": 2},
                    "limit": {"type": "term", "value": 1},
                    filter_name: filter_value
                }
            }

            try:
                response = self.scraper.post(self.api_url, json=payload, headers=headers, timeout=20)
                if response.status_code != 200:
                    logging.info(f"   Direct load for offer {offer_id} with {filter_name}: status {response.status_code}")
                    continue

                data = response.json()
                offers = data.get('data', {}).get('offersSerialized', [])
                for offer_data in offers:
                    flat = self._parse_offer_json(offer_data)
                    if flat and str(flat.get("offer_id")) == offer_id:
                        flat["url"] = page_url
                        return flat
            except Exception as e:
                logging.info(f"   Direct load failed for offer {offer_id} with {filter_name}: {e}")

        return None

    def _extract_offer_id_from_url(self, url: str):
        """Returns (category, offer_id): category is 'flat' or 'suburban' (houses)."""
        match = re.search(r"cian\.ru/sale/(flat|suburban)/(\d+)", str(url))
        if not match:
            raise ValueError(
                "Please provide a direct CIAN sale URL, for example "
                "https://www.cian.ru/sale/flat/318640805/ or https://www.cian.ru/sale/suburban/325992012/."
            )
        return match.group(1), match.group(2)

    def _extract_offer_json_from_page(self, page_text: str, offer_id: str):
        decoder = json.JSONDecoder()
        target_indices = []

        for target in [f'"cianId":{offer_id}', f'"id":{offer_id}']:
            target_indices.extend(match.start() for match in re.finditer(re.escape(target), page_text))

        for target_index in reversed(target_indices):
            start_window = max(0, target_index - 150000)
            segment_before_target = page_text[start_window:target_index]
            object_starts = [match.start() + start_window for match in re.finditer(r"\{", segment_before_target)]

            for start in reversed(object_starts):
                try:
                    offer, _ = decoder.raw_decode(page_text[start:])
                except json.JSONDecodeError:
                    continue

                if not isinstance(offer, dict):
                    continue

                current_id = str(offer.get("cianId") or offer.get("id") or offer.get("offerId"))
                if current_id == offer_id and offer.get("bargainTerms"):
                    return offer

        return None

    def get_average_rent(self, rooms: int, district_id: int, total_area: float, walk_min=None,
                          region: int = 1, geo_type: str = "district",
                          property_type: str = PROPERTY_FLAT) -> dict:
        """
        Ищет похожие квартиры (или дома, если property_type="house") в аренду в том же районе
        с учетом площади и удаленности от метро.

        region: Cian region id (1 = Москва, 4593 = Московская область).
        geo_type: тип geo-объекта в district_id — "district" для района/микрорайона,
                  "location" для целого города (используется как фолбэк для городов МО
                  без деления на микрорайоны).
        """
        try:
            # Считаем диапазон площади для точности (±30%)
            min_area = round(float(total_area) * 0.7)
            max_area = round(float(total_area) * 1.3)

            # ШАГ 1: "Прогрев" сессии для раздела АРЕНДЫ
            is_house = property_type == PROPERTY_HOUSE
            offer_type = "suburban" if is_house else "flat"
            warmup_url = f"https://www.cian.ru/cat.php?deal_type=rent&offer_type={offer_type}&region={region}&district%5B0%5D={district_id}"
            self.scraper.get(warmup_url, timeout=10)

            # Базовые параметры запроса
            json_query = {
                "_type": "flatrent",
                "for_rent_main_type": {"type": "term", "value": "long"},
                "region": {"type": "terms", "value": [region]},
                "room": {"type": "terms", "value": [rooms]},
                "total_area": {"type": "range", "value": {"gte": min_area, "lte": max_area}},
                "engine_version": {"type": "term", "value": 2},
                "flat_share": {"type": "term", "value": False},
                "geo": {
                    "type": "geo",
                    "value": [{"type": geo_type, "id": district_id}]
                },
                "page": {"type": "term", "value": 1},
                "limit": {"type": "term", "value": 28}
            }

            if is_house:
                # Дома: другой тип запроса, долгосрочная аренда (for_day "!1" = не посуточная),
                # без комнат, долей и фильтров по метро
                json_query["_type"] = "suburbanrent"
                json_query["for_day"] = {"type": "term", "value": "!1"}
                json_query["object_type"] = {"type": "terms", "value": [HOUSE_OBJECT_TYPE]}
                for flat_only_key in ("for_rent_main_type", "room", "flat_share"):
                    json_query.pop(flat_only_key, None)
            # НОВОЕ: Умный фильтр по удаленности от метро
            elif walk_min is not None:
                # Если квартира в пешей доступности (например, 10 мин),
                # ищем аналоги пешком с запасом +5 мин
                search_walk_limit = max(walk_min + 7, 15)
                json_query["foot_min"] = {"type": "range", "value": {"lte": search_walk_limit}}
                json_query["only_foot"] = {"type": "term", "value": 2} # 2 = пешком
            else:
                # Если квартира транспортная, ищем аналоги на транспорте
                json_query["only_foot"] = {"type": "term", "value": 1} # 1 = транспортом

            payload = {
                "jsonQuery": json_query
            }

            headers = {
                "Accept": "application/json",
                "Referer": warmup_url,
                "Host": "api.cian.ru"
            }

            response = self.scraper.post(self.api_url, json=payload, headers=headers, timeout=15)
            if response.status_code != 200: return {"avg_price": 0, "count": 0}

            data = response.json()
            offers = data.get('data', {}).get('offersSerialized',[])
            if not offers: return {"avg_price": 0, "count": 0}

            prices =[]
            for offer in offers:
                # Фильтруем мусор
                if offer.get('flatType') == 'flatShare' or offer.get('shareAmount') is not None:
                    continue

                bt = offer.get('bargainTerms', {})
                p = bt.get('priceRur') or bt.get('price')

                if p:
                    # Санитарный фильтр: отсекаем комнаты и фейки (ниже 25к в Москве)
                    if int(p) < 25000: continue
                    prices.append(int(p))

            if not prices: return {"avg_price": 0, "count": 0}

            # Считаем МЕДИАНУ
            prices.sort()
            n = len(prices)
            if n % 2 == 1:
                median_price = prices[n//2]
            else:
                median_price = (prices[n//2 - 1] + prices[n//2]) / 2

            return {
                "avg_price": int(median_price),
                "count": len(prices)
            }
        except Exception as e:
            logging.error(f"Rent API Error: {e}")
            return {"avg_price": 0, "count": 0}

    def _parse_offer_json(self, offer):
        try:
            bargain = offer.get('bargainTerms', {})
            price = int(bargain.get('priceRur') or bargain.get('price') or bargain.get('prices', {}).get('rur') or 0)
            total = float(offer.get('totalArea') or 0)
            if price == 0 or total == 0: return None

            # --- Проверка на долю ---
            is_legal_share = offer.get('shareAmount') is not None or offer.get('flatType') == 'flatShare'

            # Словарь для перевода типов отделки
            decor_map = {
                "without": "без отделки (бетон)",
                "rough": "черновая отделка",
                "fine": "чистовая отделка",
                "cosmetic": "косметический ремонт",
                "euro": "евроремонт",
                "design": "дизайнерский ремонт",
                "no": "без ремонта"
            }
            decor_raw = offer.get('decoration') or offer.get('repairType') or 'н/д'
            repair_type = decor_map.get(decor_raw, decor_raw) # если кода нет в словаре, оставит как есть

            # Наличие мебели (в API это true/false/null)
            has_furniture = offer.get('hasFurniture')
            furniture_info = "Есть" if has_furniture is True else "Нет" if has_furniture is False else "н/д"

            # --- Парсинг информации о лифте ---
            building_data = offer.get('building', {})
            elevators = []
            if building_data.get('passengerLiftsCount'):
                elevators.append('пассажирский')
            if building_data.get('cargoLiftsCount'):
                elevators.append('грузовой')
            elevator_info = f"Есть ({', '.join(elevators)})" if elevators else "Нет"

            # --- Парсинг информации о балконах и лоджиях ---
            balconies = offer.get('balconiesCount', 0) or 0
            loggias = offer.get('loggiasCount', 0) or 0
            balcony_parts = []
            if balconies > 0:
                balcony_parts.append(f"Балкон ({balconies})")
            if loggias > 0:
                balcony_parts.append(f"Лоджия ({loggias})")
            balcony_info = ", ".join(balcony_parts) if balcony_parts else "Нет"

            # --- Парсинг информации о планировке ---
            floor_plan_url = None
            photos = offer.get('photos') or []
            for photo in photos:
                if photo.get('isLayout'): # <-- Ищем isLayout вместо isPlan
                    floor_plan_url = photo.get('fullUrl')
                    break

            # Очистка ссылки
            full_url = offer.get('fullUrl', '')
            clean_url = full_url.split('?')[0] if '?' in full_url else full_url
            if not clean_url:
                # Мобильный поиск не отдаёт fullUrl: собираем ссылку из id и типа объявления
                category_path = 'suburban' if offer.get('offerType') == 'suburban' else 'flat'
                offer_id_for_url = offer.get('id') or offer.get('cianId')
                if offer_id_for_url:
                    clean_url = f"https://www.cian.ru/sale/{category_path}/{offer_id_for_url}/"

            # Дом или квартира: у загородных объявлений offerType == 'suburban'
            is_house = offer.get('offerType') == 'suburban'

            # ВАЖНО: Правильная обработка комнатности
            rooms_count = offer.get('roomsCount')
            if offer.get('flatType') == 'studio':
                rooms_count = 9
            elif not rooms_count:
                rooms_count = None if is_house else 1

            # --- Участок (только у домов): площадь в сотках ---
            land = offer.get('land') or {}
            land_area = float(land.get('area') or 0)
            if land.get('areaUnitType') == 'hectare':
                land_area *= 100
            house_category = offer.get('category')  # houseSale / cottageSale / townhouseSale ...

            # --- Коммуникации и участок (только у домов, отдаёт мобильный поиск) ---
            # None = ЦИАН не указал (неизвестно), True/False = указано. Типы -- сырые коды ЦИАН.
            utilities = {
                "gas": (offer.get('gas') or {}).get('type'),
                "has_gas": offer.get('hasGas'),
                "has_electricity": offer.get('hasElectricity'),
                "water": (offer.get('water') or {}).get('suburbanWaterType'),
                "has_water": offer.get('hasWater'),
                "drainage": (offer.get('drainage') or {}).get('type'),
                "has_drainage": offer.get('hasDrainage'),
                "has_bathhouse": offer.get('hasBathhouse'),
                "has_garage": offer.get('hasGarage'),
                "has_pool": offer.get('hasPool'),
                "has_security": offer.get('hasSecurity'),
            }

            # --- Район и адрес ---
            geo_data = offer.get('geo', {})
            districts_list = geo_data.get('districts', [])
            address_items = geo_data.get('address', [])

            is_moscow = any(
                item.get('type') == 'location' and (item.get('id') == 1 or item.get('name') == 'Москва')
                for item in address_items
            )
            # Любой регион кроме Москвы (Подмосковье, Татарстан, Краснодарский край и т.д.):
            # address[0] -- сам регион (type='location'), дальше идёт город.
            location_items = [a for a in address_items if a.get('type') == 'location']
            is_regional = not is_moscow and bool(location_items)

            # Ищем район для последующего поиска аренды по нему.
            # Москва (старая, внутри МКАД): districts[] содержит type 'raion'/'poselenie'.
            # Подмосковье: districts[] содержит type 'mikroraion' (только если у города
            # есть деление на микрорайоны, иначе districts[] может быть пустым).
            # Новая Москва (НАО/ТАО) и прочие случаи без явного raion/poselenie/mikroraion:
            # 'districts' может отсутствовать вовсе (другая сериализация на HTML-странице
            # объявления), а в address[] нужный населённый пункт идёт как type='location'
            # (например "Кокошкино"), а не raion/poselenie.
            target_dist = next((d for d in districts_list if d.get('type') in ['raion', 'poselenie', 'mikroraion']), {})
            if not target_dist and districts_list:
                target_dist = districts_list[-1]
            if not target_dist:
                target_dist = next((d for d in address_items if d.get('type') in ['raion', 'poselenie', 'mikroraion']), {})

            used_location_fallback = False
            if not target_dist:
                # Фолбэк: самый детальный 'location' в адресе, который не сама Москва (id=1)
                # и не сам регион МО (id=4593). Берём последний по порядку (адреса у Cian
                # идут от общего к частному), это даёт наиболее точный населённый пункт/СНТ/ЖК.
                location_candidates = [
                    a for a in address_items
                    if a.get('type') == 'location' and a.get('id') not in (1, 4593)
                ]
                if location_candidates:
                    target_dist = location_candidates[-1]
                    used_location_fallback = True

            district_id = target_dist.get('id')
            district_name = target_dist.get('name', 'н/д')

            # geo.type / region для последующего вызова get_average_rent():
            # - Москва (raion/poselenie найден обычным способом): geo.type "district", region 1
            # - Москва, но сработал location-фолбэк (Новая Москва, поселения и т.п.):
            #   geo.type "location", region 1 -- т.к. сам geo-объект имеет type='location'
            # - МО + найден микрорайон: geo.type "district" (microdistrict id), region 4593
            # - МО без микрорайона (hasDistricts=False у города): фолбэк на сам ГОРОД,
            #   geo.type "location", region 4593
            if is_regional:
                # Регион объявления (4593 для МО, 4618 для Татарстана и т.д.) -- первый location в адресе
                subject_id = location_items[0].get('id')
                rent_region = subject_id
                if district_id and target_dist.get('type') in ('mikroraion', 'raion'):
                    rent_geo_type = "district"
                else:
                    # Фолбэк: ищем сам город (type='location') среди address_items.
                    # Если город не отличается от региона (Санкт-Петербург, Севастополь) -- берём его же.
                    city_item = next(
                        (a for a in location_items if a.get('id') != subject_id),
                        location_items[-1]
                    )
                    district_id = city_item.get('id') or district_id
                    district_name = city_item.get('name') or district_name
                    rent_geo_type = "location"
            elif used_location_fallback:
                rent_region = 1
                rent_geo_type = "location"
            else:
                rent_region = 1
                rent_geo_type = "district"


            # Формируем текстовый адрес
            address_list = [
                a.get('name', '') for a in geo_data.get('address', [])
                if a.get('type') in ['location', 'street', 'house']
            ]
            full_address = ", ".join(address_list)

            #  --- МКАД И шоссе ---
            # Москва: distanceFromMkad + шоссе. Подмосковье: только шоссе с расстоянием от МКАД.
            # В остальных регионах МКАД не имеет смысла.
            highways = geo_data.get('highways', [])
            highway_info = "Не указано"
            is_mo = is_regional and location_items[0].get('id') == REGION_MO
            mkad_distance = float(geo_data.get('distanceFromMkad') or 0) if is_moscow else None
            min_dist_to_mkad = mkad_distance
            if (is_moscow or is_mo) and highways:
                best_highway = min(highways, key=lambda x: float(x.get('distance', 999)))
                highway_info = f"{best_highway['name']} шоссе, {best_highway['distance']} км от МКАД"
                min_dist_to_mkad = float(best_highway['distance'])

            # ВЫЗЫВАЕМ ФУНКЦИЮ МЕТРО (ЭТОЙ СТРОКИ НЕ ХВАТАЛО)
            metro_data = self._get_metro_info(offer)

            return {
                # Собираем ID всех необходимых полей API
                "offer_id": str(offer.get('id') or offer.get('offerId') or offer.get('cianId')),
                "district_id": district_id,
                "district": district_name,
                "rent_region": rent_region,
                "rent_geo_type": rent_geo_type,
                "rooms_count": rooms_count,
                "is_share": is_legal_share,
                "price": price,
                "elevator_info": elevator_info,
                "balcony_info": balcony_info,
                "floor_plan_url": floor_plan_url,
                "repair": repair_type,
                "furniture": furniture_info,
                "price_per_m2": int(price / total),
                "total_meters": total,
                "metro_walk_min": metro_data["walk_min"],
                "living_meters": float(offer.get('livingArea') or 0),
                "kitchen_meters": float(offer.get('kitchenArea') or 0),
                "floor": int(offer.get('floorNumber') or 0),
                "floors_count": int(offer.get('building', {}).get('floorsCount') or 0),
                "material": offer.get('building', {}).get('materialType'),
                "build_year": offer.get('building', {}).get('buildYear') or "н/д",
                "metro": metro_data["display"],
                "mkad_distance": mkad_distance,
                "mkad_info": highway_info,
                "mkad_distance_real": min_dist_to_mkad,
                "address": full_address,
                "is_apartment": offer.get('isApartments', False),
                "property_type": PROPERTY_HOUSE if is_house else PROPERTY_FLAT,
                "house_category": house_category if is_house else None,
                "land_area": land_area if is_house else None,
                "land_status": land.get('status') if is_house else None,
                "bedrooms_count": offer.get('bedroomsCount') if is_house else None,
                "heating": building_data.get('heatingType') if is_house else None,
                "utilities": utilities if is_house else None,
                "price_per_sotka": int(price / land_area) if is_house and land_area else None,
                "url": clean_url
            }
        except Exception as e:
            logging.error(f"Ошибка при парсинге квартиры: {e}")
            return None

    def get_detailed_history(self, offer_id: str) -> str:
        """
        Получает полную историю цен через Price Estimator API.
        Использует двухшаговую схему для обхода защиты.
        """
        try:
            # Шаг 1: "Прогрев" сессии (заходим на страницу квартиры)
            page_url = f"https://www.cian.ru/sale/flat/{offer_id}/"
            self.scraper.get(page_url, timeout=10)

            # Шаг 2: Запрос к API истории
            api_url = f"https://api.cian.ru/price-estimator/v1/get-estimation-and-trend-web/?cianOfferId={offer_id}"
            headers = {
                "Accept": "application/json",
                "Referer": page_url,
                "Host": "api.cian.ru"
            }

            response = self.scraper.get(api_url, headers=headers, timeout=10)
            if response.status_code != 200:
                return "История временно недоступна"

            data = response.json()
            graphs = data.get('graphs', [])

            # Ищем самый длинный доступный период
            target_graph = None
            for period in ['estimatePeriodHalfYear', 'estimatePeriodMonth', 'estimatePeriodWeek']:
                target_graph = next((g for g in graphs if g.get('key') == period), None)
                if target_graph and target_graph.get('priceHistory'):
                    break

            if not target_graph:
                return "История изменений не найдена"

            history_points = target_graph.get('priceHistory', [])
            formatted_points = []
            last_price = None

            for point in history_points:
                price = int(point.get('price', 0))
                dt = datetime.datetime.fromtimestamp(point.get('date') / 1000.0)
                date_str = dt.strftime('%d.%m.%Y')

                if price != last_price:
                    formatted_points.append(f"{price:,} ₽ ({date_str})")
                    last_price = price

            return " ➔ ".join(formatted_points) if formatted_points else "Без изменений"
        except Exception as e:
            return f"Ошибка при получении истории: {e}"

    def _get_metro_info(self, offer):
        metros = offer.get('geo', {}).get('undergrounds',[])
        if not metros:
            return {"display": "Без метро", "walk_min": None}

        # Берем ближайшую станцию
        m = metros[0]
        name = m.get('name', 'н/д')
        time = m.get('time') or m.get('travelTime') or 0

        t_type = m.get('transportType') or m.get('travelType')

        if t_type in ["walk", "byFoot"]:
            type_str = "пешком"
            walk_min = time
        else:
            type_str = "транспортом"
            walk_min = None

        return {
            "display": f"{name} ({time} мин {type_str})",
            "walk_min": walk_min
        }

    def close(self):
        self.scraper.close()
# --- END OF FILE http_parser.py ---