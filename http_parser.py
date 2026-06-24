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
        self.geo_tree_url = "https://www.cian.ru/api/geo/get-districts-tree/"

    def get_mo_city_districts(self, city_id: int):
        """
        Fetches and registers the microdistrict tree for a Moscow Oblast city.
        Returns the raw list (also cached into district_utils for name->id lookups
        via get_mo_district_id()).

        Example: get_mo_city_districts(174292) fetches all microdistricts of Балашиха
        (Кучино, Салтыковка, etc.) and registers them so get_mo_district_id(174292, "Кучино")
        works afterwards.
        """
        try:
            warmup_url = f"https://www.cian.ru/cat.php?deal_type=sale&offer_type=flat&region={REGION_MO}"
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

    def _resolve_geo_value(self, okrug, district, mo_city, mo_district):
        """
        Builds the `geo.value` list for jsonQuery based on which location params were given.

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
                           okrug, district, mo_city=None, mo_district=None):
        """
        This function creates the special "request body" (payload) in the format
        that the Cian API understands.
        """
        geo_value, region_id = self._resolve_geo_value(okrug, district, mo_city, mo_district)

        json_data = {
            "jsonQuery": {
                "_type": "flatsale",
                "region": {"type": "terms", "value": [region_id or REGION_MOSCOW]},
                "room": {"type": "terms", "value": [rooms]},
                "price": {"type": "range", "value": {"gte": min_price, "lte": max_price}},
                "floor": {"type": "range", "value": {"gte": min_floor}},
                "engine_version": {"type": "term", "value": 2},
                "page": {"type": "term", "value": page},
                "limit": {"type": "term", "value": 28}
            }
        }

        if geo_value:
            json_data['jsonQuery']['geo'] = {
                "type": "geo",
                "value": geo_value
            }
            logging.info(f"   Page {page}: Searching with geo {geo_value} (region {region_id}) via API")

        return json_data

    def search_flats(self, rooms=1, max_price=12000000, min_price=5000000, min_floor=2, pages=10,
                      okrug=None, district=None, mo_city=None, mo_district=None):
        all_flats = []

        for page in range(1, pages + 1):
            # 1. Create the JSON payload for the current page
            payload = self._get_json_payload(
                page, rooms, min_price, max_price, min_floor,
                okrug, district, mo_city, mo_district
            )

            try:
                # 2. Send a POST request to the API with our payload
                response = self.scraper.post(self.api_url, json=payload, timeout=20)

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
        Loads one apartment directly from a CIAN sale URL.
        """
        offer_id = self._extract_offer_id_from_url(url)
        page_url = f"https://www.cian.ru/sale/flat/{offer_id}/"

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
                    "_type": "flatsale",
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

    def _extract_offer_id_from_url(self, url: str) -> str:
        match = re.search(r"cian\.ru/sale/flat/(\d+)", str(url))
        if not match:
            raise ValueError("Please provide a direct CIAN apartment sale URL, for example https://www.cian.ru/sale/flat/318640805/.")
        return match.group(1)

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
                          region: int = 1, geo_type: str = "district") -> dict:
        """
        Ищет похожие квартиры в аренду в том же районе с учетом площади и удаленности от метро.

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
            warmup_url = f"https://www.cian.ru/cat.php?deal_type=rent&offer_type=flat&region={region}&district%5B0%5D={district_id}"
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

            # НОВОЕ: Умный фильтр по удаленности от метро
            if walk_min is not None:
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
                "design": "дизайнерский ремонт"
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

            # ВАЖНО: Правильная обработка комнатности
            rooms_count = offer.get('roomsCount')
            if offer.get('flatType') == 'studio':
                rooms_count = 9
            elif not rooms_count:
                rooms_count = 1

            # --- Район и адрес ---
            geo_data = offer.get('geo', {})
            districts_list = geo_data.get('districts', [])
            address_items = geo_data.get('address', [])

            is_moscow = any(
                item.get('type') == 'location' and (item.get('id') == 1 or item.get('name') == 'Москва')
                for item in address_items
            )
            is_mo = any(item.get('type') == 'location' and item.get('id') == 4593 for item in address_items)

            # Ищем район для последующего поиска аренды по нему.
            # Москва: districts[] содержит type 'raion'/'poselenie'.
            # Подмосковье: districts[] содержит type 'mikroraion' (только если у города
            # есть деление на микрорайоны, иначе districts[] может быть пустым).
            target_dist = next((d for d in districts_list if d.get('type') in ['raion', 'poselenie', 'mikroraion']), {})
            if not target_dist and districts_list:
                target_dist = districts_list[-1]
            if not target_dist:
                target_dist = next((d for d in address_items if d.get('type') in ['raion', 'poselenie', 'mikroraion']), {})

            district_id = target_dist.get('id')
            district_name = target_dist.get('name', 'н/д')

            # geo.type / region для последующего вызова get_average_rent():
            # - Москва: всегда geo.type "district", region 1
            # - МО + найден микрорайон: geo.type "district" (microdistrict id), region 4593
            # - МО без микрорайона (hasDistricts=False у города): фолбэк на сам ГОРОД,
            #   geo.type "location" (city id), region 4593
            if is_mo:
                rent_region = 4593
                if district_id and target_dist.get('type') == 'mikroraion':
                    rent_geo_type = "district"
                else:
                    # Фолбэк: ищем сам город (type='location', locationTypeId==1 у Cian
                    # обычно соответствует городу) среди address_items
                    city_item = next(
                        (a for a in address_items if a.get('type') == 'location' and a.get('id') != 4593),
                        {}
                    )
                    district_id = city_item.get('id') or district_id
                    district_name = city_item.get('name') or district_name
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
            highways = geo_data.get('highways', [])
            highway_info = "Не указано"
            mkad_distance = float(geo_data.get('distanceFromMkad') or 0) if is_moscow else None
            min_dist_to_mkad = mkad_distance
            if is_moscow and highways:
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