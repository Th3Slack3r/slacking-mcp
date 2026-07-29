#!/usr/bin/env python3
"""MCP Streamable HTTP server for slacking.biz — per-request API key auth.
Compatible with Smithery, MCP.so, Official MCP Registry, and SkillExchange.

Endpoints:
  POST /mcp       — MCP Streamable HTTP endpoint (JSON-RPC 2.0)
  GET  /health    — Health check

API key extraction (checked in order):
  1. X-API-Key header
  2. Authorization: Bearer <key> header
  3. api_key query parameter (for URL-based configs)
  4. SLACKING_API_KEY env var (fallback for stdio/local usage)

Marketplace platforms (Smithery, etc.) pass the user's API key via
Authorization header or X-API-Key header, which is then used for
per-user rate limiting and plan enforcement by the REST API.

Usage:
  python3 mcp-server.py                        # No default key
  SLACKING_API_KEY=xxx python3 mcp-server.py   # Fallback key for local use
"""

import json
import os
import sys
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import httpx

API_BASE = os.environ.get("SLACKING_API_BASE", "http://localhost:8001")
FALLBACK_API_KEY = os.environ.get("SLACKING_API_KEY", "")
PORT = int(os.environ.get("MCP_PORT", 8003))

# ─── MCP Protocol Constants ───────────────────────────────────────────
MCP_PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "slacking-biz"
SERVER_VERSION = "1.2.0"

