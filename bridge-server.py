#!/usr/bin/env python3
"""SkillExchange bridge — HTTP endpoint for slacking.biz MCP skill.
Receives requests from SkillExchange marketplace, routes to internal REST API.

Usage:
  python3 skillexchange-bridge.py          # Dev server on port 8002
  SLACKING_API_KEY=xxx python3 skillexchange-bridge.py
"""

import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx

API_BASE = os.environ.get("SLACKING_API_BASE", "http://localhost:8001")
API_KEY = os.environ.get("SLACKING_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"} if API_KEY else {}
PORT = int(os.environ.get("PORT", 8002))

# Tool definitions — maps tool names to REST endpoints and parameter handling
TOOLS = {
    "get_company_health": {"endpoint": "/v1/financial/{ticker}", "method": "GET", "params": ["ticker"]},
    "get_trends": {"endpoint": "/v1/financial/{ticker}/trends", "method": "GET", "params": ["ticker"]},
    "industry_comparison": {"endpoint": "/v1/financial/{ticker}/vs-industry", "method": "GET", "params": ["ticker"]},
    "get_revenue_segments": {"endpoint": "/v1/financial/{ticker}/segments", "method": "GET", "params": ["ticker"]},
    "get_income_statement": {"endpoint": "/v1/financial/{ticker}/income-statement", "method": "GET", "params": ["ticker"]},
    "get_balance_sheet": {"endpoint": "/v1/financial/{ticker}/balance-sheet", "method": "GET", "params": ["ticker"]},
    "get_cash_flow": {"endpoint": "/v1/financial/{ticker}/cash-flow", "method": "GET", "params": ["ticker"]},
    "get_all_financial_data": {"endpoint": "/v1/financial/{ticker}/full", "method": "GET", "params": ["ticker"]},
    "get_company_profile": {"endpoint": "/v1/financial/{ticker}/profile", "method": "GET", "params": ["ticker"]},
    "get_filing_types": {"endpoint": "/v1/financial/{ticker}/filing-types", "method": "GET", "params": ["ticker"]},
    "search_filings": {"endpoint": "/v1/financial/{ticker}/filings/search", "method": "GET", "params": ["ticker", "form_types", "count"]},
    "get_insider_trades": {"endpoint": "/v1/financial/{ticker}/insider", "method": "GET", "params": ["ticker", "limit"]},
    "get_sentiment": {"endpoint": "/v1/financial/{ticker}/sentiment", "method": "GET", "params": ["ticker", "filing", "year"]},
    "compare_companies": {"endpoint": "/v1/financial/compare", "method": "POST", "params": ["tickers"]},
    "screen_companies": {"endpoint": "/v1/financial/screener", "method": "POST", "params": ["min_profit_margin", "min_revenue_growth", "max_debt_ratio", "min_grade", "limit"]},
    "batch_query": {"endpoint": "/v1/financial/batch", "method": "POST", "params": ["tickers", "endpoints"]},
    "list_capabilities": {"endpoint": "", "method": "LOCAL", "params": []},
}


def call_api(tool_name, params):
    """Execute a tool by calling the REST API."""
    if tool_name == "list_capabilities":
        return {"capabilities": list(TOOLS.keys())[:-1], "description": "SEC financial data API — 16 tools for company research"}

    tool = TOOLS.get(tool_name)
    if not tool:
        return {"error": f"Unknown tool: {tool_name}", "available_tools": list(TOOLS.keys())}

    # Build endpoint URL
    if tool["method"] == "GET":
        endpoint = tool["endpoint"]
        query_params = {}
        for p in tool["params"]:
            if p in params:
                if tool_name == "search_filings" and p == "form_types":
                    endpoint = endpoint.replace("{form_types}", str(params[p]))
                elif tool_name == "search_filings" and p == "count":
                    query_params["count"] = params[p]
                elif tool_name == "get_insider_trades" and p == "limit":
                    query_params["limit"] = params[p]
                elif tool_name == "get_sentiment" and p in ("filing", "year"):
                    if p == "year" and params[p] is not None:
                        query_params["year"] = params[p]
                    elif p == "filing":
                        query_params["filing"] = params[p]
                else:
                    endpoint = endpoint.replace("{" + p + "}", str(params[p]))
        
        # Remove unfilled placeholders
        endpoint = endpoint.split("?")[0]
        if query_params:
            qs = "&".join(f"{k}={v}" for k, v in query_params.items())
            endpoint = f"{endpoint}?{qs}"

        response = httpx.get(f"{API_BASE}{endpoint}", headers=HEADERS, timeout=30)
        return response.json()

    elif tool["method"] == "POST":
        body = {}
        for p in tool["params"]:
            if p in params:
                body[p] = params[p]
        response = httpx.post(f"{API_BASE}{tool['endpoint']}", json=body, headers=HEADERS, timeout=60)
        return response.json()


class SkillExchangeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health" or self.path == "/skillexchange/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "version": "1.0.0",
                "tools": len(TOOLS) - 1,
                "authenticated": bool(API_KEY),
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path not in ("/execute", "/skillexchange/execute"):
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid_json"})
            return

        tool = data.get("tool")
        params = data.get("params", {})
        
        if not tool:
            self._respond(400, {"error": "missing_tool", "available_tools": list(TOOLS.keys())})
            return

        try:
            result = call_api(tool, params)
            self._respond(200, {
                "output": result,
                "metadata": {
                    "tool": tool,
                    "executedAt": __import__("datetime").datetime.now().isoformat(),
                    "version": "1.0.0",
                }
            })
        except Exception as e:
            self._respond(500, {"error": "execution_error", "message": str(e)})

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        sys.stderr.write(f"[skillexchange] {args[0]} {args[1]} {args[2]}\n")


def main():
    if not API_KEY:
        print("WARNING: SLACKING_API_KEY not set. Only health check will work.", file=sys.stderr)
    
    server = HTTPServer(("0.0.0.0", PORT), SkillExchangeHandler)
    print(f"✅ SkillExchange bridge running on port {PORT}", file=sys.stderr)
    print(f"   Health: http://localhost:{PORT}/health", file=sys.stderr)
    print(f"   Execute: http://localhost:{PORT}/execute", file=sys.stderr)
    print(f"   API: {API_BASE}", file=sys.stderr)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
