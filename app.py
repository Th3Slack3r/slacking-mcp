#!/usr/bin/env python3
"""slacking.biz - SEC Financial Data via REST API. Repackages SEC EDGAR data into clean JSON endpoints."""

import json
import logging
import os
import re
import sqlite3
import time
import uuid
import defusedxml.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

import bcrypt
import requests
from fastapi import FastAPI, Request, Response, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# ── Logging ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("slacking.biz")

# ── Configuration ──
DB_PATH = Path("/root/tsn-api/tsn_api.db")
CACHE_DIR = Path("/root/tsn-api/cache")
CACHE_TTL = 6 * 3600  # 6 hours in seconds
USER_AGENT = "slacking.biz (admin@tsn.pw)"
SEC_BASE = "https://data.sec.gov"
SEC_FILES = "https://www.sec.gov/files"

# ── FRED & Census API Keys ──
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
if not FRED_API_KEY:
    try:
        FRED_API_KEY = Path("/root/.fred_api_key").read_text().strip()
    except Exception:
        pass

CENSUS_API_KEY = os.environ.get("CENSUS_API_KEY", "")
if not CENSUS_API_KEY:
    try:
        CENSUS_API_KEY = Path("/root/.census_api_key").read_text().strip()
    except Exception:
        pass

# Plan limits
PLAN_LIMITS = {
    "free": 20,
    "starter": 500,
    "pro": 5000,
    "enterprise": 50000,
}

# Loughran-McDonald dictionary (loaded from sec_words.json)
WORD_DIR = Path("/root")
LM_POSITIVE = set()
LM_NEGATIVE = set()
LM_UNCERTAINTY = set()

WORD_FILE = WORD_DIR / "sec_words.json"
if WORD_FILE.exists():
    wd = json.loads(WORD_FILE.read_text())
    LM_POSITIVE = set(wd.get("positive", []))
    LM_NEGATIVE = set(wd.get("negative", []))
    LM_UNCERTAINTY = set(wd.get("uncertainty", []))

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── Database Setup ──
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'free',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            key TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            requests INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS feedback_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.commit()
    conn.close()


# ── FastAPI App ──
app = FastAPI(
    title="slacking.biz API",
    description="SEC EDGAR Financial Data API — Health scores, sentiment, financial statements, insider trades, and more.",
    version="1.0.0",
)

# ── CORS ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://slacking.biz"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth Rate Limiter ──
_login_attempts: Dict[str, list] = {}

def _check_auth_rate_limit(ip: str):
    """Simple in-memory rate limiter for auth endpoints."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Prune attempts older than 15 minutes
    attempts = [t for t in attempts if now - t < 900]
    if len(attempts) >= 10:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.")
    attempts.append(now)
    _login_attempts[ip] = attempts
    # Keep the dict bounded
    if len(_login_attempts) > 10000:
        for k in list(_login_attempts.keys()):
            _login_attempts[k] = [t for t in _login_attempts[k] if now - t < 900]
            if not _login_attempts[k]:
                del _login_attempts[k]

# ── Password Policy ──
def _validate_password(password: str):
    """Enforce password strength."""
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not re.search(r"[A-Z]", password):
        raise HTTPException(status_code=400, detail="Password must contain an uppercase letter")
    if not re.search(r"[a-z]", password):
        raise HTTPException(status_code=400, detail="Password must contain a lowercase letter")
    if not re.search(r"[0-9]", password):
        raise HTTPException(status_code=400, detail="Password must contain a digit")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>_\-]", password):
        raise HTTPException(status_code=400, detail="Password must contain a special character")


# ── SEC Client ──
class SECClient:
    """Rate-limited SEC EDGAR client with caching."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.last_request = 0.0
        self.min_delay = 0.11  # SEC rate limit: ~10 req/s max

    def _rate_limit(self):
        """Ensure minimum delay between requests to comply with SEC rate limits."""
        elapsed = time.time() - self.last_request
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)
        self.last_request = time.time()

    def get(self, url: str, **kwargs) -> requests.Response:
        """Rate-limited GET request."""
        self._rate_limit()
        resp = self.session.get(url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def get_json(self, url: str, **kwargs) -> dict:
        resp = self.get(url, **kwargs)
        return resp.json()


# Global SEC client instance
_SEC_CLIENT = SECClient()

# ── Ticker Map ──
_TICKER_MAP: Dict[str, dict] = {}
_COMPANY_TICKERS_FILE = CACHE_DIR / "company_tickers.json"


def _load_ticker_map():
    """Load the company tickers map from cache or SEC."""
    global _TICKER_MAP
    if _TICKER_MAP:
        return _TICKER_MAP
    if _COMPANY_TICKERS_FILE.exists():
        age = time.time() - _COMPANY_TICKERS_FILE.stat().st_mtime
        if age < CACHE_TTL:
            _TICKER_MAP = json.loads(_COMPANY_TICKERS_FILE.read_text())
            return _TICKER_MAP
    try:
        url = f"{SEC_FILES}/company_tickers.json"
        data = _SEC_CLIENT.get_json(url)
        # SEC returns a dict keyed by index; normalize to ticker -> {cik, title}
        result = {}
        for _k, v in data.items():
            ticker = v.get("ticker", "").upper().strip()
            if ticker:
                result[ticker] = {"cik": v.get("cik_str", v.get("cik", 0)), "title": v.get("title", "")}
        _COMPANY_TICKERS_FILE.write_text(json.dumps(result, indent=2))
        _TICKER_MAP = result
    except Exception as e:
        # If we have a cached version, use it even if stale
        if _COMPANY_TICKERS_FILE.exists():
            _TICKER_MAP = json.loads(_COMPANY_TICKERS_FILE.read_text())
    return _TICKER_MAP


def lookup_cik(ticker: str) -> Optional[int]:
    """Look up CIK number for a ticker symbol."""
    ticker = ticker.upper().strip()
    ticker_map = _load_ticker_map()
    entry = ticker_map.get(ticker)
    if entry:
        return int(entry["cik"])
    # Try loading fresh
    _TICKER_MAP.clear()
    ticker_map = _load_ticker_map()
    entry = ticker_map.get(ticker)
    if entry:
        return int(entry["cik"])
    return None


# ── Data Extraction Helpers ──
def extract_metric(facts: dict, concept: str, max_vals: int = 1, prefer_annual: bool = True) -> list:
    """Extract metric values from SEC facts data for a given concept (XBRL tag).
    Returns list of {value, fy, end} dicts, newest first.
    """
    results = []
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    if concept in us_gaap:
        concept_data = us_gaap[concept]
        # SEC facts JSON has structure: {label, description, units: {USD: [...]}}
        units_data = concept_data.get("units", concept_data)
        for unit_key, entries in units_data.items():
            if unit_key.startswith("USD") or unit_key == "pure" or unit_key.startswith("shares"):
                for entry in entries:
                    val = entry.get("val")
                    fy = entry.get("fy")
                    end = entry.get("end")
                    frame = entry.get("frame", "")
                    if val is not None:
                        is_annual = not frame or "Q" not in frame
                        results.append({
                            "value": val,
                            "fy": fy,
                            "end": end,
                            "is_annual": is_annual,
                            "frame": frame,
                        })
    # Filter by annual if preferred
    if prefer_annual and max_vals <= 3:
        annuals = [r for r in results if r["is_annual"]]
        if annuals:
            results = annuals
    # Sort by end date descending, then by fy descending
    def sort_key(r):
        return (r.get("end") or "", r.get("fy") or 0)
    results.sort(key=sort_key, reverse=True)
    # Return up to max_vals, without internal keys
    out = []
    seen = set()
    for r in results:
        key = (r["fy"], r["end"])
        if key not in seen:
            seen.add(key)
            out.append({"value": r["value"], "fy": r["fy"], "end": r["end"]})
            if len(out) >= max_vals:
                break
    return out


def safe_get(d, *keys, default=None):
    """Safely traverse nested dict/list structure."""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, (list, tuple)) and isinstance(k, int) and 0 <= k < len(d):
            d = d[k]
        else:
            return default
        if d is None:
            return default
    return d


# ── Company Facts ──
def fetch_company_facts(ticker: str) -> dict:
    """Fetch company facts from SEC or cache."""
    ticker = ticker.upper().strip()
    cik = lookup_cik(ticker)
    if not cik:
        raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found")

    cache_key = f"{ticker}_facts.json"
    cache_path = CACHE_DIR / cache_key

    # Check cache
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            data = json.loads(cache_path.read_text())
            # Cache might have extra fields, ensure basic structure
            if "facts" in data and "cik" in data:
                return data

    # Fetch from SEC
    cik_padded = str(cik).zfill(10)
    url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    try:
        data = _SEC_CLIENT.get_json(url)
    except Exception as e:
        # Try cached even if stale
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Failed to fetch data from SEC: {str(e)}")

    # Enrich
    data["_cik"] = cik
    data["_company_name"] = _TICKER_MAP.get(ticker, {}).get("title", data.get("entityName", ""))
    data["_cik_padded"] = cik_padded

    cache_path.write_text(json.dumps(data, default=str))
    return data


# ── Health Score ──
def compute_health_score(ticker: str) -> dict:
    """Compute financial health score (A-F) from SEC facts."""
    facts = fetch_company_facts(ticker)
    company_name = facts.get("entityName", "")

    # Extract key metrics
    revenue = extract_metric(facts, "RevenueFromContractWithCustomerExcludingAssessedTax", max_vals=3)
    net_income = extract_metric(facts, "NetIncomeLoss", max_vals=3)
    operating_income = extract_metric(facts, "OperatingIncomeLoss", max_vals=3)
    gross_profit = extract_metric(facts, "GrossProfit", max_vals=3)
    total_assets = extract_metric(facts, "Assets", max_vals=2)
    total_liabilities = extract_metric(facts, "Liabilities", max_vals=2)
    current_assets = extract_metric(facts, "AssetsCurrent", max_vals=2)
    current_liabilities = extract_metric(facts, "LiabilitiesCurrent", max_vals=2)
    cash_and_equiv = extract_metric(facts, "CashAndCashEquivalentsAtCarryingValue", max_vals=2)
    total_equity = extract_metric(facts, "StockholdersEquity", max_vals=2)
    operating_cf = extract_metric(facts, "NetCashProvidedByOperatingActivities", max_vals=2)
    long_term_debt = extract_metric(facts, "LongTermDebtNoncurrent", max_vals=2)
    interest_expense = extract_metric(facts, "InterestExpense", max_vals=2)
    eps = extract_metric(facts, "EarningsPerShareBasic", max_vals=2)

    # Get latest values
    latest_revenue = safe_get(revenue, 0, "value") or 0
    prev_revenue = safe_get(revenue, 1, "value") or 0
    latest_net_income = safe_get(net_income, 0, "value") or 0
    prev_net_income = safe_get(net_income, 1, "value") or 0
    latest_op_income = safe_get(operating_income, 0, "value") or 0
    latest_gross_profit = safe_get(gross_profit, 0, "value") or 0
    latest_assets = safe_get(total_assets, 0, "value") or 1
    latest_liabilities = safe_get(total_liabilities, 0, "value") or 0
    latest_current_assets = safe_get(current_assets, 0, "value") or 0
    latest_current_liabilities = safe_get(current_liabilities, 0, "value") or 1
    latest_cash = safe_get(cash_and_equiv, 0, "value") or 0
    latest_equity = safe_get(total_equity, 0, "value") or 1
    latest_ocf = safe_get(operating_cf, 0, "value") or 0
    latest_debt = safe_get(long_term_debt, 0, "value") or 0
    latest_interest = safe_get(interest_expense, 0, "value") or 1
    latest_eps = safe_get(eps, 0, "value") or 0

    # Compute ratios
    profit_margin = (latest_net_income / latest_revenue * 100) if latest_revenue else 0
    operating_margin = (latest_op_income / latest_revenue * 100) if latest_revenue else 0
    gross_margin = (latest_gross_profit / latest_revenue * 100) if latest_revenue else 0
    debt_ratio = (latest_debt / latest_assets * 100) if latest_assets else 0
    debt_equity = (latest_debt / latest_equity) if latest_equity else 0
    current_ratio = (latest_current_assets / latest_current_liabilities) if latest_current_liabilities else 0
    roa = (latest_net_income / latest_assets * 100) if latest_assets else 0
    roe = (latest_net_income / latest_equity * 100) if latest_equity else 0
    cash_ratio = (latest_cash / latest_current_liabilities) if latest_current_liabilities else 0
    interest_coverage = (latest_op_income / latest_interest) if latest_interest else 0

    # Revenue growth
    revenue_growth = ((latest_revenue - prev_revenue) / prev_revenue * 100) if prev_revenue else 0
    net_income_growth = ((latest_net_income - prev_net_income) / abs(prev_net_income) * 100) if prev_net_income else 0
    ocf_ratio = (latest_ocf / latest_revenue * 100) if latest_revenue else 0

    # Scoring (0-100)
    score = 0.0

    # Profitability (30 pts)
    if profit_margin > 20:
        score += 30
    elif profit_margin > 10:
        score += 22
    elif profit_margin > 5:
        score += 15
    elif profit_margin > 0:
        score += 8
    else:
        score += 2

    # Operating efficiency (15 pts)
    if operating_margin > 25:
        score += 15
    elif operating_margin > 15:
        score += 11
    elif operating_margin > 5:
        score += 7
    elif operating_margin > 0:
        score += 3
    else:
        score += 1

    # Leverage (15 pts)
    if debt_equity < 0.3:
        score += 15
    elif debt_equity < 1.0:
        score += 12
    elif debt_equity < 2.0:
        score += 8
    elif debt_equity < 5.0:
        score += 4
    else:
        score += 1

    # Liquidity (10 pts)
    if current_ratio > 2.5:
        score += 10
    elif current_ratio > 1.5:
        score += 8
    elif current_ratio > 1.0:
        score += 5
    elif current_ratio > 0.5:
        score += 3
    else:
        score += 1

    # Growth (15 pts)
    if revenue_growth > 30:
        score += 15
    elif revenue_growth > 15:
        score += 12
    elif revenue_growth > 5:
        score += 8
    elif revenue_growth > 0:
        score += 5
    else:
        score += 1

    # Cash position (10 pts)
    if cash_ratio > 1.0:
        score += 10
    elif cash_ratio > 0.5:
        score += 7
    elif cash_ratio > 0.2:
        score += 4
    else:
        score += 1

    # Interest coverage (5 pts)
    if interest_coverage > 10:
        score += 5
    elif interest_coverage > 5:
        score += 4
    elif interest_coverage > 2:
        score += 2
    elif interest_coverage > 1:
        score += 1
    else:
        score += 0

    # Determine grade
    if score >= 80:
        grade = "A"
        interpretation = "Excellent — Very strong financial health, minimal risk"
    elif score >= 65:
        grade = "B"
        interpretation = "Above Average — Healthy financials, moderate risk"
    elif score >= 50:
        grade = "C"
        interpretation = "Average — Stable financials, some risk factors"
    elif score >= 35:
        grade = "D"
        interpretation = "Below Average — Weak financials, elevated risk"
    else:
        grade = "F"
        interpretation = "Poor — Significant financial distress, high risk"

    latest_fy = safe_get(revenue, 0, "fy") or ""
    latest_end = safe_get(revenue, 0, "end") or ""

    result = {
        "ticker": ticker,
        "company_name": company_name,
        "health_score": round(score, 1),
        "grade": grade,
        "interpretation": interpretation,
        "metrics": {
            "revenue": round(latest_revenue, 2) if latest_revenue else None,
            "net_income": round(latest_net_income, 2) if latest_net_income else None,
            "profit_margin": round(profit_margin, 2),
            "operating_margin": round(operating_margin, 2),
            "gross_margin": round(gross_margin, 2),
            "debt_ratio": round(debt_ratio, 2),
            "debt_to_equity": round(debt_equity, 2),
            "current_ratio": round(current_ratio, 2),
            "return_on_assets": round(roa, 2),
            "return_on_equity": round(roe, 2),
            "cash_ratio": round(cash_ratio, 2),
            "interest_coverage": round(interest_coverage, 2),
            "revenue_growth_pct": round(revenue_growth, 2),
            "net_income_growth_pct": round(net_income_growth, 2),
            "operating_cash_flow_ratio": round(ocf_ratio, 2),
            "eps": round(latest_eps, 2) if latest_eps else None,
            "total_assets": round(latest_assets, 2) if latest_assets else None,
            "total_liabilities": round(latest_liabilities, 2) if latest_liabilities else None,
            "cash_and_equivalents": round(latest_cash, 2) if latest_cash else None,
            "long_term_debt": round(latest_debt, 2) if latest_debt else None,
        },
        "fiscal_year": latest_fy,
        "period_end": latest_end,
    }
    return result


# ── Filing Text / Sentiment ──
def fetch_filing_text(filing_url: str) -> str:
    """Fetch and extract text from an SEC filing HTML page."""
    try:
        resp = _SEC_CLIENT.get(filing_url)
        html = resp.text
        # Remove scripts, styles, tags
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
        html = re.sub(r'<[^>]+>', ' ', html)
        html = re.sub(r'\s+', ' ', html)
        text = html.strip()
        return text
    except Exception as e:
        return ""


def analyze_sentiment(ticker: str, form_type: str = "10-K", year: Optional[str] = None) -> dict:
    """Analyze sentiment of SEC filing text using Loughran-McDonald dictionary."""
    ticker = ticker.upper().strip()
    cik = lookup_cik(ticker)
    if not cik:
        raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found")

    # Try to get the specific filing
    filing_url = get_filing_url(ticker, form_type, year)
    if not filing_url:
        raise HTTPException(status_code=404, detail=f"No {form_type} filing found for {ticker}")

    cache_key = f"sentiment_{ticker}_{form_type}_{year or 'latest'}.json"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    text = fetch_filing_text(filing_url)
    if not text:
        raise HTTPException(status_code=502, detail="Could not fetch filing text")

    # Loughran-McDonald analysis
    words = text.lower().split()
    word_count = len(words)
    positive_count = sum(1 for w in words if w in LM_POSITIVE)
    negative_count = sum(1 for w in words if w in LM_NEGATIVE)
    uncertainty_count = sum(1 for w in words if w in LM_UNCERTAINTY)

    # Sentiment score = (positive - negative) / total * 100 (normalized)
    sentiment_score = round((positive_count - negative_count) / max(word_count, 1) * 100, 3)

    # Get top phrases
    pos_phrases = sorted(LM_POSITIVE, key=lambda w: words.count(w), reverse=True)[:20]
    neg_phrases = sorted(LM_NEGATIVE, key=lambda w: words.count(w), reverse=True)[:20]

    result = {
        "ticker": ticker,
        "filing": form_type,
        "year": year or "latest",
        "sentiment_score": sentiment_score,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "uncertainty_count": uncertainty_count,
        "total_words": word_count,
        "positive_phrases": [w for w in pos_phrases if words.count(w) > 0],
        "negative_phrases": [w for w in neg_phrases if words.count(w) > 0],
        "text_length": len(text),
        "filing_url": filing_url,
        "interpretation": "Positive" if sentiment_score > 0.5 else "Negative" if sentiment_score < -0.5 else "Neutral",
    }

    cache_path.write_text(json.dumps(result, default=str))
    return result


# ── Filing URL Helper ──
def get_filing_url(ticker: str, form_type: str = "10-K", year: Optional[str] = None) -> Optional[str]:
    """Find the URL for a specific filing."""
    filings = get_company_filings(ticker, count=50)
    for filing in filings:
        if filing.get("form_type", "").upper() == form_type.upper():
            if year:
                filing_year = filing.get("filing_date", "")[:4]
                if filing_year == year:
                    return filing.get("url")
            else:
                return filing.get("url")
    return None


# ── Company Filings ──
def get_company_filings(ticker: str, count: int = 10) -> list:
    """Fetch recent SEC filings for a company from the submissions API."""
    ticker = ticker.upper().strip()
    cik = lookup_cik(ticker)
    if not cik:
        raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found")

    cache_key = f"filings_{ticker}_{count}.json"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    cik_padded = str(cik).zfill(10)
    url = f"{SEC_BASE}/submissions/CIK{cik_padded}.json"
    try:
        data = _SEC_CLIENT.get_json(url)
    except Exception as e:
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Failed to fetch filings: {str(e)}")

    filings = []
    recent_filings = data.get("filings", {}).get("recent", {})
    forms = recent_filings.get("form", [])
    dates = recent_filings.get("filingDate", [])
    descriptions = recent_filings.get("primaryDocumentDescription", [])
    primary_docs = recent_filings.get("primaryDocument", [])
    accession_numbers = recent_filings.get("accessionNumber", [])

    for i in range(min(count, len(forms))):
        form = forms[i] if i < len(forms) else ""
        date = dates[i] if i < len(dates) else ""
        desc = descriptions[i] if i < len(descriptions) else ""
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        accession = accession_numbers[i] if i < len(accession_numbers) else ""

        # Build URL
        acc_no = accession.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no}/{primary_doc}" if primary_doc and accession else ""

        filings.append({
            "form_type": form,
            "filing_date": date,
            "description": desc,
            "url": filing_url,
            "accession": accession,
        })

    cache_path.write_text(json.dumps(filings, default=str))
    return filings


# ── Insider Trades ──
def get_insider_trades(ticker: str, count: int = 10) -> list:
    """Fetch insider trading transactions from SEC Form 4 filings."""
    ticker = ticker.upper().strip()
    cik = lookup_cik(ticker)
    if not cik:
        raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found")

    cache_key = f"insider_{ticker}_{count}.json"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    cik_padded = str(cik).zfill(10)
    url = f"{SEC_BASE}/submissions/CIK{cik_padded}.json"
    try:
        data = _SEC_CLIENT.get_json(url)
    except Exception as e:
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Failed to fetch insider data: {str(e)}")

    transactions = []
    recent_filings = data.get("filings", {}).get("recent", {})
    forms = recent_filings.get("form", [])

    # Find Form 4 filings
    form4_indices = [i for i, f in enumerate(forms) if f == "4"]
    form4_indices = form4_indices[:count]

    accession_numbers = recent_filings.get("accessionNumber", [])
    filing_dates = recent_filings.get("filingDate", [])
    primary_docs = recent_filings.get("primaryDocument", [])

    for idx in form4_indices:
        accession = accession_numbers[idx] if idx < len(accession_numbers) else ""
        filing_date = filing_dates[idx] if idx < len(filing_dates) else ""
        primary_doc = primary_docs[idx] if idx < len(primary_docs) else ""
        acc_no = accession.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no}/{primary_doc}" if primary_doc and accession else ""

        # Try to fetch Form 4 XML for details
        insider_name = ""
        relationship = ""
        transaction_type = ""
        transaction_code = ""
        shares = ""
        price = ""
        security_title = ""
        shares_owned_after = ""

        if filing_url and filing_url.endswith(".xml"):
            try:
                xml_text = _SEC_CLIENT.get(filing_url).text
                root = ET.fromstring(xml_text)
                for elem in root.iter():
                    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                    if tag == "rptOwnerName":
                        insider_name = elem.text or ""
                    elif tag == "rptOwnerRelationship":
                        for child in elem:
                            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                            if ctag == "officerTitle":
                                relationship = f"Officer ({child.text})" if child.text else "Officer"
                                break
                            relationship = child.text or ""
                    elif tag == "transactionType":
                        transaction_type = elem.text or ""
                    elif tag == "transactionCode":
                        transaction_code = elem.text or ""
                    elif tag == "securityTitle":
                        for child in elem:
                            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                            if ctag == "value":
                                security_title = child.text or ""
                    elif tag == "transactionShares":
                        for child in elem:
                            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                            if ctag == "value":
                                shares = str(child.text or "")
                    elif tag == "transactionPricePerShare":
                        for child in elem:
                            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                            if ctag == "value":
                                price = str(child.text or "")
                    elif tag == "sharesOwnedFollowingTransaction":
                        for child in elem:
                            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                            if ctag == "value":
                                shares_owned_after = str(child.text or "")
            except Exception:
                pass

        total_value = None
        try:
            if shares and price:
                total_value = float(shares) * float(price)
        except (ValueError, TypeError):
            pass

        transactions.append({
            "insider_name": insider_name,
            "insider_cik": "",
            "relationship": relationship,
            "transaction_type": transaction_type,
            "transaction_code": transaction_code,
            "security_title": security_title,
            "transaction_date": "",
            "filing_date": filing_date,
            "shares": shares,
            "price_per_share": price,
            "total_value": total_value,
            "shares_owned_after": shares_owned_after,
            "filing_url": filing_url,
        })

    cache_path.write_text(json.dumps(transactions, default=str))
    return transactions