# ─── Tool Definitions ──────────────────────────────────────────────────
TOOLS = {
    "get_company_health": {
        "endpoint": "/v1/financial/{ticker}",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get a company's overall financial health score (A-F grade) including summary metrics",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL, MSFT, TSLA)"}
            },
            "required": ["ticker"]
        }
    },
    "get_trends": {
        "endpoint": "/v1/financial/{ticker}/trends",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get financial trends over time for a company — revenue, profit, margins across reporting periods",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "industry_comparison": {
        "endpoint": "/v1/financial/{ticker}/vs-industry",
        "method": "GET",
        "params": ["ticker"],
        "description": "Compare a company's financial metrics against its industry averages",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_revenue_segments": {
        "endpoint": "/v1/financial/{ticker}/segments",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get revenue breakdown by product/service segments for a company",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_income_statement": {
        "endpoint": "/v1/financial/{ticker}/income",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get detailed income statement for a company (revenue, expenses, earnings per share)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_balance_sheet": {
        "endpoint": "/v1/financial/{ticker}/balance",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get detailed balance sheet for a company (assets, liabilities, equity)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_cash_flow": {
        "endpoint": "/v1/financial/{ticker}/cashflow",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get detailed cash flow statement for a company (operating, investing, financing)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_all_financial_data": {
        "endpoint": "/v1/financial/{ticker}/all",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get all financial data (income statement, balance sheet, cash flow) in one call",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_company_profile": {
        "endpoint": "/v1/financial/{ticker}/profile",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get company profile — sector, industry, employees, description, market cap",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_filing_types": {
        "endpoint": "/v1/financial/{ticker}/filings/types",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get filing types available for a company (10-K, 10-Q, 8-K, etc.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "search_filings": {
        "endpoint": "/v1/financial/{ticker}/filings",
        "method": "GET",
        "params": ["ticker"],
        "description": "Search filings for a company — search by form type, date range, keywords. Use get_filing_types first to see what's available.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"},
                "form_type": {"type": "string", "description": "Form type filter (e.g. 10-K, 10-Q, 8-K)"},
                "limit": {"type": "number", "description": "Number of filings to return (default 10)"}
            },
            "required": ["ticker"]
        }
    },
    "get_insider_trades": {
        "endpoint": "/v1/financial/{ticker}/insider",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get recent insider trading activity for a company (Form 4 filings)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_sentiment": {
        "endpoint": "/v1/financial/{ticker}/sentiment",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get sentiment analysis from SEC filings for a company — positive/negative/neutral signals from 10-K/10-Q reports",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "compare_companies": {
        "endpoint": "/v1/financial/compare",
        "method": "GET",
        "params": ["tickers"],
        "description": "Compare financial metrics across multiple companies side by side",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tickers": {"type": "string", "description": "Comma-separated ticker symbols (e.g. AAPL,MSFT,GOOGL)"}
            },
            "required": ["tickers"]
        }
    },
    "screen_companies": {
        "endpoint": "/v1/financial/screen",
        "method": "GET",
        "params": [],
        "description": "Screen companies by financial criteria (high growth, low debt, profitable, high margin, cash rich, undervalued)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "screen": {"type": "string", "description": "Screen type (growth, value, quality, momentum)"},
                "limit": {"type": "number", "description": "Max results (default 20)"}
            }
        }
    },
    "batch_query": {
        "endpoint": "/v1/financial/batch",
        "method": "POST",
        "params": ["tickers"],
        "description": "Batch query multiple tickers at once for efficiency",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tickers": {"type": "string", "description": "Comma-separated ticker symbols (e.g. AAPL,MSFT,GOOGL)"}
            },
            "required": ["tickers"]
        }
    },
    # ── Foreign Exchange Tools (Frankfurter — Free, no key needed) ──
    "fx_get_rates": {
        "endpoint": "/v1/fx/rates",
        "method": "GET",
        "params": [],
        "description": "Get live exchange rates for any currency. Sources: European Central Bank + 84 central banks, 201 currencies. No API key needed — free for commercial use.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "Base currency code (e.g. USD, EUR, GBP). Default: EUR"},
                "quotes": {"type": "string", "description": "Comma-separated target currencies to filter (e.g. USD,EUR,GBP). Empty = all."}
            }
        }
    },
    "fx_get_pair_rate": {
        "endpoint": "/v1/fx/rate/{from_currency}/{to_currency}",
        "method": "GET",
        "params": ["from_currency", "to_currency"],
        "description": "Get the exchange rate between any two currencies. Sources: ECB + 84 central banks. Free for commercial use.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_currency": {"type": "string", "description": "Source currency code (e.g. USD, EUR, GBP)"},
                "to_currency": {"type": "string", "description": "Target currency code (e.g. EUR, JPY, BRL)"}
            },
            "required": ["from_currency", "to_currency"]
        }
    },
    "fx_convert": {
        "endpoint": "/v1/fx/convert",
        "method": "GET",
        "params": [],
        "description": "Convert an amount from one currency to another at current exchange rates. Sources: ECB + 84 central banks. Free for commercial use.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_currency": {"type": "string", "description": "Source currency code (e.g. USD, EUR, GBP). Default: USD"},
                "to_currency": {"type": "string", "description": "Target currency code (e.g. EUR, JPY, BRL). Default: EUR"},
                "amount": {"type": "number", "description": "Amount to convert (e.g. 100, 2500.50). Default: 1.0"}
            }
        }
    },
    "fx_list_currencies": {
        "endpoint": "/v1/fx/currencies",
        "method": "GET",
        "params": [],
        "description": "List all 31 available currency codes and names for foreign exchange queries. Covers USD, EUR, GBP, JPY, CNY, and more.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    # ── Economic Data Tools (FRED) ──
    "get_gdp": {
        "endpoint": "/v1/econ/gdp",
        "method": "GET",
        "params": [],
        "description": "Get latest US GDP data from FRED",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "get_inflation": {
        "endpoint": "/v1/econ/inflation",
        "method": "GET",
        "params": [],
        "description": "Get latest CPI/inflation data from FRED",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "get_rates": {
        "endpoint": "/v1/econ/rates",
        "method": "GET",
        "params": [],
        "description": "Get latest Fed funds rate, mortgage rates, and treasury yields from FRED",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "get_employment": {
        "endpoint": "/v1/econ/employment",
        "method": "GET",
        "params": [],
        "description": "Get latest unemployment rate and jobless claims from FRED",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "get_housing": {
        "endpoint": "/v1/econ/housing",
        "method": "GET",
        "params": [],
        "description": "Get latest housing starts and existing home sales data from FRED",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "get_econ_summary": {
        "endpoint": "/v1/econ/summary",
        "method": "GET",
        "params": [],
        "description": "Get all key economic indicators (GDP, CPI, rates, employment, housing) in one call from FRED",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    # ── Demographics Tools (Census) ──
    "get_zip_demographics": {
        "endpoint": "/v1/demo/zip/{zip_code}",
        "method": "GET",
        "params": ["zip_code"],
        "description": "Get demographics for a ZIP code (population, income, age, housing, education) from the US Census Bureau",
        "inputSchema": {
            "type": "object",
            "properties": {
                "zip_code": {"type": "string", "description": "5-digit US ZIP code (e.g. '90210', '10001')"}
            },
            "required": ["zip_code"]
        }
    },
    "get_county_demographics": {
        "endpoint": "/v1/demo/county/{fips}",
        "method": "GET",
        "params": ["fips"],
        "description": "Get county-level demographics data by FIPS code from the US Census Bureau",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fips": {"type": "string", "description": "5-digit county FIPS code (e.g. '06037' for Los Angeles County, CA)"}
            },
            "required": ["fips"]
        }
    }
}

# ─── API Key Extraction ────────────────────────────────────────────────

def extract_api_key(headers, query_params=None):
    """Extract API key from request, checking multiple sources in priority order.
    
    Checks:
    1. X-API-Key header (standard)
    2. Authorization: Bearer <key> header
    3. Authorization: Basic base64(<key>:) header
    4. api_key query parameter
    5. FALLBACK_API_KEY env var (for local/stdio usage)
    """
    # 1. X-API-Key header
    api_key = headers.get("X-API-Key", "") or headers.get("x-api-key", "")
    if api_key:
        return api_key.strip()
    
    # 2. Authorization: Bearer
    auth = headers.get("Authorization", "") or headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    
    # 3. Authorization: Basic
    if auth.startswith("Basic "):
        try:
            import base64
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            # Format: "key:" or "key:anything"
            key = decoded.split(":")[0]
            if key:
                return key.strip()
        except Exception:
            pass
    
    # 4. Query parameter
    if query_params:
        qp_key = query_params.get("api_key", [None])[0]
        if qp_key:
            return qp_key.strip()
    
    # 5. Fallback env var (for local/stdio usage)
    if FALLBACK_API_KEY:
        return FALLBACK_API_KEY
    
    return ""


# ─── MCP JSON-RPC Messages ──────────────────────────────────────────────

def make_jsonrpc_error(code, message, id=None):
    return {
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message}
    }

def make_jsonrpc_result(result, id=None):
    return {
        "jsonrpc": "2.0",
        "id": id,
        "result": result
    }


# ─── MCP Handlers ───────────────────────────────────────────────────────

def handle_initialize(params, msg_id):
    """MCP initialize handshake — public, no auth required."""
    client_version = params.get("protocolVersion", "unknown")
    client_info = params.get("clientInfo", {})
    client_name = client_info.get("name", "unknown")
    
    print(f"  [mcp] initialize: client={client_name} proto={client_version}", file=sys.stderr)
    
    return make_jsonrpc_result({
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {}
        },
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION
        }
    }, msg_id)

def handle_resources_list(params, msg_id):
    return make_jsonrpc_result({"resources": []}, msg_id)

def handle_prompts_list(params, msg_id):
    return make_jsonrpc_result({"prompts": []}, msg_id)

def handle_tools_list(params, msg_id):
    """List all available tools with schemas — public, no auth required."""
    tool_list = []
    for name, tool in TOOLS.items():
        tool_list.append({
            "name": name,
            "description": tool["description"],
            "inputSchema": tool["inputSchema"]
        })
    
    return make_jsonrpc_result({"tools": tool_list}, msg_id)

def handle_tools_call(params, msg_id, api_key):
    """Execute a tool — requires API key for data access."""
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})
    
    if not tool_name:
        return make_jsonrpc_error(-32602, "Tool name is required", msg_id)
    
    tool = TOOLS.get(tool_name)
    if not tool:
        return make_jsonrpc_error(-32602, f"Unknown tool: {tool_name}", msg_id)
    
    # Auth check — API key required for data calls
    if not api_key:
        return make_jsonrpc_error(
            -32000,
            "API key required. Sign up at https://slacking.biz to get your free API key. "
            "Then pass it via X-API-Key header or Authorization: Bearer <key>",
            msg_id
        )
    
    print(f"  [mcp] tools/call: {tool_name} args={json.dumps(arguments)[:200]}", file=sys.stderr)
    
    try:
        result = call_api(tool, arguments, api_key)
        return make_jsonrpc_result({
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ],
            "isError": "detail" in result or ("error" in result and result.get("error"))
        }, msg_id)
    except Exception as e:
        return make_jsonrpc_error(-32000, f"Execution error: {str(e)}", msg_id)


