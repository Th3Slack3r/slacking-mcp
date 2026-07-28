# slacking.biz — MCP Server for SEC Financial Data, FRED Economics & Census Demographics

**28 MCP tools** for **SEC financial data**, **FRED economic indicators**, and **Census demographics** — company health, financials, insider trades, GDP, CPI, employment, housing, ZIP/county data. MCP Streamable HTTP server.

[![MCP Protocol](https://img.shields.io/badge/MCP-Streamable%20HTTP-blue)](https://modelcontextprotocol.io)
[![Smithery](https://img.shields.io/badge/Smithery-Deployed-brightgreen)](https://smithery.ai/server/@Th3Slack3r/slacking-mcp)
[![Official Registry](https://img.shields.io/badge/MCP%20Registry-Registered-orange)](https://registry.modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**MCP Endpoint:** `https://slacking.biz/mcp`

| Resource | URL |
|----------|-----|
| 📚 Documentation | [https://slacking.biz/docs](https://slacking.biz/docs) |
| 🏪 Marketplace Dashboard | [https://slacking.biz/marketplace](https://slacking.biz/marketplace) |
| 🔌 MCP Endpoint | `https://slacking.biz/mcp` |
| 🤖 Smithery | [https://smithery.ai/server/@Th3Slack3r/slacking-mcp](https://smithery.ai/server/@Th3Slack3r/slacking-mcp) |
| 📋 MCP Official Registry | [https://registry.modelcontextprotocol.io](https://registry.modelcontextprotocol.io) |

---

## Table of Contents

- [MCP Installation](#mcp-installation-streamable-http--stdio)
- [SEC Financial Data Tools (16)](#sec-financial-data-tools---company-research)
- [World Bank International Data Tools (4)](#world-bank-international-data-tools)
- [FRED US Economics Data Tools (6)](#fred-us-economics-data-tools---economic-indicators)
- [Census Demographics Data Tools (2)](#census-demographics-data-tools)
- [Quick Start & Examples](#quick-start--examples)
- [Data Sources](#data-sources)
- [Repository Contents](#repository-contents)
- [License](#license)

---

## MCP Installation: Streamable HTTP + Stdio

### Streamable HTTP (Recommended — No Local Installation)

The `slacking.biz` MCP server is hosted at `https://slacking.biz/mcp`. Configure your MCP client:

**Claude Desktop:**
```json
{
  "mcpServers": {
    "slacking-biz": {
      "type": "streamable-http",
      "url": "https://slacking.biz/mcp"
    }
  }
}
```

**Cursor:** Settings → Features → MCP Servers → Add:
- Name: `slacking-biz`
- Type: `streamable-http`
- URL: `https://slacking.biz/mcp`

**Generic MCP client:**
```json
{
  "mcpServers": {
    "slacking-biz": {
      "type": "streamable-http",
      "url": "https://slacking.biz/mcp"
    }
  }
}
```

### Stdio (Local Python Server)

Run the MCP server locally with stdio transport:

```bash
# Install dependencies
pip install mcp httpx

# Run the server (requires SLACKING_API_KEY)
export SLACKING_API_KEY=your_key_here
python3 slacking_mcp_server.py
```

Configure your client:
```json
{
  "mcpServers": {
    "slacking-biz": {
      "command": "python3",
      "args": ["/path/to/slacking_mcp_server.py"],
      "env": {
        "SLACKING_API_KEY": "your_key_here"
      }
    }
  }
}
```

---

## SEC Financial Data Tools (16) — Company Research

Analyze US public companies using SEC EDGAR filings (10-K, 10-Q, 8-K, Form 4, Form 13F). Covers 120+ major companies across 51 industries.

### Financial Health & Ratios

| Tool | Description |
|------|-------------|
| `get_company_health` | Overall financial health score (A-F) with key metrics, ratios, and interpretation |
| `get_trends` | Financial trends over 6+ quarters — revenue, profit, margins, EPS trajectory |
| `industry_comparison` | Compare company metrics against industry averages (same SIC code) |
| `get_revenue_segments` | Revenue breakdown by product/service segments and geography |

### Financial Statements — GAAP Data

| Tool | Description |
|------|-------------|
| `get_income_statement` | Detailed income statement — revenue, COGS, R&D, SG&A, operating income, net income, EPS |
| `get_balance_sheet` | Balance sheet — assets, liabilities, equity, cash, debt, retained earnings |
| `get_cash_flow` | Cash flow statement — operating, investing, financing activities, free cash flow |
| `get_all_financial_data` | All 500+ GAAP financial data points from SEC XBRL filings in one call |

### Company Reference & SEC Filings

| Tool | Description |
|------|-------------|
| `get_company_profile` | Company metadata — CIK, SIC code, industry, exchanges, address, website |
| `get_filing_types` | Categorized summary of all SEC filing types a company has ever filed |
| `search_filings` | Search SEC filings by form type (10-K, 10-Q, 8-K, Form 4, 13F, S-1, etc.) |
| `get_insider_trades` | Recent insider trading transactions from SEC Form 4 filings |
| `get_sentiment` | Sentiment analysis from SEC filing text (Loughran-McDonald financial dictionary) |

### Multi-Company & Screening

| Tool | Description |
|------|-------------|
| `compare_companies` | Side-by-side financial health comparison of up to 10 companies |
| `screen_companies` | Screen/filter 120+ companies by profit margin, revenue growth, debt ratio, grade |
| `batch_query` | Query multiple tickers and endpoints in a single batch request |

---

## World Bank International Data Tools (4)

Access global economic indicators for 55+ countries via the World Bank Open Data API (CC BY 4.0).

| Tool | Description |
|------|-------------|
| `worldbank_list_countries` | List all 55+ available country/region codes and names |
| `worldbank_country_economics` | Key economic indicators for any country — GDP, GDP growth, population, inflation, unemployment, trade |
| `worldbank_specific_indicator` | Time-series data for a specific indicator (14 indicators: gdp, gdp_growth, gdp_per_capita, population, inflation_cpi, unemployment, exports, imports, fdi, poverty, life_expectancy, education_expenditure) |
| `worldbank_compare_countries` | Compare GDP, GDP growth, and population across multiple countries |

---

## FRED US Economics Data Tools (6) — Economic Indicators

US macroeconomic data sourced from the Federal Reserve Economic Data (FRED) API.

| Tool | Description |
|------|-------------|
| `get_gdp` | US GDP data — nominal GDP, real GDP, quarterly growth rates |
| `get_inflation` | US CPI / inflation data — Consumer Price Index from the Bureau of Labor Statistics |
| `get_rates` | US interest rates — Federal Funds rate, 30-year mortgage rate, 10-year Treasury yield |
| `get_employment` | US employment data — unemployment rate, initial jobless claims |
| `get_housing` | US housing market data — housing starts, 30-year mortgage rates |
| `get_econ_summary` | All key US economic indicators in one call — GDP, CPI, Fed funds rate, unemployment, housing |

---

## Census Demographics Data Tools (2)

US demographics data sourced from the US Census Bureau American Community Survey (ACS) 5-year estimates.

| Tool | Description |
|------|-------------|
| `get_zip_demographics` | ZIP code demographics — population, median income, median age, housing units, median home value, median rent, education levels |
| `get_county_demographics` | County demographics by FIPS code — population, median income, median age, housing units, median home value, median rent, education levels |

---

## Quick Start & Examples

### Test the MCP endpoint (initialize handshake)

```bash
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"client","version":"1.0"}}}'
```

### List all 28 available tools

```bash
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"2","method":"tools/list","params":{}}'
```

### Call a tool — get Apple's financial health

```bash
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"3","method":"tools/call","params":{"name":"get_company_health","arguments":{"ticker":"AAPL"}}}'
```

### Get insider trades for Tesla

```bash
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"4","method":"tools/call","params":{"name":"get_insider_trades","arguments":{"ticker":"TSLA","limit":5}}}'
```

### Compare three companies side-by-side

```bash
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"5","method":"tools/call","params":{"name":"compare_companies","arguments":{"tickers":["AAPL","MSFT","GOOGL"]}}}'
```

### Get US economic indicators (GDP, inflation, rates, employment, housing)

```bash
# All indicators in one call
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"6","method":"tools/call","params":{"name":"get_econ_summary","arguments":{}}}'

# Specific economic data
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"7","method":"tools/call","params":{"name":"get_gdp","arguments":{}}}'
```

### Get demographics for a ZIP code

```bash
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"8","method":"tools/call","params":{"name":"get_zip_demographics","arguments":{"zip_code":"90210"}}}'
```

---

## Data Sources

| Data Source | Coverage | Update Frequency |
|-------------|----------|-----------------|
| **SEC EDGAR** — 10-K, 10-Q, 8-K, Form 4, Form 13F | 120+ major US public companies | Continuous (filings processed daily) |
| **FRED** — Federal Reserve Economic Data | US GDP, CPI, interest rates, employment, housing | Updated as released by BLS, Fed, Census |
| **US Census Bureau** — American Community Survey (ACS) | ZIP code and county demographics | Annual (5-year estimates) |
| **World Bank** — Open Data API | 55+ countries, 14 economic indicators | Quarterly/Annual |

---

## Repository Contents

| File | Description |
|------|-------------|
| `mcp-server.py` | MCP Streamable HTTP server — JSON-RPC 2.0 protocol for remote HTTP access |
| `slacking_mcp_server.py` | MCP stdio server — all 28 tool definitions using FastMCP |
| `bridge-server.py` | SkillExchange HTTP bridge |
| `openapi.json` | OpenAPI 3.0 specification |
| `server.json` | MCP Official Registry manifest |
| `glama.json` | Glama MCP server manifest |
| `install.sh` | One-line MCP client configuration helper |

---

## License

MIT — see [LICENSE](LICENSE).
