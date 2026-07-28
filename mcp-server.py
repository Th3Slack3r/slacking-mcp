#!/usr/bin/env python3
"""MCP Streamable HTTP server for slacking.biz — proper JSON-RPC protocol.
Compatible with Smithery, MCP.so, Official MCP Registry, and SkillExchange.

Endpoints:
  POST /mcp       — MCP Streamable HTTP endpoint (JSON-RPC 2.0)
  GET  /health    — Health check

MCP methods implemented:
  - initialize         — Handshake (public, no auth required)
  - tools/list         — List all 16 tools with schemas (public, no auth required)
  - tools/call         — Execute a tool (requires API key for data calls)

Usage:
  python3 mcp-server.py            # Dev on port 8003
  SLACKING_API_KEY=xxx python3 mcp-server.py  # With API key
"""

import json
import os
import sys
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

import httpx

API_BASE = os.environ.get("SLACKING_API_BASE", "http://localhost:8001")
API_KEY = os.environ.get("SLACKING_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"} if API_KEY else {}
PORT = int(os.environ.get("MCP_PORT", 8003))
AUTH_REQUIRED = bool(os.environ.get("MCP_AUTH_REQUIRED", "1"))

# ─── MCP Protocol Constants ───────────────────────────────────────────
MCP_PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "slacking-biz"
SERVER_VERSION = "1.0.0"

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
        "description": "Get revenue breakdown by segment/product line for a company",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_income_statement": {
        "endpoint": "/v1/financial/{ticker}/income-statement",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get detailed income statement data for a company",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_balance_sheet": {
        "endpoint": "/v1/financial/{ticker}/balance-sheet",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get detailed balance sheet data for a company",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_cash_flow": {
        "endpoint": "/v1/financial/{ticker}/cash-flow",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get detailed cash flow statement for a company",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_all_financial_data": {
        "endpoint": "/v1/financial/{ticker}/full",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get ALL financial data for a company in one call — health, income, balance sheet, cash flow, trends",
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
        "description": "Get company profile information including sector, industry, employees, description",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "get_filing_types": {
        "endpoint": "/v1/financial/{ticker}/filing-types",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get available SEC filing types (10-K, 10-Q, 8-K, etc.) for a company",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"}
            },
            "required": ["ticker"]
        }
    },
    "search_filings": {
        "endpoint": "/v1/financial/{ticker}/filings/search",
        "method": "GET",
        "params": ["ticker"],
        "description": "Search SEC filings for a company by form type (10-K, 10-Q, 8-K) with optional count limit",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"},
                "form_types": {"type": "string", "description": "Comma-separated form types (e.g. '10-K,10-Q')"},
                "count": {"type": "integer", "description": "Number of results to return (default: 10)", "default": 10}
            },
            "required": ["ticker"]
        }
    },
    "get_insider_trades": {
        "endpoint": "/v1/financial/{ticker}/insider",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get recent insider trading activity for a company",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"},
                "limit": {"type": "integer", "description": "Number of trades to return (default: 20)", "default": 20}
            },
            "required": ["ticker"]
        }
    },
    "get_sentiment": {
        "endpoint": "/v1/financial/{ticker}/sentiment",
        "method": "GET",
        "params": ["ticker"],
        "description": "Get sentiment analysis from SEC filings for a company — positive/negative/neutral signals",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"},
                "filing": {"type": "string", "description": "Filing type to analyze (e.g. '10-K', '10-Q')"},
                "year": {"type": "integer", "description": "Filing year to analyze"}
            },
            "required": ["ticker"]
        }
    },
    "compare_companies": {
        "endpoint": "/v1/financial/compare",
        "method": "POST",
        "params": ["tickers"],
        "description": "Compare financial metrics across multiple companies side by side",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Array of ticker symbols to compare (e.g. ['AAPL', 'MSFT', 'GOOGL'])"
                }
            },
            "required": ["tickers"]
        }
    },
    "screen_companies": {
        "endpoint": "/v1/financial/screener",
        "method": "POST",
        "params": ["min_profit_margin", "min_revenue_growth", "max_debt_ratio", "min_grade", "limit"],
        "description": "Screen/filter companies by financial criteria — find companies matching specific metrics",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_profit_margin": {"type": "number", "description": "Minimum profit margin (e.g. 0.15 for 15%)"},
                "min_revenue_growth": {"type": "number", "description": "Minimum revenue growth rate (e.g. 0.1 for 10%)"},
                "max_debt_ratio": {"type": "number", "description": "Maximum debt-to-equity ratio"},
                "min_grade": {"type": "string", "description": "Minimum health grade (A, B, C, D, F)"},
                "limit": {"type": "integer", "description": "Maximum number of results", "default": 20}
            }
        }
    },
    "batch_query": {
        "endpoint": "/v1/financial/batch",
        "method": "POST",
        "params": ["tickers", "endpoints"],
        "description": "Query multiple endpoints for multiple tickers in a single batch request",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Array of ticker symbols"
                },
                "endpoints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Array of endpoint names (e.g. ['income-statement', 'health', 'insider'])"
                }
            },
            "required": ["tickers", "endpoints"]
        }
    }
}

# ─── MCP JSON-RPC Handler ──────────────────────────────────────────────

def make_jsonrpc_error(code, message, id=None):
    """Create a JSON-RPC error response."""
    return {
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message}
    }

