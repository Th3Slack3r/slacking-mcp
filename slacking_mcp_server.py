#!/usr/bin/env python3
"""slacking.biz MCP Server — AI-native SEC financial data.
Give this to any AI agent (Claude Code, Cursor, Cline, Continue.dev, etc.)
to let it query real SEC EDGAR data through slacking.biz.

Setup: export SLACKING_API_KEY=your_key_here
Usage (Claude Code): claude --mcp '{"slacking":{"command":"python3","args":["/root/tsn-api/slacking_mcp_server.py"]}}'
"""
import json
import os
import sys

import httpx
from mcp.server import FastMCP

# ── Configuration ──
API_BASE = os.environ.get("SLACKING_API_BASE", "http://localhost:8001")
API_KEY = os.environ.get("SLACKING_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"} if API_KEY else {}

mcp = FastMCP("slacking.biz")

# ── API Client ──

def _get(endpoint: str) -> dict:
    """GET request to slacking.biz API."""
    try:
        r = httpx.get(f"{API_BASE}{endpoint}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        return {"status": "error", "code": e.response.status_code, "detail": e.response.text[:500]}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _post(endpoint: str, data: dict) -> dict:
    """POST request to slacking.biz API."""
    try:
        r = httpx.post(f"{API_BASE}{endpoint}", json=data, headers=HEADERS, timeout=60)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        return {"status": "error", "code": e.response.status_code, "detail": e.response.text[:500]}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ═══════════════════════════════════════════════
# TOOL CATEGORIES:
#   Health & Ratios  → snapshot metrics
#   Financial Statements → GAAP fundamentals
#   Company Reference → metadata, profile
#   Filings & SEC → forms, searches, insider trades
#   Screening & Comparison → multi-company analysis
# ═══════════════════════════════════════════════

# ── Category: Information (for AI Discovery) ──

@mcp.tool()
def list_capabilities() -> str:
    """List all available slacking.biz API tools organized by category. Use this first to discover what data is available."""
    tools = {
        "health_category": "QUICK FINANCIAL SNAPSHOTS — Best for initial research",
        "tools_health": "get_company_health, get_trends, industry_comparison, get_revenue_segments",
        "statements_category": "FINANCIAL STATEMENTS — Detailed GAAP data",
        "tools_statements": "get_income_statement, get_balance_sheet, get_cash_flow, get_all_financial_data",
        "company_category": "COMPANY REFERENCE — Metadata and identifiers",
        "tools_company": "get_company_profile, get_filing_types, search_filings",
        "insider_category": "INSIDER & SENTIMENT — Alternative signals",
        "tools_insider": "get_insider_trades, get_sentiment",
        "multi_category": "MULTI-COMPANY & SCREENING — Compare and discover",
        "tools_multi": "compare_companies, screen_companies, batch_query",
        "worldbank_category": "WORLD BANK — International development data",
        "tools_worldbank": "worldbank_country_economics, worldbank_specific_indicator, worldbank_list_countries, worldbank_compare_countries",
        "econ_category": "US ECONOMICS — FRED economic indicators (GDP, inflation, rates, employment, housing)",
        "tools_econ": "get_gdp, get_inflation, get_rates, get_employment, get_housing, get_econ_summary",
        "demo_category": "US DEMOGRAPHICS — Census Bureau data by ZIP code and county",
        "tools_demo": "get_zip_demographics, get_county_demographics",
        "notes": "All tools return JSON data sourced from SEC EDGAR filings (10-K, 10-Q, 8-K, Form 4/3/5, 13F, etc.), US Census Bureau ACS, and FRED economic data. Data is cached for up to 6 hours. For a paid plan with higher rate limits visit https://slacking.biz/upgrade",
    }
    return json.dumps(tools, indent=2)


# ── Category: Financial Health & Ratios ──

@mcp.tool()
def get_company_health(ticker: str) -> str:
    """Get a company's financial health score (A-F grade), key ratios, and metrics from the latest annual filing.
    Best for: quick financial check, investment research, comparing overall company strength.
    Returns: letter grade (A-F), health score (0-100), interpretation, ratios (profit_margin, debt_ratio, ROA, ROE, etc.), key_metrics (revenue, net_income, cash, etc.)
    Args:
        ticker: Stock ticker symbol (e.g., AAPL, MSFT, NVDA)
    """
    return json.dumps(_get(f"/v1/financial/{ticker}"), indent=2)


@mcp.tool()
def get_trends(ticker: str) -> str:
    """Get 6+ quarters of historical financial performance showing trajectory.
    Best for: seeing whether a company's revenue, net income, and EPS are improving or declining over time.
    Returns: quarterly periods with revenue, net_income, operating_income, gross_profit, EPS + computed trend direction (up/down/stable) and percentage change.
    Args:
        ticker: Stock ticker symbol
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/trends"), indent=2)


@mcp.tool()
def industry_comparison(ticker: str) -> str:
    """Compare a company's financial metrics against its industry peers (same SIC code).
    Best for: seeing how a company stacks up against competitors. Shows percentile ranking.
    Returns: company metrics vs industry averages, differences, and what percentile the company ranks in for each metric.
    Data sourced from cached companies (120+ major companies across 51 industries).
    Args:
        ticker: Stock ticker symbol
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/vs-industry"), indent=2)


@mcp.tool()
def get_revenue_segments(ticker: str) -> str:
    """Get revenue breakdown by product/service segments and geography from SEC filings.
    Best for: understanding what drives a company's revenue (which products, which regions).
    Returns: total revenue time series, segment breakdowns if available, geographic revenue if available, year-over-year growth rate.
    Note: Segment data availability varies by company and how they report in their 10-K filings.
    Args:
        ticker: Stock ticker symbol
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/segments"), indent=2)


# ── Category: Financial Statements ──

@mcp.tool()
def get_income_statement(ticker: str) -> str:
    """Get a company's income statement — revenue, cost of goods sold, R&D, SG&A, operating income, net income, EPS.
    Best for: detailed profitability analysis, margin calculations, earnings quality.
    Returns: ~17 line items with annual values from the most recent 10-K filing.
    Args:
        ticker: Stock ticker symbol
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/income-statement"), indent=2)


@mcp.tool()
def get_balance_sheet(ticker: str) -> str:
    """Get a company's balance sheet — assets, liabilities, equity breakdown.
    Best for: analyzing financial position, leverage, liquidity, working capital.
    Returns: ~20 line items with annual values: cash, receivables, inventory, PPE, goodwill, debt, retained earnings, etc.
    Args:
        ticker: Stock ticker symbol
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/balance-sheet"), indent=2)


@mcp.tool()
def get_cash_flow(ticker: str) -> str:
    """Get a company's cash flow statement — operating, investing, and financing activities.
    Best for: understanding cash generation, free cash flow, buyback/dividend sustainability.
    Returns: ~12 line items: operating cash flow, CapEx, stock repurchases, dividends, debt issuance/repayment.
    Args:
        ticker: Stock ticker symbol
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/cash-flow"), indent=2)


@mcp.tool()
def get_all_financial_data(ticker: str) -> str:
    """Get 500+ GAAP financial metrics for a company — the most comprehensive endpoint.
    Best for: deep-dive analysis, finding specific XBRL data points not in standard statements.
    Returns: ALL available XBRL financial data points from the company's SEC filings.
    This is the rawest, most detailed data available. Response can be large.
    Args:
        ticker: Stock ticker symbol
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/full"), indent=2)


# ── Category: Company Reference ──

@mcp.tool()
def get_company_profile(ticker: str) -> str:
    """Get company metadata — name, SIC code, industry description, exchanges, address, phone, website, former names.
    Best for: identifying companies, getting industry classification, contact info.
    Returns: ticker, CIK, SIC code and description, exchange listing, EIN, addresses, fiscal year end, former names.
    Args:
        ticker: Stock ticker symbol
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/profile"), indent=2)


@mcp.tool()
def get_filing_types(ticker: str) -> str:
    """Get a categorized summary of ALL SEC filing types a company has ever filed.
    Best for: understanding what filings a company produces and how often.
    Returns: total filing count, then organized by category:
    - annual: 10-K, 20-F, 40-F (yearly reports)
    - quarterly: 10-Q, 6-K (quarterly updates)
    - current: 8-K (material events)
    - insider: Forms 3, 4, 5 (ownership changes)
    - ownership: 13F, 13D, 13G (institutional holdings)
    - registration: S-1, S-3, S-8, 424B4 (securities offerings)
    - other: all remaining form types
    Each category shows count, most recent filing date, and form types.
    Args:
        ticker: Stock ticker symbol
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/filing-types"), indent=2)


@mcp.tool()
def search_filings(ticker: str, form_types: str = "10-K,10-Q,8-K", count: int = 20) -> str:
    """Search SEC filings by form type for a specific company.
    Best for: finding specific filings (e.g., latest 10-K annual reports, 8-K material events, Form 4 insider trades).
    Returns: list of filings with form_type, filing_date, description, and direct SEC document URL.
    Args:
        ticker: Stock ticker symbol
        form_types: Comma-separated SEC form types to filter by. Common types: '10-K' (annual), '10-Q' (quarterly), '8-K' (current report), '4' (insider), '3' (initial ownership), '13F' (institutional holdings), 'S-1' (IPO), 'DEF 14A' (proxy)
        count: Max filings to return (1-50, default 20)
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/filings/search?form_types={form_types}&count={count}"), indent=2)


# ── Category: Insider & Sentiment ──

@mcp.tool()
def get_insider_trades(ticker: str, limit: int = 15) -> str:
    """Get recent insider trading transactions from SEC Form 4 filings.
    Best for: seeing if executives (CEO, CFO, directors) are buying or selling shares.
    Returns: recent insider transactions with owner name, relationship (Director/Officer), transaction type (BUY/SELL/GRANT), shares, price, and value.
    Args:
        ticker: Stock ticker symbol
        limit: Max trades to return (1-50, default 15)
    """
    return json.dumps(_get(f"/v1/financial/{ticker}/insider?limit={limit}"), indent=2)


@mcp.tool()
def get_sentiment(ticker: str, filing: str = "10-K", year: int = None) -> str:
    """Analyze the tone and sentiment of a company's SEC filing text using the Loughran-McDonald financial dictionary.
    Best for: detecting management's optimism, pessimism, or uncertainty in annual/quarterly reports.
    Uses the Loughran-McDonald word list specifically designed for financial text (not generic sentiment).
    Returns: sentiment score (-1 to 1), positive phrases, negative phrases, text length.
    Args:
        ticker: Stock ticker symbol
        filing: Filing type — '10-K' (annual report, default) or '10-Q' (quarterly)
        year: Fiscal year (default: latest available)
    """
    url = f"/v1/financial/{ticker}/sentiment?filing={filing}"
    if year:
        url += f"&year={year}"
    return json.dumps(_get(url), indent=2)


# ── Category: Multi-Company & Screening ──

@mcp.tool()
def compare_companies(tickers: list) -> str:
    """Compare the financial health scores of up to 10 companies side-by-side.
    Best for: competitive analysis, portfolio comparison, identifying the strongest company in a group.
    Returns: each company's health_score, grade, ratios, and key_metrics in a single response.
    Args:
        tickers: List of ticker symbols to compare, e.g., ["AAPL","MSFT","GOOGL","AMZN","NVDA"]
    """
    return json.dumps(_post("/v1/financial/compare", {"tickers": tickers}), indent=2)


@mcp.tool()
def screen_companies(min_profit_margin: float = None,
                     min_revenue_growth: float = None,
                     max_debt_ratio: float = None,
                     min_grade: str = None,
                     limit: int = 20) -> str:
    """Screen/filter companies by financial metrics to find ones matching your criteria.
    Best for: discovering investment candidates, finding companies with specific financial profiles.
    Searches across 120+ cached major companies. Add filters to narrow results.
    Returns: matching companies with ticker, company_name, grade, health_score, and key metrics.
    Args:
        min_profit_margin: Minimum profit margin percentage (e.g., 15 for 15%)
        min_revenue_growth: Minimum year-over-year revenue growth percentage (e.g., 10 for 10%)
        max_debt_ratio: Maximum debt-to-assets ratio percentage (e.g., 50 for 50%)
        min_grade: Minimum health grade — 'A', 'B', 'C', 'D', or 'F'
        limit: Max results to return (default 20)
    """
    params = {"limit": limit}
    if min_profit_margin is not None: params["min_profit_margin"] = min_profit_margin
    if min_revenue_growth is not None: params["min_revenue_growth"] = min_revenue_growth
    if max_debt_ratio is not None: params["max_debt_ratio"] = max_debt_ratio
    if min_grade is not None: params["min_grade"] = min_grade
    return json.dumps(_post("/v1/financial/screener", params), indent=2)


@mcp.tool()
def batch_query(tickers: list, endpoints: list = None) -> str:
    """Get data for multiple tickers in a single request (efficient batch querying).
    Best for: research workflows that need data on many companies at once.
    By default returns health scores. Can also include profile data and full financial data.
    Args:
        tickers: List of ticker symbols, e.g., ["AAPL","MSFT","NVDA","GOOGL","AMZN"]
        endpoints: Data to return per ticker. Options: "health" (default), "profile", "full". Examples: ["health"], ["health","profile"], ["health","profile","full"]
    """
    params = {"tickers": tickers}
    if endpoints:
        params["endpoints"] = endpoints
    return json.dumps(_post("/v1/financial/batch", params), indent=2)


# ═══════════════════════════════════════════════════
#  WORLD BANK — International Development Data
# ═══════════════════════════════════════════════════

_INDICATOR_DESCRIPTIONS = [
    "gdp", "gdp_growth", "gdp_per_capita", "gdp_per_capita_growth",
    "population", "population_growth", "inflation_cpi", "unemployment",
    "exports", "imports", "fdi", "poverty", "life_expectancy", "education_expenditure",
]


@mcp.tool()
def worldbank_country_economics(country_code: str) -> str:
    """Get key economic indicators (GDP, GDP growth, population, inflation, unemployment, trade) for any country from the World Bank.
    Covers 55+ countries/regions including USA, China, India, UK, Germany, Japan, Brazil, and more.
    Data sourced from World Bank Open Data (CC BY 4.0 licensed — free for commercial use with attribution).
    Args:
        country_code: ISO 3166-1 alpha-3 country code (e.g., USA, CHN, GBR, IND, DEU, JPN, BRA). Use worldbank_list_countries() to see all codes.
    """
    return json.dumps(_get(f"/v1/worldbank/{country_code}"), indent=2)


@mcp.tool()
def worldbank_specific_indicator(country_code: str, indicator: str, years: int = 10) -> str:
    """Get a specific World Bank indicator time-series for a country.
    Supports 14 key indicators covering GDP, population, inflation, unemployment, trade, FDI, poverty, life expectancy, and education.
    Args:
        country_code: ISO 3166-1 alpha-3 country code (e.g., USA, GBR, CHN)
        indicator: Indicator key — one of: gdp, gdp_growth, gdp_per_capita, gdp_per_capita_growth, population, population_growth, inflation_cpi, unemployment, exports, imports, fdi, poverty, life_expectancy, education_expenditure
        years: Number of historical years to return (default: 10, max: 50)
    """
    return json.dumps(_get(f"/v1/worldbank/{country_code}/{indicator}?years={years}"), indent=2)


@mcp.tool()
def worldbank_list_countries() -> str:
    """List all 55+ available country/region codes and names for World Bank data queries.
    Use this to discover which country codes are available before querying indicators.
    Includes major economies (USA, CHN, JPN, DEU, GBR, IND, BRA, etc.) and regional aggregates (WLD=World, EAS=East Asia, etc.).
    """
    return json.dumps(_get("/v1/worldbank/countries"), indent=2)


@mcp.tool()
def worldbank_compare_countries(countries: str) -> str:
    """Compare key economic indicators (GDP, GDP growth, population) across multiple countries in one call.
    Best for: quick cross-country economic comparison, identifying largest economies, fastest-growing markets.
    Args:
        countries: Comma-separated ISO country codes, e.g., "USA,CHN,GBR,IND,DEU,JPN,BRA". Use worldbank_list_countries() to see codes.
    """
    return json.dumps(_get(f"/v1/worldbank/summary?countries={countries}"), indent=2)


# ═══════════════════════════════════════════════════
#  US ECONOMICS — FRED Economic Data (via slacking.biz)
# ═══════════════════════════════════════════════════

@mcp.tool()
def get_gdp() -> str:
    """Get US GDP data — nominal GDP, real GDP, and quarterly growth rates.
    Best for: understanding the overall size and growth trajectory of the US economy.
    Returns: current GDP, quarterly growth rate, real GDP, nominal GDP trends.
    """
    return json.dumps(_get("/v1/econ/gdp"), indent=2)


@mcp.tool()
def get_inflation() -> str:
    """Get US CPI / inflation data — Consumer Price Index from the Bureau of Labor Statistics.
    Best for: tracking inflation trends, purchasing power, and cost of living changes.
    Returns: latest CPI value, annual change, and historical data.
    """
    return json.dumps(_get("/v1/econ/inflation"), indent=2)


@mcp.tool()
def get_rates() -> str:
    """Get US interest rates — Federal Funds rate, 30-year mortgage rate, and 10-year Treasury yield.
    Best for: understanding monetary policy stance, borrowing costs, and bond market conditions.
    Returns: federal funds rate, mortgage 30yr, treasury 10yr.
    """
    return json.dumps(_get("/v1/econ/rates"), indent=2)


@mcp.tool()
def get_employment() -> str:
    """Get US employment data — unemployment rate and initial jobless claims.
    Best for: labor market analysis, economic health assessment, recession monitoring.
    Returns: unemployment rate, initial jobless claims.
    """
    return json.dumps(_get("/v1/econ/employment"), indent=2)


@mcp.tool()
def get_housing() -> str:
    """Get US housing market data — housing starts and 30-year mortgage rates.
    Best for: real estate market analysis, construction sector trends, housing affordability.
    Returns: housing starts, mortgage 30yr rate.
    """
    return json.dumps(_get("/v1/econ/housing"), indent=2)


@mcp.tool()
def get_econ_summary() -> str:
    """Get ALL key US economic indicators in one call — GDP, CPI, Fed funds rate, unemployment, housing, mortgage rates, treasury yields, jobless claims.
    Best for: comprehensive economic overview, macro research, dashboard reports.
    Returns: GDP, inflation CPI, federal funds rate, unemployment rate, housing starts, mortgage rate, treasury yield, jobless claims.
    """
    return json.dumps(_get("/v1/econ/summary"), indent=2)


# ═══════════════════════════════════════════════════
#  US DEMOGRAPHICS — Census Bureau Data (via slacking.biz)
# ═══════════════════════════════════════════════════

@mcp.tool()
def get_zip_demographics(zip_code: str) -> str:
    """Get demographics for a ZIP code — population, median income, median age, housing units, median home value, median rent, and education levels.
    Best for: local market analysis, demographic research, real estate assessment.
    Data sourced from US Census Bureau American Community Survey (ACS) 5-year estimates.
    Args:
        zip_code: 5-digit US ZIP code (e.g., '90210', '10001')
    """
    return json.dumps(_get(f"/v1/demo/zip/{zip_code}"), indent=2)


@mcp.tool()
def get_county_demographics(fips: str) -> str:
    """Get county-level demographics by FIPS code — population, median income, median age, housing units, median home value, median rent, and education levels.
    Best for: county-level analysis, regional comparisons, demographic research.
    Data sourced from US Census Bureau American Community Survey (ACS) 5-year estimates.
    Args:
        fips: 5-digit county FIPS code (e.g., '06037' for Los Angeles County, '17031' for Cook County)
    """
    return json.dumps(_get(f"/v1/demo/county/{fips}"), indent=2)


# ── Main ──
if __name__ == "__main__":
    if not API_KEY:
        print("WARNING: SLACKING_API_KEY environment variable not set. Export it: export SLACKING_API_KEY=your_key", file=sys.stderr)
    mcp.run(transport="stdio")
