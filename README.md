# CIAN MCP Server

MCP server for searching and analyzing apartment listings on CIAN (all of Russia) using the internal API.

This project provides a set of tools exposed via Model Context Protocol (MCP) that allow querying real estate listings, retrieving price history, and performing basic investment analysis.

---

## Features

- Search apartments by district or administrative area
- Filter by price, rooms, floor, and other parameters
- Retrieve full price history for a listing
- Estimate rental yield and payback period
- In-memory caching for faster repeated queries

---

## Tech Stack

- Python
- FastMCP
- Cloudscraper (for interacting with CIAN API)
- Docker (for deployment)

---

## Project Structure

```
cian-mcp/
├─ server.py             # MCP server and tools
├─ http_parser.py        # CIAN API interaction
├─ district_utils.py     # District/okrug mapping
├─ districts.json        # Static data for locations
├─ pyproject.toml        # Dependencies
├─ uv.lock               # Locked dependency versions
├─ Dockerfile            # Container setup
└─ README.md
```

---

## Running Locally

### 1. Install dependencies

```
uv sync
```

### 2. Start the server

```
uv run python server.py
```

The server will start on:

```
http://localhost:8080/mcp
```

---

## Running with Docker

### Build image

```
docker build -t cian-mcp .
```

### Run container

```
docker run -p 8080:8080 cian-mcp
```

---

## Health Check

For simple verification (non-MCP):

```
http://localhost:8080/health
```

Expected response:

```
{"status":"ok"}
```

---

## MCP Endpoint

The MCP endpoint is available at:

```
/mcp
```

Example:

```
http://localhost:8080/mcp
```

Note: This endpoint is intended for MCP clients. It will not return meaningful data in a browser.

---

## Available Tools

### search_flats
Search for apartments with filtering options. Location is set by one of three groups:

- Moscow: `okrug` or `district`
- Moscow Oblast: `mo_city` (+ optional `mo_district`)
- Anywhere else in Russia: `city` (+ optional `region` to tell apart same-name cities, + optional `city_district`),
  or `region` alone for the whole region. Cities are found by name through the CIAN geo search, no local list needed.

Use `property_type="house"` to search houses and cottages instead of apartments. Houses have their own
parameters: `min_area`/`max_area` (house, m²), `min_land_area`/`max_land_area` (land plot, sotkas) and
`min_floors`; `rooms` and `min_floor` are ignored and the default price range is not applied.
You can also keep only houses with communications: `gas`, `electricity`, `water` (True).
House results have no metro, elevator or balcony, but contain `house_area_m2`, `land_area_sotki`,
`price_per_sotka`, `floors_count`, `bedrooms_count`, `material`, `heating`, `utilities` (gas, electricity,
water, sewerage, bathhouse, garage, pool, security; `н/д` means CIAN did not specify) and, in Moscow and
Moscow Oblast, `mkad_distance_km`. Houses are loaded through the CIAN mobile search, which has these fields.
The mobile search has no districts, so the district and link are taken from the desktop search of the same query
(one extra request per page); if that request fails, the district of a house is the whole city. If the mobile
search fails, the desktop search is used alone (without communications).

### load_flat_by_url
Load one offer by a direct CIAN link (`/sale/flat/<id>/` for apartments, `/sale/suburban/<id>/` for houses).
Outside Moscow CIAN may show an anti-bot check instead of the page; then the offer is not found.

### get_price_history
Retrieve full price history for a listing.

### analyze_investment
Estimate rental yield and payback period.

### clear_cache
Clear in-memory cache.

---

## Notes

- The project relies on CIAN internal API. Changes on their side may affect functionality.
- Cache is stored in memory and resets on restart.
- Designed for personal use or lightweight deployments.

---

## Deployment

The project is container-ready and can be deployed to:

- Render
- Cloud Run
- Railway
- Any Docker-compatible platform

---

## License

Private project. Use at your own discretion.