def make_jsonrpc_result(result, id=None):
    """Create a JSON-RPC success response."""
    return {
        "jsonrpc": "2.0",
        "id": id,
        "result": result
    }

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
    """MCP resources/list — return empty (no resources exposed)."""
    return make_jsonrpc_result({
        "resources": []
    }, msg_id)

def handle_prompts_list(params, msg_id):
    """MCP prompts/list — return empty (no prompts exposed)."""
    return make_jsonrpc_result({
        "prompts": []
    }, msg_id)

def handle_tools_list(params, msg_id):
    """List all available tools with schemas — public, no auth required."""
    tool_list = []
    for name, tool in TOOLS.items():
        tool_list.append({
            "name": name,
            "description": tool["description"],
            "inputSchema": tool["inputSchema"]
        })
    
    return make_jsonrpc_result({
        "tools": tool_list
    }, msg_id)

def handle_tools_call(params, msg_id):
    """Execute a tool — requires API key for data access."""
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})
    
    if not tool_name:
        return make_jsonrpc_error(-32602, "Tool name is required", msg_id)
    
    tool = TOOLS.get(tool_name)
    if not tool:
        return make_jsonrpc_error(-32602, f"Unknown tool: {tool_name}", msg_id)
    
    # Print for logging
    print(f"  [mcp] tools/call: {tool_name} args={json.dumps(arguments)[:200]}", file=sys.stderr)
    
    # Build and execute API call
    try:
        result = call_api(tool, arguments)
        return make_jsonrpc_result({
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ],
            "isError": "error" in result
        }, msg_id)
    except Exception as e:
        return make_jsonrpc_error(-32000, f"Execution error: {str(e)}", msg_id)

def call_api(tool, params):
    """Call the underlying REST API for a tool."""
    import httpx
    
    if tool["method"] == "GET":
        endpoint = tool["endpoint"]
        query_params = {}
        for p in tool["params"]:
            if p in params:
                endpoint = endpoint.replace("{" + p + "}", str(params[p]))
        
        # Handle remaining query params (non-path params)
        extra_params = [p for p in tool["params"] if p not in params]
        for p in extra_params:
            if p in params:
                # Remove unfilled placeholders — they get added as query params
                endpoint = re.sub(r'\{' + p + r'\}', '', endpoint)
        
        # Clean any remaining unfilled placeholders
        endpoint = re.sub(r'\{[^}]+\}', '', endpoint)
        
        if query_params:
            qs = "&".join(f"{k}={v}" for k, v in query_params.items())
            separator = "&" if "?" in endpoint else "?"
            endpoint = f"{endpoint}{separator}{qs}"
        
        response = httpx.get(f"{API_BASE}{endpoint}", headers=HEADERS, timeout=30)
        return response.json()
    
    elif tool["method"] == "POST":
        body = {}
        for p in tool["params"]:
            if p in params:
                body[p] = params[p]
        response = httpx.post(f"{API_BASE}{tool['endpoint']}", json=body, headers=HEADERS, timeout=60)
        return response.json()


# ─── HTTP Handler ───────────────────────────────────────────────────────

class MCPHandler(BaseHTTPRequestHandler):
    """HTTP server that handles MCP Streamable HTTP transport."""
    
    def do_GET(self):
        if self.path in ("/health", "/mcp/health"):
            self._respond(200, {
                "status": "ok",
                "server": SERVER_NAME,
                "version": SERVER_VERSION,
                "tools": len(TOOLS),
                "authenticated": bool(API_KEY),
                "protocol": "MCP Streamable HTTP",
                "protocolVersion": MCP_PROTOCOL_VERSION
            })
        else:
            self._respond(404, {"error": "not_found"})
    
    def do_POST(self):
        if self.path not in ("/mcp", "/mcp/"):
            self._respond(404, {"error": "not_found"})
            return
        
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, make_jsonrpc_error(-32700, "Parse error"))
            return
        
        # Validate JSON-RPC structure
        if not isinstance(request, dict) or "method" not in request:
            self._respond(400, make_jsonrpc_error(-32600, "Invalid Request"))
            return
        
        msg_id = request.get("id")
        method = request["method"]
        params = request.get("params", {})
        
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
            response = handle_tools_call(params, msg_id)
        else:
            response = make_jsonrpc_error(-32601, f"Method not found: {method}", msg_id)
        
        self._respond(200, response)
    
    def _respond(self, status, data):
        body_bytes = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body_bytes)
    
    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()
    
    def log_message(self, format, *args):
        sys.stderr.write(f"[mcp-server] {args[0]} {args[1]} {args[2]}\n")


# ─── Entry Point ────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("WARNING: SLACKING_API_KEY not set. Only initialize and tools/list will work.", file=sys.stderr)
    
    server = HTTPServer(("0.0.0.0", PORT), MCPHandler)
    print(f"🚀 MCP Streamable HTTP server running on port {PORT}", file=sys.stderr)
    print(f"   Endpoint: http://localhost:{PORT}/mcp", file=sys.stderr)
    print(f"   Health:   http://localhost:{PORT}/health", file=sys.stderr)
    print(f"   Protocol: {MCP_PROTOCOL_VERSION}", file=sys.stderr)
    print(f"   Tools:    {len(TOOLS)}", file=sys.stderr)
    print(f"   Auth:     {'enabled' if API_KEY else 'DISABLED'}", file=sys.stderr)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
