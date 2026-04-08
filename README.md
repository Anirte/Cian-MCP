# CIAN MCP Server

MCP server for searching and analyzing apartment listings on CIAN (Moscow) using the internal API.

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
Search for apartments with filtering options.

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