# ── Company Profile ──
def get_company_profile(ticker: str) -> dict:
    """Get company profile information from SEC submissions data."""
    ticker = ticker.upper().strip()
    cik = lookup_cik(ticker)
    if not cik:
        raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found")

    cache_key = f"profile_{ticker}.json"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    cik_padded = str(cik).zfill(10)
    url = f"{SEC_BASE}/submissions/CIK{cik_padded}.json"
    try:
        data = _SEC_CLIENT.get_json(url)
    except Exception as e:
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Failed to fetch profile: {str(e)}")

    profile = {
        "ticker": ticker,
        "cik": cik,
        "name": data.get("name", ""),
        "sic": data.get("sic", ""),
        "sic_description": data.get("sicDescription", ""),
        "exchanges": data.get("exchanges", []),
        "ein": data.get("ein", ""),
        "description": data.get("description", ""),
        "website": data.get("website", ""),
        "investor_website": data.get("investorWebsite", ""),
        "phone": data.get("phone", ""),
        "fiscal_year_end": data.get("fiscalYearEnd", ""),
        "state_of_incorporation": data.get("stateOfIncorporation", ""),
        "category": data.get("category", ""),
        "entity_type": data.get("entityType", ""),
        "former_names": [{"name": n.get("name", ""), "from": n.get("from", ""), "to": n.get("to", "")} for n in data.get("formerNames", [])],
        "address_business": {
            "street1": safe_get(data, "addresses", "business", "street1", default=""),
            "street2": safe_get(data, "addresses", "business", "street2"),
            "city": safe_get(data, "addresses", "business", "city", default=""),
            "stateOrCountry": safe_get(data, "addresses", "business", "stateOrCountry", default=""),
            "zipCode": safe_get(data, "addresses", "business", "zipCode", default=""),
            "stateOrCountryDescription": safe_get(data, "addresses", "business", "stateOrCountryDescription", default=""),
        },
        "address_mailing": {
            "street1": safe_get(data, "addresses", "mailing", "street1", default=""),
            "street2": safe_get(data, "addresses", "mailing", "street2"),
            "city": safe_get(data, "addresses", "mailing", "city", default=""),
            "stateOrCountry": safe_get(data, "addresses", "mailing", "stateOrCountry", default=""),
            "zipCode": safe_get(data, "addresses", "mailing", "zipCode", default=""),
            "stateOrCountryDescription": safe_get(data, "addresses", "mailing", "stateOrCountryDescription", default=""),
        },
    }

    cache_path.write_text(json.dumps(profile, default=str))
    return profile


# ── Second extract_metric (for financial statements) ──
def extract_metric_from_facts(facts: dict, concept: str) -> Optional[dict]:
    """Extract a single metric value from facts data, returning the most recent annual value."""
    values = extract_metric(facts, concept, max_vals=1)
    if values:
        return values[0]
    return None


# ── Financial Statement ──
def get_financial_statement(ticker: str, statement_type: str) -> dict:
    """Get an income statement, balance sheet, or cash flow statement for a ticker."""
    ticker = ticker.upper().strip()
    facts = fetch_company_facts(ticker)
    company_name = facts.get("entityName", "")
    cik = facts.get("_cik", lookup_cik(ticker))

    # Map statement types to XBRL concepts
    statement_map = {
        "income-statement": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": "Revenue",
            "CostOfRevenue": "Cost of Revenue",
            "GrossProfit": "Gross Profit",
            "OperatingExpenses": "Operating Expenses",
            "ResearchAndDevelopmentExpense": "R&D Expenses",
            "SellingGeneralAndAdministrativeExpense": "SG&A Expenses",
            "OperatingIncomeLoss": "Operating Income",
            "NonoperatingIncomeExpense": "Non-Operating Income/Expense",
            "InterestExpense": "Interest Expense",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxExpenseBenefit": "Pre-Tax Income",
            "IncomeTaxExpenseBenefit": "Income Tax Expense",
            "NetIncomeLoss": "Net Income",
            "EarningsPerShareBasic": "EPS (Basic)",
            "EarningsPerShareDiluted": "EPS (Diluted)",
            "WeightedAverageNumberOfSharesOutstandingBasic": "Weighted Avg. Shares (Basic)",
            "WeightedAverageNumberOfSharesOutstandingDiluted": "Weighted Avg. Shares (Diluted)",
            "DividendsCommonStock": "Dividends Paid",
            "NetIncomeLossAvailableToCommonStockholdersBasic": "Net Income Available to Common",
            "DepreciationDepletionAndAmortization": "Depreciation & Amortization",
            "Ebitda": "EBITDA",
        },
        "balance-sheet": {
            "Assets": "Assets",
            "AssetsCurrent": "Current Assets",
            "CashAndCashEquivalentsAtCarryingValue": "Cash and Equivalents",
            "ShortTermInvestments": "Short-Term Investments",
            "AccountsReceivableNetCurrent": "Accounts Receivable",
            "InventoryNet": "Inventory",
            "PrepaidExpenseAndOtherAssetsCurrent": "Prepaid Expenses",
            "AssetsNoncurrent": "Noncurrent Assets",
            "PropertyPlantAndEquipmentNet": "PP&E",
            "Goodwill": "Goodwill",
            "IntangibleAssetsNetExcludingGoodwill": "Intangible Assets",
            "LongTermInvestments": "Long-Term Investments",
            "DeferredTaxAssetsNet": "Deferred Tax Assets",
            "OtherAssetsNoncurrent": "Other Noncurrent Assets",
            "Liabilities": "Liabilities",
            "LiabilitiesCurrent": "Current Liabilities",
            "AccountsPayableCurrent": "Accounts Payable",
            "AccruedLiabilitiesCurrent": "Accrued Liabilities",
            "ShortTermDebt": "Short-Term Debt",
            "DeferredRevenueCurrent": "Deferred Revenue (Current)",
            "LiabilitiesNoncurrent": "Noncurrent Liabilities",
            "LongTermDebtNoncurrent": "Long-Term Debt",
            "DeferredRevenueNoncurrent": "Deferred Revenue (Noncurrent)",
            "DeferredTaxLiabilities": "Deferred Tax Liabilities",
            "CommitmentsAndContingencies": "Commitments & Contingencies",
            "StockholdersEquity": "Stockholders' Equity",
            "CommonStocksIncludingAdditionalPaidInCapital": "Common Stock & APIC",
            "RetainedEarningsAccumulatedDeficit": "Retained Earnings",
            "AccumulatedOtherComprehensiveIncomeLossNetOfTax": "Accumulated OCI",
            "TreasuryStockCommonValue": "Treasury Stock",
        },
        "cash-flow": {
            "NetCashProvidedByOperatingActivities": "Operating Cash Flow",
            "NetCashProvidedByUsedInInvestingActivities": "Investing Cash Flow",
            "NetCashProvidedByUsedInFinancingActivities": "Financing Cash Flow",
            "CashAndCashEquivalentsPeriodIncreaseDecrease": "Net Change in Cash",
            "CashAndCashEquivalentsAtCarryingValue": "Cash & Equivalents (End of Period)",
            "DepreciationDepletionAndAmortization": "Depreciation & Amortization",
            "ShareBasedCompensation": "Stock-Based Compensation",
            "AdjustmentsToReconcileNetIncomeLossToCashProvidedByUsedInOperatingActivities": "Operating Adjustments",
            "IncreaseDecreaseInAccountsReceivable": "Change in Accounts Receivable",
            "IncreaseDecreaseInInventories": "Change in Inventory",
            "IncreaseDecreaseInAccountsPayable": "Change in Accounts Payable",
            "IncreaseDecreaseInAccruedLiabilities": "Change in Accrued Liabilities",
            "PaymentsOfDividends": "Dividends Paid",
            "ProceedsFromIssuanceOfDebt": "Debt Issuance",
            "RepaymentsOfDebt": "Debt Repayment",
            "ProceedsFromIssuanceOfCommonStock": "Stock Issuance",
            "PaymentsRelatedToTaxWithholdingForShareBasedCompensation": "Tax Withholding for SBC",
            "PaymentsForRepurchaseOfCommonStock": "Stock Repurchases",
            "NetCashProvidedByUsedInContinuingOperations": "Cash from Continuing Operations",
        },
    }

    concepts = statement_map.get(statement_type, {})
    if not concepts:
        raise HTTPException(status_code=400, detail=f"Unknown statement type: {statement_type}. Use income-statement, balance-sheet, or cash-flow.")

    items = []
    for concept, label in concepts.items():
        vals = extract_metric(facts, concept, max_vals=1)
        if vals:
            items.append({
                "label": label,
                "concept": concept,
                "value": vals[0]["value"],
                "fiscal_year": vals[0]["fy"],
                "period_end": vals[0]["end"],
            })
        else:
            items.append({
                "label": label,
                "concept": concept,
                "value": None,
                "fiscal_year": None,
                "period_end": None,
            })

    result = {
        "ticker": ticker,
        "company_name": company_name,
        "statement_type": statement_type,
        "cik": cik,
        "items": items,
    }

    return result


# ── Full Facts ──
def get_full_facts(ticker: str) -> dict:
    """Get all available financial metrics for a company from SEC facts."""
    ticker = ticker.upper().strip()
    facts = fetch_company_facts(ticker)
    company_name = facts.get("entityName", "")
    cik = facts.get("_cik", lookup_cik(ticker))

    cache_key = f"full_{ticker}.json"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    metrics = {}

    for concept, concept_data in us_gaap.items():
        units_data = concept_data.get("units", concept_data)
        for unit_key, entries in units_data.items():
            if unit_key.startswith("USD") or unit_key == "pure" or unit_key.startswith("shares"):
                # Get the most recent annual value
                best = None
                for entry in entries:
                    if entry.get("val") is not None:
                        frame = entry.get("frame", "")
                        is_annual = not frame or "Q" not in frame
                        if is_annual:
                            if best is None or (entry.get("end") or "") > (best.get("end") or ""):
                                best = entry
                if best is None and entries:
                    # Fall back to most recent any
                    best = entries[0]
                if best:
                    metrics[concept] = {
                        "value": best.get("val"),
                        "fy": best.get("fy"),
                        "end": best.get("end"),
                    }

    result = {
        "ticker": ticker,
        "company_name": company_name,
        "cik": cik,
        "metric_count": len(metrics),
        "metrics": metrics,
    }

    cache_path.write_text(json.dumps(result, default=str))
    return result


