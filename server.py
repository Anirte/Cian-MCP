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
from district_utils import get_okrug_id, get_district_id

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

@mcp.tool
def search_flats(
    page: int = 1,
    page_size: int = 5,
    rooms: Optional[int] = 1,
    max_price: Optional[int] = 12000000,
    min_price: Optional[int] = 5000000,
    min_floor: Optional[int] = 2,
    okrug: Optional[str] = "",
    district: Optional[str] = "",
    include_outside_mkad: Optional[bool] = False,
    include_shares: Optional[bool] = False,
    exclude_apartments: Optional[bool] = True
) -> str:
    """
    Search for apartments on CIAN.
    Returns a paginated list of apartments.
    Use 'page' parameter to get more results (page=1, 2, 3...).

    Args:
        okrug: Administrative district (e.g., 'юзао', 'зао', 'сао'). REQUIRED.
        rooms: Number of rooms. If not specified by user, defaults to 1.
        max_price: Maximum price in RUB. If user specifies a price, use it instead of default.
        min_price: Minimum price in RUB.
        min_floor: Minimum floor level.
        district: Specific city district or settlement name.
        include_outside_mkad: Set to True if you want to see properties outside MKAD.
        include_shares: Set to True if you want to see 'shares' (доли) of properties.
        exclude_apartments: If True (default), filters out 'апартаменты'. Set to False to include them.
    ...
    If the user provides specific price or room count, you MUST use those values.
    Always prioritize user input over default values.
    """

    if not okrug and not district:
        return json.dumps({"status": "error", "message": "Specify 'okrug' or 'district'."}, ensure_ascii=False)

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

    # 1. Формируем параметры для ключа кэша (БЕЗ page и page_size)
    cache_params = {
        "rooms": rooms, "max_price": max_price, "min_price": min_price,
        "min_floor": min_floor, "okrug": okrug, "district": district,
        "include_outside_mkad": include_outside_mkad,
        "include_shares": include_shares, "exclude_apartments": exclude_apartments
    }

    cache_key = cache.get_key(cache_params)
    cached_flats = cache.get(cache_key)

    try:
        if cached_flats is None:
            # Если данных нет в кэше, идем в ЦИАН (качаем 3 страницы)
            parser = get_parser()
            flats = parser.search_flats(
                rooms=rooms, max_price=max_price, min_price=min_price,
                min_floor=min_floor, pages=3, okrug=okrug, district=district
            )

            # Применяем фильтры ко всему списку
            if not include_outside_mkad:
                flats = [
                    f for f in flats
                    if f.get("mkad_distance_real") is None
                    or f.get("mkad_distance_real", 999) <= 0.5
                    or f.get("mkad_distance_real") == 999
                ]
            if not include_shares:
                flats = [f for f in flats if not f.get("is_share", False)]
            if exclude_apartments:
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
        results = []
        for flat in paginated_flats:
            ru_material = {"monolith": "монолит", "brick": "кирпич", "panel": "панель", "block": "блок"}.get(
                str(flat.get('material', '')).lower(), 'неизвестно'
            )
            dist = flat.get("mkad_distance_real")
            mkad_str = f", {dist} км от МКАД" if isinstance(dist, (int, float)) and dist < 500 else ""

            summary = (
                f"Метраж: {flat.get('total_meters', 0)} м², жилая {flat.get('living_meters', 0)} м², кухня {flat.get('kitchen_meters', 0)} м²\n"
                f"Здание: этаж {flat.get('floor', 0)}/{flat.get('floors_count', 0)}, тип здания {ru_material}\n"
                f"Ремонт: {flat.get('repair', 'н/д')}, Мебель: {flat.get('furniture', 'н/д')}\n"
                f"Удобства: Лифт - {flat.get('elevator_info', 'н/д')}, Балкон/лоджия - {flat.get('balcony_info', 'н/д')}, "
                f"Планировка: {'Есть' if flat.get('floor_plan_url') else 'Нет'}\n"
                f"Расположение: {flat.get('metro', 'н/д')}{mkad_str}"
            )

            results.append({
                "offer_id": str(flat.get("offer_id", "")),
                "district_id": flat.get("district_id"),
                "rooms_count": flat.get("rooms_count"),
                "summary": str(summary or "Нет описания"),
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
                "material": str(ru_material or "неизвестно"),
                "build_year": str(flat.get("build_year") or "н/д"),
                "metro": str(flat.get("metro") or "н/д"),
                "metro_walk_min": flat.get("metro_walk_min"),
                "address": str(flat.get("address") or "Адрес не указан"),
                "district": str(flat.get("district") or "н/д"),
                "url": str(flat.get("url") or "")
            })

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
    Load one apartment directly by a CIAN sale URL and save it to cache.
    Use this tool when the user provides a direct link like https://www.cian.ru/sale/flat/318640805/.
    Do not use search_flats for direct CIAN apartment links.
    """
    try:
        parser = get_parser()
        flat = parser.get_flat_by_url(url)

        if not flat:
            return json.dumps({
                "status": "error",
                "message": "Apartment was not found by this direct CIAN URL."
            }, ensure_ascii=False)

        cache_key = cache.get_key({
            "source": "direct_url",
            "offer_id": str(flat.get("offer_id"))
        })
        cache.set(cache_key, [flat])

        ru_material = {"monolith": "монолит", "brick": "кирпич", "panel": "панель", "block": "блок"}.get(
            str(flat.get('material', '')).lower(), 'неизвестно'
        )
        dist = flat.get("mkad_distance_real")
        mkad_str = f", {dist} км от МКАД" if isinstance(dist, (int, float)) and dist < 500 else ""

        summary = (
            f"Метраж: {flat.get('total_meters', 0)} м², жилая {flat.get('living_meters', 0)} м², кухня {flat.get('kitchen_meters', 0)} м²\n"
            f"Здание: этаж {flat.get('floor', 0)}/{flat.get('floors_count', 0)}, тип здания {ru_material}\n"
            f"Ремонт: {flat.get('repair', 'н/д')}, Мебель: {flat.get('furniture', 'н/д')}\n"
            f"Удобства: Лифт - {flat.get('elevator_info', 'н/д')}, Балкон/лоджия - {flat.get('balcony_info', 'н/д')}, "
            f"Планировка: {'Есть' if flat.get('floor_plan_url') else 'Нет'}\n"
            f"Расположение: {flat.get('metro', 'н/д')}{mkad_str}"
        )

        return json.dumps({
            "status": "ok",
            "message": "Apartment loaded and saved to cache.",
            "result": {
                "offer_id": str(flat.get("offer_id", "")),
                "district_id": flat.get("district_id"),
                "rooms_count": flat.get("rooms_count"),
                "summary": str(summary or "Нет описания"),
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
                "material": str(ru_material or "неизвестно"),
                "build_year": str(flat.get("build_year") or "н/д"),
                "metro": str(flat.get("metro") or "н/д"),
                "metro_walk_min": flat.get("metro_walk_min"),
                "address": str(flat.get("address") or "Адрес не указан"),
                "district": str(flat.get("district") or "н/д"),
                "url": str(flat.get("url") or url)
            }
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

        if not d_id:
            return json.dumps({"status": "error", "message": "No district ID"}, ensure_ascii=False)

        # 3. Запрос средней аренды (ТЕПЕРЬ 4 ПАРАМЕТРА)
        rent_data = parser.get_average_rent(int(rooms), int(d_id), float(area), walk_min)
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

        return json.dumps({
            "status": "ok",
            "district": d_name,
            "rooms": rooms,
            "avg_monthly_rent": avg_rent,
            "roi_percentage": round(roi, 2),
            "payback_years": round(payback, 1),
            "sample_size": rent_data['count']
        }, ensure_ascii=False)

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