# ─── REST API Proxy ─────────────────────────────────────────────────────

def call_api(tool, params, api_key):
    """Call the underlying REST API for a tool, using the request's API key."""
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json"
    }
    
    if tool["method"] == "GET":
        endpoint = tool["endpoint"]
        query_params = {}
        for p in tool["params"]:
            if p in params:
                placeholder = "{" + p + "}"
                if placeholder in endpoint:
                    endpoint = endpoint.replace(placeholder, str(params[p]))
                else:
                    query_params[p] = params[p]
        
        # Remove any unfilled placeholders (for optional params not provided)
        endpoint = re.sub(r'\{[^}]+\}', '', endpoint)
        
        if query_params:
            qs = "&".join(f"{k}={v}" for k, v in query_params.items())
            separator = "&" if "?" in endpoint else "?"
            endpoint = f"{endpoint}{separator}{qs}"
        
        response = httpx.get(f"{API_BASE}{endpoint}", headers=headers, timeout=30)
        return response.json()
    
    elif tool["method"] == "POST":
        body = {}
        for p in tool["params"]:
            if p in params:
                body[p] = params[p]
        response = httpx.post(f"{API_BASE}{tool['endpoint']}", json=body, headers=headers, timeout=60)
        return response.json()


# ─── HTTP Handler ───────────────────────────────────────────────────────

