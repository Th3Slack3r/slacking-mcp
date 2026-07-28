# slacking.biz — MCP Server for SEC Financial Data

SEC financial data for AI agents — 16 MCP tools for company research directly from SEC EDGAR filings. Covers 120+ major US public companies with continuous updates.

**MCP Endpoint:** `https://slacking.biz/mcp`

| Resource | URL |
|----------|-----|
| 📚 Documentation | [https://slacking.biz/docs](https://slacking.biz/docs) |
| 🏪 Marketplace Dashboard | [https://slacking.biz/marketplace](https://slacking.biz/marketplace) |
| 🔌 MCP Endpoint | `https://slacking.biz/mcp` |

---

## Quick Start

### Test the MCP endpoint (initialize handshake)

```bash
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"client","version":"1.0"}}}'
```

### List available tools

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

### Compare two companies

```bash
curl -X POST https://slacking.biz/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"5","method":"tools/call","params":{"name":"compare_companies","arguments":{"tickers":["AAPL","MSFT","GOOGL"]}}}'
```

---

## Tools (16 Total)

### Company Analysis

| Tool | Description |
|------|-------------|
| `get_company_health` | Overall financial health score (A-F) with key metrics |
| `get_trends` | Financial trends over time — revenue, profit, margins |
| `industry_comparison` | Compare against industry averages |
| `get_revenue_segments` | Revenue breakdown by segment/product line |

### Financial Statements

| Tool | Description |
|------|-------------|
| `get_income_statement` | Detailed income statement |
| `get_balance_sheet` | Detailed balance sheet |
| `get_cash_flow` | Cash flow statement |
| `get_all_financial_data` | Everything in one call — health, income, balance sheet, cash flow, trends |

### Company Intelligence

| Tool | Description |
|------|-------------|
| `get_company_profile` | Sector, industry, employees, description |
| `get_filing_types` | Available SEC filing types |
| `search_filings` | Search filings by form type (10-K, 10-Q, 8-K) |
| `get_insider_trades` | Recent insider trading activity |
| `get_sentiment` | Sentiment analysis from filings |

### Multi-Company

| Tool | Description |
|------|-------------|
| `compare_companies` | Side-by-side financial comparison |
| `screen_companies` | Filter by financial criteria |
| `batch_query` | Multiple tickers + endpoints in one call |

---

## Data Source

All data sourced from SEC EDGAR filings (10-K, 10-Q, 8-K, Form 4, Form 13F) via the slacking.biz API. Covers 120+ major US public companies with continuous updates.

---

## Repository Contents

| File | Description |
|------|-------------|
| `mcp-server.py` | MCP Streamable HTTP server — JSON-RPC 2.0 protocol |
| `bridge-server.py` | SkillExchange HTTP bridge |
| `openapi.json` | OpenAPI 3.0 specification |
| `server.json` | MCP official registry manifest |
| `install.sh` | One-line MCP client configuration helper |

---

## License

MIT — see [LICENSE](LICENSE).