# ── Auth & Rate Limiting Middleware ──
async def auth_and_rate_limit_middleware(request: Request, call_next):
    """Middleware for API key authentication and rate limiting."""
    path = request.url.path

    # Skip auth for non-API paths and static pages
    if not path.startswith("/v1/"):
        response = await call_next(request)
        return response

    # Get API key from header
    api_key = request.headers.get("X-API-Key", "")
    if not api_key:
        logger.warning(f"API request missing key: {request.url.path} from {request.client.host if request.client else 'unknown'}")
        return JSONResponse(status_code=401, content={"detail": "Missing X-API-Key header"})

    # Look up key
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM api_keys WHERE key = ?", (api_key,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        logger.warning(f"API request with invalid key: {request.url.path}")
        return JSONResponse(status_code=401, content={"detail": "Invalid API key"})

    user_id = row["user_id"]

    # Get user plan
    cursor.execute("SELECT plan FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return JSONResponse(status_code=401, content={"detail": "User not found"})

    plan = user["plan"]
    limit = PLAN_LIMITS.get(plan, 20)

    # Check monthly usage
    month = datetime.utcnow().strftime("%Y-%m")
    cursor.execute("SELECT requests FROM usage_log WHERE user_id = ? AND month = ?", (user_id, month))
    usage_row = cursor.fetchone()
    used = usage_row["requests"] if usage_row else 0

    if used >= limit:
        conn.close()
        return JSONResponse(status_code=429, content={
            "detail": f"Monthly request limit ({limit}) reached. Upgrade your plan at /upgrade",
            "plan": plan,
            "used": used,
            "limit": limit,
        })

    # Increment usage
    if usage_row:
        cursor.execute("UPDATE usage_log SET requests = requests + 1 WHERE user_id = ? AND month = ?", (user_id, month))
    else:
        cursor.execute("INSERT INTO usage_log (user_id, month, requests) VALUES (?, ?, 1)", (user_id, month))
    conn.commit()
    conn.close()

    # Pass user info in request state
    request.state.user_id = user_id
    request.state.plan = plan
    request.state.used = used
    request.state.limit = limit

    response = await call_next(request)
    return response


app.middleware("http")(auth_and_rate_limit_middleware)


# ── Session / Auth Helpers ──
async def _parse_body(request: Request) -> dict:
    """Parse request body as JSON or form-encoded data."""
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        return dict(form)
    try:
        return await request.json()
    except Exception:
        return {}

def get_session_user(request: Request) -> Optional[dict]:
    """Get user from session cookie."""
    token = request.cookies.get("session")
    if not token:
        return None
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.id, u.email, u.plan, u.is_admin
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token = ?
    """, (token,))
    user = cursor.fetchone()
    conn.close()
    if user:
        return {"id": user["id"], "email": user["email"], "plan": user["plan"], "is_admin": user["is_admin"]}
    return None


def require_session(request: Request) -> dict:
    """Require a valid session, raising 401 if not authenticated."""
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def html_response(content: str) -> HTMLResponse:
    """Return an HTML response with proper content type."""
    return HTMLResponse(content=content)


# ══════════════════════════════════════════════════
#  HTML TEMPLATES — using $variable convention to avoid CSS brace conflicts
# ══════════════════════════════════════════════════
from string import Template

SIGNIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign In — slacking.biz</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#08090b;color:#eef2f6;min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;-webkit-font-smoothing:antialiased;font-feature-settings:'cv01','ss03'}
.container{max-width:400px;width:100%;padding:24px}
.card{background:#111317;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:32px;box-shadow:0 4px 12px rgba(0,0,0,0.4),0 1px 3px rgba(0,0,0,0.3)}
.logo{display:flex;align-items:center;gap:10px;justify-content:center;margin-bottom:28px;text-decoration:none}
.logo-icon{width:26px;height:26px;border-radius:6px;background:linear-gradient(135deg,#14b8a6,#0d9488);box-shadow:0 0 12px rgba(20,184,166,0.3)}
.logo-text{font-size:18px;font-weight:600;color:#eef2f6;letter-spacing:-0.3px}
.card h2{font-size:22px;margin-bottom:8px;color:#eef2f6;text-align:center;font-weight:600;letter-spacing:-0.03em}
.card .subtitle{font-size:14px;color:#939bb3;margin-bottom:24px;text-align:center}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;color:#939bb3;margin-bottom:6px;font-weight:500}
.form-group input{width:100%;padding:10px 14px;background:#08090b;border:1px solid rgba(255,255,255,0.06);border-radius:6px;color:#eef2f6;font-size:14px;outline:none;transition:border-color .2s;font-family:'Inter',sans-serif}
.form-group input:focus{border-color:#14b8a6}
.form-group input::placeholder{color:#5c6378}
.btn{width:100%;padding:11px;background:#14b8a6;color:#08090b;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s;font-family:'Inter',sans-serif}
.btn:hover{background:#2dd4bf}
.btn:disabled{opacity:.5;cursor:not-allowed}
.error{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#ef4444;font-size:13px;padding:10px 14px;border-radius:6px;margin-bottom:16px;display:none}
.footer{text-align:center;margin-top:20px;padding-top:20px;border-top:1px solid rgba(255,255,255,0.04);font-size:13px;color:#5c6378}
.footer a{color:#14b8a6;text-decoration:none;font-weight:500;transition:opacity .2s}
.footer a:hover{opacity:.7}
.back-link{display:flex;align-items:center;gap:6px;justify-content:center;margin-top:16px;font-size:13px;color:#5c6378}
.back-link a{color:#5c6378;text-decoration:none;transition:color .2s}
.back-link a:hover{color:#eef2f6}
</style>
</head>
<body>
<div class="container">
<a href="/" class="logo">
<div class="logo-icon"></div>
<span class="logo-text">slacking.biz</span>
</a>
<div class="card">
<h2>Welcome back</h2>
<p class="subtitle">Sign in to your account</p>
<div class="error" id="errorMsg"></div>
<form id="loginForm">
<div class="form-group">
<label>Email</label>
<input type="email" id="email" placeholder="you@example.com" required autofocus>
</div>
<div class="form-group">
<label>Password</label>
<input type="password" id="password" placeholder="Enter your password" required>
</div>
<button type="submit" class="btn">Sign In</button>
</form>
<div class="footer">
Don't have an account? <a href="/signup">Create one</a>
</div>
</div>
<div class="back-link"><a href="/">← Back to home</a></div>
</div>
<script>
document.getElementById('loginForm').onsubmit=async(e)=>{
e.preventDefault();
const btn=e.target.querySelector('button');
const err=document.getElementById('errorMsg');
btn.disabled=true;btn.textContent='Signing in...';
err.style.display='none';
try{
const r=await fetch('/login',{
method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({email:document.getElementById('email').value,password:document.getElementById('password').value})
});
const d=await r.json();
if(r.ok){window.location.href='/dashboard'}else{
err.textContent=d.detail||d.error||'Invalid email or password';
err.style.display='block';
}
}catch(e){
err.textContent='Network error. Please try again.';
err.style.display='block';
}finally{btn.disabled=false;btn.textContent='Sign In';}
};
</script>
</body>
</html>"""

SIGNUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign Up — slacking.biz</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#08090b;color:#eef2f6;min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;-webkit-font-smoothing:antialiased;font-feature-settings:'cv01','ss03'}
.container{max-width:420px;width:100%;padding:24px}
.card{background:#111317;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:32px;box-shadow:0 4px 12px rgba(0,0,0,0.4),0 1px 3px rgba(0,0,0,0.3)}
.logo{display:flex;align-items:center;gap:10px;justify-content:center;margin-bottom:28px;text-decoration:none}
.logo-icon{width:26px;height:26px;border-radius:6px;background:linear-gradient(135deg,#14b8a6,#0d9488);box-shadow:0 0 12px rgba(20,184,166,0.3)}
.logo-text{font-size:18px;font-weight:600;color:#eef2f6;letter-spacing:-0.3px}
.card h2{font-size:22px;margin-bottom:8px;color:#eef2f6;text-align:center;font-weight:600;letter-spacing:-0.03em}
.card .subtitle{font-size:14px;color:#939bb3;margin-bottom:24px;text-align:center}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;color:#939bb3;margin-bottom:6px;font-weight:500}
.form-group input{width:100%;padding:10px 14px;background:#08090b;border:1px solid rgba(255,255,255,0.06);border-radius:6px;color:#eef2f6;font-size:14px;outline:none;transition:border-color .2s;font-family:'Inter',sans-serif}
.form-group input:focus{border-color:#14b8a6}
.form-group input::placeholder{color:#5c6378}
.btn{width:100%;padding:11px;background:#14b8a6;color:#08090b;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s;font-family:'Inter',sans-serif}
.btn:hover{background:#2dd4bf}
.btn:disabled{opacity:.5;cursor:not-allowed}
.error{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#ef4444;font-size:13px;padding:10px 14px;border-radius:6px;margin-bottom:16px;display:none}
.success{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:#22c55e;font-size:13px;padding:10px 14px;border-radius:6px;margin-bottom:16px;display:none}
.api-key-display{background:#08090b;border:1px solid rgba(255,255,255,0.06);border-radius:6px;padding:12px 14px;margin:16px 0;word-break:break-all;font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:13px;color:#14b8a6;display:none;line-height:1.5}
.footer{text-align:center;margin-top:20px;padding-top:20px;border-top:1px solid rgba(255,255,255,0.04);font-size:13px;color:#5c6378}
.footer a{color:#14b8a6;text-decoration:none;font-weight:500;transition:opacity .2s}
.footer a:hover{opacity:.7}
.back-link{display:flex;align-items:center;gap:6px;justify-content:center;margin-top:16px;font-size:13px;color:#5c6378}
.back-link a{color:#5c6378;text-decoration:none;transition:color .2s}
.back-link a:hover{color:#eef2f6}
.features-list{display:flex;gap:8px;justify-content:center;margin-bottom:24px;flex-wrap:wrap}
.features-list span{font-size:12px;color:#939bb3;background:#111317;border:1px solid rgba(255,255,255,0.06);padding:4px 10px;border-radius:6px;font-weight:500}
.features-list span::before{content:'✓ ';color:#14b8a6}
</style>
</head>
<body>
<div class="container">
<a href="/" class="logo">
<div class="logo-icon"></div>
<span class="logo-text">slacking.biz</span>
</a>
<div class="card">
<h2>Create your account</h2>
<p class="subtitle">Get started free — no credit card needed</p>
<div class="features-list">
<span>10K+ companies</span>
<span>Health scores</span>
<span>Sentiment analysis</span>
<span>20 free/mo</span>
</div>
<div class="error" id="errorMsg"></div>
<div class="success" id="successMsg">Account created! Your API key is below.</div>
<div class="api-key-display" id="apiKeyDisplay"></div>
<form id="signupForm">
<div class="form-group">
<label>Email</label>
<input type="email" id="email" placeholder="you@example.com" required autofocus>
</div>
<div class="form-group">
<label>Password</label>
<input type="password" id="password" placeholder="Create a password (min 8 characters)" required minlength="8">
</div>
<div class="form-group">
<label>Confirm Password</label>
<input type="password" id="confirm" placeholder="Repeat your password" required>
</div>
<button type="submit" class="btn" id="submitBtn">Create Account</button>
</form>
<div class="footer" id="footerLinks">
Already have an account? <a href="/login">Sign in</a>
</div>
</div>
<div class="back-link"><a href="/">← Back to home</a></div>
</div>
<script>
document.getElementById('signupForm').onsubmit=async(e)=>{
e.preventDefault();
const btn=document.getElementById('submitBtn');
const pwd=document.getElementById('password').value;
const confirm=document.getElementById('confirm').value;
const err=document.getElementById('errorMsg');
const suc=document.getElementById('successMsg');
const keyEl=document.getElementById('apiKeyDisplay');
err.style.display='none';suc.style.display='none';keyEl.style.display='none';
if(pwd!==confirm){
err.textContent='Passwords do not match';
err.style.display='block';
return;
}
btn.disabled=true;btn.textContent='Creating account...';
try{
const r=await fetch('/signup',{
method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({email:document.getElementById('email').value,password:pwd})
});
const d=await r.json();
if(r.ok){
suc.style.display='block';
keyEl.textContent='API Key: '+d.api_key;
keyEl.style.display='block';
document.getElementById('footerLinks').innerHTML='<a href="/login">Go to login →</a>';
btn.style.display='none';
}else{
err.textContent=d.detail||d.error||'Signup failed';
err.style.display='block';
btn.disabled=false;btn.textContent='Create Account';
}
}catch(e){
err.textContent='Network error. Please try again.';
err.style.display='block';
btn.disabled=false;btn.textContent='Create Account';
}
};
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — slacking.biz</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:#07080a;color:#eef2f6;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background-image:radial-gradient(rgba(255,255,255,0.04) 1px,transparent 1px);background-size:24px 24px;pointer-events:none;z-index:0}

.nav{position:sticky;top:0;z-index:100;padding:0 24px;display:flex;align-items:center;height:56px;justify-content:space-between;max-width:1120px;margin:0 auto;width:100%}
.nav-brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;color:#eef2f6;text-decoration:none;letter-spacing:-0.3px}
.nav-brand-logo{width:22px;height:22px;border-radius:5px;background:linear-gradient(135deg,#14b8a6,#0d9488);box-shadow:0 0 10px rgba(20,184,166,0.3)}
.nav-links{display:flex;gap:4px;align-items:center}
.nav-links a{color:#6b7280;text-decoration:none;font-size:13px;font-weight:500;padding:6px 12px;border-radius:5px;transition:all .2s}
.nav-links a:hover{color:#eef2f6;background:rgba(255,255,255,0.04)}
.nav-links .btn-nav{padding:6px 16px;background:#14b8a6;color:#07080a;border-radius:5px;font-weight:600;font-size:12.5px;line-height:1}
.nav-links .email{color:#4b5563;font-size:12.5px;padding:0 6px;font-weight:400}
.container{max-width:900px;margin:0 auto;padding:32px 24px}
h1{font-size:26px;margin-bottom:24px;font-weight:600;letter-spacing:-0.03em}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:32px}
.stat-card{background:#111317;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:20px}
.stat-card .label{font-size:13px;color:#939bb3;margin-bottom:4px}
.stat-card .value{font-size:26px;font-weight:700;color:#eef2f6}
.stat-card .value.green{color:#22c55e}
.stat-card .value.yellow{color:#f59e0b}
.stat-card .value.red{color:#ef4444}
.api-section{background:#111317;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:24px;margin-bottom:20px}
.api-section h2{font-size:17px;margin-bottom:12px;font-weight:600;letter-spacing:-0.02em}
.api-key-box{display:flex;align-items:center;background:#08090b;border:1px solid rgba(255,255,255,0.06);border-radius:6px;padding:10px 14px;margin-bottom:12px}
.api-key-box code{flex:1;font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:13px;color:#14b8a6;word-break:break-all}
.api-key-box button{background:transparent;border:1px solid rgba(255,255,255,0.06);color:#939bb3;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;transition:all .2s;margin-left:8px;font-family:'Inter',sans-serif;font-weight:500}
.api-key-box button:hover{border-color:#14b8a6;color:#14b8a6}
.plan-badge{display:inline-block;padding:4px 12px;border-radius:6px;font-size:12px;font-weight:600;text-transform:capitalize;background:#111317;border:1px solid rgba(255,255,255,0.06);color:#939bb3}
.plan-badge.free{color:#939bb3}
.plan-badge.starter{border-color:rgba(245,158,11,0.3);color:#f59e0b}
.plan-badge.pro{border-color:rgba(20,184,166,0.3);color:#14b8a6}
.plan-badge.enterprise{border-color:rgba(34,197,94,0.3);color:#22c55e}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:10px 22px;border-radius:6px;font-size:14px;font-weight:600;text-decoration:none;transition:all .25s;cursor:pointer;border:none;gap:6px;font-family:'Inter',sans-serif}
.btn-primary{background:#14b8a6;color:#08090b}
.btn-primary:hover{background:#2dd4bf;box-shadow:0 0 20px rgba(20,184,166,0.3)}
.btn-outline{background:transparent;border:1px solid rgba(255,255,255,0.06);color:#eef2f6}
.btn-outline:hover{border-color:#14b8a6;color:#14b8a6}
.actions{display:flex;gap:12px;margin-top:20px}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-brand"><div class="nav-brand-logo"></div>slacking.biz</a>
<div class="nav-links">
<a href="/#features">Features</a>
<a href="/#pricing">Pricing</a>
<a href="/mcp">MCP Setup</a>
<a href="/status">Status</a>
$nav_right
</div>
</nav>
<div class="container">
<h1>Dashboard</h1>
<div class="stats-grid">
<div class="stat-card">
<div class="label">Requests Used</div>
<div class="value $used_class">$used</div>
</div>
<div class="stat-card">
<div class="label">Requests Remaining</div>
<div class="value $remaining_class">$remaining</div>
</div>
<div class="stat-card">
<div class="label">Monthly Limit</div>
<div class="value">$limit</div>
</div>
<div class="stat-card">
<div class="label">Current Plan</div>
<div class="value"><span class="plan-badge $plan">$plan</span></div>
</div>
</div>
<div class="api-section">
<h2>Your API Key</h2>
<div class="api-key-box">
<code id="apiKey">$api_key</code>
<button onclick="copyKey()">Copy</button>
<button onclick="toggleKey()" id="toggleBtn">Hide</button>
</div>
<p style="color:#8b949e;font-size:13px">Include this key in the <code style="color:#58a6ff">X-API-Key</code> header when making API requests.</p>
</div>
<div class="api-section">
<h2>Quick Start</h2>
<p style="color:#8b949e;font-size:14px;margin-bottom:12px">Try the API right now in the <a href="/playground" style="color:#58a6ff">Playground</a> or use curl:</p>
<pre style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:14px;font-size:13px;overflow-x:auto;color:#8b949e">curl -H "X-API-Key: $api_key" https://slacking.biz/v1/financial/NVDA</pre>
</div>
<div class="actions">
<a href="/playground" class="btn btn-primary">Try Playground</a>
<a href="/status" class="btn btn-outline">View Status</a>
<a href="/upgrade" class="btn btn-outline">Upgrade Plan</a>
</div>
</div>
<script>
function copyKey(){
const el=document.getElementById('apiKey');
navigator.clipboard.writeText(el.textContent).then(()=>{
const btn=event.target;
const t=btn.textContent;
btn.textContent='Copied!';
setTimeout(()=>btn.textContent=t,2000);
});
}
function toggleKey(){
const el=document.getElementById('apiKey');
const btn=document.getElementById('toggleBtn');
if(el.style.filter==='blur(4px)'||el.dataset.hidden==='true'){
el.style.filter='none';el.dataset.hidden='false';btn.textContent='Hide';
}else{
el.style.filter='blur(4px)';el.dataset.hidden='true';btn.textContent='Show';
}
}
</script>
</body>
</html>"""

PLAYGROUND_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Playground — slacking.biz</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:#07080a;color:#eef2f6;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background-image:radial-gradient(rgba(255,255,255,0.04) 1px,transparent 1px);background-size:24px 24px;pointer-events:none;z-index:0}

.nav{position:sticky;top:0;z-index:100;padding:0 24px;display:flex;align-items:center;height:56px;justify-content:space-between;max-width:1120px;margin:0 auto;width:100%}
.nav-brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;color:#eef2f6;text-decoration:none;letter-spacing:-0.3px}
.nav-brand-logo{width:22px;height:22px;border-radius:5px;background:linear-gradient(135deg,#14b8a6,#0d9488);box-shadow:0 0 10px rgba(20,184,166,0.3)}
.nav-links{display:flex;gap:4px;align-items:center}
.nav-links a{color:#6b7280;text-decoration:none;font-size:13px;font-weight:500;padding:6px 12px;border-radius:5px;transition:all .2s}
.nav-links a:hover{color:#eef2f6;background:rgba(255,255,255,0.04)}
.nav-links .btn-nav{padding:6px 16px;background:#14b8a6;color:#07080a;border-radius:5px;font-weight:600;font-size:12.5px;line-height:1}
.nav-links .email{color:#4b5563;font-size:12.5px;padding:0 6px;font-weight:400}
.container{max-width:900px;margin:0 auto;padding:32px 24px}
h1{font-size:26px;margin-bottom:24px;font-weight:600;letter-spacing:-0.03em}
.card{background:#111317;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:24px;margin-bottom:20px;box-shadow:0 1px 2px rgba(0,0,0,0.3)}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;color:#939bb3;margin-bottom:6px;font-weight:500}
.form-group select,.form-group input,.form-group textarea{width:100%;padding:10px 12px;background:#08090b;border:1px solid rgba(255,255,255,0.06);border-radius:6px;color:#eef2f6;font-size:14px;outline:none;transition:border-color .2s;font-family:'Inter',sans-serif}
.form-group select:focus,.form-group input:focus,.form-group textarea:focus{border-color:#14b8a6}
.form-group textarea{min-height:80px;resize:vertical}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:10px 24px;background:#14b8a6;color:#08090b;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;transition:all .25s;font-family:'Inter',sans-serif;gap:6px}
.btn:hover{background:#2dd4bf;box-shadow:0 0 20px rgba(20,184,166,0.3)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.response-area{background:#08090b;border:1px solid rgba(255,255,255,0.06);border-radius:6px;padding:16px;margin-top:16px;min-height:100px;font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:13px;white-space:pre-wrap;word-break:break-all;color:#939bb3;overflow-x:auto;max-height:500px;overflow-y:auto}
.spinner{display:none;text-align:center;padding:20px;color:#5c6378}
.spinner.active{display:block}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-brand"><div class="nav-brand-logo"></div>slacking.biz</a>
<div class="nav-links">
<a href="/#features">Features</a>
<a href="/#pricing">Pricing</a>
<a href="/mcp">MCP Setup</a>
<a href="/status">Status</a>
$nav_right
</div>
</nav>
<div class="container">
<h1>API Playground</h1>
<div class="card">
<div class="form-row">
<div class="form-group">
<label>Endpoint</label>
<select id="endpoint">
<option value="/v1/financial/{ticker}">GET /v1/financial/{ticker} - Health Score</option>
<option value="/v1/financial/{ticker}/sentiment">GET /v1/financial/{ticker}/sentiment - Sentiment</option>
<option value="/v1/financial/{ticker}/insider">GET /v1/financial/{ticker}/insider - Insider Trades</option>
<option value="/v1/financial/{ticker}/filings">GET /v1/financial/{ticker}/filings - Filings Feed</option>
<option value="/v1/financial/{ticker}/profile">GET /v1/financial/{ticker}/profile - Company Profile</option>
<option value="/v1/financial/{ticker}/full">GET /v1/financial/{ticker}/full - All Financial Data</option>
<option value="/v1/financial/{ticker}/income-statement">GET /v1/financial/{ticker}/income-statement - Income Statement</option>
<option value="/v1/financial/{ticker}/balance-sheet">GET /v1/financial/{ticker}/balance-sheet - Balance Sheet</option>
<option value="/v1/financial/{ticker}/cash-flow">GET /v1/financial/{ticker}/cash-flow - Cash Flow</option>
<option value="/v1/financial/{ticker}/trends">GET /v1/financial/{ticker}/trends - Historical Trends</option>
<option value="/v1/financial/{ticker}/vs-industry">GET /v1/financial/{ticker}/vs-industry - Industry Comparison</option>
<option value="/v1/financial/{ticker}/filings/search">GET /v1/financial/{ticker}/filings/search - Filing Search</option>
<option value="/v1/financial/{ticker}/segments">GET /v1/financial/{ticker}/segments - Revenue Segments</option>
<option value="/v1/financial/{ticker}/filing-types">GET /v1/financial/{ticker}/filing-types - Filing Types</option>
<option value="/v1/financial/screener">POST /v1/financial/screener - Stock Screener</option>
<option value="/v1/financial/batch">POST /v1/financial/batch - Multi-Ticker Batch</option>
</select>
</div>
<div class="form-group">
<label>Ticker Symbol</label>
<input type="text" id="ticker" placeholder="e.g. AAPL, NVDA, MSFT" value="NVDA">
</div>
</div>
<div class="form-group" id="extraParams" style="display:none">
<label>Extra Parameters (JSON)</label>
<textarea id="params" placeholder='{"form_types": "10-K,10-Q,8-K"}'></textarea>
</div>
<button class="btn" onclick="execute()">▶ Execute</button>
<div class="spinner" id="spinner">Loading...</div>
<div class="response-area" id="response">Response will appear here...</div>
</div>
</div>
<script>
document.getElementById('endpoint').onchange=function(){
const v=this.value;
document.getElementById('extraParams').style.display=(v.includes('search')||v.includes('screener')||v.includes('batch'))?'block':'none';
};
async function execute(){
const endpoint=document.getElementById('endpoint').value;
const ticker=document.getElementById('ticker').value.trim().toUpperCase()||'NVDA';
let url=endpoint.replace('{ticker}',ticker);
const paramsText=document.getElementById('params').value;
const btn=document.querySelector('.btn');
const spinner=document.getElementById('spinner');
const resp=document.getElementById('response');
btn.disabled=true;spinner.classList.add('active');resp.textContent='Loading...';
let queryString='';
if(paramsText){
try{
const params=JSON.parse(paramsText);
queryString='?'+new URLSearchParams(params).toString();
}catch(e){resp.textContent='Invalid JSON in params';btn.disabled=false;spinner.classList.remove('active');return;}
}
try{
const r=await fetch(url+queryString,{headers:{'X-API-Key':'$api_key'}});
const data=await r.json();
resp.textContent=JSON.stringify(data,null,2);
}catch(e){resp.textContent='Error: '+e.message;}
finally{btn.disabled=false;spinner.classList.remove('active');}
}
</script>
</body>
</html>"""

UPGRADE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Upgrade — slacking.biz</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:#07080a;color:#eef2f6;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background-image:radial-gradient(rgba(255,255,255,0.04) 1px,transparent 1px);background-size:24px 24px;pointer-events:none;z-index:0}

.nav{position:sticky;top:0;z-index:100;padding:0 24px;display:flex;align-items:center;height:56px;justify-content:space-between;max-width:1120px;margin:0 auto;width:100%}
.nav-brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;color:#eef2f6;text-decoration:none;letter-spacing:-0.3px}
.nav-brand-logo{width:22px;height:22px;border-radius:5px;background:linear-gradient(135deg,#14b8a6,#0d9488);box-shadow:0 0 10px rgba(20,184,166,0.3)}
.nav-links{display:flex;gap:4px;align-items:center}
.nav-links a{color:#6b7280;text-decoration:none;font-size:13px;font-weight:500;padding:6px 12px;border-radius:5px;transition:all .2s}
.nav-links a:hover{color:#eef2f6;background:rgba(255,255,255,0.04)}
.nav-links .btn-nav{padding:6px 16px;background:#14b8a6;color:#07080a;border-radius:5px;font-weight:600;font-size:12.5px;line-height:1}
.nav-links .email{color:#4b5563;font-size:12.5px;padding:0 6px;font-weight:400}
.container{max-width:1000px;margin:0 auto;padding:32px 24px}
h1{font-size:26px;margin-bottom:8px;font-weight:600;letter-spacing:-0.03em}
.subtitle{color:#939bb3;font-size:14px;margin-bottom:32px}
.pricing-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.plan-card{background:#111317;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:28px 20px;text-align:center;position:relative;transition:all .3s}
.plan-card:hover{border-color:rgba(255,255,255,0.10)}
.plan-card.featured{border-color:rgba(20,184,166,0.3);background:linear-gradient(180deg,rgba(20,184,166,0.05),#111317)}
.plan-badge-tag{background:#14b8a6;color:#08090b;font-size:10px;font-weight:700;padding:3px 10px;border-radius:10px;position:absolute;top:-9px;left:50%;transform:translateX(-50%);white-space:nowrap;letter-spacing:0.3px}
.plan-name{font-size:15px;font-weight:600;margin-bottom:3px;letter-spacing:-0.02em}
.plan-price{font-size:32px;font-weight:700;color:#eef2f6;margin:10px 0;letter-spacing:-0.03em}
.plan-price span{font-size:13px;color:#5c6378;font-weight:400}
.plan-desc{color:#5c6378;font-size:13px;margin-bottom:16px;min-height:40px}
.plan-features{list-style:none;padding:0;text-align:left;margin-bottom:18px}
.plan-features li{padding:7px 0;font-size:13px;border-bottom:1px solid rgba(255,255,255,0.04);color:#eef2f6}
.plan-features li::before{content:'\u2713';color:#14b8a6;margin-right:8px;font-weight:700}
.plan-features li.missing{color:#5c6378}
.plan-features li.missing::before{content:'\u2717';color:#5c6378;opacity:.4}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:10px 28px;border-radius:6px;font-size:14px;font-weight:600;text-decoration:none;transition:all .25s;cursor:pointer;border:none;width:100%;gap:6px;font-family:'Inter',sans-serif}
.btn-primary{background:#14b8a6;color:#08090b}
.btn-primary:hover{background:#2dd4bf;box-shadow:0 0 20px rgba(20,184,166,0.3)}
.btn-outline{background:transparent;border:1px solid rgba(255,255,255,0.06);color:#eef2f6}
.btn-outline:hover{border-color:#14b8a6;color:#14b8a6}
.current-badge{font-size:12px;color:#5c6378;margin-top:8px}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-brand"><div class="nav-brand-logo"></div>slacking.biz</a>
<div class="nav-links">
<a href="/#features">Features</a>
<a href="/#pricing">Pricing</a>
<a href="/mcp">MCP Setup</a>
<a href="/status">Status</a>
$nav_right
</div>
</nav>
<div class="container">
<h1>Choose Your Plan</h1>
<p class="subtitle">Get access to SEC financial data with simple, transparent pricing.</p>
<div class="pricing-grid">
<div class="plan-card $free_featured">
$free_badge
<div class="plan-name">Free</div>
<div class="plan-price">$0<span>/mo</span></div>
<div class="plan-desc">For exploration and testing</div>
<ul class="plan-features">
<li>$free_reqs API requests/month</li>
<li>Health scores &amp; financials</li>
<li>Sentiment analysis</li>
<li>SEC filings feed</li>
<li class="missing">Company comparisons</li>
<li class="missing">Insider trading</li>
<li class="missing">Company profiles</li>
<li class="missing">Filing search</li>
<li class="missing">Industry comparison</li>
<li class="missing">Historical trends</li>
<li class="missing">Stock screener</li>
<li class="missing">Multi-ticker batch</li>
<li class="missing">Revenue segments</li>
<li class="missing">Filing types summary</li>
<li class="missing">Priority support</li>
</ul>
$free_button
$free_current
</div>
<div class="plan-card $starter_featured">
$starter_badge
<div class="plan-name">Starter</div>
<div class="plan-price">$29<span>/mo</span></div>
<div class="plan-desc">For individual developers</div>
<ul class="plan-features">
<li>500 API requests/month</li>
<li>Health scores &amp; financials</li>
<li>Sentiment analysis</li>
<li>SEC filings feed</li>
<li>Company comparisons</li>
<li>Insider trading</li>
<li>Company profiles</li>
<li>Filing search</li>
<li>Industry comparison</li>
<li class="missing">Historical trends</li>
<li class="missing">Stock screener</li>
<li class="missing">Multi-ticker batch</li>
<li class="missing">Revenue segments</li>
<li class="missing">Filing types summary</li>
<li class="missing">Priority support</li>
</ul>
$starter_button
$starter_current
</div>
<div class="plan-card $pro_featured">
$pro_badge
<div class="plan-name">Pro</div>
<div class="plan-price">$99<span>/mo</span></div>
<div class="plan-desc">For serious applications</div>
<ul class="plan-features">
<li>5,000 API requests/month</li>
<li>Health scores &amp; financials</li>
<li>Sentiment analysis</li>
<li>SEC filings feed</li>
<li>Company comparisons</li>
<li>Insider trading</li>
<li>Company profiles</li>
<li>Filing search</li>
<li>Industry comparison</li>
<li>Historical trends</li>
<li>Stock screener</li>
<li>Multi-ticker batch</li>
<li>Revenue segments</li>
<li>Filing types summary</li>
<li>Priority email support</li>
</ul>
$pro_button
$pro_current
</div>
<div class="plan-card $ent_featured">
$ent_badge
<div class="plan-name">Enterprise</div>
<div class="plan-price">$299<span>/mo</span></div>
<div class="plan-desc">For business-critical needs</div>
<ul class="plan-features">
<li>50,000 API requests/month</li>
<li>Health scores &amp; financials</li>
<li>Sentiment analysis</li>
<li>SEC filings feed</li>
<li>Company comparisons</li>
<li>Insider trading</li>
<li>Company profiles</li>
<li>Filing search</li>
<li>Industry comparison</li>
<li>Historical trends</li>
<li>Stock screener</li>
<li>Multi-ticker batch</li>
<li>Revenue segments</li>
<li>Filing types summary</li>
<li>24/7 phone &amp; email support</li>
</ul>
$ent_button
$ent_current
</div>
</div>
<div style="text-align:center;margin-top:32px;padding:24px;background:#161b22;border:1px solid #30363d;border-radius:8px">
<p style="color:#8b949e;font-size:14px">All plans include access to SEC EDGAR live data via our REST API. <br>Payment processing coming soon. For now, contact <a href="mailto:sales@slacking.biz" style="color:#58a6ff">sales@tsn.pw</a> to upgrade.</p>
</div>
</div>
</body>
</html>"""

STATUS_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Our Services — slacking.biz</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:#07080a;color:#eef2f6;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background-image:radial-gradient(rgba(255,255,255,0.04) 1px,transparent 1px);background-size:24px 24px;pointer-events:none;z-index:0}

.nav{position:sticky;top:0;z-index:100;padding:0 24px;display:flex;align-items:center;height:56px;justify-content:space-between;max-width:1120px;margin:0 auto;width:100%}
.nav-brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;color:#eef2f6;text-decoration:none;letter-spacing:-0.3px}
.nav-brand-logo{width:22px;height:22px;border-radius:5px;background:linear-gradient(135deg,#14b8a6,#0d9488);box-shadow:0 0 10px rgba(20,184,166,0.3)}
.nav-links{display:flex;gap:4px;align-items:center}
.nav-links a{color:#6b7280;text-decoration:none;font-size:13px;font-weight:500;padding:6px 12px;border-radius:5px;transition:all .2s}
.nav-links a:hover{color:#eef2f6;background:rgba(255,255,255,0.04)}
.nav-links .btn-nav{padding:6px 16px;background:#14b8a6;color:#07080a;border-radius:5px;font-weight:600;font-size:12.5px;line-height:1}
.nav-links .email{color:#4b5563;font-size:12.5px;padding:0 6px;font-weight:400}
.container{max-width:800px;margin:0 auto;padding:32px 24px}
h1{font-size:26px;margin-bottom:8px;font-weight:600;letter-spacing:-0.03em}
.subtitle{color:#939bb3;font-size:14px;margin-bottom:28px;line-height:1.6}
.status-grid{display:grid;gap:10px}
.service-card{background:#111317;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:20px;display:flex;align-items:center;gap:16px;transition:all .3s}
.service-card:hover{border-color:rgba(255,255,255,0.10);background:#191c22}
.service-icon{font-size:24px;width:40px;text-align:center}
.service-info{flex:1}
.service-name{font-size:15px;font-weight:600;margin-bottom:2px}
.service-desc{font-size:13px;color:#939bb3;line-height:1.5}
.service-status{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;white-space:nowrap}
.status-dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.status-dot.green{background:#22c55e;box-shadow:0 0 8px rgba(34,197,94,.4)}
.status-dot.red{background:#ef4444;box-shadow:0 0 8px rgba(239,68,68,.4)}
.status-label.green{color:#22c55e}
.status-label.red{color:#ef4444}
.feedback-section{background:#111317;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:24px;margin-top:24px}
.feedback-section h2{font-size:16px;margin-bottom:8px;font-weight:600;letter-spacing:-0.02em}
.feedback-section p{font-size:13px;color:#939bb3;margin-bottom:16px}
.form-group{margin-bottom:12px}
.form-group input,.form-group textarea{width:100%;padding:10px 14px;background:#08090b;border:1px solid rgba(255,255,255,0.06);border-radius:6px;color:#eef2f6;font-size:14px;outline:none;transition:border-color .2s;font-family:'Inter',sans-serif}
.form-group input:focus,.form-group textarea:focus{border-color:#14b8a6}
.form-group textarea{min-height:80px;resize:vertical}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:10px 24px;background:#14b8a6;color:#08090b;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;transition:all .25s;font-family:'Inter',sans-serif;gap:6px}
.btn:hover{background:#2dd4bf;box-shadow:0 0 20px rgba(20,184,166,0.3)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.fb-status{font-size:13px;margin-top:8px;display:none}
.login-prompt{text-align:center;padding:40px 20px;color:#5c6378;font-size:14px}
.login-prompt a{color:#14b8a6;text-decoration:none;font-weight:600;transition:opacity .2s}
.login-prompt a:hover{opacity:.7}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-brand"><div class="nav-brand-logo"></div>slacking.biz</a>
<div class="nav-links">
<a href="/#features">Features</a>
<a href="/#pricing">Pricing</a>
<a href="/mcp">MCP Setup</a>
<a href="/status">Status</a>
$nav_right
</div>
</nav>
<div class="container">
<h1>Our Services</h1>
<p class="subtitle">All slacking.biz services are live and ready to use</p>
<div class="status-grid">
<div class="service-card">
<div class="service-icon">📊</div>
<div class="service-info">
<div class="service-name">Company Financials</div>
<div class="service-desc">Income statements, balance sheets, cash flow data</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">🏥</div>
<div class="service-info">
<div class="service-name">Health Scores</div>
<div class="service-desc">Company financial health grading (A-F)</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">🔍</div>
<div class="service-info">
<div class="service-name">Sentiment Analysis</div>
<div class="service-desc">Loughran-McDonald filing sentiment analysis</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">📋</div>
<div class="service-info">
<div class="service-name">Insider Trading</div>
<div class="service-desc">Form 4 insider transaction data</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">🏢</div>
<div class="service-info">
<div class="service-name">Company Profiles</div>
<div class="service-desc">Company information, SIC codes, addresses</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">📄</div>
<div class="service-info">
<div class="service-name">SEC Filings Feed</div>
<div class="service-desc">Recent SEC filing listing and details</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">📐</div>
<div class="service-info">
<div class="service-name">Full Financial Data</div>
<div class="service-desc">500+ GAAP metrics per company</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">🔎</div>
<div class="service-info">
<div class="service-name">Filing Search</div>
<div class="service-desc">Filter SEC filings by form type</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">🏭</div>
<div class="service-info">
<div class="service-name">Industry Comparison</div>
<div class="service-desc">Compare against SIC industry peers</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">📈</div>
<div class="service-info">
<div class="service-name">Historical Trends</div>
<div class="service-desc">6+ quarters of financial trajectory</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">🔬</div>
<div class="service-info">
<div class="service-name">Stock Screener</div>
<div class="service-desc">Filter companies by financial criteria</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">📦</div>
<div class="service-info">
<div class="service-name">Multi-Ticker Batch</div>
<div class="service-desc">Up to 50 tickers in one API call</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">🧩</div>
<div class="service-info">
<div class="service-name">Revenue Segments</div>
<div class="service-desc">Product and geographic revenue breakdown</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">🗂️</div>
<div class="service-info">
<div class="service-name">Filing Types Summary</div>
<div class="service-desc">Categorized SEC filing profile</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
<div class="service-card">
<div class="service-icon">🤖</div>
<div class="service-info">
<div class="service-name">MCP Server</div>
<div class="service-desc">AI agent integration via Model Context Protocol</div>
</div>
<div class="service-status">
<span class="status-dot green"></span><span class="status-label green">Operational</span>
</div>
</div>
</div>
<div class="feedback-section">
<h2>Report an Issue</h2>
<p>Something not working? Let us know and we'll look into it.</p>
<div id="feedbackForm">
<div class="form-group">
<input type="text" id="fbEndpoint" placeholder="Endpoint (e.g., /v1/financial/AAPL)">
</div>
<div class="form-group">
<textarea id="fbMessage" placeholder="Describe the issue..."></textarea>
</div>
<button class="btn" onclick="submitFeedback()">Submit Report</button>
<div class="fb-status" id="fbStatus"></div>
</div>
</div>
</div>
<script>
async function submitFeedback(){
const btn=document.querySelector('.feedback-section .btn');
const endpoint=document.getElementById('fbEndpoint').value;
const message=document.getElementById('fbMessage').value;
const status=document.getElementById('fbStatus');
if(!endpoint||!message){status.textContent='Please fill in both fields.';status.style.display='block';return;}
btn.disabled=true;btn.textContent='Submitting...';
try{
const r=await fetch('/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint,message})
});
const d=await r.json();
status.textContent=r.ok?'Report submitted. Thank you! Status: '+d.status:'Error: '+(d.detail||'Failed');
status.style.display='block';status.style.color=r.ok?'#22c55e':'#ef4444';
}catch(e){status.textContent='Network error';status.style.display='block';status.style.color='#ef4444';}
finally{btn.disabled=false;btn.textContent='Submit Report';}
}
</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin — slacking.biz</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:#07080a;color:#eef2f6;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background-image:radial-gradient(rgba(255,255,255,0.04) 1px,transparent 1px);background-size:24px 24px;pointer-events:none;z-index:0}
.nav{position:sticky;top:0;z-index:100;padding:0 24px;display:flex;align-items:center;height:56px;justify-content:space-between;max-width:1120px;margin:0 auto;width:100%}
.nav-brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;color:#eef2f6;text-decoration:none;letter-spacing:-0.3px}
.nav-brand-logo{width:22px;height:22px;border-radius:5px;background:linear-gradient(135deg,#14b8a6,#0d9488);box-shadow:0 0 10px rgba(20,184,166,0.3)}
.nav-links{display:flex;gap:4px;align-items:center}
.nav-links a{color:#6b7280;text-decoration:none;font-size:13px;font-weight:500;padding:6px 12px;border-radius:5px;transition:all .2s}
.nav-links a:hover{color:#eef2f6;background:rgba(255,255,255,0.04)}
.nav-links .btn-nav{padding:6px 16px;background:#14b8a6;color:#07080a;border-radius:5px;font-weight:600;font-size:12.5px;line-height:1}
.nav-links .email{color:#4b5563;font-size:12.5px;padding:0 6px;font-weight:400}
.container{max-width:1100px;margin:0 auto;padding:32px 24px}
h1{font-size:26px;margin-bottom:8px;font-weight:600;letter-spacing:-0.03em}
.subtitle{color:#939bb3;font-size:14px;margin-bottom:24px}
table{width:100%;border-collapse:collapse;background:#111317;border-radius:10px;overflow:hidden;border:1px solid rgba(255,255,255,0.06)}
th{text-align:left;padding:12px 14px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:#5c6378;border-bottom:1px solid rgba(255,255,255,0.06);background:rgba(255,255,255,0.02)}
td{padding:10px 14px;font-size:13px;color:#eef2f6;border-bottom:1px solid rgba(255,255,255,0.04)}
tr:hover td{background:rgba(255,255,255,0.02)}
.plan-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;text-transform:capitalize}
.plan-free{background:rgba(255,255,255,0.06);color:#939bb3}
.plan-starter{background:rgba(245,158,11,0.12);color:#f59e0b}
.plan-pro{background:rgba(20,184,166,0.12);color:#14b8a6}
.plan-enterprise{background:rgba(34,197,94,0.12);color:#22c55e}
select{background:#08090b;color:#eef2f6;border:1px solid rgba(255,255,255,0.1);border-radius:4px;padding:4px 8px;font-size:12px;font-family:'Inter',sans-serif;cursor:pointer}
select:hover{border-color:rgba(255,255,255,0.2)}
.update-btn{padding:4px 12px;background:#14b8a6;color:#07080a;border:none;border-radius:4px;font-size:11px;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;margin-left:6px}
.update-btn:hover{background:#2dd4bf}
.msg{padding:10px 14px;border-radius:6px;font-size:13px;margin-bottom:16px;display:none}
.msg.success{display:block;background:rgba(34,197,94,0.1);color:#22c55e;border:1px solid rgba(34,197,94,0.15)}
.msg.error{display:block;background:rgba(239,68,68,0.1);color:#ef4444;border:1px solid rgba(239,68,68,0.15)}
.code{font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:11px;color:#6b7280}
.admin-badge{display:inline-block;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:600;background:rgba(20,184,166,0.15);color:#14b8a6;margin-left:4px}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-brand"><div class="nav-brand-logo"></div>slacking.biz</a>
<div class="nav-links">
<a href="/#features">Features</a>
<a href="/#pricing">Pricing</a>
<a href="/mcp">MCP Setup</a>
<a href="/status">Status</a>
<a href="/dashboard">Dashboard</a>
<a href="/admin" style="color:#14b8a6">Admin</a>
$nav_right
</div>
</nav><div class="container">
<h1>Admin Panel</h1>
<p class="subtitle">Manage users, plans, and API keys</p>
<div id="msg" class="msg"></div>
<table>
<thead>
<tr><th>ID</th><th>Email</th><th>Plan</th><th>API Key</th><th>Usage</th><th>Joined</th><th>Action</th></tr>
</thead>
<tbody>
$user_rows
</tbody>
</table>
</div>
</body>
</html>"""

MCP_SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MCP Setup — slacking.biz</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:#07080a;color:#eef2f6;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background-image:radial-gradient(rgba(255,255,255,0.04) 1px,transparent 1px);background-size:24px 24px;pointer-events:none;z-index:0}
.nav{position:sticky;top:0;z-index:100;padding:0 24px;display:flex;align-items:center;height:56px;justify-content:space-between;max-width:1120px;margin:0 auto;width:100%}
.nav-brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;color:#eef2f6;text-decoration:none;letter-spacing:-0.3px}
.nav-brand-logo{width:22px;height:22px;border-radius:5px;background:linear-gradient(135deg,#14b8a6,#0d9488);box-shadow:0 0 10px rgba(20,184,166,0.3)}
.nav-links{display:flex;gap:4px;align-items:center}
.nav-links a{color:#6b7280;text-decoration:none;font-size:13px;font-weight:500;padding:6px 12px;border-radius:5px;transition:all .2s}
.nav-links a:hover{color:#eef2f6;background:rgba(255,255,255,0.04)}
.nav-links .btn-nav{padding:6px 16px;background:#14b8a6;color:#07080a;border-radius:5px;font-weight:600;font-size:12.5px;line-height:1}
.nav-links .email{color:#4b5563;font-size:12.5px;padding:0 6px;font-weight:400}
.wrap{max-width:960px;margin:0 auto;padding:0 24px;position:relative;z-index:1}
h1{font-size:clamp(1.8rem,3.5vw,2.6rem);font-weight:700;letter-spacing:-0.04em;margin:0 0 6px;background:linear-gradient(135deg,#f0f2f5 40%,#14b8a6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.subtitle{color:#6b7280;font-size:15px;max-width:640px;margin:0 auto 48px;line-height:1.65;text-align:center}
.hero{text-align:center;padding:64px 0 32px}
.hero-badge{display:inline-flex;align-items:center;gap:6px;background:rgba(20,184,166,0.08);color:#14b8a6;border:1px solid rgba(20,184,166,0.15);border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;letter-spacing:0.5px;margin-bottom:20px;text-transform:uppercase}
.hero p{color:#6b7280;font-size:0.95rem;max-width:520px;margin:12px auto 0;line-height:1.65}
.section{padding:56px 0}
.section-tag{display:inline-flex;align-items:center;gap:6px;background:rgba(20,184,166,0.08);color:#14b8a6;border:1px solid rgba(20,184,166,0.15);border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;letter-spacing:0.5px;text-transform:uppercase;margin-bottom:12px}
.section h2{font-size:clamp(1.3rem,2.5vw,1.7rem);font-weight:700;margin:0 0 8px;letter-spacing:-0.03em}
.section .sub{color:#6b7280;font-size:14px;max-width:560px;margin:0 0 28px;line-height:1.6}
.card{background:rgba(12,13,16,0.6);border:1px solid rgba(255,255,255,0.05);border-radius:10px;padding:28px;backdrop-filter:blur(8px);margin-bottom:16px}
.card h3{font-size:1rem;font-weight:600;margin-bottom:8px;letter-spacing:-0.02em;display:flex;align-items:center;gap:8px}
.card p{color:#6b7280;font-size:13.5px;line-height:1.6;margin-bottom:8px}
.code-block{background:#0c0d10;border:1px solid rgba(255,255,255,0.06);border-radius:8px;overflow:hidden;margin:12px 0}
.code-head{display:flex;align-items:center;gap:8px;padding:8px 14px;background:rgba(255,255,255,0.02);border-bottom:1px solid rgba(255,255,255,0.04);font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:11px;color:#4b5563}
.code-body{padding:14px 16px;font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:12.5px;line-height:1.7;color:#6b7280;overflow-x:auto;white-space:pre-wrap;word-break:break-all}
.code-body .k{color:#60a5fa}
.code-body .s{color:#34d399}
.code-body .c{color:#4b5563;font-style:italic}
.platform-grid{display:grid;grid-template-columns:1fr;gap:14px}
.platform-card{background:rgba(12,13,16,0.6);border:1px solid rgba(255,255,255,0.05);border-radius:10px;padding:24px;backdrop-filter:blur(8px)}
.platform-card:hover{border-color:rgba(20,184,166,0.12)}
.platform-card h3{font-size:0.95rem;font-weight:600;margin-bottom:4px;letter-spacing:-0.02em;color:#eef2f6}
.platform-card .plat-desc{color:#6b7280;font-size:13px;margin-bottom:10px}
.tool-grid{display:grid;grid-template-columns:1fr;gap:6px}
.tool-row{display:flex;gap:12px;padding:10px 14px;background:rgba(12,13,16,0.4);border:1px solid rgba(255,255,255,0.04);border-radius:6px;align-items:flex-start;transition:all .2s}
.tool-row:hover{border-color:rgba(20,184,166,0.1);background:rgba(20,184,166,0.02)}
.tool-name{font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:12.5px;color:#34d399;white-space:nowrap;font-weight:500;min-width:200px;flex-shrink:0}
.tool-desc{color:#6b7280;font-size:13px;line-height:1.5}
.tool-cat{display:inline-block;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;padding:2px 8px;border-radius:4px;margin-right:6px}
.tool-cat.health{background:rgba(59,130,246,0.1);color:#60a5fa;border:1px solid rgba(59,130,246,0.15)}
.tool-cat.statement{background:rgba(139,92,246,0.1);color:#a78bfa;border:1px solid rgba(139,92,246,0.15)}
.tool-cat.reference{background:rgba(245,158,11,0.1);color:#f59e0b;border:1px solid rgba(245,158,11,0.15)}
.tool-cat.insider{background:rgba(239,68,68,0.1);color:#ef4444;border:1px solid rgba(239,68,68,0.15)}
.tool-cat.screening{background:rgba(20,184,166,0.1);color:#14b8a6;border:1px solid rgba(20,184,166,0.15)}
.why-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.why-card{background:rgba(12,13,16,0.6);border:1px solid rgba(255,255,255,0.05);border-radius:10px;padding:24px;text-align:center;backdrop-filter:blur(8px)}
.why-card .icon{font-size:1.8rem;margin-bottom:10px}
.why-card h3{font-size:0.95rem;font-weight:600;margin-bottom:4px;letter-spacing:-0.02em}
.why-card p{font-size:13px;color:#6b7280;line-height:1.55}
.cta-banner{margin-top:16px;text-align:center;padding:48px 40px;background:linear-gradient(135deg,rgba(12,13,16,0.8),rgba(20,184,166,0.04));border:1px solid rgba(255,255,255,0.05);border-radius:12px;position:relative;overflow:hidden}
.cta-banner::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:radial-gradient(ellipse at 30% 50%,rgba(20,184,166,0.15),transparent 60%);pointer-events:none}
.cta-banner>*{position:relative;z-index:1}
.cta-banner h2{font-size:1.35rem;font-weight:700;margin-bottom:8px;letter-spacing:-0.03em}
.cta-banner p{color:#6b7280;margin-bottom:20px;font-size:14px}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:10px 22px;border-radius:5px;font-size:13.5px;font-weight:600;text-decoration:none;border:none;cursor:pointer;transition:all .25s;gap:5px;font-family:'Inter',sans-serif}
.btn-primary{background:#14b8a6;color:#07080a}
.btn-primary:hover{background:#2dd4bf;box-shadow:0 0 24px rgba(20,184,166,0.3)}
.btn-secondary{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.06);color:#eef2f6}
.btn-secondary:hover{background:rgba(255,255,255,0.07);border-color:rgba(255,255,255,0.10)}
.cta-buttons{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
footer{margin-top:48px;padding:24px 0;border-top:1px solid rgba(255,255,255,0.04);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;color:#4b5563;font-size:13px}
footer a{color:#14b8a6;text-decoration:none;transition:opacity .2s}
footer a:hover{opacity:.7}
.section-header{text-align:center;margin-bottom:32px}
.section-header h2{margin-bottom:4px}
.section-header .sub{max-width:560px;margin:0 auto;color:#6b7280;font-size:14px;line-height:1.6}
@media (max-width:768px){.why-grid{grid-template-columns:1fr}.tool-row{flex-direction:column;gap:6px}.tool-name{min-width:auto}}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-brand"><div class="nav-brand-logo"></div>slacking.biz</a>
<div class="nav-links">
<a href="/#features">Features</a>
<a href="/#pricing">Pricing</a>
<a href="/mcp" style="color:#14b8a6">MCP Setup</a>
<a href="/status">Status</a>
$nav_links
</div>
</nav>
<div class="wrap">

<div class="hero">
<div class="hero-badge">🤖 Model Context Protocol</div>
<h1>slacking.biz MCP Server — AI-Native SEC Data</h1>
<p>Let any AI agent query SEC EDGAR financial data in real-time. Claude, Cursor, Cline, Continue.dev — if it speaks MCP, it speaks slacking.biz.</p>
</div>

<div class="section">
<div class="section-header">
<div class="section-tag">Why MCP?</div>
<h2>Why Connect AI Agents to SEC Data?</h2>
<p class="sub">The Model Context Protocol (MCP) lets AI agents call tools directly. Instead of pasting financial data into a chat, your AI agent can query live SEC data on demand — health scores, insider trades, financial statements, and more — using natural language.</p>
</div>
<div class="why-grid">
<div class="why-card">
<div class="icon">⚡</div>
<h3>Live Data, No Copy-Paste</h3>
<p>Your AI agent queries real SEC EDGAR data in real-time — no more pasting stale numbers into a chat window.</p>
</div>
<div class="why-card">
<div class="icon">🧠</div>
<h3>Natural Language Research</h3>
<p>"What's NVDA's profit margin trend?" — the agent calls the tools, reads the filings, and gives you the answer.</p>
</div>
<div class="why-card">
<div class="icon">🔧</div>
<h3>16 Specialized Tools</h3>
<p>Health scores, financial statements, insider trades, sentiment analysis, company screening, and more — all as agent-callable tools.</p>
</div>
</div>
</div>

<div class="section">
<div class="section-header">
<div class="section-tag">Platform Setup</div>
<h2>Connect Your AI Agent</h2>
<p class="sub">Pick your platform below. You'll need a slacking.biz API key — <a href="/signup" style="color:#14b8a6;text-decoration:none;font-weight:600">sign up free</a> to get one.</p>
</div>
<div class="platform-grid">

<div class="platform-card">
<h3>🟣 Claude Code</h3>
<p class="plat-desc">Run Claude Code with the MCP server attached. Replace <code style="color:#34d399;font-size:12px">/path/to/slacking_mcp_server.py</code> with the actual path and set your API key.</p>
<div class="code-block">
<div class="code-head"><span>bash</span></div>
<div class="code-body"><span class="c"># Replace with your actual path and key</span>
export SLACKING_API_KEY=<span class="s">your_api_key_here</span>
claude --mcp '<span class="s">{"slacking":{"command":"python3","args":["/path/to/slacking_mcp_server.py"]}}</span>'</div>
</div>
</div>

<div class="platform-card">
<h3>🟢 Cursor</h3>
<p class="plat-desc">Add this config to your project's <code style="color:#34d399;font-size:12px">.cursor/mcp.json</code> file to give Cursor access to SEC data.</p>
<div class="code-block">
<div class="code-head"><span>.cursor/mcp.json</span></div>
<div class="code-body">{
  <span class="k">"mcpServers"</span>: {
    <span class="k">"slacking-biz"</span>: {
      <span class="k">"command"</span>: <span class="s">"python3"</span>,
      <span class="k">"args"</span>: [<span class="s">"/path/to/slacking_mcp_server.py"</span>],
      <span class="k">"env"</span>: {
        <span class="k">"SLACKING_API_KEY"</span>: <span class="s">"your_api_key_here"</span>
      }
    }
  }
}</div>
</div>
</div>

<div class="platform-card">
<h3>🔵 Cline / Continue.dev</h3>
<p class="plat-desc">Add this config to <code style="color:#34d399;font-size:12px">~/.config/cline/mcp_config.json</code> (or your Continue.dev config) to enable SEC data tools in those IDEs.</p>
<div class="code-block">
<div class="code-head"><span>~/.config/cline/mcp_config.json</span></div>
<div class="code-body">{
  <span class="k">"mcpServers"</span>: {
    <span class="k">"slacking-biz"</span>: {
      <span class="k">"command"</span>: <span class="s">"python3"</span>,
      <span class="k">"args"</span>: [<span class="s">"/path/to/slacking_mcp_server.py"</span>],
      <span class="k">"env"</span>: {
        <span class="k">"SLACKING_API_KEY"</span>: <span class="s">"your_api_key_here"</span>
      }
    }
  }
}</div>
</div>
</div>

</div>
</div>

<div class="section">
<div class="section-header">
<div class="section-tag">Available Tools</div>
<h2>Every Tool Your AI Agent Can Call</h2>
<p class="sub">All 17 MCP tools organized by category. Every endpoint in the slacking.biz REST API is exposed as an agent-callable tool.</p>
</div>
<div class="tool-grid">
<div class="tool-row">
<span class="tool-name"><span class="tool-cat health">Health</span> list_capabilities</span>
<span class="tool-desc">Discover all available tools organized by category. The first tool your agent should call to learn what data is accessible.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat health">Health</span> get_company_health</span>
<span class="tool-desc">Financial health grade (A-F) with key ratios, profit margin, debt ratio, ROA, ROE, and key metrics from the latest 10-K filing.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat health">Health</span> get_trends</span>
<span class="tool-desc">6+ quarters of historical revenue, net income, EPS with computed trend direction and percentage change.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat health">Health</span> industry_comparison</span>
<span class="tool-desc">Compare a company's metrics against SIC industry peers with percentile rankings for profit margin, ROE, revenue growth, and more.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat health">Health</span> get_revenue_segments</span>
<span class="tool-desc">Revenue breakdown by product/service segments and geographic region from SEC filings, with YoY growth rates.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat statement">Statements</span> get_income_statement</span>
<span class="tool-desc">~17 line items from the income statement — revenue, COGS, R&D, SG&A, operating income, net income, EPS.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat statement">Statements</span> get_balance_sheet</span>
<span class="tool-desc">~20 line items from the balance sheet — cash, receivables, inventory, PPE, goodwill, debt, retained earnings.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat statement">Statements</span> get_cash_flow</span>
<span class="tool-desc">~12 line items from the cash flow statement — operating cash flow, CapEx, stock repurchases, dividends, debt activity.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat statement">Statements</span> get_all_financial_data</span>
<span class="tool-desc">500+ GAAP financial metrics from SEC XBRL data — the most comprehensive endpoint for deep-dive analysis.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat reference">Reference</span> get_company_profile</span>
<span class="tool-desc">Company metadata — ticker, CIK, SIC code, industry description, exchange listing, address, fiscal year end, former names.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat reference">Reference</span> get_filing_types</span>
<span class="tool-desc">Categorized summary of all SEC filing types a company has ever filed — annual, quarterly, current, insider, ownership, registration.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat reference">Reference</span> search_filings</span>
<span class="tool-desc">Search SEC filings by form type (10-K, 10-Q, 8-K, Form 4, 13F, etc.) with direct links to source documents.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat insider">Insider</span> get_insider_trades</span>
<span class="tool-desc">Recent insider trading transactions from SEC Form 4 filings — executive buy/sell activity with prices and values.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat insider">Insider</span> get_sentiment</span>
<span class="tool-desc">Loughran-McDonald financial sentiment analysis of 10-K and 10-Q filing text — detects management optimism and uncertainty.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat screening">Screening</span> compare_companies</span>
<span class="tool-desc">Side-by-side financial health comparison of up to 10 companies — health scores, grades, ratios, and key metrics.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat screening">Screening</span> screen_companies</span>
<span class="tool-desc">Screen/filter 120+ companies by min profit margin, revenue growth, max debt ratio, or minimum health grade.</span>
</div>
<div class="tool-row">
<span class="tool-name"><span class="tool-cat screening">Screening</span> batch_query</span>
<span class="tool-desc">Batch query multiple tickers in one request — returns health scores, profiles, or full financial data for up to 50 companies.</span>
</div>
</div>
</div>

<div class="cta-banner">
<h2>Start Building an AI That Understands Finance</h2>
<p>Sign up free — 20 requests/month, no credit card required. Get your API key and connect any MCP-compatible agent in minutes.</p>
<div class="cta-buttons">
<a href="/signup" class="btn btn-primary">Get Started Free →</a>
<a href="/" class="btn btn-secondary">Back to Home</a>
</div>
</div>

<footer>
<span>Powered by SlackNet &middot; slacking.biz</span>
<span><a href="/status">Status</a> &middot; <a href="/dashboard">Dashboard</a> &middot; <a href="/mcp">MCP Setup</a></span>
</footer>
</div>
</body>
</html>"""

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>slacking.biz — Financial Data API</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:#07080a;color:#eef2f6;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}

body::before{content:'';position:fixed;top:0;left:0;width:100%;height:100%;background-image:radial-gradient(rgba(255,255,255,0.04) 1px,transparent 1px);background-size:24px 24px;pointer-events:none;z-index:0}

.nav{position:sticky;top:0;z-index:100;padding:0 24px;display:flex;align-items:center;height:56px;justify-content:space-between;max-width:1120px;margin:0 auto;width:100%}
.nav-brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;color:#eef2f6;text-decoration:none;letter-spacing:-0.3px}
.nav-brand-logo{width:22px;height:22px;border-radius:5px;background:linear-gradient(135deg,#14b8a6,#0d9488);box-shadow:0 0 10px rgba(20,184,166,0.3)}
.nav-links{display:flex;gap:4px;align-items:center}
.nav-links a{color:#6b7280;text-decoration:none;font-size:13px;font-weight:500;padding:6px 12px;border-radius:5px;transition:all .2s}
.nav-links a:hover{color:#eef2f6;background:rgba(255,255,255,0.04)}
.nav-links .btn-nav{padding:6px 16px;background:#14b8a6;color:#07080a;border-radius:5px;font-weight:600;font-size:12.5px;line-height:1}
.nav-links .email{color:#4b5563;font-size:12.5px;padding:0 6px;font-weight:400}

.wrap{max-width:1120px;margin:0 auto;padding:0 24px;position:relative;z-index:1}

.hero{display:grid;grid-template-columns:1fr 1fr;gap:48px;padding:72px 0 0;align-items:center}
.hero-badge{display:inline-flex;align-items:center;gap:6px;background:rgba(20,184,166,0.08);color:#14b8a6;border:1px solid rgba(20,184,166,0.15);border-radius:20px;padding:4px 14px;font-size:12px;font-weight:600;letter-spacing:0.2px;margin-bottom:20px}
.hero h1{font-size:clamp(2rem,3.8vw,3.2rem);font-weight:700;line-height:1.08;letter-spacing:-0.04em;margin:0 0 14px;background:linear-gradient(135deg,#f0f2f5 40%,#14b8a6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero p{color:#6b7280;font-size:0.95rem;max-width:480px;margin:0 0 28px;line-height:1.65}
.hero-cta{display:flex;gap:10px}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:10px 22px;border-radius:5px;font-size:13.5px;font-weight:600;text-decoration:none;border:none;cursor:pointer;transition:all .25s;gap:5px;font-family:'Inter',sans-serif}
.btn-primary{background:#14b8a6;color:#07080a}
.btn-primary:hover{background:#2dd4bf;box-shadow:0 0 24px rgba(20,184,166,0.3)}
.btn-secondary{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.06);color:#eef2f6}
.btn-secondary:hover{background:rgba(255,255,255,0.07);border-color:rgba(255,255,255,0.10)}

.terminal{background:#0c0d10;border:1px solid rgba(255,255,255,0.06);border-radius:10px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.5),0 0 60px rgba(20,184,166,0.06)}
.term-head{display:flex;align-items:center;gap:8px;padding:10px 14px;background:rgba(255,255,255,0.02);border-bottom:1px solid rgba(255,255,255,0.04)}
.term-dot{width:8px;height:8px;border-radius:50%}
.term-dot:nth-child(1){background:#ef4444}
.term-dot:nth-child(2){background:#f59e0b}
.term-dot:nth-child(3){background:#22c55e}
.term-title{font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:11px;color:#4b5563;margin-left:6px;letter-spacing:-0.1px}
.term-body{padding:16px 18px;font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:12.5px;line-height:1.85;color:#6b7280;overflow-x:auto}
.term-body .k{color:#60a5fa}
.term-body .s{color:#34d399}
.term-body .n{color:#fbbf24}
.term-body .m{color:#f472b6}
.term-cursor{display:inline-block;width:7px;height:15px;background:#14b8a6;vertical-align:text-bottom;animation:blink .9s step-end infinite;border-radius:1px}
@keyframes blink{50%{opacity:0}}

.stats-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:rgba(255,255,255,0.04);border-radius:8px;overflow:hidden;margin-top:48px;border:1px solid rgba(255,255,255,0.04)}
.stat-box{padding:18px 20px;background:#07080a;text-align:center}
.stat-box .num{font-size:1.5rem;font-weight:700;color:#14b8a6;letter-spacing:-0.03em}
.stat-box .lbl{font-size:12px;color:#4b5563;margin-top:2px;font-weight:500;text-transform:uppercase;letter-spacing:0.5px}

.data-section{padding:80px 0 0}
.data-section .tag{display:inline-flex;align-items:center;gap:6px;background:rgba(20,184,166,0.08);color:#14b8a6;border:1px solid rgba(20,184,166,0.15);border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;letter-spacing:0.5px;text-transform:uppercase;margin-bottom:12px}
.data-section h2{font-size:clamp(1.5rem,3vw,2rem);font-weight:700;margin:0 0 8px;letter-spacing:-0.03em}
.data-section .sub{color:#6b7280;font-size:14px;max-width:580px;margin:0 0 36px;line-height:1.6}
.data-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.data-card{background:rgba(12,13,16,0.6);border:1px solid rgba(255,255,255,0.05);border-radius:10px;padding:24px;backdrop-filter:blur(8px)}
.data-card .label{font-size:11px;color:#4b5563;text-transform:uppercase;letter-spacing:0.8px;font-weight:600;margin-bottom:12px}
.data-card .big{font-size:2.8rem;font-weight:700;line-height:1;letter-spacing:-0.04em}
.data-card .big.grade{color:#14b8a6}
.data-row{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.03);font-size:14px}
.data-row:last-child{border-bottom:none}
.data-row .rk{color:#6b7280}
.data-row .rv{color:#eef2f6;font-weight:500;font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:13px}

.section{padding:80px 0 0}
.section-tag{display:inline-flex;align-items:center;gap:6px;background:rgba(20,184,166,0.08);color:#14b8a6;border:1px solid rgba(20,184,166,0.15);border-radius:20px;padding:4px 14px;font-size:11.5px;font-weight:600;letter-spacing:0.5px;text-transform:uppercase;margin-bottom:12px}
.section h2{font-size:clamp(1.5rem,3vw,2rem);font-weight:700;text-align:center;margin:0 0 8px;letter-spacing:-0.03em}
.section .subtitle{color:#6b7280;font-size:14px;text-align:center;max-width:580px;margin:0 auto 36px;line-height:1.6}
.features-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.feature-card{background:rgba(12,13,16,0.6);border:1px solid rgba(255,255,255,0.05);border-radius:10px;padding:24px;transition:all .3s;position:relative;backdrop-filter:blur(8px)}
.feature-card:hover{border-color:rgba(20,184,166,0.15);background:rgba(20,184,166,0.03)}
.feature-icon{font-size:1.2rem;margin-bottom:12px}
.feature-card h3{font-size:0.95rem;font-weight:600;margin-bottom:6px;letter-spacing:-0.02em}
.feature-card p{font-size:13px;color:#6b7280;line-height:1.6}
.feature-card .code-snip{display:block;background:#07080a;border:1px solid rgba(255,255,255,0.04);border-radius:5px;padding:8px 10px;margin-top:10px;font-family:'JetBrains Mono','SF Mono',ui-monospace,monospace;font-size:11px;color:#34d399;overflow-x:auto;line-height:1.5}

.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.step-card{background:rgba(12,13,16,0.6);border:1px solid rgba(255,255,255,0.05);border-radius:10px;padding:24px;text-align:center;backdrop-filter:blur(8px)}
.step-num{width:34px;height:34px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;background:rgba(20,184,166,0.1);border:1px solid rgba(20,184,166,0.2);color:#14b8a6;font-weight:700;font-size:13px;margin-bottom:12px}
.step-card h3{font-size:0.95rem;font-weight:600;margin-bottom:5px;letter-spacing:-0.02em}
.step-card p{font-size:13px;color:#6b7280;line-height:1.55}

.pricing-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.plan-card{background:rgba(12,13,16,0.6);border:1px solid rgba(255,255,255,0.05);border-radius:10px;padding:24px 18px;text-align:center;position:relative;transition:all .3s;backdrop-filter:blur(8px)}
.plan-card:hover{border-color:rgba(255,255,255,0.08);background:rgba(20,184,166,0.02)}
.plan-card.featured{border-color:rgba(20,184,166,0.3);background:linear-gradient(180deg,rgba(20,184,166,0.06),rgba(12,13,16,0.8))}
.plan-badge-tag{background:#14b8a6;color:#07080a;font-size:9.5px;font-weight:700;padding:2px 10px;border-radius:10px;position:absolute;top:-8px;left:50%;transform:translateX(-50%);white-space:nowrap;letter-spacing:0.3px}
.plan-name{font-size:14px;font-weight:600;margin-bottom:2px;letter-spacing:-0.02em}
.plan-price{font-size:30px;font-weight:700;color:#eef2f6;margin:8px 0;letter-spacing:-0.03em}
.plan-price span{font-size:12px;color:#4b5563;font-weight:400}
.plan-desc{color:#4b5563;font-size:12px;margin-bottom:14px;min-height:32px}
.plan-features{list-style:none;padding:0;text-align:left;margin-bottom:16px}
.plan-features li{padding:6px 0;font-size:12.5px;border-bottom:1px solid rgba(255,255,255,0.03);color:#d1d5db}
.plan-features li::before{content:'\u2713';color:#14b8a6;margin-right:7px;font-weight:700;font-size:11px}
.plan-features li.missing{color:#4b5563}
.plan-features li.missing::before{content:'\u2717';color:#4b5563;opacity:.4}
.plan-card .btn{width:100%;font-size:13px;padding:9px 16px}
.pricing-foot{margin-top:16px;text-align:center;padding:16px;background:rgba(12,13,16,0.4);border:1px solid rgba(255,255,255,0.04);border-radius:8px}
.pricing-foot p{color:#4b5563;font-size:13px}
.pricing-foot a{color:#14b8a6;text-decoration:none}
.pricing-foot a:hover{opacity:.7}

.cta-banner{margin-top:80px;text-align:center;padding:48px 40px;background:linear-gradient(135deg,rgba(12,13,16,0.8),rgba(20,184,166,0.04));border:1px solid rgba(255,255,255,0.05);border-radius:12px;position:relative;overflow:hidden}
.cta-banner::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:radial-gradient(ellipse at 30% 50%,rgba(20,184,166,0.15),transparent 60%);pointer-events:none}
.cta-banner>*{position:relative;z-index:1}
.cta-banner h2{font-size:1.35rem;font-weight:700;margin-bottom:8px;letter-spacing:-0.03em}
.cta-banner p{color:#6b7280;margin-bottom:20px;font-size:14px}

footer{margin-top:60px;padding:24px 0;border-top:1px solid rgba(255,255,255,0.04);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;color:#4b5563;font-size:13px}
footer a{color:#14b8a6;text-decoration:none;transition:opacity .2s}
footer a:hover{opacity:.7}

@media (max-width:768px){.hero{grid-template-columns:1fr;gap:32px;padding:48px 0 0}.features-grid{grid-template-columns:1fr}.steps{grid-template-columns:1fr}.pricing-grid{grid-template-columns:1fr 1fr}.stats-strip{grid-template-columns:1fr 1fr}.data-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<nav class="nav">
<a href="/" class="nav-brand"><div class="nav-brand-logo"></div>slacking.biz</a>
<div class="nav-links">
<a href="#features">Features</a>
<a href="#pricing">Pricing</a>
<a href="/mcp">MCP Setup</a>
<a href="/status">Status</a>
$nav_links
</div>
</nav>
<div class="wrap">

<div class="hero">
<div class="hero-left">
<div class="hero-badge">⚡ Live — First AI-Native SEC Data Platform</div>
<h1>Financial data from the same source Bloomberg charges $20K/yr for</h1>
<p>SEC EDGAR financials, health scores, sentiment analysis, and company comparisons — delivered via REST API. No data licensing fees, no 87-symbol locks, no enterprise nonsense. The natural IEX Cloud replacement.</p>
<div class="hero-cta">
$hero_cta
<a href="#features" class="btn btn-secondary">See Features</a>
</div>
</div>
<div class="terminal">
<div class="term-head">
<span class="term-dot"></span><span class="term-dot"></span><span class="term-dot"></span>
<span class="term-title">GET /v1/financial/NVDA</span>
</div>
<div class="term-body">
{<span class="k">"ticker"</span>: <span class="s">"NVDA"</span>,<br>
<span class="k">"company"</span>: <span class="s">"NVIDIA Corporation"</span>,<br>
<span class="k">"health_score"</span>: <span class="n">67.3</span>,<br>
<span class="k">"grade"</span>: <span class="s">"B"</span>,<br>
<span class="k">"revenue_growth"</span>: <span class="s">"+126%"</span>,<br>
<span class="k">"profit_margin"</span>: <span class="s">"55.2%"</span>,<br>
<span class="k">"debt_equity"</span>: <span class="s">"0.21"</span>,<br>
<span class="k">"cash_position"</span>: <span class="s">"25.9B"</span>,<br>
<span class="k">"r_and_d_spend"</span>: <span class="s">"8.7B"</span><br>
}<span class="term-cursor"></span>
</div>
</div>
</div>

<div class="stats-strip">
<div class="stat-box"><div class="num">10K+</div><div class="lbl">Companies</div></div>
<div class="stat-box"><div class="num">20</div><div class="lbl">Free Requests/mo</div></div>
<div class="stat-box"><div class="num">15</div><div class="lbl">Endpoints</div></div>
<div class="stat-box"><div class="num">⚡</div><div class="lbl">Live from SEC</div></div>
</div>

<div class="data-section">
<div class="tag">See the Data</div>
<h2>What you actually get</h2>
<p class="sub">Real output from the health endpoint. Every response includes grades, ratios, and growth metrics computed from the latest SEC filings.</p>
<div class="data-grid">
<div class="data-card">
<div class="label">Health Score</div>
<div class="big grade">67.3 / 100</div>
<div style="margin-top:12px;display:flex;gap:16px;flex-wrap:wrap">
<div><div style="font-size:11px;color:#4b5563;text-transform:uppercase;letter-spacing:0.5px">Grade</div><div style="font-size:24px;font-weight:700;color:#22c55e;margin-top:2px">B</div></div>
<div><div style="font-size:11px;color:#4b5563;text-transform:uppercase;letter-spacing:0.5px">Interpretation</div><div style="font-size:14px;color:#eef2f6;margin-top:4px">Above Average — Healthy financials, moderate risk</div></div>
</div>
</div>
<div class="data-card">
<div class="label">Key Metrics</div>
<div class="data-row"><span class="rk">Revenue Growth</span><span class="rv">+126%</span></div>
<div class="data-row"><span class="rk">Profit Margin</span><span class="rv">55.2%</span></div>
<div class="data-row"><span class="rk">Debt-to-Equity</span><span class="rv">0.21</span></div>
<div class="data-row"><span class="rk">Cash Position</span><span class="rv">$25.9B</span></div>
<div class="data-row"><span class="rk">R&amp;D Spend</span><span class="rv">$8.7B</span></div>
<div class="data-row"><span class="rk">Employees</span><span class="rv">32,000</span></div>
</div>
</div>
</div>

<div class="section" id="features">
<div class="section-tag">API Endpoints</div>
<h2>15 Endpoints. Every Metric.</h2>
<p class="subtitle">All endpoints included in every plan. Health scores, sentiment, comparisons, insider trades, SEC filings, company profiles, financial statements, filing search, industry comparison, historical trends, stock screener, batch, revenue segments, filing types, and 500+ GAAP metrics — all from SEC EDGAR. Plus an AI-native MCP server for agentic access.</p>
<div class="features-grid">
<div class="feature-card">
<div class="feature-icon">📊</div>
<h3>Company Health</h3>
<p>Instant financial health grade (A–F) for any public company. Revenue, profit margins, debt ratios, cash position, operating efficiency — all computed from the latest 10-K filings.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/NVDA</code>
</div>
<div class="feature-card">
<div class="feature-icon">📜</div>
<h3>Sentiment Analysis</h3>
<p>Loughran-McDonald dictionary analysis of 10-K and 10-Q filings. See whether management's own language signals optimism or concern, tracked year over year.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/AAPL/sentiment</code>
</div>
<div class="feature-card">
<div class="feature-icon">⚖️</div>
<h3>Company Comparison</h3>
<p>Side-by-side financial comparison of up to 10 companies. Profitability, leverage, growth rates, and health scores on one page. Perfect for competitive intelligence.</p>
<code class="code-snip">curl -X POST -H "X-API-Key: your-key" -d '{"tickers":["AAPL","MSFT","NVDA"]}' slacking.biz/v1/financial/compare</code>
</div>
<div class="feature-card">
<div class="feature-icon">🔍</div>
<h3>Insider Trading</h3>
<p>See when executives and directors buy or sell their own stock. Real insider transactions from SEC Form 4 filings — including purchase/sale amounts, prices, and holdings after trade.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/NVDA/insider</code>
</div>
<div class="feature-card">
<div class="feature-icon">📋</div>
<h3>SEC Filings Feed</h3>
<p>Full list of recent SEC filings for any company — 10-K annual reports, 10-Q quarterly updates, 8-K material events, and more. Direct links to the source documents.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/NVDA/filings</code>
</div>
<div class="feature-card">
<div class="feature-icon">🏢</div>
<h3>Company Profile</h3>
<p>Everything about a company — SIC code, industry description, exchange listing, phone, address, fiscal year end, EIN, former names, and SEC filing status.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/NVDA/profile</code>
</div>
<div class="feature-card">
<div class="feature-icon">📑</div>
<h3>Financial Statements</h3>
<p>Structured income statement, balance sheet, and cash flow statement from XBRL data. Revenue, EPS, assets, debt, operating cash flow — all the fundamentals.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/AAPL/income-statement</code>
</div>
<div class="feature-card">
<div class="feature-icon">📐</div>
<h3>All Financial Data</h3>
<p>Every single GAAP metric reported to the SEC — over 500 data points per company. Revenue breakdowns, segment data, tax details, lease obligations, and much more.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/AAPL/full</code>
</div>
<div class="feature-card">
<div class="feature-icon">🔎</div>
<h3>Filing Search</h3>
<p>Search SEC filings by form type — filter by 10-K, 10-Q, 8-K, and more. Get only the documents you need with precise form-type filtering and direct links to source filings.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" 'slacking.biz/v1/financial/NVDA/filings/search?form_types=10-K,10-Q'</code>
</div>
<div class="feature-card">
<div class="feature-icon">🏭</div>
<h3>Industry Comparison</h3>
<p>Compare a company's financial metrics against its SIC industry peers. See percentile rankings for profit margin, ROE, revenue growth, and more — know where your stock stands.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/NVDA/vs-industry</code>
</div>
<div class="feature-card">
<div class="feature-icon">📈</div>
<h3>Historical Trends</h3>
<p>See 6+ quarters of revenue, net income, operating income, gross profit, and EPS — all in one endpoint. Trend direction and percentage change computed automatically.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/NVDA/trends</code>
</div>
<div class="feature-card">
<div class="feature-icon">🔬</div>
<h3>Stock Screener</h3>
<p>Screen thousands of companies by financial criteria — min revenue growth, max debt ratio, minimum grade, and more. Find undervalued gems or overvalued duds instantly.</p>
<code class="code-snip">curl -X POST -H "X-API-Key: your-key" -H "Content-Type: application/json" -d '{"min_revenue_growth":20,"max_debt_ratio":0.5}' slacking.biz/v1/financial/screener</code>
</div>
<div class="feature-card">
<div class="feature-icon">📦</div>
<h3>Multi-Ticker Batch</h3>
<p>Fetch health, profile, or full financial data for up to 50 tickers in a single API call. Dramatically reduce latency when tracking a portfolio or research universe.</p>
<code class="code-snip">curl -X POST -H "X-API-Key: your-key" -d '{"tickers":["AAPL","MSFT","NVDA"],"endpoints":["health","profile"]}' slacking.biz/v1/financial/batch</code>
</div>
<div class="feature-card">
<div class="feature-icon">🧩</div>
<h3>Revenue Segments</h3>
<p>Get revenue breakdowns by product line, business segment, and geographic region from SEC disclosures. See exactly which parts of the business are driving growth.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/AAPL/segments</code>
</div>
<div class="feature-card">
<div class="feature-icon">🗂️</div>
<h3>Filing Types Summary</h3>
<p>Categorized summary of all SEC filing types for a company — annual, quarterly, current, insider, ownership, registration, and more. Instantly see the filing profile.</p>
<code class="code-snip">curl -H "X-API-Key: your-key" slacking.biz/v1/financial/NVDA/filing-types</code>
</div>
</div>
</div>

<div class="section">
<div class="section-tag">🤖 AI-Native Platform</div>
<h2>First AI-Native SEC Data Platform</h2>
<p class="subtitle">slacking.biz is the <strong>first SEC fundamentals platform built for AI agents</strong>. Our built-in Model Context Protocol (MCP) server lets Claude, Codex, Cursor, Cline, and any MCP-compatible agent query live SEC data in natural language — no API wrappers, no SDKs, no glue code.</p>
<div class="features-grid">
<div class="feature-card" style="grid-column:1/-1;text-align:center;padding:32px">
<div class="feature-icon" style="font-size:2rem">🤖</div>
<h3>AI Agents Can Now Read SEC Filings</h3>
<p style="max-width:600px;margin:6px auto 16px">Give your AI agent the MCP server file and it instantly gains 17 tools: financial health scores, insider trades, SEC filing search, sentiment analysis, balance sheets, income statements, cash flow, multi-company comparison, and more. The agent calls the API — you ask in English.</p>
<code class="code-snip" style="font-size:11.5px;text-align:left;line-height:1.6"># One config line for Claude/Cursor/Continue:<br>{<br>  "mcpServers": {<br>    "slacking-biz": {<br>      "command": "python3",<br>      "args": ["/path/to/slacking_mcp_server.py"],<br>      "env": {"SLACKING_API_KEY": "your-key"}<br>    }<br>  }<br>}</code>
<div style="margin-top:16px;display:flex;gap:10px;justify-content:center;flex-wrap:wrap">
<a href="/mcp" class="btn btn-primary">Full Setup Guide →</a>
<a href="/signup" class="btn btn-secondary">Get API Key</a>
</div>
</div>
</div>
</div>

<div class="section">
<div class="section-tag">How It Works</div>
<h2>From Signup to Data in 30 Seconds</h2>
<p class="subtitle">No sales call, no credit card required to start.</p>
<div class="steps">
<div class="step-card">
<div class="step-num">1</div>
<h3>Create an Account</h3>
<p>Sign up with your email and password. You get an API key instantly — no approval needed, no credit card.</p>
</div>
<div class="step-card">
<div class="step-num">2</div>
<h3>Make Requests</h3>
<p>Use the playground to test endpoints from your browser, or copy your API key and hit the API from your own tools.</p>
</div>
<div class="step-card">
<div class="step-num">3</div>
<h3>Get Your Data</h3>
<p>JSON responses with health scores, sentiment, trends, and comparisons. Fresh data from SEC EDGAR, cached for performance.</p>
</div>
</div>
</div>

<div class="section" id="pricing">
<div class="section-tag">Simple Pricing</div>
<h2>Pay for Volume, Not Features</h2>
<p class="subtitle">Every plan gets every endpoint. The only difference is how many requests you get.</p>
<div class="pricing-grid">
<div class="plan-card $free_featured">
$free_badge
<div class="plan-name">Free</div>
<div class="plan-price">$0<span>/mo</span></div>
<div class="plan-desc">For exploration and testing</div>
<ul class="plan-features">
<li>$free_reqs API requests/month</li>
<li>Health scores &amp; metrics</li>
<li>Sentiment analysis</li>
<li>SEC filings feed</li>
<li class="missing">Company comparisons</li>
<li class="missing">Insider trading</li>
<li class="missing">Company profiles</li>
<li class="missing">Filing search</li>
<li class="missing">Industry comparison</li>
<li class="missing">Historical trends</li>
<li class="missing">Stock screener</li>
<li class="missing">Multi-ticker batch</li>
<li class="missing">Revenue segments</li>
<li class="missing">Filing types summary</li>
<li class="missing">Priority support</li>
</ul>
$free_button
</div>
<div class="plan-card $starter_featured">
$starter_badge
<div class="plan-name">Starter</div>
<div class="plan-price">$29<span>/mo</span></div>
<div class="plan-desc">For individual developers</div>
<ul class="plan-features">
<li>500 API requests/month</li>
<li>Health scores &amp; metrics</li>
<li>Sentiment analysis</li>
<li>SEC filings feed</li>
<li>Company comparisons</li>
<li>Insider trading</li>
<li>Company profiles</li>
<li>Filing search</li>
<li>Industry comparison</li>
<li class="missing">Historical trends</li>
<li class="missing">Stock screener</li>
<li class="missing">Multi-ticker batch</li>
<li class="missing">Revenue segments</li>
<li class="missing">Filing types summary</li>
<li class="missing">Priority support</li>
</ul>
$starter_button
</div>
<div class="plan-card $pro_featured">
$pro_badge
<div class="plan-name">Pro</div>
<div class="plan-price">$99<span>/mo</span></div>
<div class="plan-desc">For serious applications</div>
<ul class="plan-features">
<li>5,000 API requests/month</li>
<li>Health scores &amp; metrics</li>
<li>Sentiment analysis</li>
<li>SEC filings feed</li>
<li>Company comparisons</li>
<li>Insider trading</li>
<li>Company profiles</li>
<li>Filing search</li>
<li>Industry comparison</li>
<li>Historical trends</li>
<li>Stock screener</li>
<li>Multi-ticker batch</li>
<li>Revenue segments</li>
<li>Filing types summary</li>
<li>Priority email support</li>
</ul>
$pro_button
</div>
<div class="plan-card $ent_featured">
$ent_badge
<div class="plan-name">Enterprise</div>
<div class="plan-price">$299<span>/mo</span></div>
<div class="plan-desc">For business-critical needs</div>
<ul class="plan-features">
<li>50,000 API requests/month</li>
<li>Health scores &amp; metrics</li>
<li>Sentiment analysis</li>
<li>SEC filings feed</li>
<li>Company comparisons</li>
<li>Insider trading</li>
<li>Company profiles</li>
<li>Filing search</li>
<li>Industry comparison</li>
<li>Historical trends</li>
<li>Stock screener</li>
<li>Multi-ticker batch</li>
<li>Revenue segments</li>
<li>Filing types summary</li>
<li>24/7 phone &amp; email support</li>
</ul>
$ent_button
</div>
</div>
<div class="pricing-foot">
<p>All plans include live SEC EDGAR data via REST API.  <a href="/status">Endpoint status &amp; reporting</a></p>
</div>
</div>

<div class="cta-banner">
<h2>Start getting the same data institutions pay $20K/yr for</h2>
<p>Sign up free — 20 requests/month, no credit card required. Upgrade when you need more.</p>
$cta_button
</div>

<footer>
<span>Powered by SlackNet &middot; slacking.biz</span>
<span><a href="/status">Status</a> &middot; <a href="/dashboard">Dashboard</a></span>
</footer>
</div>
</body>
</html>"""

# ══════════════════════════════════════════════════
#  WEB ROUTES
# ══════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Landing page."""
    user = get_session_user(request)
    if user:
        admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ''
        nav_links = f'{admin_link}<a href="/dashboard">Dashboard</a><a href="/upgrade">Upgrade</a><span class="email">{user["email"]}</span><a href="/logout" class="btn-nav">Logout</a>'
        hero_cta = '<a href="/dashboard" class="btn btn-primary">Dashboard →</a>'
        cta_button = '<a href="/dashboard" class="btn btn-primary">Go to Dashboard →</a>'
    else:
        nav_links = '<a href="/login" class="btn-nav">Sign In</a>'
        hero_cta = '<a href="/signup" class="btn btn-primary">Get Started Free →</a>'
        cta_button = '<a href="/signup" class="btn btn-primary">Get Started Free →</a>'

    html = Template(LANDING_HTML).safe_substitute(
        nav_links=nav_links,
        hero_cta=hero_cta,
        cta_button=cta_button,
        free_featured="",
        free_badge="",
        free_reqs="20",
        free_button='<a href="/signup" class="btn btn-primary">Get Started</a>',
        starter_featured="featured",
        starter_badge='<div class="plan-badge-tag">Popular</div>',
        starter_button='<a href="/signup" class="btn btn-primary">Start Free</a>',
        pro_featured="",
        pro_badge="",
        pro_button='<a href="/signup" class="btn btn-primary">Start Free</a>',
        ent_featured="",
        ent_badge="",
        ent_button='<a href="/contact" class="btn btn-outline">Contact Sales</a>',
    )
    return HTMLResponse(content=html)


@app.get("/.well-known/mcp.json")
async def mcp_discovery():
    """MCP discovery manifest — makes slacking.biz findable by AI agents."""
    return JSONResponse(content={
        "schema_version": "1.0",
        "name": "slacking.biz — SEC Financial Data + US Economics + Demographics",
        "description": "28 MCP tools across 3 categories: financial (SEC EDGAR: health scores, financial statements, insider trades, sentiment, screening), economic (FRED: GDP, CPI, rates, employment, housing), demographics (Census: ZIP/county data). No API key needed for basic access.",
        "base_url": "https://slacking.biz",
        "mcp_command": "python3",
        "mcp_script": "https://slacking.biz/static/slacking_mcp_server.py",
        "mcp_tools": 28,
        "auth": "X-API-Key header (optional for light use)",
        "pricing_url": "https://slacking.biz/upgrade",
        "signup_url": "https://slacking.biz/signup",
        "docs_url": "https://slacking.biz/mcp",
        "playground_url": "https://slacking.biz/playground",
        "tags": ["financial-data", "sec", "stock-market", "investing", "stock-screener", "fundamentals", "economic-data", "demographics", "fred", "census", "mcp-server"],
        "endpoints": {
            "health": "/v1/financial/{ticker}",
            "income_statement": "/v1/financial/{ticker}/income-statement",
            "balance_sheet": "/v1/financial/{ticker}/balance-sheet",
            "cash_flow": "/v1/financial/{ticker}/cash-flow",
            "full_data": "/v1/financial/{ticker}/full",
            "profile": "/v1/financial/{ticker}/profile",
            "insider_trades": "/v1/financial/{ticker}/insider",
            "filings": "/v1/financial/{ticker}/filings",
            "filing_search": "/v1/financial/{ticker}/filings/search",
            "sentiment": "/v1/financial/{ticker}/sentiment",
            "trends": "/v1/financial/{ticker}/trends",
            "industry_comparison": "/v1/financial/{ticker}/vs-industry",
            "segments": "/v1/financial/{ticker}/segments",
            "filing_types": "/v1/financial/{ticker}/filing-types",
            "compare": "POST /v1/financial/compare",
            "screener": "POST /v1/financial/screener",
            "batch": "POST /v1/financial/batch",
            "gdp": "/v1/econ/gdp",
            "inflation": "/v1/econ/inflation",
            "rates": "/v1/econ/rates",
            "employment": "/v1/econ/employment",
            "housing": "/v1/econ/housing",
            "econ_summary": "/v1/econ/summary",
            "zip_demographics": "/v1/demo/zip/{zip_code}",
            "county_demographics": "/v1/demo/county/{fips}"
        }
    })


@app.get("/mcp", response_class=HTMLResponse)
async def mcp_setup(request: Request):
    """MCP setup guide page — no auth required."""
    user = get_session_user(request)
    if user:
        admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ''
        nav_links = f'{admin_link}<a href="/dashboard">Dashboard</a><a href="/upgrade">Upgrade</a><span class="email">{user["email"]}</span><a href="/logout" class="btn-nav">Logout</a>'
    else:
        nav_links = '<a href="/login" class="btn-nav">Sign In</a>'
    html = Template(MCP_SETUP_HTML).safe_substitute(nav_links=nav_links)
    return HTMLResponse(content=html)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Login page."""
    return HTMLResponse(content=SIGNIN_HTML)


@app.post("/login")
async def login(request: Request):
    """Login endpoint."""
    client_ip = request.client.host if request.client else "unknown"
    _check_auth_rate_limit(client_ip)

    data = await _parse_body(request)
    email = data.get("email", "").lower().strip()
    password = data.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, password_hash, plan, is_admin FROM users WHERE email = ?", (email,))
    user = cursor.fetchone()

    if not user or not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8")):
        conn.close()
        logger.warning(f"Failed login attempt for {email} from {client_ip}")
        raise HTTPException(status_code=401, detail="Invalid email or password")

    logger.info(f"Successful login: {email} from {client_ip}")

    # Create session
    token = str(uuid.uuid4())
    cursor.execute("INSERT INTO sessions (user_id, token) VALUES (?, ?)", (user["id"], token))
    conn.commit()
    conn.close()

    response = JSONResponse(content={"ok": True, "email": user["email"]})
    response.set_cookie(key="session", value=token, httponly=True, secure=True, max_age=86400 * 30, samesite="lax")
    return response


@app.get("/signup", response_class=HTMLResponse)
async def signup_page():
    """Signup page."""
    return HTMLResponse(content=SIGNUP_HTML)


@app.post("/signup")
async def signup(request: Request):
    """Signup endpoint."""
    client_ip = request.client.host if request.client else "unknown"
    _check_auth_rate_limit(client_ip)

    data = await _parse_body(request)
    email = data.get("email", "").lower().strip()
    password = data.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")
    _validate_password(password)
    if "@" not in email or "." not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    conn = get_db()
    cursor = conn.cursor()

    # Check existing
    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Email already registered")

    logger.info(f"New user signing up: {email} from {client_ip}")

    # Create user
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    cursor.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, pw_hash))
    user_id = cursor.lastrowid

    # Create API key
    api_key = str(uuid.uuid4())
    cursor.execute("INSERT INTO api_keys (user_id, key) VALUES (?, ?)", (user_id, api_key))

    # Create session
    token = str(uuid.uuid4())
    cursor.execute("INSERT INTO sessions (user_id, token) VALUES (?, ?)", (user_id, token))

    conn.commit()
    conn.close()

    response = JSONResponse(content={"ok": True, "api_key": api_key, "email": email})
    response.set_cookie(key="session", value=token, httponly=True, secure=True, max_age=86400 * 30, samesite="lax")
    return response


@app.get("/me")
async def get_me(request: Request):
    """Get current user info."""
    user = require_session(request)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT key FROM api_keys WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],))
    key_row = cursor.fetchone()
    conn.close()
    return {
        "email": user["email"],
        "plan": user["plan"],
        "api_key": key_row["key"] if key_row else None,
    }


@app.get("/logout")
async def logout(request: Request):
    """Logout — clear session."""
    token = request.cookies.get("session")
    if token:
        conn = get_db()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
    response = RedirectResponse(url="/")
    response.delete_cookie("session")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard page."""
    user = require_session(request)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT key FROM api_keys WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],))
    key_row = cursor.fetchone()
    api_key = key_row["key"] if key_row else "No API key"

    month = datetime.utcnow().strftime("%Y-%m")
    cursor.execute("SELECT requests FROM usage_log WHERE user_id = ? AND month = ?", (user["id"], month))
    usage_row = cursor.fetchone()
    used = usage_row["requests"] if usage_row else 0
    conn.close()

    plan = user["plan"]
    limit = PLAN_LIMITS.get(plan, 20)
    remaining = max(0, limit - used)

    pct = (used / limit * 100) if limit else 0
    if pct >= 90:
        used_class = "red"
    elif pct >= 70:
        used_class = "yellow"
    else:
        used_class = "green"

    if remaining <= 5:
        remaining_class = "red"
    elif remaining <= 20:
        remaining_class = "yellow"
    else:
        remaining_class = "green"

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ''
    nav_right = f'{admin_link}<a href="/dashboard">Dashboard</a><a href="/upgrade">Upgrade</a><span class="email">{user["email"]}</span><a href="/logout" class="btn-nav">Logout</a>'
    html = Template(DASHBOARD_HTML).safe_substitute(
        nav_right=nav_right,
        used=str(used),
        used_class=used_class,
        remaining=str(remaining),
        remaining_class=remaining_class,
        limit=str(limit),
        plan=plan,
        api_key=api_key,
    )
    return HTMLResponse(content=html)


@app.get("/playground", response_class=HTMLResponse)
async def playground(request: Request):
    """API Playground page."""
    user = require_session(request)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT key FROM api_keys WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],))
    key_row = cursor.fetchone()
    api_key = key_row["key"] if key_row else ""
    conn.close()
    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ''
    nav_right = f'{admin_link}<a href="/dashboard">Dashboard</a><a href="/upgrade">Upgrade</a><span class="email">{user["email"]}</span><a href="/logout" class="btn-nav">Logout</a>'
    html = Template(PLAYGROUND_HTML).safe_substitute(nav_right=nav_right, api_key=api_key)
    return HTMLResponse(content=html)


@app.get("/upgrade", response_class=HTMLResponse)
async def upgrade(request: Request):
    """Upgrade/pricing page."""
    user = require_session(request)
    plan = user["plan"]

    plans_config = {
        "free": {
            "featured": "",
            "badge": "",
            "button": '<a href="#" class="btn btn-outline">Current Plan</a>',
            "current": '<div class="current-badge">Current plan</div>',
            "reqs": "20",
        },
        "starter": {
            "featured": "",
            "badge": "",
            "button": '<a href="mailto:sales@tsn.pw?subject=Upgrade to Starter" class="btn btn-primary">Upgrade</a>',
            "current": "",
            "reqs": "500",
        },
        "pro": {
            "featured": "",
            "badge": "",
            "button": '<a href="mailto:sales@tsn.pw?subject=Upgrade to Pro" class="btn btn-primary">Upgrade</a>',
            "current": "",
            "reqs": "5000",
        },
        "enterprise": {
            "featured": "",
            "badge": "",
            "button": '<a href="mailto:sales@tsn.pw?subject=Enterprise Inquiry" class="btn btn-primary">Contact Sales</a>',
            "current": "",
            "reqs": "50000",
        },
    }

    # Mark current plan
    if plan in plans_config:
        plans_config[plan]["featured"] = "featured"
        plans_config[plan]["badge"] = '<div class="plan-badge-tag">Current Plan</div>'
        plans_config[plan]["button"] = '<div style="margin-top:8px"><span style="color:#3fb950;font-size:13px">Current plan</span></div>'
        plans_config[plan]["current"] = ""

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ''
    nav_right = f'{admin_link}<a href="/dashboard">Dashboard</a><a href="/upgrade">Upgrade</a><span class="email">{user["email"]}</span><a href="/logout" class="btn-nav">Logout</a>'
    html = Template(UPGRADE_HTML).safe_substitute(
        nav_right=nav_right,
        free_featured=plans_config["free"]["featured"],
        free_badge=plans_config["free"]["badge"],
        free_button=plans_config["free"]["button"],
        free_current=plans_config["free"]["current"],
        free_reqs=plans_config["free"]["reqs"],
        starter_featured=plans_config["starter"]["featured"],
        starter_badge=plans_config["starter"]["badge"],
        starter_button=plans_config["starter"]["button"],
        starter_current=plans_config["starter"]["current"],
        pro_featured=plans_config["pro"]["featured"],
        pro_badge=plans_config["pro"]["badge"],
        pro_button=plans_config["pro"]["button"],
        pro_current=plans_config["pro"]["current"],
        ent_featured=plans_config["enterprise"]["featured"],
        ent_badge=plans_config["enterprise"]["badge"],
        ent_button=plans_config["enterprise"]["button"],
        ent_current=plans_config["enterprise"]["current"],
    )
    return HTMLResponse(content=html)


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    """Status page with feedback form."""
    user = get_session_user(request)
    if user:
        admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ''
        nav_right = f'{admin_link}<a href="/dashboard">Dashboard</a><a href="/upgrade">Upgrade</a><span class="email">{user["email"]}</span><a href="/logout" class="btn-nav">Logout</a>'
    else:
        nav_right = '<a href="/login" class="btn-nav">Sign In</a>'
    html = Template(STATUS_HTML).safe_substitute(nav_right=nav_right)
    return HTMLResponse(content=html)


@app.post("/feedback")
async def submit_feedback(request: Request):
    """Submit feedback report."""
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    data = await request.json()
    endpoint = data.get("endpoint", "")
    message = data.get("message", "")
    if not endpoint or not message:
        raise HTTPException(status_code=400, detail="Endpoint and message required")
    conn = get_db()
    conn.execute(
        "INSERT INTO feedback_reports (user_id, endpoint, message) VALUES (?, ?, ?)",
        (user["id"], endpoint, message),
    )
    conn.commit()
    conn.close()
    return {"status": "submitted", "message": "Thank you for your feedback"}


@app.get("/feedback/status")
async def feedback_status(request: Request, endpoint: str = ""):
    """Check feedback status for an endpoint."""
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    if endpoint:
        cursor.execute(
            "SELECT endpoint, message, status, created_at FROM feedback_reports WHERE user_id = ? AND endpoint = ? ORDER BY created_at DESC",
            (user["id"], endpoint),
        )
    else:
        cursor.execute(
            "SELECT endpoint, message, status, created_at FROM feedback_reports WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        )
    rows = cursor.fetchall()
    conn.close()
    return {"reports": [dict(r) for r in rows]}


# ══════════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════════

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin panel — manage users and plans."""
    user = require_session(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.id, u.email, u.plan, u.is_admin, u.created_at,
               ak.key AS api_key,
               COALESCE(ul.requests, 0) AS usage
        FROM users u
        LEFT JOIN api_keys ak ON ak.user_id = u.id AND ak.id = (SELECT MAX(id) FROM api_keys WHERE user_id = u.id)
        LEFT JOIN usage_log ul ON ul.user_id = u.id AND ul.month = strftime('%Y-%m', 'now')
        ORDER BY u.id
    """)
    rows = cursor.fetchall()
    conn.close()

    user_rows = ""
    PLANS = ["free", "starter", "pro", "enterprise"]
    for row in rows:
        uid, email, plan, is_admin, created_at, api_key, usage = row
        plan_badge_class = f"plan-{plan}"
        admin_tag = '<span class="admin-badge">admin</span>' if is_admin else ""
        key_display = api_key[:16] + "..." if api_key and len(api_key) > 16 else (api_key or "—")
        opts = "".join(f'<option value="{p}" {"selected" if p == plan else ""}>{p.title()}</option>' for p in PLANS)
        user_rows += f"""<tr>
<td style="color:#5c6378">{uid}</td>
<td>{email} {admin_tag}</td>
<td><span class="plan-badge {plan_badge_class}">{plan}</span></td>
<td><span class="code">{key_display}</span></td>
<td style="color:#939bb3">{usage}</td>
<td style="color:#5c6378;font-size:12px">{created_at[:10] if created_at else "—"}</td>
<td>
<form method="POST" action="/admin/update-plan" style="display:inline" onsubmit="return confirm('Change {email}\\'s plan?')">
<input type="hidden" name="user_id" value="{uid}">
<select name="plan">{opts}</select>
<button type="submit" class="update-btn">Update</button>
</form>
</td>
</tr>
"""

    nav_right = f'<a href="/dashboard">Dashboard</a><a href="/upgrade">Upgrade</a><span class="email">{user["email"]}</span><a href="/logout" class="btn-nav">Logout</a>'
    html = Template(ADMIN_HTML).safe_substitute(user_rows=user_rows, nav_right=nav_right)
    return HTMLResponse(content=html)


@app.post("/admin/update-plan")
async def admin_update_plan(request: Request):
    """Update a user's plan."""
    user = require_session(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    data = await request.form()
    target_id = int(data.get("user_id"))
    new_plan = data.get("plan", "free")

    if new_plan not in ("free", "starter", "pro", "enterprise"):
        raise HTTPException(status_code=400, detail="Invalid plan")

    conn = get_db()
    conn.execute("UPDATE users SET plan = ? WHERE id = ?", (new_plan, target_id))
    conn.commit()
    conn.close()

    return RedirectResponse(url="/admin", status_code=303)


# ══════════════════════════════════════════════════
#  API ROUTES
# ══════════════════════════════════════════════════

@app.get("/v1/financial/{ticker}")
async def get_financial_data(ticker: str):
    """Get financial health score for a ticker."""
    result = compute_health_score(ticker)
    return result


@app.get("/v1/financial/{ticker}/sentiment")
async def get_sentiment(ticker: str, form_type: str = "10-K", year: str = None):
    """Get sentiment analysis for a ticker's SEC filing."""
    result = analyze_sentiment(ticker, form_type, year)
    return result


@app.post("/v1/financial/compare")
async def compare_companies(data: dict = Body(...)):
    """Compare financial health across companies."""
    tickers = data.get("tickers", [])
    if not tickers:
        raise HTTPException(status_code=400, detail="At least one ticker required")
    if len(tickers) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 tickers")

    results = []
    for t in tickers:
        try:
            health = compute_health_score(t)
            results.append({
                "ticker": t.upper(),
                "company_name": health.get("company_name", ""),
                "health_score": health.get("health_score"),
                "grade": health.get("grade"),
                "profit_margin": health.get("metrics", {}).get("profit_margin"),
                "revenue_growth": health.get("metrics", {}).get("revenue_growth_pct"),
                "debt_to_equity": health.get("metrics", {}).get("debt_to_equity"),
                "current_ratio": health.get("metrics", {}).get("current_ratio"),
                "return_on_equity": health.get("metrics", {}).get("return_on_equity"),
            })
        except HTTPException as e:
            results.append({"ticker": t.upper(), "error": e.detail})
        except Exception as e:
            results.append({"ticker": t.upper(), "error": str(e)})

    return {"comparison": results}


@app.get("/v1/financial/{ticker}/insider")
async def get_insider_trades_endpoint(ticker: str, count: int = 10):
    """Get insider trading data for a ticker."""
    count = min(max(count, 1), 100)
    transactions = get_insider_trades(ticker, count)
    return {"ticker": ticker.upper(), "insider_transactions": transactions}


@app.get("/v1/financial/{ticker}/filings")
async def get_filings_endpoint(ticker: str, count: int = 10):
    """Get recent SEC filings for a ticker."""
    count = min(max(count, 1), 100)
    filings = get_company_filings(ticker, count)
    return {"ticker": ticker.upper(), "recent_filings": filings, "total": len(filings)}


# ═══ NEW ENDPOINT 1: Filing Search ═══
@app.get("/v1/financial/{ticker}/filings/search")
async def get_filing_search(ticker: str, form_types: str = "10-K,10-Q,8-K", count: int = 20):
    """Search SEC filings by form type for a ticker."""
    ticker = ticker.upper().strip()
    count = min(max(count, 1), 50)
    form_type_list = [ft.strip().upper() for ft in form_types.split(",") if ft.strip()]

    cik = lookup_cik(ticker)
    if not cik:
        raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found")

    cache_key = f"filings_search_{ticker}_{form_types}_{count}.json"
    cache_path = CACHE_DIR / cache_key
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    cik_padded = str(cik).zfill(10)
    url = f"{SEC_BASE}/submissions/CIK{cik_padded}.json"
    try:
        data = _SEC_CLIENT.get_json(url)
    except Exception as e:
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Failed to fetch filings: {str(e)}")

    filings = []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    descriptions = recent.get("primaryDocumentDescription", [])
    primary_docs = recent.get("primaryDocument", [])
    accession_numbers = recent.get("accessionNumber", [])

    for i in range(len(forms)):
        form = forms[i] if i < len(forms) else ""
        if form.upper() in form_type_list:
            date = dates[i] if i < len(dates) else ""
            desc = descriptions[i] if i < len(descriptions) else ""
            primary_doc = primary_docs[i] if i < len(primary_docs) else ""
            accession = accession_numbers[i] if i < len(accession_numbers) else ""
            acc_no = accession.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no}/{primary_doc}" if primary_doc and accession else ""

            filings.append({
                "form_type": form,
                "filing_date": date,
                "description": desc,
                "url": filing_url,
                "accession": accession,
            })
            if len(filings) >= count:
                break

    result = {
        "ticker": ticker,
        "form_type_filter": form_type_list,
        "filings": filings,
        "total": len(filings),
    }

    cache_path.write_text(json.dumps(result, default=str))
    return result


# ── SIC Industry Map (lazy, from cache only) ──
_SIC_MAP_PATH = CACHE_DIR / "sic_map.json"
_SIC_TTL = 24 * 3600
_SIC_PEER_LIMIT = 25


def _load_sic_map():
    """Load SIC map from cache — never builds on demand."""
    if _SIC_MAP_PATH.exists():
        try:
            age = time.time() - _SIC_MAP_PATH.stat().st_mtime
            if age < _SIC_TTL:
                return json.loads(_SIC_MAP_PATH.read_text())
        except Exception:
            pass
    return {}


def _get_peer_tickers(sic_code: str, exclude: str) -> list:
    """Get peer tickers for a SIC code, limited to already-cached facts only."""
    sic_map = _load_sic_map()
    industry_data = sic_map.get(sic_code, {"companies": []})
    all_peers = industry_data.get("companies", [])

    results = []
    for peer in all_peers:
        pt = peer.get("ticker", "")
        if pt == exclude:
            continue
        # Only include peers whose facts are already cached
        cache_file = CACHE_DIR / f"{pt}_facts.json"
        if cache_file.exists():
            results.append(pt)
        if len(results) >= _SIC_PEER_LIMIT:
            break
    return results


@app.get("/v1/financial/{ticker}/vs-industry")
async def vs_industry(ticker: str):
    """Compare a company's financial metrics against its industry peers (same SIC code).
    Uses only cached peer data — slow on first call for a SIC, fast after cache warms up.
    """
    ticker = ticker.upper().strip()
    cik = lookup_cik(ticker)
    if not cik:
        raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found")

    profile = get_company_profile(ticker)
    sic_code = profile.get("sic", "")
    sic_description = profile.get("sic_description", "")

    if not sic_code:
        raise HTTPException(status_code=400, detail=f"No SIC code found for {ticker}")

    # Get our metrics
    our_facts = fetch_company_facts(ticker)
    our_health = compute_health_score(ticker)
    our_company_name = our_facts.get("entityName", our_facts.get("_company_name", ticker))
    our_metrics = our_health.get("metrics", {})

    peer_tickers = _get_peer_tickers(sic_code, exclude=ticker)
    if not peer_tickers:
        return {
            "ticker": ticker,
            "company_name": our_company_name,
            "sic": sic_code,
            "sic_description": sic_description,
            "peer_count": 0,
            "note": "No cached peer data available yet. Peers will appear as their data is cached.",
            "your_metrics": {},
            "industry_averages": None,
            "difference": None,
            "percentile_ranking": None,
        }

    peer_scores = []
    for pticker in peer_tickers:
        try:
            pf = fetch_company_facts(pticker)
            ph = compute_health_score(pticker)
            pr = ph.get("metrics", {})
            pname = pf.get("entityName", pf.get("_company_name", pticker))
            peer_scores.append({
                "ticker": pticker,
                "name": pname,
                "profit_margin": pr.get("profit_margin"),
                "operating_margin": pr.get("operating_margin"),
                "gross_margin": pr.get("gross_margin"),
                "debt_ratio": pr.get("debt_ratio"),
                "roa": pr.get("return_on_assets"),
                "roe": pr.get("return_on_equity"),
                "revenue_growth": pr.get("revenue_growth_pct"),
                "cash_ratio": pr.get("cash_ratio"),
            })
        except Exception:
            continue

    if not peer_scores:
        return {
            "ticker": ticker,
            "company_name": our_company_name,
            "sic": sic_code,
            "sic_description": sic_description,
            "peer_count": 0,
            "note": "Cached peers found but no metrics available.",
            "your_metrics": {},
            "industry_averages": None,
            "difference": None,
            "percentile_ranking": None,
        }

    def avg(lst, key):
        vals = [s.get(key) for s in lst if s.get(key) is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 2)

    industry_averages = {
        "profit_margin": avg(peer_scores, "profit_margin"),
        "operating_margin": avg(peer_scores, "operating_margin"),
        "gross_margin": avg(peer_scores, "gross_margin"),
        "debt_ratio": avg(peer_scores, "debt_ratio"),
        "ROA": avg(peer_scores, "roa"),
        "ROE": avg(peer_scores, "roe"),
        "revenue_growth": avg(peer_scores, "revenue_growth"),
        "cash_ratio": avg(peer_scores, "cash_ratio"),
    }

    def diff(our_val, ind_val):
        if our_val is None or ind_val is None:
            return None
        return round(our_val - ind_val, 2)

    difference = {
        "profit_margin": diff(our_metrics.get("profit_margin"), industry_averages["profit_margin"]),
        "operating_margin": diff(our_metrics.get("operating_margin"), industry_averages["operating_margin"]),
        "gross_margin": diff(our_metrics.get("gross_margin"), industry_averages["gross_margin"]),
        "debt_ratio": diff(our_metrics.get("debt_ratio"), industry_averages["debt_ratio"]),
        "ROA": diff(our_metrics.get("return_on_assets"), industry_averages["ROA"]),
        "ROE": diff(our_metrics.get("return_on_equity"), industry_averages["ROE"]),
        "revenue_growth": diff(our_metrics.get("revenue_growth_pct"), industry_averages["revenue_growth"]),
        "cash_ratio": diff(our_metrics.get("cash_ratio"), industry_averages["cash_ratio"]),
    }

    def percentile(our_val, peers, key):
        if our_val is None:
            return None
        vals = [s.get(key) for s in peers if s.get(key) is not None]
        if not vals:
            return None
        worse = sum(1 for v in vals if v <= our_val)
        return round(worse / len(vals) * 100, 1)

    percentile_ranking = {
        "profit_margin": percentile(our_metrics.get("profit_margin"), peer_scores, "profit_margin"),
        "operating_margin": percentile(our_metrics.get("operating_margin"), peer_scores, "operating_margin"),
        "gross_margin": percentile(our_metrics.get("gross_margin"), peer_scores, "gross_margin"),
        "debt_ratio": percentile(our_metrics.get("debt_ratio"), peer_scores, "debt_ratio"),
        "ROA": percentile(our_metrics.get("return_on_assets"), peer_scores, "roa"),
        "ROE": percentile(our_metrics.get("return_on_equity"), peer_scores, "roe"),
        "revenue_growth": percentile(our_metrics.get("revenue_growth_pct"), peer_scores, "revenue_growth"),
        "cash_ratio": percentile(our_metrics.get("cash_ratio"), peer_scores, "cash_ratio"),
    }

    your_metrics = {
        "profit_margin": our_metrics.get("profit_margin"),
        "operating_margin": our_metrics.get("operating_margin"),
        "gross_margin": our_metrics.get("gross_margin"),
        "debt_ratio": our_metrics.get("debt_ratio"),
        "ROA": our_metrics.get("return_on_assets"),
        "ROE": our_metrics.get("return_on_equity"),
        "revenue_growth": our_metrics.get("revenue_growth_pct"),
        "cash_ratio": our_metrics.get("cash_ratio"),
    }

    return {
        "ticker": ticker,
        "company_name": our_company_name,
        "sic": sic_code,
        "sic_description": sic_description,
        "peer_count": len(peer_scores),
        "your_metrics": your_metrics,
        "industry_averages": industry_averages,
        "difference": difference,
        "percentile_ranking": percentile_ranking,
    }


# ═══ NEW ENDPOINT 3: Historical Trends ═══
@app.get("/v1/financial/{ticker}/trends")
async def get_historical_trends(ticker: str):
    """Get 6+ quarters of key financial metrics showing trajectory."""
    ticker = ticker.upper().strip()
    facts = fetch_company_facts(ticker)
    company_name = facts.get("entityName", "")
    cik = facts.get("_cik", lookup_cik(ticker))

    cache_key = f"trends_{ticker}.json"
    cache_path = CACHE_DIR / cache_key
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    revenue_vals = extract_metric(facts, "RevenueFromContractWithCustomerExcludingAssessedTax", max_vals=8)
    net_income_vals = extract_metric(facts, "NetIncomeLoss", max_vals=8)
    op_income_vals = extract_metric(facts, "OperatingIncomeLoss", max_vals=8)
    gross_profit_vals = extract_metric(facts, "GrossProfit", max_vals=8)
    eps_vals = extract_metric(facts, "EarningsPerShareBasic", max_vals=8)

    all_periods = {}
    for label, vals in [("revenue", revenue_vals), ("net_income", net_income_vals),
                        ("operating_income", op_income_vals), ("gross_profit", gross_profit_vals),
                        ("eps", eps_vals)]:
        for v in vals:
            end = v.get("end", "")
            if end:
                if end not in all_periods:
                    all_periods[end] = {"end_date": end}
                all_periods[end][label] = v.get("value")

    sorted_dates = sorted(all_periods.keys(), reverse=True)
    periods = [all_periods[d] for d in sorted_dates[:6]]

    def compute_trend(periods_list, key):
        vals = [p.get(key) for p in periods_list if p.get(key) is not None]
        if len(vals) < 2:
            return {"direction": "stable", "change_pct": 0.0}
        first = vals[-1]
        last = vals[0]
        if first == 0:
            change_pct = 0.0
        else:
            change_pct = round((last - first) / abs(first) * 100, 2)
        direction = "up" if change_pct > 5 else ("down" if change_pct < -5 else "stable")
        return {"direction": direction, "change_pct": change_pct}

    trends = {
        "revenue": compute_trend(periods, "revenue"),
        "net_income": compute_trend(periods, "net_income"),
        "operating_income": compute_trend(periods, "operating_income"),
        "gross_profit": compute_trend(periods, "gross_profit"),
        "eps": compute_trend(periods, "eps"),
    }

    result = {
        "ticker": ticker,
        "company_name": company_name,
        "periods": periods,
        "trends": trends,
    }

    cache_path.write_text(json.dumps(result, default=str))
    return result


@app.get("/v1/financial/{ticker}/profile")
async def get_profile(ticker: str):
    """Get company profile."""
    result = get_company_profile(ticker)
    return result


@app.get("/v1/financial/{ticker}/full")
async def get_full(ticker: str):
    """Get all financial data (500+ GAAP metrics)."""
    result = get_full_facts(ticker)
    return result


# ═══ ENDPOINT: Stock Screener ═══
@app.post("/v1/financial/screener")
async def stock_screener(data: dict = Body(...)):
    """Screen stocks by financial criteria using cached fact data."""
    filters = {
        "min_market_cap": data.get("min_market_cap"),
        "min_revenue_growth": data.get("min_revenue_growth"),
        "max_debt_ratio": data.get("max_debt_ratio"),
        "min_profit_margin": data.get("min_profit_margin"),
        "min_grade": data.get("min_grade"),
        "limit": data.get("limit", 50),
    }

    ticker_map = _load_ticker_map()
    results = []
    limit = min(max(filters["limit"] or 50, 1), 200)

    for cache_file in sorted(CACHE_DIR.glob("*_facts.json")):
        ticker = cache_file.stem.replace("_facts", "").upper()
        if not ticker:
            continue
        try:
            health = compute_health_score(ticker)
        except Exception:
            continue

        metrics = health.get("metrics", {})
        grade = health.get("grade", "")
        health_score = health.get("health_score", 0)
        profit_margin = metrics.get("profit_margin") or 0
        revenue_growth = metrics.get("revenue_growth_pct") or 0
        debt_ratio = metrics.get("debt_ratio") or 0

        # Apply filters
        min_mc = filters["min_market_cap"]
        if min_mc is not None and (metrics.get("market_cap") or 0) < min_mc:
            continue
        min_rg = filters["min_revenue_growth"]
        if min_rg is not None and revenue_growth < min_rg:
            continue
        max_dr = filters["max_debt_ratio"]
        if max_dr is not None and (debt_ratio > max_dr or debt_ratio is None):
            continue
        min_pm = filters["min_profit_margin"]
        if min_pm is not None and profit_margin < min_pm:
            continue
        min_gr = filters["min_grade"]
        if min_gr:
            grade_order = {"A": 5, "B": 4, "C": 3, "D": 2, "F": 1}
            if grade_order.get(grade, 0) < grade_order.get(min_gr.upper(), 3):
                continue

        company_entry = ticker_map.get(ticker, {})
        results.append({
            "ticker": ticker,
            "company_name": health.get("company_name", company_entry.get("title", "")),
            "grade": grade,
            "health_score": round(health_score, 1),
            "profit_margin": round(profit_margin, 2),
            "revenue_growth": round(revenue_growth, 2),
            "debt_ratio": round(debt_ratio, 2),
            "market_cap": metrics.get("market_cap"),
        })

        if len(results) >= limit:
            break

    return {
        "total": len(results),
        "results": results,
        "filters_applied": {k: v for k, v in filters.items() if v is not None},
    }


# ═══ ENDPOINT: Multi-Ticker Batch ═══
@app.post("/v1/financial/batch")
async def multi_ticker_batch(data: dict = Body(...)):
    """Fetch financial data for multiple tickers in one call."""
    tickers = data.get("tickers", [])
    endpoints = data.get("endpoints", ["health"])

    if not tickers:
        raise HTTPException(status_code=400, detail="At least one ticker required")
    if len(tickers) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 tickers per batch request")

    valid_endpoints = {"health", "profile", "full"}
    for ep in endpoints:
        if ep not in valid_endpoints:
            raise HTTPException(status_code=400, detail=f"Unknown endpoint '{ep}'. Valid: {', '.join(sorted(valid_endpoints))}")

    results = []
    errors = []

    for t in tickers:
        ticker = t.upper().strip()
        entry = {"ticker": ticker}
        try:
            if "health" in endpoints:
                entry["health"] = compute_health_score(ticker)
            if "profile" in endpoints:
                entry["profile"] = get_company_profile(ticker)
            if "full" in endpoints:
                entry["full"] = get_full_facts(ticker)
            results.append(entry)
        except HTTPException as e:
            errors.append({"ticker": ticker, "error": e.detail})
        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})

    return {
        "results": results,
        "errors": errors,
        "total": len(results) + len(errors),
        "failed": len(errors),
    }


# ═══ ENDPOINT: Revenue Segments ═══
@app.get("/v1/financial/{ticker}/segments")
async def get_revenue_segments(ticker: str):
    """Get revenue breakdown by segment/product/geography for a company."""
    ticker = ticker.upper().strip()
    facts = fetch_company_facts(ticker)
    company_name = facts.get("entityName", "")

    cache_key = f"segments_{ticker}.json"
    cache_path = CACHE_DIR / cache_key
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    # Total revenue (latest 3 years)
    total_revenue = extract_metric(facts, "RevenueFromContractWithCustomerExcludingAssessedTax", max_vals=3)

    # Segment revenue — try to find segment breakdowns in the facts
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    dei = facts.get("facts", {}).get("dei", {})

    # Product/service line segments
    segments = []
    product_concepts = [
        "RevenueFromContractWithCustomerByProductAndServiceLine",
        "RevenueFromContractWithCustomerByProductLine",
        "RevenueFromContractWithCustomerByServiceLine",
        "RevenueFromContractWithCustomerByBusinessLine",
        "SegmentRevenue",
        "RevenueBySegment",
        "RevenueFromContractWithCustomerByOperatingSegment",
    ]

    # Geographic revenue
    geo_concepts = [
        "RevenueFromContractWithCustomerByGeography",
        "RevenueFromContractWithCustomerByCountry",
        "RevenueByGeographicArea",
        "RevenueFromContractWithCustomerByRegion",
        "GeographicRevenue",
    ]

    for concept in product_concepts:
        concept_data = us_gaap.get(concept)
        if concept_data:
            units_data = concept_data.get("units", concept_data)
            for unit_key, entries in units_data.items():
                if unit_key.startswith("USD"):
                    # Group by the segment label from the dimension
                    seg_map = {}
                    for entry in entries:
                        val = entry.get("val")
                        end = entry.get("end")
                        if val is not None:
                            seg_label = "Unknown"
                            # Try to extract segment label from dimension data
                            dim = entry.get("dimension", {})
                            if dim:
                                mem = dim.get("srt:ProductAndServiceAxis", dim.get("srt:StatementBusinessSegmentsAxis", ""))
                                if mem:
                                    seg_label = mem.split("/")[-1].replace("Member", "").replace("srt:", "")
                            if seg_label not in seg_map or (end or "") > (seg_map[seg_label].get("end") or ""):
                                seg_map[seg_label] = {"segment": seg_label, "value": val, "end": end}
                    for seg in seg_map.values():
                        segments.append(seg)
                    break

    # If no product segments found, try extracting from member-axis patterns
    if not segments:
        # Scan all us-gaap concepts for member-axis segmented values
        seen_segments = {}
        for concept, concept_data in us_gaap.items():
            units_data = concept_data.get("units", concept_data)
            for unit_key, entries in units_data.items():
                if not unit_key.startswith("USD"):
                    continue
                for entry in entries:
                    dim = entry.get("dimension", {})
                    if dim:
                        for axis_key, member in dim.items():
                            if "Segment" in axis_key or "Product" in axis_key or "Service" in axis_key:
                                seg_name = member.split("/")[-1].replace("Member", "").replace("srt:", "")
                                if seg_name and seg_name != "Member":
                                    val = entry.get("val")
                                    end = entry.get("end")
                                    if val is not None and (seg_name not in seen_segments or (end or "") > seen_segments[seg_name].get("end", "")):
                                        seen_segments[seg_name] = {
                                            "segment": seg_name,
                                            "concept": concept,
                                            "value": val,
                                            "end": end,
                                        }
        segments = list(seen_segments.values())

    # Geographic revenue
    geographic_revenue = []
    for concept in geo_concepts:
        concept_data = us_gaap.get(concept)
        if concept_data:
            units_data = concept_data.get("units", concept_data)
            for unit_key, entries in units_data.items():
                if unit_key.startswith("USD"):
                    geo_map = {}
                    for entry in entries:
                        val = entry.get("val")
                        end = entry.get("end")
                        if val is not None:
                            loc = "Unknown"
                            dim = entry.get("dimension", {})
                            if dim:
                                mem = dim.get("srt:StatementGeographicalAxis", dim.get("srt:CountryAxis", dim.get("dei:CountryRegionAxis", "")))
                                if mem:
                                    loc = mem.split("/")[-1].replace("Member", "").replace("srt:", "").replace("dei:", "")
                            if loc not in geo_map or (end or "") > (geo_map[loc].get("end") or ""):
                                geo_map[loc] = {"region": loc, "value": val, "end": end}
                    geographic_revenue = list(geo_map.values())
                    break

    # Year-over-year growth
    yoy_growth = 0.0
    if len(total_revenue) >= 2:
        latest = total_revenue[0]["value"]
        previous = total_revenue[1]["value"]
        if previous:
            yoy_growth = round((latest - previous) / abs(previous) * 100, 2)

    result = {
        "ticker": ticker,
        "company_name": company_name,
        "total_revenue": total_revenue[:3] if total_revenue else [],
        "segments": segments[:20] if segments else [],
        "geographic_revenue": geographic_revenue[:10] if geographic_revenue else [],
        "year_over_year_growth": yoy_growth,
        "has_segment_data": len(segments) > 0,
    }

    cache_path.write_text(json.dumps(result, default=str))
    return result


# ═══ ENDPOINT: Filing Types Summary ═══
@app.get("/v1/financial/{ticker}/filing-types")
async def get_filing_types_summary(ticker: str):
    """Get a categorized summary of all filing types for a company."""
    ticker = ticker.upper().strip()
    cik = lookup_cik(ticker)
    if not cik:
        raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found")

    cache_key = f"filing_types_{ticker}.json"
    cache_path = CACHE_DIR / cache_key
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    cik_padded = str(cik).zfill(10)
    url = f"{SEC_BASE}/submissions/CIK{cik_padded}.json"
    try:
        data = _SEC_CLIENT.get_json(url)
    except Exception as e:
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Failed to fetch filings: {str(e)}")

    ticker_map_entry = _load_ticker_map().get(ticker, {})
    company_name = data.get("name", ticker_map_entry.get("title", ticker))

    # Categorize form types
    form_categories = {
        "annual": {"forms": {"10-K", "20-F", "40-F"}, "form_types": [], "count": 0, "most_recent": ""},
        "quarterly": {"forms": {"10-Q", "6-K"}, "form_types": [], "count": 0, "most_recent": ""},
        "current": {"forms": {"8-K"}, "form_types": [], "count": 0, "most_recent": ""},
        "insider": {"forms": {"3", "4", "5"}, "form_types": [], "count": 0, "most_recent": ""},
        "ownership": {"forms": {"13F", "13D", "13G", "13F-NT", "13F-HR"}, "form_types": [], "count": 0, "most_recent": ""},
        "registration": {"forms": {"S-1", "S-3", "S-8", "424B4", "424B3", "424B2", "424B1", "S-4", "S-11"}, "form_types": [], "count": 0, "most_recent": ""},
    }

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])

    # Track all seen form types beyond the predefined categories
    all_seen_forms = {}
    for i in range(len(forms)):
        form = forms[i] if i < len(forms) else ""
        date = dates[i] if i < len(dates) else ""
        if not form:
            continue

        categorized = False
        for cat_name, cat_data in form_categories.items():
            if form in cat_data["forms"]:
                cat_data["count"] += 1
                cat_data["form_types"].append(form)
                if date and (date > cat_data["most_recent"] or not cat_data["most_recent"]):
                    cat_data["most_recent"] = date
                categorized = True
                break

        if not categorized:
            if form not in all_seen_forms:
                all_seen_forms[form] = {"count": 0, "most_recent": ""}
            all_seen_forms[form]["count"] += 1
            if date and (date > all_seen_forms[form]["most_recent"] or not all_seen_forms[form]["most_recent"]):
                all_seen_forms[form]["most_recent"] = date

    # Deduplicate form_types lists and sort
    by_category = {}
    for cat_name, cat_data in form_categories.items():
        by_category[cat_name] = {
            "count": cat_data["count"],
            "most_recent": cat_data["most_recent"] if cat_data["most_recent"] else None,
            "form_types": sorted(set(cat_data["form_types"])),
        }

    # Add "other" category
    other_form_types = sorted(all_seen_forms.keys())
    other_count = sum(v["count"] for v in all_seen_forms.values())
    other_most_recent = max((v["most_recent"] for v in all_seen_forms.values() if v["most_recent"]), default="")
    by_category["other"] = {
        "count": other_count,
        "most_recent": other_most_recent if other_most_recent else None,
        "form_types": other_form_types,
    }

    total_filings = sum(cat["count"] for cat in by_category.values())

    result = {
        "ticker": ticker,
        "company_name": company_name,
        "total_filings": total_filings,
        "by_category": by_category,
    }

    cache_path.write_text(json.dumps(result, default=str))
    return result


# ═══ CATCH-ALL ROUTE: MUST BE LAST ═══
@app.get("/v1/financial/{ticker}/{statement}")
async def get_statement(ticker: str, statement: str):
    """Get a specific financial statement (income-statement, balance-sheet, cash-flow)."""
    valid_statements = ["income-statement", "balance-sheet", "cash-flow"]
    if statement not in valid_statements:
        raise HTTPException(status_code=400, detail=f"Unknown statement: {statement}. Use one of: {', '.join(valid_statements)}")
    result = get_financial_statement(ticker, statement)
    return result


# ══════════════════════════════════════════════════
#  FRED ECONOMICS API
# ══════════════════════════════════════════════════

# ── FRED Series Map ──
FRED_SERIES = {
    "gdp": {"id": "GDP", "name": "Gross Domestic Product", "unit": "billions USD"},
    "cpi": {"id": "CPIAUCSL", "name": "Consumer Price Index (All Urban)", "unit": "index 1982-1984=100"},
    "fed_funds": {"id": "FEDFUNDS", "name": "Federal Funds Effective Rate", "unit": "percent"},
    "unemployment": {"id": "UNRATE", "name": "Unemployment Rate", "unit": "percent"},
    "housing_starts": {"id": "HOUST", "name": "Housing Starts", "unit": "thousands of units"},
    "mortgage_30yr": {"id": "MORTGAGE30US", "name": "30-Year Fixed Rate Mortgage Average", "unit": "percent"},
    "treasury_10yr": {"id": "DGS10", "name": "10-Year Treasury Constant Maturity Rate", "unit": "percent"},
    "initial_claims": {"id": "ICSA", "name": "Initial Claims", "unit": "thousands"},
}


def _fred_api_key() -> str:
    """Get FRED API key from env var or key file."""
    if FRED_API_KEY:
        return FRED_API_KEY
    raise HTTPException(
        status_code=503,
        detail="FRED_API_KEY not configured. Set the FRED_API_KEY environment variable "
               "or place your API key in /root/.fred_api_key",
    )


def _fred_fetch(series_id: str) -> dict:
    """Fetch the latest observation from FRED for a given series ID. Results cached for CACHE_TTL."""
    cache_key = f"fred_{series_id}.json"
    cache_path = CACHE_DIR / cache_key
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    key = _fred_api_key()
    url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={key}&file_type=json&sort_order=desc&limit=2"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"FRED API error for series {series_id}: {str(e)[:200]}")
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Failed to fetch FRED data for series {series_id}")
    except Exception as e:
        logger.error(f"Unexpected FRED error for series {series_id}: {str(e)[:200]}")
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Unexpected error fetching FRED data")

    observations = data.get("observations", [])
    result = {
        "series_id": series_id,
        "observations": observations,
    }
    cache_path.write_text(json.dumps(result, default=str))
    return result


def _fred_observation(series_id: str) -> dict:
    """Fetch the latest FRED observation and return a clean dict with value, date, and metadata."""
    data = _fred_fetch(series_id)
    series_info = None
    for key, info in FRED_SERIES.items():
        if info["id"] == series_id:
            series_info = info
            break
    observations = data.get("observations", [])
    latest = observations[0] if observations else {}
    return {
        "series_id": series_id,
        "name": series_info["name"] if series_info else series_id,
        "unit": series_info["unit"] if series_info else "",
        "date": latest.get("date", ""),
        "value": latest.get("value", ""),
    }


@app.get("/v1/econ/gdp")
async def get_gdp():
    """Get latest US GDP data."""
    return {"indicator": "gdp", "data": _fred_observation("GDP")}


@app.get("/v1/econ/inflation")
async def get_inflation():
    """Get latest CPI / inflation data."""
    return {"indicator": "inflation", "data": _fred_observation("CPIAUCSL")}


@app.get("/v1/econ/rates")
async def get_rates():
    """Get latest Fed funds rate, mortgage rates, and treasury yields."""
    fed_funds = _fred_observation("FEDFUNDS")
    mortgage = _fred_observation("MORTGAGE30US")
    treasury = _fred_observation("DGS10")
    return {
        "indicator": "rates",
        "federal_funds_rate": fed_funds,
        "mortgage_30yr": mortgage,
        "treasury_10yr": treasury,
    }


@app.get("/v1/econ/employment")
async def get_employment():
    """Get latest unemployment rate and jobless claims."""
    unemployment = _fred_observation("UNRATE")
    initial_claims = _fred_observation("ICSA")
    return {
        "indicator": "employment",
        "unemployment_rate": unemployment,
        "initial_jobless_claims": initial_claims,
    }


@app.get("/v1/econ/housing")
async def get_housing():
    """Get latest housing starts and existing home sales data."""
    housing_starts = _fred_observation("HOUST")
    mortgage = _fred_observation("MORTGAGE30US")
    return {
        "indicator": "housing",
        "housing_starts": housing_starts,
        "mortgage_30yr_rate": mortgage,
    }


@app.get("/v1/econ/summary")
async def get_econ_summary():
    """Get all key economic indicators in one call."""
    gdp = _fred_observation("GDP")
    cpi = _fred_observation("CPIAUCSL")
    fed_funds = _fred_observation("FEDFUNDS")
    unemployment = _fred_observation("UNRATE")
    housing_starts = _fred_observation("HOUST")
    mortgage = _fred_observation("MORTGAGE30US")
    treasury = _fred_observation("DGS10")
    claims = _fred_observation("ICSA")
    return {
        "gdp": gdp,
        "inflation_cpi": cpi,
        "federal_funds_rate": fed_funds,
        "unemployment_rate": unemployment,
        "housing_starts": housing_starts,
        "mortgage_30yr": mortgage,
        "treasury_10yr": treasury,
        "initial_jobless_claims": claims,
    }


# ══════════════════════════════════════════════════
#  CENSUS BUREAU DEMOGRAPHICS API
# ══════════════════════════════════════════════════

# ── Census Variables ──
CENSUS_VARS_ZIP = {
    "population": "B01001_001E",
    "median_income": "B19013_001E",
    "median_age": "B01002_001E",
    "housing_units": "B25001_001E",
    "median_home_value": "B25077_001E",
    "median_rent": "B25064_001E",
    "bachelor_degree_or_higher": "B15003_022E",
    "total_education": "B15003_001E",
}


def _census_api_key() -> str:
    """Get Census API key from env var or key file."""
    if CENSUS_API_KEY:
        return CENSUS_API_KEY
    raise HTTPException(
        status_code=503,
        detail="CENSUS_API_KEY not configured. Set the CENSUS_API_KEY environment variable "
               "or place your API key in /root/.census_api_key",
    )


def _census_fetch(variable_list: list, geo_type: str, geo_value: str) -> dict:
    """Fetch data from the Census Bureau ACS5 API. Results cached for CACHE_TTL."""
    cache_key = f"census_{geo_type}_{geo_value}.json"
    cache_path = CACHE_DIR / cache_key
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    key = _census_api_key()
    vars_str = ",".join(variable_list)
    if geo_type == "zip":
        geo_spec = f"zip%20code%20tabulation%20area:{geo_value}"
    elif geo_type == "county":
        geo_spec = f"county:{geo_value}"
        # Need state prefix — if FIPS is 5 digits, first 2 are state
        if len(geo_value) == 5:
            state_fips = geo_value[:2]
            geo_spec = f"state:{state_fips}&in=state:{state_fips}&for=county:{geo_value[2:]}"
    else:
        raise HTTPException(status_code=400, detail=f"Unknown geography type: {geo_type}")

    url = f"https://api.census.gov/data/2022/acs/acs5?get={vars_str},NAME&for={geo_spec}&key={key}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Census API error: {str(e)[:200]}")
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Failed to fetch Census data")
    except Exception as e:
        logger.error(f"Unexpected Census error: {str(e)[:200]}")
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail=f"Unexpected error fetching Census data")

    result = data
    cache_path.write_text(json.dumps(result, default=str))
    return result


def _parse_census_zip(zip_code: str) -> dict:
    """Fetch and parse ZIP code demographics into a clean dict."""
    data = _census_fetch(list(CENSUS_VARS_ZIP.values()), "zip", zip_code)
    if len(data) < 2:
        raise HTTPException(status_code=404, detail=f"ZIP code '{zip_code}' not found in Census data")

    rows = data[1:]  # Skip header row
    if not rows:
        raise HTTPException(status_code=404, detail=f"No census data found for ZIP code '{zip_code}'")

    row = rows[0]
    headers = data[0]

    result = {
        "zip_code": zip_code,
        "name": row[headers.index("NAME")] if "NAME" in headers else "",
    }

    var_keys = {v: k for k, v in CENSUS_VARS_ZIP.items()}
    for i, header in enumerate(headers):
        if header in var_keys and i < len(row):
            key = var_keys[header]
            val = row[i]
            try:
                result[key] = int(val) if val and val != "null" else None
            except ValueError:
                try:
                    result[key] = float(val) if val and val != "null" else None
                except ValueError:
                    result[key] = val if val and val != "null" else None

    # Compute education percentage
    total_edu = result.get("total_education")
    bachelors = result.get("bachelor_degree_or_higher")
    if total_edu and bachelors:
        result["percent_bachelor_or_higher"] = round(bachelors / total_edu * 100, 1)
    else:
        result["percent_bachelor_or_higher"] = None

    # Clean up raw variables
    result.pop("total_education", None)
    result.pop("bachelor_degree_or_higher", None)

    return result


def _parse_census_county(fips: str) -> dict:
    """Fetch and parse county demographics into a clean dict."""
    data = _census_fetch(list(CENSUS_VARS_ZIP.values()), "county", fips)
    if len(data) < 2:
        raise HTTPException(status_code=404, detail=f"County FIPS '{fips}' not found in Census data")

    rows = data[1:]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No census data found for county FIPS '{fips}'")

    row = rows[0]
    headers = data[0]

    result = {
        "fips": fips,
        "name": row[headers.index("NAME")] if "NAME" in headers else "",
    }

    var_keys = {v: k for k, v in CENSUS_VARS_ZIP.items()}
    for i, header in enumerate(headers):
        if header in var_keys and i < len(row):
            key = var_keys[header]
            val = row[i]
            try:
                result[key] = int(val) if val and val != "null" else None
            except ValueError:
                try:
                    result[key] = float(val) if val and val != "null" else None
                except ValueError:
                    result[key] = val if val and val != "null" else None

    # Compute education percentage
    total_edu = result.get("total_education")
    bachelors = result.get("bachelor_degree_or_higher")
    if total_edu and bachelors:
        result["percent_bachelor_or_higher"] = round(bachelors / total_edu * 100, 1)
    else:
        result["percent_bachelor_or_higher"] = None

    result.pop("total_education", None)
    result.pop("bachelor_degree_or_higher", None)

    return result


@app.get("/v1/demo/zip/{zip_code}")
async def get_zip_demographics(zip_code: str):
    """Get demographics for a ZIP code (population, income, age, housing, education)."""
    zip_code = zip_code.strip().zfill(5)
    if not re.match(r"^\d{5}$", zip_code):
        raise HTTPException(status_code=400, detail="Invalid ZIP code format (must be 5 digits)")
    result = _parse_census_zip(zip_code)
    return result


@app.get("/v1/demo/county/{fips}")
async def get_county_demographics(fips: str):
    """Get county-level demographics data by FIPS code."""
    fips = fips.strip().zfill(5)
    if not re.match(r"^\d{5}$", fips):
        raise HTTPException(status_code=400, detail="Invalid FIPS code format (must be 5 digits)")
    result = _parse_census_county(fips)
    return result


# ══════════════════════════════════════════════════
#  WORLD BANK OPEN DATA API
# ══════════════════════════════════════════════════

# ── World Bank Indicators ──
WORLDBANK_INDICATORS = {
    "gdp": {"id": "NY.GDP.MKTP.CD", "name": "GDP (current US$)", "unit": "current US$"},
    "gdp_growth": {"id": "NY.GDP.MKTP.KD.ZG", "name": "GDP growth (annual %)", "unit": "annual %"},
    "gdp_per_capita": {"id": "NY.GDP.PCAP.CD", "name": "GDP per capita (current US$)", "unit": "current US$"},
    "gdp_per_capita_growth": {"id": "NY.GDP.PCAP.KD.ZG", "name": "GDP per capita growth (annual %)", "unit": "annual %"},
    "population": {"id": "SP.POP.TOTL", "name": "Population, total", "unit": "count"},
    "population_growth": {"id": "SP.POP.GROW", "name": "Population growth (annual %)", "unit": "annual %"},
    "inflation_cpi": {"id": "FP.CPI.TOTL.ZG", "name": "Inflation, consumer prices (annual %)", "unit": "annual %"},
    "unemployment": {"id": "SL.UEM.TOTL.ZS", "name": "Unemployment, total (% of labor force)", "unit": "% of labor force"},
    "exports": {"id": "NE.EXP.GNFS.ZS", "name": "Exports of goods and services (% of GDP)", "unit": "% of GDP"},
    "imports": {"id": "NE.IMP.GNFS.ZS", "name": "Imports of goods and services (% of GDP)", "unit": "% of GDP"},
    "fdi": {"id": "BX.KLT.DINV.WD.GD.ZS", "name": "Foreign direct investment, net inflows (% of GDP)", "unit": "% of GDP"},
    "poverty": {"id": "SI.POV.DDAY", "name": "Poverty headcount ratio at $2.15/day (% of population)", "unit": "% of population"},
    "life_expectancy": {"id": "SP.DYN.LE00.IN", "name": "Life expectancy at birth, total (years)", "unit": "years"},
    "education_expenditure": {"id": "SE.XPD.TOTL.GD.ZS", "name": "Government expenditure on education (% of GDP)", "unit": "% of GDP"},
}

# ── Popular country codes ──
WORLDBANK_COUNTRIES = {
    "USA": "United States", "CAN": "Canada", "GBR": "United Kingdom",
    "DEU": "Germany", "FRA": "France", "JPN": "Japan", "CHN": "China",
    "IND": "India", "BRA": "Brazil", "AUS": "Australia", "CHE": "Switzerland",
    "SGP": "Singapore", "NLD": "Netherlands", "SWE": "Sweden", "NOR": "Norway",
    "DNK": "Denmark", "FIN": "Finland", "ITA": "Italy", "ESP": "Spain",
    "KOR": "South Korea", "MEX": "Mexico", "ZAF": "South Africa",
    "RUS": "Russia", "IDN": "Indonesia", "TUR": "Turkey", "ARG": "Argentina",
    "SAU": "Saudi Arabia", "NGA": "Nigeria", "EGY": "Egypt", "VNM": "Vietnam",
    "THA": "Thailand", "ISR": "Israel", "ARE": "United Arab Emirates",
    "HKG": "Hong Kong", "NZL": "New Zealand", "PAK": "Pakistan",
    "BGD": "Bangladesh", "PHL": "Philippines", "MYS": "Malaysia",
    "CHL": "Chile", "COL": "Colombia", "POL": "Poland", "CZE": "Czechia",
    "UKR": "Ukraine", "ROU": "Romania", "PRT": "Portugal", "GRC": "Greece",
    "HUN": "Hungary", "IRL": "Ireland", "AUT": "Austria", "BEL": "Belgium",
    "WLD": "World", "EAS": "East Asia & Pacific", "SAS": "South Asia",
    "LCN": "Latin America & Caribbean", "MEA": "Middle East & North Africa",
    "SSF": "Sub-Saharan Africa", "ECS": "Europe & Central Asia",
    "NAC": "North America",
}


def _worldbank_fetch(indicator_id: str, country_code: str = "USA", years: int = 10) -> dict:
    """Fetch indicator data from the World Bank API. Results cached for CACHE_TTL.
    No API key required for basic access.
    """
    cache_key = f"wb_{country_code}_{indicator_id}.json"
    cache_path = CACHE_DIR / cache_key
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            return json.loads(cache_path.read_text())

    url = (
        f"https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator_id}"
        f"?format=json&per_page={years}&sort=desc"
    )
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"World Bank API error: {str(e)[:200]}")
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch World Bank data for {country_code}/{indicator_id}"
        )
    except Exception as e:
        logger.error(f"Unexpected World Bank error: {str(e)[:200]}")
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        raise HTTPException(status_code=502, detail="Unexpected error fetching World Bank data")

    # Normalize response
    if isinstance(data, list) and len(data) > 1:
        entries = data[1] if data[1] else []
        result = {
            "indicator_id": indicator_id,
            "country_code": country_code,
            "entries": entries,
        }
    else:
        result = {"indicator_id": indicator_id, "country_code": country_code, "entries": []}

    cache_path.write_text(json.dumps(result, default=str))
    return result


def _worldbank_get_indicator(indicator_id: str, country_code: str = "USA", years: int = 10) -> dict:
    """Fetch and parse a World Bank indicator into a clean time-series dict."""
    data = _worldbank_fetch(indicator_id, country_code, years)
    indicator_info = None
    for key, info in WORLDBANK_INDICATORS.items():
        if info["id"] == indicator_id:
            indicator_info = info
            break

    entries = data.get("entries", [])
    series = []
    for entry in entries:
        if entry and entry.get("value") is not None:
            series.append({
                "date": entry.get("date", ""),
                "value": entry.get("value"),
            })

    return {
        "indicator_id": indicator_id,
        "indicator_name": indicator_info["name"] if indicator_info else indicator_id,
        "unit": indicator_info["unit"] if indicator_info else "",
        "country_code": country_code,
        "country_name": WORLDBANK_COUNTRIES.get(country_code, country_code),
        "latest": series[0] if series else None,
        "series": series,
    }


# ── Endpoints ──

@app.get("/v1/worldbank/countries")
async def wb_list_countries():
    """Get list of all available World Bank country codes and names."""
    return {
        "source": "World Bank Open Data",
        "count": len(WORLDBANK_COUNTRIES),
        "countries": [{"code": k, "name": v} for k, v in WORLDBANK_COUNTRIES.items()],
    }


@app.get("/v1/worldbank/summary")
async def wb_summary(countries: str = Query("USA,CAN,GBR,DEU,JPN,CHN,IND,BRA,AUS", description="Comma-separated ISO country codes")):
    """Get a summary of key economic indicators for multiple countries (up to 10)."""
    country_list = [c.strip().upper() for c in countries.split(",")][:10]
    result = {"source": "World Bank Open Data", "attribution": "Data licensed under CC BY 4.0", "countries": {}}
    for code in country_list:
        if code in WORLDBANK_COUNTRIES:
            try:
                gdp = _worldbank_get_indicator("NY.GDP.MKTP.CD", code, 2)
                gdp_growth = _worldbank_get_indicator("NY.GDP.MKTP.KD.ZG", code, 2)
                population = _worldbank_get_indicator("SP.POP.TOTL", code, 2)
                result["countries"][code] = {
                    "name": WORLDBANK_COUNTRIES[code],
                    "gdp_current_usd": gdp["latest"],
                    "gdp_growth_pct": gdp_growth["latest"],
                    "population": population["latest"],
                }
            except HTTPException:
                result["countries"][code] = {"name": WORLDBANK_COUNTRIES[code], "error": "Failed to fetch data"}
    return result


@app.get("/v1/worldbank/{country_code}")
async def wb_country_economics(country_code: str):
    """Get key economic indicators for a specific country from the World Bank.
    Returns GDP, GDP growth, GDP per capita, population, inflation, unemployment, and more.
    """
    country_code = country_code.upper()
    if country_code not in WORLDBANK_COUNTRIES:
        raise HTTPException(
            status_code=404,
            detail=f"Country code '{country_code}' not found. Use /v1/worldbank/countries to see available codes."
        )

    # Fetch GDP, growth, population, inflation, unemployment, trade in parallel
    gdp = _worldbank_get_indicator("NY.GDP.MKTP.CD", country_code, 5)
    gdp_growth = _worldbank_get_indicator("NY.GDP.MKTP.KD.ZG", country_code, 5)
    gdp_per_capita = _worldbank_get_indicator("NY.GDP.PCAP.CD", country_code, 5)
    population = _worldbank_get_indicator("SP.POP.TOTL", country_code, 2)
    inflation = _worldbank_get_indicator("FP.CPI.TOTL.ZG", country_code, 5)
    unemployment = _worldbank_get_indicator("SL.UEM.TOTL.ZS", country_code, 5)
    exports = _worldbank_get_indicator("NE.EXP.GNFS.ZS", country_code, 5)
    imports = _worldbank_get_indicator("NE.IMP.GNFS.ZS", country_code, 5)

    return {
        "source": "World Bank Open Data",
        "attribution": "Data licensed under CC BY 4.0 — https://datacatalog.worldbank.org/",
        "country_code": country_code,
        "country_name": WORLDBANK_COUNTRIES.get(country_code, country_code),
        "gdp": gdp,
        "gdp_growth": gdp_growth,
        "gdp_per_capita": gdp_per_capita,
        "population": population,
        "inflation": inflation,
        "unemployment": unemployment,
        "exports": exports,
        "imports": imports,
    }


@app.get("/v1/worldbank/{country_code}/{indicator_key}")
async def wb_specific_indicator(country_code: str, indicator_key: str, years: int = Query(10, ge=1, le=50)):
    """Get a specific World Bank indicator for a country.
    Args:
        country_code: ISO 3166-1 alpha-3 country code (e.g., USA, CHN, GBR)
        indicator_key: One of: gdp, gdp_growth, gdp_per_capita, gdp_per_capita_growth, population, population_growth, inflation_cpi, unemployment, exports, imports, fdi, poverty, life_expectancy, education_expenditure
        years: Number of years of historical data (default: 10, max: 50)
    """
    country_code = country_code.upper()
    indicator_key = indicator_key.lower().replace("-", "_")

    if country_code not in WORLDBANK_COUNTRIES:
        raise HTTPException(status_code=404, detail=f"Country code '{country_code}' not found. Use /v1/worldbank/countries to see available codes.")

    if indicator_key not in WORLDBANK_INDICATORS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown indicator '{indicator_key}'. Available: {', '.join(sorted(WORLDBANK_INDICATORS.keys()))}"
        )

    indicator_info = WORLDBANK_INDICATORS[indicator_key]
    result = _worldbank_get_indicator(indicator_info["id"], country_code, years)

    return {
        "source": "World Bank Open Data",
        "attribution": "Data licensed under CC BY 4.0",
        "country_code": country_code,
        "country_name": WORLDBANK_COUNTRIES.get(country_code, country_code),
        **result,
    }


# ── Startup ──
@app.on_event("startup")
async def startup():
    """Initialize database and load ticker map on startup."""
    init_db()
    _load_ticker_map()
