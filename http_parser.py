# --- START OF FILE http_parser.py ---

"""
Парсер ЦИАН через API
"""
import json
import time
import datetime
import cloudscraper
# We still use our helper for district IDs
from district_utils import get_okrug_id, get_district_id

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

    def _get_json_payload(self, page, rooms, min_price, max_price, min_floor, okrug, district):
        """
        This function creates the special "request body" (payload) in the format
        that the Cian API understands.
        """
        json_data = {
            "jsonQuery": {
                "_type": "flatsale",
                "region": {"type": "terms", "value": [1]}, # 1 is Moscow
                "room": {"type": "terms", "value": [rooms]},
                "price": {"type": "range", "value": {"gte": min_price, "lte": max_price}},
                "floor": {"type": "range", "value": {"gte": min_floor}},
                "engine_version": {"type": "term", "value": 2},
                "page": {"type": "term", "value": page},
                "limit": {"type": "term", "value": 28}
            }
        }

        location_id = None
        if district:
            location_id = get_district_id(district)
        elif okrug:
            location_id = get_okrug_id(okrug)

        # FIX: The correct syntax for Cian API to filter by district/okrug
        if location_id:
            json_data['jsonQuery']['geo'] = {
                "type": "geo",
                "value": [{"type": "district", "id": location_id}]
            }
            logging.info(f"   Page {page}: Searching with location ID {location_id} via API")

        return json_data

    def search_flats(self, rooms=1, max_price=12000000, min_price=5000000, min_floor=2, pages=10, okrug=None, district=None):
        all_flats = []

        for page in range(1, pages + 1):
            # 1. Create the JSON payload for the current page
            payload = self._get_json_payload(page, rooms, min_price, max_price, min_floor, okrug, district)

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

    def get_average_rent(self, rooms: int, district_id: int, total_area: float, walk_min=None) -> dict:
        """
        Ищет похожие квартиры в аренду в том же районе с учетом площади и удаленности от метро.
        """
        try:
            # Считаем диапазон площади для точности (±30%)
            min_area = round(float(total_area) * 0.7)
            max_area = round(float(total_area) * 1.3)

            # ШАГ 1: "Прогрев" сессии для раздела АРЕНДЫ
            warmup_url = f"https://www.cian.ru/cat.php?deal_type=rent&offer_type=flat&region=1&district%5B0%5D={district_id}"
            self.scraper.get(warmup_url, timeout=10)

            # Базовые параметры запроса
            json_query = {
                "_type": "flatrent",
                "for_rent_main_type": {"type": "term", "value": "long"},
                "region": {"type": "terms", "value": [1]},
                "room": {"type": "terms", "value": [rooms]},
                "total_area": {"type": "range", "value": {"gte": min_area, "lte": max_area}},
                "engine_version": {"type": "term", "value": 2},
                "flat_share": {"type": "term", "value": False},
                "geo": {
                    "type": "geo",
                    "value": [{"type": "district", "id": district_id}]
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
            price = int(bargain.get('priceRur') or bargain.get('price') or 0)
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
            decor_raw = offer.get('decoration', 'н/д')
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
            photos = offer.get('photos', [])
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
            # Ищем Район/Поселение (нужно для API аренды)
            target_dist = next((d for d in districts_list if d.get('type') in ['raion', 'poselenie']), {})
            if not target_dist and districts_list: target_dist = districts_list[-1]

            district_id = target_dist.get('id')
            district_name = target_dist.get('name', 'н/д')

            # Формируем текстовый адрес
            address_list = [
                a.get('name', '') for a in geo_data.get('address', [])
                if a.get('type') in ['location', 'street', 'house']
            ]
            full_address = ", ".join(address_list)

            #  --- МКАД И шоссе ---
            highways = geo_data.get('highways', [])
            highway_info = "Не указано"
            min_dist_to_mkad = 999.0
            if highways:
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
                "mkad_distance": float(geo_data.get('distanceFromMkad') or 0),
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
        time = m.get('time', 0)

        t_type = m.get('transportType')

        if t_type == "walk":
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