class MCPHandler(BaseHTTPRequestHandler):
    """HTTP server that handles MCP Streamable HTTP transport."""
    
    def _get_api_key(self):
        """Extract API key from this request."""
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)
        return extract_api_key(self.headers, query_params)
    
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        
        if path in ("/health", "/mcp/health"):
            api_key = self._get_api_key()
            self._respond(200, {
                "status": "ok",
                "server": SERVER_NAME,
                "version": SERVER_VERSION,
                "tools": len(TOOLS),
                "authenticated": bool(api_key),
                "protocol": "MCP Streamable HTTP",
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "auth_method": "per-request (X-API-Key header or Authorization: Bearer)"
            })
        else:
            self._respond(404, {"error": "not_found"})
    
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        
        if path not in ("/mcp", "/mcp/"):
            self._respond(404, {"error": "not_found"})
            return
        
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, make_jsonrpc_error(-32700, "Parse error"))
            return
        
        if not isinstance(request, dict) or "method" not in request:
            self._respond(400, make_jsonrpc_error(-32600, "Invalid Request"))
            return
        
        msg_id = request.get("id")
        method = request["method"]
        params = request.get("params", {})
        
        # Extract API key for this request
        api_key = self._get_api_key()
        
        # Route to handler
        if method == "initialize":
            response = handle_initialize(params, msg_id)
        elif method == "resources/list":
            response = handle_resources_list(params, msg_id)
        elif method == "prompts/list":
            response = handle_prompts_list(params, msg_id)
        elif method == "tools/list":
            response = handle_tools_list(params, msg_id)
        elif method == "tools/call":
            response = handle_tools_call(params, msg_id, api_key)
        else:
            response = make_jsonrpc_error(-32601, f"Method not found: {method}", msg_id)
        
        self._respond(200, response)
    
    def _respond(self, status, data):
        body_bytes = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body_bytes)
    
    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()
    
    def log_message(self, format, *args):
        sys.stderr.write(f"[mcp-server] {args[0]} {args[1]} {args[2]}\n")


# ─── Entry Point ────────────────────────────────────────────────────────

def main():
    print(f"🚀 MCP Streamable HTTP server running on port {PORT}", file=sys.stderr)
    print(f"   Endpoint: http://localhost:{PORT}/mcp", file=sys.stderr)
    print(f"   Health:   http://localhost:{PORT}/health", file=sys.stderr)
    print(f"   Protocol: {MCP_PROTOCOL_VERSION}", file=sys.stderr)
    print(f"   Tools:    {len(TOOLS)}", file=sys.stderr)
    print(f"   Auth:     per-request (X-API-Key header, Authorization: Bearer, or query param)", file=sys.stderr)
    if FALLBACK_API_KEY:
        print(f"   NOTE: SLACKING_API_KEY env var set as fallback (remove for strict per-user auth)", file=sys.stderr)
    
    server = HTTPServer(("0.0.0.0", PORT), MCPHandler)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
