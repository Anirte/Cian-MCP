"""
CIAN MCP Server - Headless Online Version
"""

import sys
import os

# --- БЛОК ДЛЯ HEADLESS РЕЖИМА (PYTHONW) ---
is_headless = sys.executable.endswith("pythonw.exe")
log_path = os.path.join(os.path.dirname(__file__), "server_debug.log")

if is_headless:
    f = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f
# ------------------------------------------

import json
import logging
import time
import hashlib
from typing import Optional
from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.responses import JSONResponse, FileResponse, HTMLResponse

from http_parser import CianHttpParser
from district_utils import get_okrug_id, get_district_id, get_mo_city_id, mo_city_has_districts

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
handlers = [logging.FileHandler(log_path, encoding='utf-8')]
if not is_headless:
    handlers.append(logging.StreamHandler(sys.stdout)) # Добавляем консоль только если есть консоль

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=handlers
)
logger = logging.getLogger(__name__)

# Кэш
class SearchCache:
    def __init__(self, ttl_seconds=300):
        self.cache = {}
        self.ttl = ttl_seconds

    def get_key(self, params):
        param_str = json.dumps(params, sort_keys=True, default=str)
        return hashlib.md5(param_str.encode()).hexdigest()

    def get(self, key):
        if key in self.cache:
            timestamp, data = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return data
            del self.cache[key]
        return None

    def set(self, key, data):
        self.cache[key] = (time.time(), data)

cache = SearchCache()


DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/*",
    "https://chat.openai.com/*",
    "http://localhost:*",
    "http://127.0.0.1:*",
    "cursor://anysphere.cursor-mcp/oauth/callback",
]


def load_env_file(file_path: str = ".env") -> None:
    if not os.path.exists(file_path):
        return

    with open(file_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def parse_redirect_uri_list(raw_value: str | None) -> list[str] | None:
    if raw_value is None or not raw_value.strip():
        return DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS

    normalized = raw_value.strip()
    if normalized.lower() in {"*", "all"}:
        return None

    normalized = normalized.replace("\n", ",").replace(";", ",")
    redirect_uris = [
        uri.strip()
        for uri in normalized.split(",")
        if uri.strip()
    ]

    return redirect_uris or DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS


load_env_file()

auth0_domain = os.environ["AUTH0_DOMAIN"]
auth0_client_id = os.environ["AUTH0_CLIENT_ID"]
auth0_client_secret = os.environ["AUTH0_CLIENT_SECRET"]
auth0_audience = os.environ["AUTH0_AUDIENCE"]
base_url = os.environ["BASE_URL"].rstrip("/")
jwt_signing_key = os.environ["JWT_SIGNING_KEY"]
allowed_client_redirect_uris = parse_redirect_uri_list(os.environ.get("ALLOWED_CLIENT_REDIRECT_URIS"))

token_verifier = JWTVerifier(
    jwks_uri=f"https://{auth0_domain}/.well-known/jwks.json",
    issuer=f"https://{auth0_domain}/",
    audience=auth0_audience,
)

auth = OAuthProxy(
    upstream_authorization_endpoint=f"https://{auth0_domain}/authorize",
    upstream_token_endpoint=f"https://{auth0_domain}/oauth/token",
    upstream_client_id=auth0_client_id,
    upstream_client_secret=auth0_client_secret,
    token_verifier=token_verifier,
    base_url=base_url,
    jwt_signing_key=jwt_signing_key,
    extra_authorize_params={"audience": auth0_audience},
    extra_token_params={"audience": auth0_audience},
    allowed_client_redirect_uris=allowed_client_redirect_uris,
)

mcp = FastMCP("CIAN Parser", auth=auth)

_parser = None


@mcp.custom_route("/", methods=["GET"])
async def index(request):
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Cian MCP</title>
            <link rel="icon" href="/favicon.ico">
        </head>
        <body>
            <h1>Cian MCP Server</h1>
            <p>MCP endpoint: <code>/mcp</code></p>
            <p>Health check: <code>/health</code></p>
        </body>
        </html>
        """
    )

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "ok"})

@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon(request):
    return FileResponse("favicon.ico")

def get_parser():
    global _parser
    if _parser is None:
        logger.info("Создание HTTP-парсера...")
        _parser = CianHttpParser()
    return _parser

RU_MATERIALS = {
    "monolith": "монолит", "brick": "кирпич", "panel": "панель", "block": "блок", "wood": "дерево",
    "foamConcreteBlock": "пенобетонный блок", "monolithBrick": "монолитно-кирпичный",
    "aerocreteBlock": "газобетонный блок", "wireframe": "каркасный",
}
HOUSE_CATEGORIES = {"houseSale": "дом", "cottageSale": "коттедж"}
LAND_STATUSES = {"individualHousingConstruction": "ИЖС"}
HEATING_TYPES = {
    "centralGas": "центральный газ", "autonomousGas": "автономный газ", "electric": "электрическое",
    "stove": "печное", "central": "центральное", "no": "нет",
}

GAS_TYPES = {"main": "магистральный", "border": "по границе участка"}
WATER_TYPES = {"central": "центральное", "borehole": "скважина", "well": "колодец"}
DRAINAGE_TYPES = {"central": "центральная", "cesspool": "выгребная яма", "septicTank": "септик"}

# Цена по умолчанию действует только для квартир; для домов границы цены не заданы
DEFAULT_FLAT_MIN_PRICE = 5000000
DEFAULT_FLAT_MAX_PRICE = 12000000


def _ru_material(flat: dict) -> str:
    material = str(flat.get("material") or "").strip()
    if not material:
        return "неизвестно"
    return RU_MATERIALS.get(material, RU_MATERIALS.get(material.lower(), material))


def _mkad_km(flat: dict):
    """Distance from MKAD in km if CIAN gave one (Moscow and Moscow Oblast), else None."""
    dist = flat.get("mkad_distance_real")
    return dist if isinstance(dist, (int, float)) and dist < 500 else None


def _house_type(flat: dict) -> str:
    category = flat.get("house_category")
    return HOUSE_CATEGORIES.get(category, category or "дом")


def _yes_no(value) -> str:
    return "есть" if value is True else "нет" if value is False else "н/д"


def _utility(kind_code, types: dict, has_flag) -> str:
    """One communication: the type if CIAN gave it, else есть/нет/н/д from the has-flag."""
    if has_flag is False:
        return "нет"
    if kind_code:
        return types.get(kind_code, kind_code)
    return _yes_no(has_flag)


def _build_utilities(flat: dict) -> dict:
    """Communications and extras of a house in Russian. 'н/д' means CIAN did not say."""
    u = flat.get("utilities") or {}
    return {
        "gas": _utility(u.get("gas"), GAS_TYPES, u.get("has_gas")),
        "electricity": _yes_no(u.get("has_electricity")),
        "water": _utility(u.get("water"), WATER_TYPES, u.get("has_water")),
        "sewerage": _utility(u.get("drainage"), DRAINAGE_TYPES, u.get("has_drainage")),
        "bathhouse": _yes_no(u.get("has_bathhouse")),
        "garage": _yes_no(u.get("has_garage")),
        "pool": _yes_no(u.get("has_pool")),
        "security": _yes_no(u.get("has_security")),
    }


def _build_summary(flat: dict) -> str:
    """Short human-readable description of an apartment or a house."""
    ru_material = _ru_material(flat)
    dist = _mkad_km(flat)
    mkad_str = f", {dist} км от МКАД" if dist is not None else ""

    if flat.get("property_type") == "house":
        land = flat.get("land_area")
        land_status = flat.get("land_status")
        land_str = f"{land:g} сот." if land else "н/д"
        if land_status:
            land_str += f" ({LAND_STATUSES.get(land_status, land_status)})"
        parts = [
            f"Тип: {_house_type(flat)}, площадь дома {flat.get('total_meters', 0)} м², участок {land_str}",
            f"Здание: этажей {flat.get('floors_count', 0)}, материал {ru_material}, год постройки {flat.get('build_year', 'н/д')}",
        ]
        if flat.get("bedrooms_count"):
            parts.append(f"Спален: {flat['bedrooms_count']}")
        if flat.get("heating"):
            parts.append(f"Отопление: {HEATING_TYPES.get(flat['heating'], flat['heating'])}")
        if flat.get("utilities"):
            u = _build_utilities(flat)
            parts.append(
                f"Коммуникации: газ - {u['gas']}, вода - {u['water']}, "
                f"канализация - {u['sewerage']}, электричество - {u['electricity']}"
            )
            extras = [name for key, name in (("bathhouse", "баня"), ("garage", "гараж"),
                                             ("pool", "бассейн"), ("security", "охрана"))
                      if u[key] == "есть"]
            if extras:
                parts.append(f"На участке: {', '.join(extras)}")
        parts.append(f"Расположение: {flat.get('address') or 'н/д'}{mkad_str}")
        return "\n".join(parts)

    return (
        f"Метраж: {flat.get('total_meters', 0)} м², жилая {flat.get('living_meters', 0)} м², кухня {flat.get('kitchen_meters', 0)} м²\n"
        f"Здание: этаж {flat.get('floor', 0)}/{flat.get('floors_count', 0)}, тип здания {ru_material}\n"
        f"Ремонт: {flat.get('repair', 'н/д')}, Мебель: {flat.get('furniture', 'н/д')}\n"
        f"Удобства: Лифт - {flat.get('elevator_info', 'н/д')}, Балкон/лоджия - {flat.get('balcony_info', 'н/д')}, "
        f"Планировка: {'Есть' if flat.get('floor_plan_url') else 'Нет'}\n"
        f"Расположение: {flat.get('metro', 'н/д')}{mkad_str}"
    )


def _build_flat_result(flat: dict, fallback_url: str = "") -> dict:
    return {
        "offer_id": str(flat.get("offer_id", "")),
        "property_type": "flat",
        "district_id": flat.get("district_id"),
        "rooms_count": flat.get("rooms_count"),
        "summary": _build_summary(flat) or "Нет описания",
        "price": int(flat.get("price") or 0),
        "price_per_m2": int(flat.get("price_per_m2") or 0),
        "total_meters": float(flat.get("total_meters") or 0),
        "living_meters": float(flat.get("living_meters") or 0),
        "kitchen_meters": float(flat.get("kitchen_meters") or 0),
        "elevator": str(flat.get("elevator_info", "н/д")),
        "balcony": str(flat.get("balcony_info", "н/д")),
        "repair": str(flat.get("repair", "н/д")),
        "furniture": str(flat.get("furniture", "н/д")),
        "floor": int(flat.get("floor") or 0),
        "floors_count": int(flat.get("floors_count") or 0),
        "material": _ru_material(flat),
        "build_year": str(flat.get("build_year") or "н/д"),
        "metro": str(flat.get("metro") or "н/д"),
        "metro_walk_min": flat.get("metro_walk_min"),
        "address": str(flat.get("address") or "Адрес не указан"),
        "district": str(flat.get("district") or "н/д"),
        "url": str(flat.get("url") or fallback_url)
    }


def _build_house_result(flat: dict, fallback_url: str = "") -> dict:
    """House: no rooms, floor, elevator, balcony, metro; land, heating and bedrooms instead."""
    result = {
        "offer_id": str(flat.get("offer_id", "")),
        "property_type": "house",
        "house_type": _house_type(flat),
        "district_id": flat.get("district_id"),
        "summary": _build_summary(flat),
        "price": int(flat.get("price") or 0),
        "price_per_m2": int(flat.get("price_per_m2") or 0),
        "price_per_sotka": flat.get("price_per_sotka"),
        "house_area_m2": float(flat.get("total_meters") or 0),
        "land_area_sotki": float(flat.get("land_area") or 0),
        "land_status": LAND_STATUSES.get(flat.get("land_status"), flat.get("land_status")),
        "floors_count": int(flat.get("floors_count") or 0),
        "bedrooms_count": flat.get("bedrooms_count"),
        "material": _ru_material(flat),
        "heating": HEATING_TYPES.get(flat.get("heating"), flat.get("heating")),
        "utilities": _build_utilities(flat),
        "build_year": str(flat.get("build_year") or "н/д"),
        "mkad_distance_km": _mkad_km(flat),
        "address": str(flat.get("address") or "Адрес не указан"),
        "district": str(flat.get("district") or "н/д"),
        "url": str(flat.get("url") or fallback_url)
    }
    # Ремонт и мебель у домов указывают редко: показываем только если есть
    for key in ("repair", "furniture"):
        if flat.get(key) not in (None, "н/д"):
            result[key] = str(flat[key])
    return result


def _build_result(flat: dict, fallback_url: str = "") -> dict:
    """JSON-ready dict for one offer: houses and apartments have different sets of fields."""
    if flat.get("property_type") == "house":
        return _build_house_result(flat, fallback_url)
    return _build_flat_result(flat, fallback_url)


@mcp.tool
def search_flats(
    page: int = 1,
    page_size: int = 5,
    rooms: Optional[int] = 1,
    max_price: Optional[int] = None,
    min_price: Optional[int] = None,
    min_floor: Optional[int] = 2,
    okrug: Optional[str] = "",
    district: Optional[str] = "",
    mo_city: Optional[str] = "",
    mo_district: Optional[str] = "",
    include_outside_mkad: Optional[bool] = False,
    include_shares: Optional[bool] = False,
    exclude_apartments: Optional[bool] = True,
    region: Optional[str] = "",
    city: Optional[str] = "",
    city_district: Optional[str] = "",
    property_type: Optional[str] = "flat",
    min_area: Optional[float] = None,
    max_area: Optional[float] = None,
    min_land_area: Optional[float] = None,
    max_land_area: Optional[float] = None,
    min_floors: Optional[int] = None,
    gas: Optional[bool] = None,
    electricity: Optional[bool] = None,
    water: Optional[bool] = None
) -> str:
    """
    Search for apartments or houses on CIAN anywhere in Russia: Moscow, Moscow Oblast (Подмосковье)
    or any other region/city.
    Returns a paginated list of offers.
    Use 'page' parameter to get more results (page=1, 2, 3...).

    Args:
        property_type: 'flat' (apartments, default) or 'house' (houses and cottages). Set 'house' when the user
            asks for a house, cottage or дом. For houses 'rooms', 'min_floor', 'include_shares' and 'exclude_apartments'
            are ignored (houses have no metro, elevator or balcony); use the house parameters below instead.
            Results for houses contain house_area_m2, land_area_sotki, price_per_sotka, floors_count, bedrooms_count,
            material, heating, utilities (gas, electricity, water, sewerage, bathhouse, garage, pool, security;
            'н/д' = CIAN did not specify) and mkad_distance_km (Moscow and Moscow Oblast only).
        min_area: Minimum area in m² (houses only: area of the house).
        max_area: Maximum area in m² (houses only: area of the house).
        min_land_area: Minimum land plot area in sotkas, 1 сотка = 100 m² (houses only).
        max_land_area: Maximum land plot area in sotkas (houses only). CIAN may return plots slightly above it.
        min_floors: Minimum number of floors of the house (houses only).
        gas: Set True to keep only houses with gas (houses only).
        electricity: Set True to keep only houses with electricity (houses only).
        water: Set True to keep only houses with water supply (houses only).
        city: City name ANYWHERE in Russia except Moscow and Moscow Oblast (e.g., 'Казань', 'Сочи', 'Екатеринбург', 'Санкт-Петербург').
        region: Region name (e.g., 'Татарстан', 'Краснодарский край'). Used together with 'city' to tell apart cities
            with the same name; used ALONE it searches the whole region.
        city_district: District or microdistrict inside 'city' (e.g., 'Вахитовский'). Optional, only valid together with 'city'.
            NOT for Moscow: for Moscow okrugs like ЗАО use 'okrug'.
        okrug: Moscow administrative okrug (e.g., 'юзао', 'зао', 'сао'). Use for searches WITHIN Moscow city.
        district: Specific Moscow city district or settlement name. Use for searches WITHIN Moscow city.
        mo_city: Moscow Oblast (Подмосковье) city name (e.g., 'Балашиха', 'Химки', 'Подольск'). REQUIRED for Oblast searches instead of okrug/district.
        mo_district: Microdistrict name within the mo_city (e.g., 'Кучино'). Optional, only valid together with mo_city.
        rooms: Number of rooms. If not specified by user, defaults to 1.
        max_price: Maximum price in RUB. For apartments defaults to 12,000,000 if not given; for houses no limit by default.
        min_price: Minimum price in RUB. For apartments defaults to 5,000,000 if not given; for houses no limit by default.
        min_floor: Minimum floor level (apartments only).
        include_outside_mkad: Set to True if you want to see properties outside MKAD. Not meaningful for mo_city searches.
        include_shares: Set to True if you want to see 'shares' (доли) of properties.
        exclude_apartments: If True (default), filters out 'апартаменты'. Set to False to include them.

    Exactly one location group must be specified: (okrug or district) for Moscow, OR mo_city for Moscow Oblast,
    OR (city and/or region) for the rest of Russia.
    If the user provides specific price or room count, you MUST use those values.
    Always prioritize user input over default values.
    """

    if property_type not in ("flat", "house"):
        return json.dumps({
            "status": "error",
            "message": "'property_type' must be 'flat' or 'house'."
        }, ensure_ascii=False)
    is_house = property_type == "house"

    if not is_house:
        if min_price is None:
            min_price = DEFAULT_FLAT_MIN_PRICE
        if max_price is None:
            max_price = DEFAULT_FLAT_MAX_PRICE

    # Слабые модели часто кладут округ/район Москвы в city_district вместо okrug/district.
    # Если 'city' не задан и других мест нет, то перенаправляем значение по смыслу.
    if city_district and not (city or region or mo_city or okrug or district):
        if get_okrug_id(city_district) is not None:
            okrug, city_district = city_district, ""
        elif get_district_id(city_district) is not None:
            district, city_district = city_district, ""

    is_moscow_search = bool(okrug or district)
    is_russia_search = bool(city or region)

    if not is_moscow_search and not mo_city and not is_russia_search:
        return json.dumps({
            "status": "error",
            "message": "Specify 'okrug' or 'district' for Moscow, 'mo_city' for Moscow Oblast, "
                       "or 'city' / 'region' for any other place in Russia."
        }, ensure_ascii=False)

    if sum([is_moscow_search, bool(mo_city), is_russia_search]) > 1:
        return json.dumps({
            "status": "error",
            "message": "Use only one location group: okrug/district (Moscow), mo_city (Moscow Oblast), or city/region (rest of Russia)."
        }, ensure_ascii=False)

    if city_district and not city:
        return json.dumps({
            "status": "error",
            "message": "'city_district' requires 'city' to be specified as well."
        }, ensure_ascii=False)

    if is_russia_search:
        _, _, place_error = get_parser().resolve_place(city, region, city_district)
        if place_error:
            return json.dumps({"status": "error", "message": place_error}, ensure_ascii=False)

    if district and get_district_id(district) is None:
        return json.dumps({
            "status": "error",
            "message": f"District '{district}' not found. Please check the spelling."
        }, ensure_ascii=False)

    if okrug and get_okrug_id(okrug) is None:
        return json.dumps({
            "status": "error",
            "message": f"Okrug '{okrug}' not found. Please use standard abbreviations (e.g., ЦАО, ЮЗАО)."
        }, ensure_ascii=False)

    if mo_city and get_mo_city_id(mo_city) is None:
        return json.dumps({
            "status": "error",
            "message": f"Moscow Oblast city '{mo_city}' not found. Please check the spelling."
        }, ensure_ascii=False)

    if mo_district and not mo_city:
        return json.dumps({
            "status": "error",
            "message": "'mo_district' requires 'mo_city' to be specified as well."
        }, ensure_ascii=False)

    # 1. Формируем параметры для ключа кэша (БЕЗ page и page_size)
    cache_params = {
        "rooms": rooms, "max_price": max_price, "min_price": min_price,
        "min_floor": min_floor, "okrug": okrug, "district": district,
        "mo_city": mo_city, "mo_district": mo_district,
        "include_outside_mkad": include_outside_mkad,
        "include_shares": include_shares, "exclude_apartments": exclude_apartments,
        "region": region, "city": city, "city_district": city_district,
        "property_type": property_type, "min_area": min_area, "max_area": max_area,
        "min_land_area": min_land_area, "max_land_area": max_land_area,
        "min_floors": min_floors, "gas": gas, "electricity": electricity, "water": water
    }

    cache_key = cache.get_key(cache_params)
    cached_flats = cache.get(cache_key)

    try:
        if cached_flats is None:
            # Если данных нет в кэше, идем в ЦИАН (качаем 3 страницы)
            parser = get_parser()
            flats = parser.search_flats(
                rooms=rooms, max_price=max_price, min_price=min_price,
                min_floor=min_floor, pages=3, okrug=okrug, district=district,
                mo_city=mo_city, mo_district=mo_district,
                city=city, region=region, city_district=city_district,
                property_type=property_type, min_area=min_area, max_area=max_area,
                min_land_area=min_land_area, max_land_area=max_land_area,
                min_floors=min_floors, gas=gas, electricity=electricity, water=water
            )

            # Применяем фильтры ко всему списку
            # MKAD-фильтр имеет смысл только для Москвы; для Подмосковья и других регионов
            # пропускаем его, т.к. mkad_distance_real там не отражает реальной границы поиска.
            if not include_outside_mkad and is_moscow_search:
                flats = [
                    f for f in flats
                    if f.get("mkad_distance_real") is None
                    or f.get("mkad_distance_real", 999) <= 0.5
                    or f.get("mkad_distance_real") == 999
                ]
            if not include_shares and not is_house:
                flats = [f for f in flats if not f.get("is_share", False)]
            if exclude_apartments and not is_house:
                flats = [f for f in flats if not f.get("is_apartment", False)]

            # Сортируем весь список
            flats.sort(key=lambda x: (x.get("price_per_m2", 999999999), x.get("price", 999999999)))

            # Сохраняем в кэш ГОТОВЫЙ список
            cached_flats = flats
            cache.set(cache_key, cached_flats)

        # 2. ПАГИНАЦИЯ (работаем с данными из кэша)
        total_found = len(cached_flats)
        start_idx = (page - 1) * page_size
        paginated_flats = cached_flats[start_idx : start_idx + page_size]

        # Подсчет количества страниц для ответа LLM
        total_pages = (total_found + page_size - 1) // page_size if total_found > 0 else 0

        # 3. Форматирование только для нужной страницы
        results = [_build_result(flat) for flat in paginated_flats]

        # Возвращаем метаданные о пагинации, чтобы LLM понимала, есть ли еще страницы
        result = json.dumps({
            "status": "ok",
            "page": page,
            "total_pages": total_pages,
            "total_found": total_found,
            "count_on_page": len(results),
            "results": results
        }, ensure_ascii=False)

        return result

    except Exception as e:
        logger.error(f"Search error: {e}")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

@mcp.tool
def load_flat_by_url(url: str) -> str:
    """
    Load one apartment or house directly by a CIAN sale URL and save it to cache.
    Use this tool when the user provides a direct link like https://www.cian.ru/sale/flat/318640805/
    (apartment) or https://www.cian.ru/sale/suburban/325992012/ (house).
    Do not use search_flats for direct CIAN links.
    """
    try:
        parser = get_parser()
        flat = parser.get_flat_by_url(url)

        if not flat:
            return json.dumps({
                "status": "error",
                "message": "Offer was not found by this direct CIAN URL. CIAN may have shown an anti-bot check "
                           "instead of the page (this happens outside Moscow); try again later or use search_flats."
            }, ensure_ascii=False)

        cache_key = cache.get_key({
            "source": "direct_url",
            "offer_id": str(flat.get("offer_id"))
        })
        cache.set(cache_key, [flat])

        return json.dumps({
            "status": "ok",
            "message": "Offer loaded and saved to cache.",
            "result": _build_result(flat, fallback_url=url)
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Direct URL load error: {e}")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

@mcp.tool
def get_price_history(offer_id: str) -> str:
    """
    Get the FULL price change history for a specific apartment by its Offer ID.
    This tool works even if the search was performed a long time ago.
    """
    try:
        parser = get_parser()

        # 1. Пытаемся найти адрес в кэше ТОЛЬКО для того, чтобы ответ был красивым
        # Если не найдем - не страшно, просто напишем "Квартира"
        address = "Квартира"
        for key in list(cache.cache.keys()):
            _, cached_data = cache.cache.get(key, (0, []))
            if isinstance(cached_data, list):
                # Ищем квартиру по ID (приводим оба к строке для надежности)
                match = next((f for f in cached_data if str(f.get('offer_id')) == str(offer_id)), None)
                if match:
                    address = match.get('address', 'Квартира')
                    break

        # 2. ГЛАВНОЕ: Вызываем наш сетевой метод.
        # Он сам зайдет на страницу и вытащит историю из Price Estimator API.
        logger.info(f"Запрос истории цен для ID {offer_id}...")
        history = parser.get_detailed_history(offer_id)

        return json.dumps({
            "status": "ok",
            "offer_id": offer_id,
            "address": address,
            "history": history
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"Error getting history for {offer_id}: {e}")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

@mcp.tool
def analyze_investment(offer_id: str) -> str:
    """Анализ окупаемости через аренду"""
    try:
        parser = get_parser()
        # 1. Достаем объект из кэша
        target_flat = None
        for key in list(cache.cache.keys()):
            _, data = cache.cache[key]
            if isinstance(data, list):
                target_flat = next((f for f in data if str(f.get('offer_id')) == offer_id), None)
                if target_flat: break

        if not target_flat:
            return json.dumps({"status": "error", "message": "Offer not found in cache"}, ensure_ascii=False)

        # 2. Получаем параметры
        d_id = target_flat.get('district_id')
        rooms = target_flat.get('rooms_count') or 1
        price = target_flat.get('price') or 0
        d_name = target_flat.get('district', 'н/д')
        area = target_flat.get('total_meters') or 30.0
        walk_min = target_flat.get('metro_walk_min') # <--- ИЗВЛЕКАЕМ МИНУТЫ ПЕШКОМ
        rent_region = target_flat.get('rent_region', 1)        # 1 = Москва, 4593 = МО
        rent_geo_type = target_flat.get('rent_geo_type', 'district')

        if not d_id:
            return json.dumps({"status": "error", "message": "No district ID"}, ensure_ascii=False)

        # 3. Запрос средней аренды
        rent_data = parser.get_average_rent(
            int(rooms), int(d_id), float(area), walk_min,
            region=rent_region, geo_type=rent_geo_type,
            property_type=target_flat.get('property_type') or 'flat'
        )
        avg_rent = rent_data['avg_price']

        if avg_rent == 0:
            return json.dumps({
                "status": "ok",
                "district": d_name,
                "message": "Нет подходящих данных по аренде (с учетом площади) в этом районе"
            }, ensure_ascii=False)

        # 4. Расчеты
        yearly = avg_rent * 12
        roi = (yearly / price) * 100
        payback = price / yearly

        analysis = {
            "status": "ok",
            "district": d_name,
            "avg_monthly_rent": avg_rent,
            "roi_percentage": round(roi, 2),
            "payback_years": round(payback, 1),
            "sample_size": rent_data['count']
        }
        if target_flat.get('property_type') != 'house':
            analysis["rooms"] = rooms
        return json.dumps(analysis, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

@mcp.tool
def clear_cache() -> str:
    """Очистить кэш"""
    cache.cache.clear()
    return json.dumps({"status": "ok", "message": "Кэш очищен"}, ensure_ascii=False)

# =================================================================
# CLOUD / REMOTE STARTUP
# =================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))

    try:
        logger.info(f"Starting MCP HTTP server on 0.0.0.0:{port}")
        mcp.run(
            transport="http",
            host="0.0.0.0",
            port=port,
            path="/mcp"
        )
    except Exception as e:
        logger.error(f"Startup error: {e}")