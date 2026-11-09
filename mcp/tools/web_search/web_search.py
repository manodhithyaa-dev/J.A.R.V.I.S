#!/usr/bin/env python3
"""
DEEPSEARCH - Recursive Web Research & Intelligence Engine (single file)

Turns one topic into a source-grounded Markdown research report using a locally
hosted Ollama model (default: reader-lm:1.5b). No cloud LLM is ever called.

Install:
    pip install fastapi uvicorn httpx beautifulsoup4 trafilatura lxml pydantic python-dotenv
    ollama pull reader-lm:1.5b

Run the API (SSE streaming):
    python web_search.py serve --port 8000
    # or: uvicorn web_search:app --port 8000

Run from the command line (progress on stderr, Markdown on stdout):
    python web_search.py research "Autonomous Vehicles" --depth 2 --urls 40 -o report.md

API:
    POST /api/research                    start a job -> {"job_id": ...}
    GET  /api/research/{id}/stream        SSE: progress + Markdown `token` events
    GET  /api/research/{id}               status / metadata (+ final markdown when done)
    POST /api/research/{id}/cancel        cancel a running job
    POST /api/research/{id}/resume        resume a cancelled / interrupted / failed job
    GET  /api/research/{id}/sources       collected source metadata
    GET  /api/health                      backend + Ollama + search providers

Pipeline:
    plan -> multi-engine search -> priority-queue recursive crawl (fetch, Trafilatura
    extraction, relevance, dedup, link/term discovery, query expansion) ->
    Reader-LM HTML->Markdown per chunk -> per-section synthesis (hierarchical when
    evidence is large) -> streamed Markdown with clickable citations.

Search providers (auto-enabled by configuration): DuckDuckGo (HTML endpoint, no key),
Brave (BRAVE_API_KEY), Bing (BING_API_KEY), Google Programmable Search
(GOOGLE_API_KEY + GOOGLE_CSE_ID), SearXNG (SEARXNG_URL).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import heapq
import html as htmllib
import ipaddress
import itertools
import json
import logging
import os
import random
import re
import socket
import sqlite3
import sys
import threading
import time
import uuid
import zlib
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional
from urllib import robotparser
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
import trafilatura
from bs4 import BeautifulSoup, NavigableString, Tag
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

log = logging.getLogger("deepsearch")


# =====================================================================================
# 1. CONFIGURATION
# =====================================================================================

def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # LLM (local Ollama only)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "reader-lm:1.5b"          # Stage 1: HTML -> Markdown
    synthesis_model: str = ""                      # Stage 4; empty => same as ollama_model
    llm_timeout: float = 600.0
    llm_retries: int = 2
    llm_concurrency: int = 1
    llm_num_ctx: int = 8192
    llm_num_predict: int = 4096
    chunk_chars: int = 6000                        # max HTML chars per Reader-LM request
    synth_max_chars: int = 9000                    # max evidence chars per synthesis request
    synth_max_packs: int = 4                       # reduce hierarchically above this

    # storage
    database_path: str = "./deepsearch.db"
    cache_ttl_hours: int = 72
    enable_search_cache: bool = True
    search_cache_ttl_hours: int = 24
    enable_content_dedup: bool = True

    # research budget
    max_depth: int = 3
    max_urls: int = 100
    max_pages: int = 60                            # max pages fetched/processed
    max_pages_per_domain: int = 10
    max_queries: int = 40
    max_concurrent: int = 5
    request_timeout: float = 30.0
    max_duration: int = 1800
    max_links_per_page: int = 12
    expand_per_wave: int = 3
    coverage_threshold: float = 0.95
    low_novelty_threshold: float = 0.10
    min_snippet_relevance: float = 0.15
    min_page_relevance: float = 0.30

    # crawling / safety
    user_agent: str = "DeepSearchBot/1.0 (local research tool)"
    robots_token: str = "DeepSearchBot"
    respect_robots: bool = True
    domain_delay: float = 1.0
    max_html_bytes: int = 3_000_000
    max_redirects: int = 5
    allow_private_network: bool = False            # SSRF guard; keep False in production

    # search providers
    disable_duckduckgo: bool = False
    brave_api_key: str = ""
    bing_api_key: str = ""
    google_api_key: str = ""
    google_cse_id: str = ""
    searxng_url: str = ""

    cors_origins: str = "http://localhost:5173"

    @classmethod
    def from_env(cls) -> "Settings":
        db_url = _env("DATABASE_URL", "sqlite:///./deepsearch.db")
        db_path = db_url.replace("sqlite:///", "", 1) if db_url.startswith("sqlite:///") else db_url
        return cls(
            ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
            ollama_model=_env("OLLAMA_MODEL", "reader-lm:1.5b"),
            synthesis_model=_env("OLLAMA_SYNTHESIS_MODEL", ""),
            llm_timeout=float(_env("LLM_TIMEOUT", "600")),
            llm_retries=int(_env("LLM_RETRIES", "2")),
            llm_concurrency=int(_env("LLM_CONCURRENCY", "1")),
            llm_num_ctx=int(_env("LLM_NUM_CTX", "8192")),
            llm_num_predict=int(_env("LLM_NUM_PREDICT", "4096")),
            chunk_chars=int(_env("CHUNK_CHARS", "6000")),
            synth_max_chars=int(_env("SYNTH_MAX_CHARS", "9000")),
            database_path=db_path,
            cache_ttl_hours=int(_env("CACHE_TTL_HOURS", "72")),
            enable_search_cache=_env_bool("ENABLE_SEARCH_CACHE", True),
            enable_content_dedup=_env_bool("ENABLE_CONTENT_DEDUPLICATION", True),
            max_depth=int(_env("MAX_RESEARCH_DEPTH", "3")),
            max_urls=int(_env("MAX_DISCOVERED_URLS", "100")),
            max_pages=int(_env("MAX_SCRAPED_PAGES", "60")),
            max_pages_per_domain=int(_env("MAX_PAGES_PER_DOMAIN", "10")),
            max_queries=int(_env("MAX_SEARCH_QUERIES", "40")),
            max_concurrent=int(_env("MAX_CONCURRENT_REQUESTS", "5")),
            request_timeout=float(_env("REQUEST_TIMEOUT", "30")),
            max_duration=int(_env("MAX_RESEARCH_DURATION", "1800")),
            coverage_threshold=float(_env("COVERAGE_THRESHOLD", "0.95")),
            respect_robots=_env_bool("RESPECT_ROBOTS", True),
            domain_delay=float(_env("DOMAIN_DELAY", "1.0")),
            allow_private_network=_env_bool("ALLOW_PRIVATE_NETWORK", False),
            disable_duckduckgo=_env_bool("DISABLE_DUCKDUCKGO", False),
            brave_api_key=_env("BRAVE_API_KEY", ""),
            bing_api_key=_env("BING_API_KEY", ""),
            google_api_key=_env("GOOGLE_API_KEY", ""),
            google_cse_id=_env("GOOGLE_CSE_ID", ""),
            searxng_url=_env("SEARXNG_URL", "").rstrip("/"),
            cors_origins=_env("CORS_ORIGINS", "http://localhost:5173"),
        )


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =====================================================================================
# 2. TEXT, URL AND SOURCE UTILITIES
# =====================================================================================

STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have how i if in into is it its "
    "of on or our s so than that the their them then there these they this those to was we were what when where "
    "which while who why will with would you your about also more most other such over per via vs not no".split()
)


def stem(word: str) -> str:
    if len(word) > 4:
        if word.endswith("ies"):
            return word[:-3] + "y"
        if word.endswith("ing"):
            return word[:-3]
        if word.endswith("ed"):
            return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 1]


TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "ref_url",
    "_hsenc", "_hsmi", "spm", "cmpid", "campaign", "trk", "share", "source", "feature", "ocid", "ncid",
}
TRACKING_PREFIXES = ("utm_", "pk_", "hsa_", "mkt_", "vero_")
SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tiff", ".mp3", ".mp4", ".avi", ".mov",
    ".wav", ".webm", ".zip", ".gz", ".tar", ".rar", ".7z", ".exe", ".dmg", ".iso", ".css", ".js", ".json",
    ".xml", ".rss", ".woff", ".woff2", ".ttf", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
}
SKIP_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com", "youtube.com", "youtu.be",
    "pinterest.com", "tiktok.com", "t.co", "bit.ly", "amazon.com", "play.google.com", "apps.apple.com",
}
SKIP_LINK_WORDS = re.compile(r"(log-?in|sign-?up|register|subscribe|cart|checkout|privacy|terms|cookie|share|print)", re.I)


def normalize_url(url: str, base: Optional[str] = None) -> Optional[str]:
    """Normalize a URL: drop fragments/tracking params, sort query, lowercase host. None if unusable."""
    try:
        url = (url or "").strip()
        if base:
            url = urljoin(base, url)
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        host = p.hostname.lower().rstrip(".")
        if ":" in host:
            host = f"[{host}]"
        port = p.port
        default = 80 if p.scheme == "http" else 443
        netloc = host if port in (None, default) else f"{host}:{port}"
        path = re.sub(r"/{2,}", "/", p.path or "/")
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        query = [
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
        ]
        query.sort()
        out = urlunparse((p.scheme, netloc, path, "", urlencode(query), ""))
        if len(out) > 2048:
            return None
        if os.path.splitext(path)[1].lower() in SKIP_EXTENSIONS:
            return None
        return out
    except ValueError:
        return None


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_skipped_domain(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in SKIP_DOMAINS)


ACADEMIC_HOSTS = (
    "arxiv.org", "nature.com", "science.org", "sciencedirect.com", "springer.com", "link.springer.com",
    "ieee.org", "ieeexplore.ieee.org", "acm.org", "dl.acm.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    "researchgate.net", "semanticscholar.org", "jstor.org", "wiley.com", "plos.org", "mdpi.com", "openreview.net",
    "aclanthology.org", "biorxiv.org", "medrxiv.org", "iopscience.iop.org", "tandfonline.com", "frontiersin.org",
)
NEWS_HOSTS = (
    "reuters.com", "bbc.com", "bbc.co.uk", "nytimes.com", "theguardian.com", "wsj.com", "bloomberg.com",
    "apnews.com", "washingtonpost.com", "ft.com", "economist.com", "wired.com", "arstechnica.com",
    "techcrunch.com", "theverge.com", "technologyreview.com", "spectrum.ieee.org", "cnbc.com", "npr.org",
)
BLOG_HOSTS = ("medium.com", "substack.com", "dev.to", "hashnode.dev", "towardsdatascience.com", "blogspot.com", "wordpress.com")
EDU_HOSTS = ("wikipedia.org", "britannica.com", "khanacademy.org", "coursera.org", "edx.org", "geeksforgeeks.org", "w3schools.com", "tutorialspoint.com")
COMMUNITY_HOSTS = ("reddit.com", "stackoverflow.com", "stackexchange.com", "quora.com", "news.ycombinator.com", "github.com", "discourse.org")

CATEGORY_CREDIBILITY = {
    "academic": 0.90, "government": 0.85, "documentation": 0.85, "institutional": 0.75,
    "educational": 0.65, "news": 0.60, "blog": 0.45, "community": 0.35, "other": 0.40,
}


def _host_match(domain: str, hosts: tuple[str, ...]) -> bool:
    return any(domain == h or domain.endswith("." + h) for h in hosts)


def classify_source(url: str) -> tuple[str, float]:
    """Heuristic source classification by domain. Returns (category, credibility 0..1)."""
    d = domain_of(url)
    path = urlparse(url).path.lower()
    if _host_match(d, ACADEMIC_HOSTS):
        cat = "academic"
    elif re.search(r"\.(gov|mil)(\.[a-z]{2})?$", d) or re.search(r"\.(gob|gouv)\.[a-z]{2}$", d) or d.endswith(".europa.eu"):
        cat = "government"
    elif re.search(r"\.edu(\.[a-z]{2})?$", d) or re.search(r"\.ac\.[a-z]{2}$", d):
        cat = "institutional" if "/research" not in path and "/papers" not in path else "academic"
    elif re.match(r"^(docs|documentation|developer|developers|dev|api|learn|support|help)\.", d) or "readthedocs" in d \
            or path.startswith(("/docs", "/documentation", "/reference")):
        cat = "documentation"
    elif _host_match(d, NEWS_HOSTS):
        cat = "news"
    elif _host_match(d, EDU_HOSTS):
        cat = "educational"
    elif _host_match(d, BLOG_HOSTS) or d.startswith("blog.") or "/blog" in path:
        cat = "blog"
    elif _host_match(d, COMMUNITY_HOSTS):
        cat = "community"
    elif d.endswith(".int") or (d.endswith(".org") and re.search(r"(institute|foundation|society|association|academy|lab)", d)):
        cat = "institutional"
    else:
        cat = "other"
    return cat, CATEGORY_CREDIBILITY[cat]


class Relevance:
    """Lexical relevance to the research topic, with vocabulary learned from relevant pages."""

    def __init__(self, topic: str):
        self.topic = topic
        self.topic_tokens = set(tokens(topic)) or {topic.lower()}
        self.phrase = " ".join(re.findall(r"[a-z0-9]+", topic.lower()))
        self.related: set[str] = set()

    def clean_term(self, term: str) -> Optional[str]:
        """Remove the topic itself from a candidate subtopic term; None if nothing new remains."""
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in self.phrase.split()) + r"s?\b" if self.phrase else None
        out = re.sub(pattern, " ", term, flags=re.I) if pattern else term
        out = re.sub(r"^(?:and|of|in|for|the|a|an|with|on|to|vs|versus)\s+|\s+(?:and|of|in|for|the|with|on|to)$", "", _squash(out).strip(" :-–—,."), flags=re.I)
        toks = set(tokens(out))
        if len(out) < 4 or not toks or toks <= self.topic_tokens:
            return None
        return out

    def learn(self, terms: list[str]) -> None:
        for t in terms:
            for tok in tokens(t):
                if tok not in self.topic_tokens and len(self.related) < 300:
                    self.related.add(tok)

    def text_score(self, text: str) -> float:
        toks = tokens(text)
        if not toks:
            return 0.0
        present = set(toks)
        coverage = len(self.topic_tokens & present) / len(self.topic_tokens)
        norm = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
        phrase = 1.0 if self.phrase and self.phrase in norm else 0.0
        density = min(1.0, sum(1 for t in toks if t in self.topic_tokens) / len(toks) * 20)
        related = min(1.0, len(self.related & present) / 10) if self.related else 0.0
        return 0.45 * coverage + 0.20 * phrase + 0.15 * density + 0.20 * related


# =====================================================================================
# 3. PERSISTENCE (SQLite)
# =====================================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, topic TEXT NOT NULL, config_json TEXT, status TEXT, phase TEXT,
    plan_json TEXT, state_json TEXT, markdown TEXT, error TEXT, stats_json TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS queries (
    job_id TEXT, query TEXT, section TEXT, subtopic TEXT, depth INTEGER,
    status TEXT DEFAULT 'pending', result_count INTEGER DEFAULT 0,
    PRIMARY KEY (job_id, query)
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, url TEXT, title TEXT, domain TEXT, author TEXT,
    pub_date TEXT, retrieved_at TEXT, category TEXT, credibility REAL, relevance REAL, score REAL,
    section TEXT, subtopic TEXT, depth INTEGER, status TEXT, reason TEXT, parent_url TEXT, snippet TEXT,
    query TEXT, ref INTEGER, fetched INTEGER DEFAULT 0, dup_blocks INTEGER DEFAULT 0,
    blocks_json TEXT, md TEXT, UNIQUE (job_id, url)
);
CREATE INDEX IF NOT EXISTS idx_sources_job ON sources (job_id, status);
CREATE TABLE IF NOT EXISTS page_cache (url TEXT PRIMARY KEY, data_json TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS search_cache (key TEXT PRIMARY KEY, results_json TEXT, ts REAL);
"""


class Database:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self.conn.close()


# =====================================================================================
# 4. SEARCH (multi-provider)
# =====================================================================================

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


class SearchError(Exception):
    pass


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    provider: str
    query: str
    date: Optional[str] = None
    rank: int = 0


def _ddg_unwrap(href: str) -> Optional[str]:
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if p.netloc.endswith("duckduckgo.com"):
        if p.path.startswith("/l/"):
            target = dict(parse_qsl(p.query)).get("uddg")
            return unquote(target) if target else None
        return None  # ads / internal pages
    return href if p.scheme in ("http", "https") else None


class SearchEngine:
    """Queries every configured provider concurrently and merges the results."""

    def __init__(self, settings: Settings, db: Database):
        self.cfg = settings
        self.db = db
        self.client = httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=True)
        self._ddg_lock = asyncio.Lock()
        self._ddg_last = 0.0

    @property
    def providers(self) -> list[str]:
        c = self.cfg
        out = []
        if not c.disable_duckduckgo:
            out.append("duckduckgo")
        if c.brave_api_key:
            out.append("brave")
        if c.bing_api_key:
            out.append("bing")
        if c.google_api_key and c.google_cse_id:
            out.append("google")
        if c.searxng_url:
            out.append("searxng")
        return out

    async def close(self) -> None:
        await self.client.aclose()

    async def search(self, query: str, per_provider: int = 10) -> tuple[list[SearchResult], dict[str, Any]]:
        providers = self.providers
        if not providers:
            raise SearchError("No search provider is configured.")
        outcomes = await asyncio.gather(*[self._cached(p, query, per_provider) for p in providers], return_exceptions=True)
        merged: dict[str, SearchResult] = {}
        info: dict[str, Any] = {}
        for provider, outcome in zip(providers, outcomes):
            if isinstance(outcome, Exception):
                info[provider] = f"error: {outcome}"
                log.warning("search provider %s failed for %r: %s", provider, query, outcome)
                continue
            info[provider] = len(outcome)
            for r in outcome:
                key = normalize_url(r.url)
                if key and key not in merged:
                    merged[key] = r
        if not merged and all(isinstance(o, Exception) for o in outcomes):
            raise SearchError("; ".join(str(v) for v in info.values()))
        return list(merged.values()), info

    async def _cached(self, provider: str, query: str, n: int) -> list[SearchResult]:
        key = hashlib.sha1(f"{provider}|{query}".encode()).hexdigest()
        if self.cfg.enable_search_cache:
            row = self.db.one("SELECT results_json, ts FROM search_cache WHERE key=?", (key,))
            if row and time.time() - row["ts"] < self.cfg.search_cache_ttl_hours * 3600:
                return [SearchResult(**r) for r in json.loads(row["results_json"])]
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                results = await getattr(self, f"_{provider}")(query, n)
                break
            except (httpx.HTTPError, SearchError) as exc:
                last_error = exc
                await asyncio.sleep(1.5 * (2 ** attempt))
        else:
            raise SearchError(f"{provider}: {last_error}")
        if self.cfg.enable_search_cache and results:
            self.db.execute(
                "INSERT OR REPLACE INTO search_cache (key, results_json, ts) VALUES (?,?,?)",
                (key, json.dumps([r.__dict__ for r in results]), time.time()),
            )
        return results

    async def _duckduckgo(self, query: str, n: int) -> list[SearchResult]:
        async with self._ddg_lock:  # DDG throttles aggressively: one request at a time, spaced out
            wait = self._ddg_last + 1.2 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self.client.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=BROWSER_HEADERS)
            finally:
                self._ddg_last = time.monotonic()
        if r.status_code != 200:
            raise SearchError(f"HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")
        results = []
        for div in soup.select("div.result"):
            if "result--ad" in (div.get("class") or []):
                continue
            a = div.select_one("a.result__a")
            if not a:
                continue
            url = _ddg_unwrap(a.get("href", ""))
            if not url:
                continue
            sn = div.select_one(".result__snippet")
            results.append(SearchResult(url, a.get_text(" ", strip=True), sn.get_text(" ", strip=True) if sn else "",
                                        "duckduckgo", query, rank=len(results)))
            if len(results) >= n:
                break
        if not results and re.search(r"anomaly|captcha|unusual traffic", r.text, re.I):
            raise SearchError("blocked by DuckDuckGo anti-bot check")
        return results

    async def _brave(self, query: str, n: int) -> list[SearchResult]:
        r = await self.client.get(
            "https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": min(n, 20)},
            headers={"X-Subscription-Token": self.cfg.brave_api_key, "Accept": "application/json"},
        )
        if r.status_code != 200:
            raise SearchError(f"HTTP {r.status_code}")
        items = (r.json().get("web") or {}).get("results") or []
        return [SearchResult(i["url"], i.get("title", ""), htmllib.unescape(re.sub(r"<[^>]+>", "", i.get("description", ""))),
                             "brave", query, i.get("page_age") or i.get("age"), rank) for rank, i in enumerate(items) if i.get("url")]

    async def _bing(self, query: str, n: int) -> list[SearchResult]:
        r = await self.client.get(
            "https://api.bing.microsoft.com/v7.0/search", params={"q": query, "count": min(n, 50)},
            headers={"Ocp-Apim-Subscription-Key": self.cfg.bing_api_key},
        )
        if r.status_code != 200:
            raise SearchError(f"HTTP {r.status_code}")
        items = (r.json().get("webPages") or {}).get("value") or []
        return [SearchResult(i["url"], i.get("name", ""), i.get("snippet", ""), "bing", query, i.get("datePublished"), rank)
                for rank, i in enumerate(items) if i.get("url")]

    async def _google(self, query: str, n: int) -> list[SearchResult]:
        r = await self.client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": self.cfg.google_api_key, "cx": self.cfg.google_cse_id, "q": query, "num": min(n, 10)},
        )
        if r.status_code != 200:
            raise SearchError(f"HTTP {r.status_code}")
        return [SearchResult(i["link"], i.get("title", ""), i.get("snippet", ""), "google", query, rank=rank)
                for rank, i in enumerate(r.json().get("items") or []) if i.get("link")]

    async def _searxng(self, query: str, n: int) -> list[SearchResult]:
        r = await self.client.get(f"{self.cfg.searxng_url}/search", params={"q": query, "format": "json"})
        if r.status_code != 200:
            raise SearchError(f"HTTP {r.status_code}")
        return [SearchResult(i["url"], i.get("title", ""), i.get("content", ""), "searxng", query, i.get("publishedDate"), rank)
                for rank, i in enumerate((r.json().get("results") or [])[:n]) if i.get("url")]


# =====================================================================================
# 5. FETCHING (SSRF-safe, robots-aware, rate-limited)
# =====================================================================================

class FetchError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Retry(Exception):
    def __init__(self, delay: float):
        self.delay = delay


class Fetcher:
    def __init__(self, settings: Settings):
        self.cfg = settings
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=min(10.0, settings.request_timeout)),
            follow_redirects=False, headers={"User-Agent": settings.user_agent, "Accept-Language": "en"},
        )
        self.global_sem = asyncio.Semaphore(settings.max_concurrent)
        self._domain_sems: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(2))
        self._domain_next: dict[str, float] = {}
        self._robots: dict[str, Optional[robotparser.RobotFileParser]] = {}
        self._robots_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def _validate(self, url: str) -> None:
        """SSRF guard: http(s) only; every resolved address must be globally routable."""
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise FetchError("invalid_url")
        if self.cfg.allow_private_network:
            return
        host = p.hostname
        try:
            ips = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
            except socket.gaierror:
                raise FetchError("dns_error")
            ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
        if not ips or any(not ip.is_global for ip in ips):
            raise FetchError("blocked_private_address")

    async def _robots_ok(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        async with self._robots_lock:
            if origin not in self._robots:
                self._robots[origin] = await self._load_robots(origin)
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.cfg.robots_token, url)

    async def _load_robots(self, origin: str) -> Optional[robotparser.RobotFileParser]:
        rp = robotparser.RobotFileParser()
        try:
            await self._validate(origin + "/robots.txt")
            r = await self.client.get(origin + "/robots.txt", timeout=10, follow_redirects=True)
        except (FetchError, httpx.HTTPError):
            return None
        if r.status_code in (401, 403):
            rp.disallow_all = True      # same convention as urllib's RobotFileParser.read()
            return rp
        if r.status_code != 200:
            return None
        rp.parse(r.text.splitlines())
        return rp

    async def _pace(self, host: str) -> None:
        now = time.monotonic()
        slot = max(now, self._domain_next.get(host, 0.0))
        self._domain_next[host] = slot + self.cfg.domain_delay
        if slot > now:
            await asyncio.sleep(slot - now)

    async def _get_once(self, url: str) -> tuple[int, httpx.Headers, bytes]:
        host = urlparse(url).hostname or ""
        attempt = 0
        while True:
            try:
                async with self.global_sem, self._domain_sems[host]:
                    await self._pace(host)
                    async with self.client.stream(
                        "GET", url, headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.2"}
                    ) as r:
                        status = r.status_code
                        if status in (301, 302, 303, 307, 308):
                            return status, r.headers, b""
                        if status in (429, 503):
                            retry_after = r.headers.get("retry-after", "")
                            delay = min(float(retry_after), 15.0) if retry_after.isdigit() else 2.0 * (2 ** attempt)
                            raise _Retry(delay)
                        if status >= 400:
                            raise FetchError(f"http_{status}")
                        ctype = r.headers.get("content-type", "").lower()
                        if ctype and not any(t in ctype for t in ("text/html", "application/xhtml")):
                            raise FetchError("unsupported_content_type")
                        declared = r.headers.get("content-length", "")
                        if declared.isdigit() and int(declared) > self.cfg.max_html_bytes:
                            raise FetchError("too_large")
                        body = bytearray()
                        async for chunk in r.aiter_bytes():
                            body += chunk
                            if len(body) > self.cfg.max_html_bytes:
                                raise FetchError("too_large")
                        return status, r.headers, bytes(body)
            except _Retry as retry:
                if attempt >= 2:
                    raise FetchError("rate_limited")
                attempt += 1
                await asyncio.sleep(retry.delay)
            except httpx.TimeoutException:
                if attempt >= 1:
                    raise FetchError("timeout")
                attempt += 1
            except httpx.HTTPError:
                if attempt >= 1:
                    raise FetchError("network_error")
                attempt += 1
                await asyncio.sleep(1.0)

    async def fetch(self, url: str) -> tuple[str, str]:
        """Fetch HTML. Returns (final_url, html). Redirect targets are re-validated at every hop."""
        current = url
        for _ in range(self.cfg.max_redirects + 1):
            await self._validate(current)
            if self.cfg.respect_robots and not await self._robots_ok(current):
                raise FetchError("blocked_by_robots")
            status, headers, body = await self._get_once(current)
            if status in (301, 302, 303, 307, 308):
                nxt = normalize_url(headers.get("location", ""), base=current)
                if not nxt:
                    raise FetchError("bad_redirect")
                current = nxt
                continue
            if not body.strip():
                raise FetchError("empty_response")
            m = re.search(r"charset=([\w\-]+)", headers.get("content-type", ""), re.I)
            try:
                return current, body.decode(m.group(1) if m else "utf-8", errors="replace")
            except LookupError:
                return current, body.decode("utf-8", errors="replace")
        raise FetchError("too_many_redirects")


# =====================================================================================
# 6. CONTENT EXTRACTION
# =====================================================================================

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
BLOCK_TAGS = HEADING_TAGS + ("p", "ul", "ol", "pre", "blockquote", "table", "dl")
CONTAINER_TAGS = {"ul", "ol", "pre", "blockquote", "table", "dl"}
BOILERPLATE_TAGS = {"nav", "header", "footer", "aside", "form", "noscript", "script", "style", "iframe"}
BOILERPLATE_ATTR = re.compile(r"(nav|menu|footer|sidebar|cookie|breadcrumb|social|share|advert|promo|comment)", re.I)
REFERENCE_HEADING = re.compile(r"(reference|citation|bibliograph|further reading|see also|sources|external links|related work)", re.I)
GENERIC_HEADINGS = re.compile(
    r"^(references?|external links|see also|contents?|table of contents|notes?|further reading|bibliography|citations?|"
    r"sources?|footnotes?|navigation menu|related (articles|posts)|share|comments?|contact|about|summary|conclusions?|"
    r"introduction|overview|abstract|acknowledg(e)?ments?|search|menu|home|faq|history|background)$", re.I)


def parse_fragment(fragment: str) -> Optional[Tag]:
    body = BeautifulSoup(fragment, "lxml").body
    return body.find(True, recursive=False) if body else None


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _fallback_main_html(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(list(BOILERPLATE_TAGS)):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body
    return str(root) if root else ""


def _blocks_from_html(clean_html: str) -> list[dict]:
    soup = BeautifulSoup(clean_html, "lxml")
    root = soup.body or soup
    blocks = []
    for el in root.find_all(list(BLOCK_TAGS)):
        if any(p.name in CONTAINER_TAGS for p in el.parents if isinstance(p, Tag)):
            continue
        text = _squash(el.get_text(" ", strip=True))
        if text:
            blocks.append({"t": el.name, "h": str(el), "x": text})
    if not blocks:  # no block structure: fall back to paragraphs of plain text
        for para in re.split(r"\n{2,}", root.get_text("\n")):
            para = _squash(para)
            if len(para) > 40:
                blocks.append({"t": "p", "h": f"<p>{htmllib.escape(para)}</p>", "x": para})
    return blocks


def _discover_links(soup: BeautifulSoup, page_url: str) -> list[dict]:
    page_norm = normalize_url(page_url)
    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        ancestors = list(itertools.islice((p for p in a.parents if isinstance(p, Tag)), 6))
        if any(p.name in BOILERPLATE_TAGS for p in ancestors):
            continue
        if any(BOILERPLATE_ATTR.search(" ".join(p.get("class") or []) + " " + (p.get("id") or "")) for p in ancestors[:4]):
            continue
        url = normalize_url(href, base=page_url)
        if not url or url == page_norm or is_skipped_domain(domain_of(url)) or SKIP_LINK_WORDS.search(urlparse(url).path):
            continue
        anchor = _squash(a.get_text(" ", strip=True))[:200]
        prev = a.find_previous(list(HEADING_TAGS))
        is_ref = bool(prev and REFERENCE_HEADING.search(prev.get_text(" ", strip=True)))
        parent = a.parent if isinstance(a.parent, Tag) else None
        context = _squash(parent.get_text(" ", strip=True))[:240] if parent else ""
        existing = found.get(url)
        if not existing or len(anchor) > len(existing["anchor"]):
            found[url] = {"url": url, "anchor": anchor, "context": context, "ref": is_ref or bool(existing and existing["ref"])}
    return list(found.values())


def extract_page(html: str, url: str) -> Optional[dict]:
    """Trafilatura-first extraction (+ BeautifulSoup for links/fallback). Returns a JSON-serialisable dict."""
    meta = None
    try:
        meta = trafilatura.extract_metadata(html, default_url=url, extensive=False)  # no guessed dates
    except Exception:  # noqa: BLE001
        log.debug("metadata extraction failed for %s", url)
    clean = None
    try:
        clean = trafilatura.extract(
            html, url=url, output_format="html", include_tables=True, include_links=True,
            include_formatting=True, include_comments=False, favor_recall=True)
    except Exception:  # noqa: BLE001
        log.debug("trafilatura failed for %s", url)
    soup = BeautifulSoup(html, "lxml")
    links = _discover_links(soup, url)
    if not clean:
        clean = _fallback_main_html(BeautifulSoup(html, "lxml"))
    blocks = _blocks_from_html(clean) if clean else []
    if not blocks or sum(len(b["x"]) for b in blocks) < 200:
        return None
    title = (getattr(meta, "title", None) or (soup.title.get_text(" ", strip=True) if soup.title else "") or domain_of(url))
    desc = getattr(meta, "description", None) or ""
    if not desc:
        tag = soup.find("meta", attrs={"name": "description"})
        desc = tag.get("content", "") if tag else ""
    return {
        "url": url, "title": _squash(title)[:300], "author": getattr(meta, "author", None) or "",
        "date": getattr(meta, "date", None) or "", "description": _squash(desc)[:500],
        "blocks": blocks, "headings": [b["x"] for b in blocks if b["t"] in HEADING_TAGS], "links": links,
    }


def extract_terms(headings: list[str], blocks: list[dict]) -> dict[str, float]:
    """Candidate subtopic terms: section headings plus frequent content bigrams."""
    terms: dict[str, float] = {}
    for h in headings:
        h = re.sub(r"\[.*?\]|\(.*?\)", "", h).strip(" :-–—.")
        words = h.split()
        if 1 <= len(words) <= 5 and len(h) >= 4 and re.search(r"[A-Za-z]{3}", h) and not GENERIC_HEADINGS.match(h):
            terms[h.lower()] = max(terms.get(h.lower(), 0), 1.0)
    text = " ".join(b["x"] for b in blocks)
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text.lower())]
    bigrams = Counter(f"{a} {b}" for a, b in zip(words, words[1:]) if a not in STOPWORDS and b not in STOPWORDS)
    for gram, count in bigrams.most_common(12):
        if count >= 3:
            terms.setdefault(gram, 0.5)
    return terms


# =====================================================================================
# 7. DEDUPLICATION (exact + MinHash/LSH near-duplicate)
# =====================================================================================

_MH_PRIME = (1 << 61) - 1


class Deduper:
    PERMS, BANDS, ROWS = 64, 16, 4

    def __init__(self, enabled: bool = True, threshold: float = 0.85):
        rnd = random.Random(1337)
        self.params = [(rnd.randrange(1, _MH_PRIME), rnd.randrange(0, _MH_PRIME)) for _ in range(self.PERMS)]
        self.enabled = enabled
        self.threshold = threshold
        self.exact: set[str] = set()
        self.sigs: list[list[int]] = []
        self.buckets: list[dict[tuple, list[int]]] = [defaultdict(list) for _ in range(self.BANDS)]

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\W+", " ", text.lower()).strip()

    def _signature(self, norm: str) -> list[int]:
        words = norm.split()
        shingles = {zlib.crc32(" ".join(words[i:i + 4]).encode()) for i in range(max(1, len(words) - 3))}
        return [min((a * h + b) % _MH_PRIME for h in shingles) for a, b in self.params]

    def check(self, text: str) -> Optional[str]:
        """Return 'exact' / 'near' if this text duplicates earlier text; otherwise register it and return None."""
        norm = self._norm(text)
        key = hashlib.sha1(norm.encode()).hexdigest()
        if key in self.exact:
            return "exact"
        self.exact.add(key)
        if len(norm.split()) < 12:
            return None
        sig = self._signature(norm)
        seen: set[int] = set()
        for b in range(self.BANDS):
            band = tuple(sig[b * self.ROWS:(b + 1) * self.ROWS])
            for idx in self.buckets[b].get(band, ()):
                if idx in seen:
                    continue
                seen.add(idx)
                if sum(x == y for x, y in zip(sig, self.sigs[idx])) / self.PERMS >= self.threshold:
                    return "near"
        idx = len(self.sigs)
        self.sigs.append(sig)
        for b in range(self.BANDS):
            self.buckets[b][tuple(sig[b * self.ROWS:(b + 1) * self.ROWS])].append(idx)
        return None

    def filter_blocks(self, blocks: list[dict]) -> tuple[list[dict], dict]:
        """Drop blocks already seen from other sources. Headings and very short blocks are always kept."""
        kept, exact, near, total_words, kept_words = [], 0, 0, 0, 0
        for b in blocks:
            words = len(b["x"].split())
            total_words += words
            verdict = None
            if self.enabled and b["t"] not in HEADING_TAGS and words >= 8:
                verdict = self.check(b["x"])
            if verdict == "exact":
                exact += 1
            elif verdict == "near":
                near += 1
            else:
                kept.append(b)
                kept_words += words
        dup_ratio = 1 - kept_words / total_words if total_words else 0.0
        return kept, {"exact": exact, "near": near, "dup_ratio": dup_ratio, "novelty": 1 - dup_ratio}


# =====================================================================================
# 8. CHUNKING + DETERMINISTIC HTML->MARKDOWN (fallback / extractive mode)
# =====================================================================================

def _split_text(text: str, limit: int) -> list[str]:
    parts, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > limit:  # hard split on whitespace; nothing is dropped
            cut = sentence.rfind(" ", 0, limit) or limit
            piece, sentence = sentence[:cut], sentence[cut:].lstrip()
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(piece)
        if len(cur) + len(sentence) + 1 > limit and cur:
            parts.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        parts.append(cur)
    return parts


def split_block(block: dict, limit: int) -> list[dict]:
    """Split an oversized block into valid smaller blocks (rows / items / sentences) without losing content."""
    if len(block["h"]) <= limit:
        return [block]
    el = parse_fragment(block["h"])
    tag = block["t"]
    out: list[dict] = []
    if el is not None and tag in ("table", "ul", "ol"):
        if tag == "table":
            rows = el.find_all("tr")
            header = str(rows[0]) if rows else ""
            items, wrap = [str(r) for r in rows[1:]] or [str(r) for r in rows], ("<table>", "</table>")
            prefix = header
        else:
            items, wrap, prefix = [str(li) for li in el.find_all("li", recursive=False)], (f"<{tag}>", f"</{tag}>"), ""
        group: list[str] = []
        size = len(prefix)
        for item in items + [None]:
            if item is None or (group and size + len(item) > limit):
                if group:
                    h = wrap[0] + prefix + "".join(group) + wrap[1]
                    out.append({"t": tag, "h": h, "x": _squash(BeautifulSoup(h, "lxml").get_text(" ", strip=True))})
                group, size = [], len(prefix)
            if item is not None:
                group.append(item)
                size += len(item)
        if out:
            return out
    text = block["x"] if tag != "pre" or el is None else el.get_text()
    wrap_tag = "pre" if tag == "pre" else "p"
    for piece in _split_text(text, int(limit * 0.8)):
        out.append({"t": wrap_tag, "h": f"<{wrap_tag}>{htmllib.escape(piece)}</{wrap_tag}>", "x": _squash(piece)})
    return out


def make_chunks(blocks: list[dict], limit: int) -> list[str]:
    """Pack blocks into HTML chunks of at most `limit` chars, splitting oversized blocks."""
    chunks, cur, size = [], [], 0
    for block in blocks:
        for part in split_block(block, limit):
            if cur and size + len(part["h"]) > limit:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(part["h"])
            size += len(part["h"]) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _inline_md(node: Any) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    inner = "".join(_inline_md(c) for c in node.children)
    name = node.name
    if name in ("strong", "b") and inner.strip():
        return f"**{inner.strip()}**"
    if name in ("em", "i") and inner.strip():
        return f"*{inner.strip()}*"
    if name == "code":
        return f"`{inner.strip()}`"
    if name == "a" and node.get("href") and inner.strip():
        return f"[{inner.strip()}]({node['href']})"
    if name == "br":
        return "  \n"
    return inner


def _element_to_md(el: Tag) -> str:
    n = el.name
    if n in HEADING_TAGS:
        return "#" * int(n[1]) + " " + _squash(_inline_md(el))
    if n in ("ul", "ol"):
        lines = []
        for i, li in enumerate(el.find_all("li", recursive=False), 1):
            lines.append(("- " if n == "ul" else f"{i}. ") + _squash(_inline_md(li)))
        return "\n".join(lines)
    if n == "pre":
        return "```\n" + el.get_text().strip("\n") + "\n```"
    if n == "blockquote":
        return "\n".join("> " + line for line in _squash(_inline_md(el)).split("\n"))
    if n == "table":
        rows = [[_squash(_inline_md(c)).replace("|", "\\|") for c in tr.find_all(["th", "td"])] for tr in el.find_all("tr")]
        rows = [r for r in rows if r]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        md = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(md)
    if n == "dl":
        return "\n".join(_squash(_inline_md(c)) for c in el.find_all(["dt", "dd"]))
    return _squash(_inline_md(el))


def blocks_to_markdown(blocks: list[dict]) -> str:
    out = []
    for b in blocks:
        el = parse_fragment(b["h"])
        md = _element_to_md(el) if el is not None else b["x"]
        if md.strip():
            out.append(md)
    return "\n\n".join(out)


def html_chunk_to_markdown(chunk: str) -> str:
    body = BeautifulSoup(chunk, "lxml").body
    if body is None:
        return ""
    parts = [_element_to_md(el) for el in body.find_all(True, recursive=False)]
    return "\n\n".join(p for p in parts if p.strip())


def looks_loopy(text: str) -> bool:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return len(lines) >= 6 and len(set(lines)) / len(lines) < 0.4


# =====================================================================================
# 9. OLLAMA (local LLM only)
# =====================================================================================

class LLMError(Exception):
    pass


class OllamaClient:
    def __init__(self, settings: Settings):
        self.cfg = settings
        self.client = httpx.AsyncClient(
            base_url=settings.ollama_base_url,
            timeout=httpx.Timeout(settings.llm_timeout, connect=10.0),
        )
        self.sem = asyncio.Semaphore(max(1, settings.llm_concurrency))

    async def close(self) -> None:
        await self.client.aclose()

    async def health(self) -> dict:
        try:
            r = await self.client.get("/api/tags", timeout=5.0)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            needed = {self.cfg.ollama_model, self.cfg.synthesis_model or self.cfg.ollama_model}
            have = lambda n: any(m == n or m.split(":")[0] == n or m == n + ":latest" for m in names)  # noqa: E731
            return {"reachable": True, "models": names, "missing": [n for n in needed if not have(n)]}
        except (httpx.HTTPError, ValueError) as exc:
            return {"reachable": False, "error": str(exc), "models": [], "missing": []}

    def _payload(self, model: str, messages: list[dict], stream: bool, temperature: float = 0.0) -> dict:
        return {
            "model": model, "messages": messages, "stream": stream, "keep_alive": "10m",
            "options": {"temperature": temperature, "repeat_penalty": 1.08,
                        "num_ctx": self.cfg.llm_num_ctx, "num_predict": self.cfg.llm_num_predict},
        }

    async def chat(self, model: str, messages: list[dict]) -> str:
        last: Optional[Exception] = None
        for attempt in range(self.cfg.llm_retries + 1):
            try:
                async with self.sem:
                    r = await self.client.post("/api/chat", json=self._payload(model, messages, False))
                if r.status_code != 200:
                    raise LLMError(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
                content = (r.json().get("message") or {}).get("content")
                if not isinstance(content, str):
                    raise LLMError("Ollama returned an unexpected response format")
                return content
            except (httpx.HTTPError, LLMError, ValueError) as exc:
                last = exc
                await asyncio.sleep(1.5 * (2 ** attempt))
        raise LLMError(str(last))

    async def stream(self, model: str, messages: list[dict]) -> AsyncIterator[str]:
        async with self.sem:
            try:
                async with self.client.stream("POST", "/api/chat", json=self._payload(model, messages, True)) as r:
                    if r.status_code != 200:
                        raise LLMError(f"Ollama HTTP {r.status_code}")
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if data.get("error"):
                            raise LLMError(str(data["error"]))
                        piece = (data.get("message") or {}).get("content")
                        if piece:
                            yield piece
                        if data.get("done"):
                            return
            except (httpx.HTTPError, ValueError) as exc:
                raise LLMError(str(exc))


READER_SYSTEM = os.getenv("STAGE1_SYSTEM_PROMPT", "Extract the main content from the given HTML and convert it to Markdown format.")

SYNTH_SYSTEM = (
    'You are a careful research writer. Write the "{section}" part of a research report on "{topic}" using ONLY the '
    "evidence the user provides.\n"
    "Rules:\n"
    "- Use only facts present in the evidence. Add no outside knowledge.\n"
    "- Keep numbers, units, dates and names exactly as written.\n"
    "- After each claim keep the source tag it came from, for example [S3].\n"
    "- If two sources disagree, state both claims with their tags.\n"
    "- Write clean Markdown: paragraphs, bullet lists, and tables when comparing. Use ### for sub-headings only.\n"
    "- Do not write a title or top-level heading. Do not wrap the answer in a code block."
)
REDUCE_SYSTEM = (
    'Condense the evidence below into a shorter, faithful summary for the report section "{section}" on "{topic}". '
    "Keep every distinct fact, number, unit, name and source tag such as [S3]. Add nothing that is not in the evidence. "
    "Output Markdown only."
)


class CitationLinker:
    """Streaming filter that turns [S3] tags into clickable [[3]](url) links."""

    TAG = re.compile(r"\[S(\d+)\]")

    def __init__(self, urls: dict[int, str]):
        self.urls = urls
        self.buf = ""

    def _sub(self, m: re.Match) -> str:
        ref = int(m.group(1))
        return f"[[{ref}]]({self.urls[ref]})" if ref in self.urls else m.group(0)

    def feed(self, text: str) -> str:
        self.buf += text
        cut = len(self.buf)
        m = re.search(r"\[S?\d*$", self.buf)
        if m:
            cut = m.start()
        ready, self.buf = self.buf[:cut], self.buf[cut:]
        return self.TAG.sub(self._sub, ready)

    def flush(self) -> str:
        out, self.buf = self.TAG.sub(self._sub, self.buf), ""
        return out


class HeadingGuard:
    """Streaming filter: demotes Markdown headings emitted by the model to #### so the report outline stays intact.
    Lines inside fenced code blocks are left untouched."""

    def __init__(self):
        self.pending, self.bol, self.fences, self.tail = "", True, 0, ""

    def feed(self, text: str) -> str:
        self.fences += (self.tail + text).count("```")
        self.tail = text[-2:]
        out = []
        for ch in text:
            if self.bol and ch == "#" and self.fences % 2 == 0:
                self.pending += ch
                continue
            if self.pending:
                out.append("#### " if ch == " " else self.pending)
                self.pending = "" if ch != " " else ""
                if ch == " ":
                    self.bol = False
                    continue
            self.bol = ch == "\n"
            out.append(ch)
        return "".join(out)

    def flush(self) -> str:
        out, self.pending = self.pending, ""
        return out


def demote_headings(text: str) -> str:
    return re.sub(r"(?m)^#{1,6}\s+", "#### ", text)


def grounding_check(output: str, evidence: str) -> tuple[bool, str]:
    """Cheap hallucination guard: output vocabulary and numbers must come from the evidence."""
    out = re.sub(r"https?://\S+|\[S\d+(?:\s*,\s*S?\d+)*\]|\[\[\d+\]\]\([^)]*\)", " ", output)
    out = re.sub(r"^\s*\d+[.)]\s", " ", out, flags=re.M)
    if len(out.strip()) < 40:
        return False, "output too short"
    words = {stem(w) for w in re.findall(r"[a-z]{4,}", out.lower())}
    ev_words = {stem(w) for w in re.findall(r"[a-z]{4,}", evidence.lower())}
    if words and len(words & ev_words) / len(words) < 0.6:
        return False, "vocabulary not found in evidence"
    nums = set(re.findall(r"\d+(?:[.,]\d+)*", out))
    ev_nums = set(re.findall(r"\d+(?:[.,]\d+)*", evidence))
    if len(nums) >= 3 and len(nums - ev_nums) / len(nums) > 0.25:
        return False, "numbers not found in evidence"
    return True, "ok"


# =====================================================================================
# 10. PLANNING
# =====================================================================================

SECTION_TITLES = {
    "intro": "Introduction", "fundamentals": "Fundamentals", "detail": "Detailed Topic Breakdown",
    "technical": "Technical Analysis", "applications": "Applications and Use Cases",
    "advantages": "Advantages and Limitations", "research": "Research Findings", "comparison": "Comparative Analysis",
    "facts": "Important Facts and Statistics", "challenges": "Challenges and Limitations",
    "future": "Future Developments", "gaps": "Knowledge Gaps", "conclusion": "Conclusion", "references": "References",
}
DIMENSIONS: dict[str, list[str]] = {
    "intro": ["{t} overview introduction", "what is {t} definition"],
    "fundamentals": ["{t} fundamentals basic concepts terminology", "{t} history and evolution"],
    "technical": ["{t} architecture algorithms how it works", "{t} technical specifications implementation"],
    "applications": ["{t} applications use cases", "{t} industry case study"],
    "advantages": ["{t} advantages and limitations"],
    "research": ["{t} recent research survey paper", "{t} experimental results findings"],
    "comparison": ["{t} comparison with alternatives"],
    "challenges": ["{t} challenges and risks"],
    "future": ["{t} future trends roadmap"],
}
MODEL_SECTIONS = ["intro", "fundamentals", "technical", "applications", "advantages", "research", "comparison", "challenges", "future"]
REPORT_ORDER = ["intro", "fundamentals", "detail", "technical", "applications", "advantages", "research",
                "comparison", "facts", "challenges", "future", "gaps", "conclusion", "references"]


def build_plan(topic: str) -> dict:
    topic = _squash(topic)
    words = topic.split()
    terminology = {topic.lower()}
    if len(words) > 1:
        terminology.add("".join(w[0] for w in words if w[0].isalpha()).upper())
    terminology.add(topic.lower().rstrip("s") if topic.lower().endswith("s") else topic.lower() + "s")
    queries = [
        {"query": tpl.format(t=topic), "section": key, "subtopic": SECTION_TITLES[key], "depth": 0}
        for key, tpls in DIMENSIONS.items() for tpl in tpls
    ]
    return {
        "objective": f"Build a comprehensive, source-grounded overview of: {topic}",
        "topic": topic,
        "terminology": sorted(terminology),
        "subtopics": [SECTION_TITLES[k] for k in DIMENSIONS],
        "research_questions": [
            f"What is {topic} and why does it matter?", f"How does {topic} work?",
            f"What are the main applications of {topic}?", f"What are the limitations and open problems of {topic}?",
            f"How is {topic} expected to evolve?",
        ],
        "queries": queries,
    }


# =====================================================================================
# 11. JOB RUNTIME + ORCHESTRATOR
# =====================================================================================

class JobRuntime:
    """In-memory event log for a running job (state itself is persisted in SQLite)."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.events: list[tuple[str, dict]] = []
        self.task: Optional[asyncio.Task] = None
        self.finished = False
        self.user_cancelled = False

    def emit(self, type_: str, **data: Any) -> None:
        self.events.append((type_, {"type": type_, "ts": utcnow(), **data}))


class AppState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.search = SearchEngine(settings, self.db)
        self.fetcher = Fetcher(settings)
        self.ollama = OllamaClient(settings)
        self.runtimes: dict[str, JobRuntime] = {}

    async def close(self) -> None:
        await asyncio.gather(self.search.close(), self.fetcher.close(), self.ollama.close(), return_exceptions=True)
        self.db.close()


class Orchestrator:
    def __init__(self, app: AppState, job_id: str, rt: JobRuntime):
        self.app, self.db, self.id, self.rt = app, app.db, job_id, rt
        job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            raise KeyError(job_id)
        self.topic = job["topic"]
        self.cfg = replace(app.settings, **json.loads(job["config_json"] or "{}"))
        self.relevance = Relevance(self.topic)
        self.dedup = Deduper(self.cfg.enable_content_dedup)
        self.frontier: list[tuple[float, int, int]] = []
        self._tick = itertools.count()
        self.terms: dict[str, dict] = {}
        self.used_terms: set[str] = set()
        self.domain_counts: Counter = Counter()
        self.pages_processed = 0
        self.novelty: list[float] = []
        self.low_yield = 0
        self.next_ref = 1
        self.started = time.monotonic()
        self.synth_model = self.cfg.synthesis_model or self.cfg.ollama_model

    # ---- small helpers ------------------------------------------------------------
    def emit(self, type_: str, **data: Any) -> None:
        self.rt.emit(type_, **data)

    def set_job(self, **fields_: Any) -> None:
        fields_["updated_at"] = utcnow()
        cols = ", ".join(f"{k}=?" for k in fields_)
        self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields_.values(), self.id))

    def set_phase(self, phase: str) -> None:
        self.set_job(phase=phase)

    def save_state(self) -> None:
        self.set_job(state_json=json.dumps({
            "terms": self.terms, "used_terms": sorted(self.used_terms), "related": sorted(self.relevance.related),
            "low_yield": self.low_yield,
        }))

    def source_count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM sources WHERE job_id=?", (self.id,))["n"]

    def query_count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM queries WHERE job_id=?", (self.id,))["n"]

    # ---- state restore (fresh start and resume share this path) --------------------
    def load_state(self) -> None:
        job = self.db.one("SELECT state_json FROM jobs WHERE id=?", (self.id,))
        if job and job["state_json"]:
            st = json.loads(job["state_json"])
            self.terms = st.get("terms", {})
            self.used_terms = set(st.get("used_terms", []))
            self.relevance.related = set(st.get("related", []))
            self.low_yield = st.get("low_yield", 0)
        for row in self.db.query("SELECT id, score, status, fetched, domain, blocks_json, ref FROM sources WHERE job_id=? ORDER BY id", (self.id,)):
            if row["status"] == "queued":
                heapq.heappush(self.frontier, (-(row["score"] or 0), next(self._tick), row["id"]))
            if row["fetched"]:
                self.pages_processed += 1
                self.domain_counts[row["domain"]] += 1
        kept = self.db.query("SELECT ref, blocks_json FROM sources WHERE job_id=? AND status='kept' ORDER BY ref", (self.id,))
        for row in kept:
            self.dedup.filter_blocks(json.loads(row["blocks_json"] or "[]"))
            self.next_ref = max(self.next_ref, (row["ref"] or 0) + 1)

    # ---- top-level run -------------------------------------------------------------
    async def run(self) -> None:
        try:
            self.set_job(status="running", error=None)
            self.emit("research_started", job_id=self.id, topic=self.topic, limits={
                "max_depth": self.cfg.max_depth, "max_urls": self.cfg.max_urls, "max_pages": self.cfg.max_pages,
                "max_queries": self.cfg.max_queries, "max_duration": self.cfg.max_duration,
                "providers": self.app.search.providers, "model": self.cfg.ollama_model})
            health = await self.app.ollama.health()
            if not health["reachable"]:
                raise RuntimeError(f"Ollama is not reachable at {self.cfg.ollama_base_url}: {health.get('error')}")
            if health["missing"]:
                raise RuntimeError(f"Ollama model(s) not installed: {', '.join(health['missing'])} (run `ollama pull ...`)")
            self.load_state()
            await self.plan()
            await self.crawl()
            await self.process_sources()
            markdown = await self.generate()
            stats = self.stats()
            self.set_job(status="completed", phase="done", markdown=markdown, stats_json=json.dumps(stats))
            self.emit("done", status="completed", job_id=self.id, stats=stats)
        except asyncio.CancelledError:
            status = "cancelled" if self.rt.user_cancelled else "interrupted"
            self.save_state()
            self.set_job(status=status)
            self.emit("done", status=status, job_id=self.id, stats=self.stats())
        except Exception as exc:  # noqa: BLE001
            log.exception("research job %s failed", self.id)
            self.save_state()
            self.set_job(status="failed", error=str(exc))
            self.emit("error", job_id=self.id, message=str(exc))
        finally:
            self.rt.finished = True

    def stats(self) -> dict:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM sources WHERE job_id=? GROUP BY status", (self.id,))
        by = {r["status"]: r["n"] for r in rows}
        return {"discovered": sum(by.values()), "kept": by.get("kept", 0), "rejected": by.get("rejected", 0),
                "failed": by.get("failed", 0), "queued": by.get("queued", 0), "queries": self.query_count(),
                "pages_processed": self.pages_processed, "duration_s": round(time.monotonic() - self.started, 1)}

    # ---- planning -------------------------------------------------------------------
    async def plan(self) -> None:
        self.set_phase("planning")
        job = self.db.one("SELECT plan_json FROM jobs WHERE id=?", (self.id,))
        plan = json.loads(job["plan_json"]) if job and job["plan_json"] else None
        if not plan:
            plan = build_plan(self.topic)
            self.set_job(plan_json=json.dumps(plan))
            for q in plan["queries"][: self.cfg.max_queries]:
                self.db.execute("INSERT OR IGNORE INTO queries (job_id, query, section, subtopic, depth) VALUES (?,?,?,?,?)",
                                (self.id, q["query"], q["section"], q["subtopic"], q["depth"]))
        self.emit("planning", objective=plan["objective"], subtopics=plan["subtopics"], terminology=plan["terminology"],
                  research_questions=plan["research_questions"], queries=[q["query"] for q in plan["queries"]])

    # ---- search ---------------------------------------------------------------------
    def score_candidate(self, text_rel: float, cred: float, depth: int, domain: str, parent_rel: float = 1.0,
                        is_ref: bool = False, date: Optional[str] = None) -> float:
        domain_novelty = 1 / (1 + self.domain_counts[domain])
        recency = 0.0
        m = re.match(r"(\d{4})", date or "")
        if m:
            recency = max(0.0, 1 - (datetime.now().year - int(m.group(1))) / 10)
        return (0.40 * text_rel + 0.20 * parent_rel * text_rel + 0.15 * cred + 0.10 * domain_novelty
                + 0.10 / (1 + depth) + 0.03 * recency + 0.02 * (1 if is_ref else 0))

    async def run_query(self, q: dict) -> list[dict]:
        self.emit("search_started", query=q["query"], section=q["section"], depth=q["depth"])
        try:
            results, info = await self.app.search.search(q["query"])
        except SearchError as exc:
            self.db.execute("UPDATE queries SET status='failed' WHERE job_id=? AND query=?", (self.id, q["query"]))
            self.emit("search_results", query=q["query"], count=0, providers={}, error=str(exc))
            return []
        cands = []
        for r in results:
            url = normalize_url(r.url)
            if not url or is_skipped_domain(domain_of(url)):
                continue
            rel = self.relevance.text_score(f"{r.title} {r.snippet}")
            if rel < self.cfg.min_snippet_relevance:
                continue
            cat, cred = classify_source(url)
            cands.append({
                "url": url, "title": r.title, "snippet": r.snippet, "query": q["query"], "section": q["section"],
                "subtopic": q["subtopic"], "depth": q["depth"], "parent": None, "date": r.date, "cat": cat, "cred": cred,
                "rel": rel, "score": self.score_candidate(rel, cred, q["depth"], domain_of(url), date=r.date)})
        self.db.execute("UPDATE queries SET status='done', result_count=? WHERE job_id=? AND query=?",
                        (len(results), self.id, q["query"]))
        self.emit("search_results", query=q["query"], count=len(results), relevant=len(cands), providers=info,
                  results=[{"url": c["url"], "title": c["title"], "score": round(c["score"], 3)} for c in cands[:10]])
        return cands

    def add_candidates(self, cands: list[dict]) -> int:
        """Insert best-scoring candidates (respecting max_urls) and push them onto the priority queue."""
        added = 0
        room = self.cfg.max_urls - self.source_count()
        for c in sorted(cands, key=lambda c: c["score"], reverse=True):
            if room <= 0:
                break
            cur = self.db.execute(
                "INSERT OR IGNORE INTO sources (job_id, url, title, domain, category, credibility, relevance, score, section, "
                "subtopic, depth, status, parent_url, snippet, query, pub_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?)",
                (self.id, c["url"], c["title"], domain_of(c["url"]), c["cat"], c["cred"], c["rel"], c["score"], c["section"],
                 c["subtopic"], c["depth"], c["parent"], c["snippet"][:500], c["query"], c.get("date")))
            if cur.rowcount:
                added += 1
                room -= 1
                heapq.heappush(self.frontier, (-c["score"], next(self._tick), cur.lastrowid))
                self.emit("url_discovered", url=c["url"], title=c["title"], depth=c["depth"], score=round(c["score"], 3),
                          category=c["cat"], via=c["query"] if not c["parent"] else c["parent"])
        return added

    async def run_queries(self, queries: list[dict]) -> int:
        sem = asyncio.Semaphore(3)

        async def one(q: dict) -> list[dict]:
            async with sem:
                return await self.run_query(q)

        batches = await asyncio.gather(*[one(q) for q in queries])
        return self.add_candidates([c for b in batches for c in b])

    # ---- query expansion from discovered terms --------------------------------------
    def register_terms(self, found: dict[str, float], page_rel: float, depth: int) -> None:
        for raw, weight in found.items():
            term = self.relevance.clean_term(raw)
            if not term:
                continue
            term = term.lower()
            cur = self.terms.get(term)
            w = weight * page_rel
            if cur:
                cur["w"] += w
                cur["depth"] = min(cur["depth"], depth)
            else:
                self.terms[term] = {"w": w, "depth": depth}

    async def expand(self) -> int:
        remaining = self.cfg.max_queries - self.query_count()
        if remaining <= 0 or self.source_count() >= self.cfg.max_urls:
            return 0
        picks = [(t, v) for t, v in sorted(self.terms.items(), key=lambda kv: kv[1]["w"], reverse=True)
                 if t not in self.used_terms and v["depth"] + 1 <= self.cfg.max_depth and v["w"] >= 0.5]
        picks = picks[: min(self.cfg.expand_per_wave, remaining)]
        if not picks:
            return 0
        queries = []
        for term, v in picks:
            self.used_terms.add(term)
            text = f"{self.topic} {term}"
            q = {"query": text, "section": "detail", "subtopic": term.title(), "depth": v["depth"] + 1}
            self.db.execute("INSERT OR IGNORE INTO queries (job_id, query, section, subtopic, depth) VALUES (?,?,?,?,?)",
                            (self.id, q["query"], q["section"], q["subtopic"], q["depth"]))
            queries.append(q)
        added = await self.run_queries(queries)
        self.low_yield = self.low_yield + 1 if added <= 1 else 0
        self.emit("recursion", kind="query_expansion", new_queries=[q["query"] for q in queries], new_urls=added,
                  max_depth=self.cfg.max_depth)
        return len(queries)

    # ---- crawling --------------------------------------------------------------------
    def pop_batch(self, n: int) -> list[dict]:
        batch = []
        while self.frontier and len(batch) < n:
            _, _, sid = heapq.heappop(self.frontier)
            row = self.db.one("SELECT * FROM sources WHERE id=?", (sid,))
            if row and row["status"] == "queued":
                batch.append(row)
        return batch

    def coverage(self) -> float:
        rows = self.db.query("SELECT section, COUNT(*) AS n FROM sources WHERE job_id=? AND status='kept' GROUP BY section", (self.id,))
        by = {r["section"]: r["n"] for r in rows}
        dims = sum(1 for k in DIMENSIONS if by.get(k, 0) >= 2) / len(DIMENSIONS)
        detail_subs = self.db.one("SELECT COUNT(DISTINCT subtopic) AS n FROM sources WHERE job_id=? AND status='kept' AND section='detail'",
                                  (self.id,))["n"]
        return 0.6 * dims + 0.4 * min(1.0, detail_subs / 6)

    def stop_reason(self) -> Optional[str]:
        if time.monotonic() - self.started > self.cfg.max_duration:
            return "maximum research duration reached"
        if self.pages_processed >= self.cfg.max_pages:
            return "maximum page budget reached"
        if self.coverage() >= self.cfg.coverage_threshold:
            return "research coverage threshold reached"
        if len(self.novelty) >= 6 and sum(self.novelty[-6:]) / 6 < self.cfg.low_novelty_threshold:
            return "additional pages add little new information"
        if self.low_yield >= 2 and not self.frontier:
            return "new searches produce very few relevant sources"
        return None

    async def crawl(self) -> None:
        self.set_phase("discovery")
        pending = self.db.query("SELECT query, section, subtopic, depth FROM queries WHERE job_id=? AND status='pending'", (self.id,))
        if pending:
            await self.run_queries(pending)
            self.save_state()
        wave = 0
        while True:
            reason = self.stop_reason()
            if reason:
                self.emit("recursion", kind="stop", reason=reason)
                break
            batch = self.pop_batch(min(self.cfg.max_concurrent * 2, self.cfg.max_pages - self.pages_processed))
            if not batch:
                if not await self.expand():
                    self.emit("recursion", kind="stop", reason="no more relevant sources to explore")
                    break
                continue
            wave += 1
            results = await asyncio.gather(*[self.process_source(src) for src in batch])
            wave_novelty = [n for n in results if n is not None]
            if wave_novelty:
                self.novelty.extend(wave_novelty)
            await self.expand()
            self.save_state()
            self.emit("recursion", kind="wave", wave=wave, queued=len(self.frontier), coverage=round(self.coverage(), 2),
                      pages_processed=self.pages_processed)

    async def get_page(self, url: str) -> dict:
        row = self.db.one("SELECT data_json, fetched_at FROM page_cache WHERE url=?", (url,))
        if row and time.time() - row["fetched_at"] < self.cfg.cache_ttl_hours * 3600:
            return json.loads(row["data_json"])
        final_url, html = await self.app.fetcher.fetch(url)
        page = await asyncio.to_thread(extract_page, html, final_url)
        if page is None:
            raise FetchError("empty_page")
        self.db.execute("INSERT OR REPLACE INTO page_cache (url, data_json, fetched_at) VALUES (?,?,?)",
                        (url, json.dumps(page), time.time()))
        return page

    def reject(self, src: dict, status: str, reason: str, **extra: Any) -> None:
        self.db.execute("UPDATE sources SET status=?, reason=?, fetched=1, retrieved_at=? WHERE id=?",
                        (status, reason, utcnow(), src["id"]))
        self.emit("page_extracted", url=src["url"], accepted=False, status=status, reason=reason, **extra)

    async def process_source(self, src: dict) -> Optional[float]:
        domain = src["domain"]
        if self.domain_counts[domain] >= self.cfg.max_pages_per_domain:
            self.db.execute("UPDATE sources SET status='rejected', reason='domain_limit' WHERE id=?", (src["id"],))
            self.emit("page_extracted", url=src["url"], accepted=False, status="rejected", reason="domain_limit")
            return None
        self.domain_counts[domain] += 1
        self.pages_processed += 1
        self.emit("scraping", url=src["url"], depth=src["depth"], title=src["title"])
        try:
            page = await self.get_page(src["url"])
        except FetchError as exc:
            self.reject(src, "failed", exc.reason)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("unexpected error processing %s: %s", src["url"], exc)
            self.reject(src, "failed", "unexpected_error")
            return None

        text_sample = " ".join([page["title"], page["description"], " ".join(page["headings"][:30]),
                                " ".join(b["x"] for b in page["blocks"][:25])[:4000]])
        rel = self.relevance.text_score(text_sample)
        if rel < self.cfg.min_page_relevance:
            self.reject(src, "rejected", "low_relevance", relevance=round(rel, 3))
            return None
        kept_blocks, dd = self.dedup.filter_blocks(page["blocks"])
        if not kept_blocks or dd["dup_ratio"] >= 0.8:
            self.reject(src, "rejected", "duplicate_content", relevance=round(rel, 3))
            return dd["novelty"]
        cat, cred = classify_source(src["url"])
        ref = self.next_ref
        self.next_ref += 1
        self.db.execute(
            "UPDATE sources SET status='kept', ref=?, title=?, author=?, pub_date=?, category=?, credibility=?, relevance=?, "
            "fetched=1, dup_blocks=?, retrieved_at=?, blocks_json=? WHERE id=?",
            (ref, page["title"] or src["title"], page["author"], page["date"] or src["pub_date"], cat, cred, rel,
             dd["exact"] + dd["near"], utcnow(), json.dumps(kept_blocks), src["id"]))
        self.emit("page_extracted", url=src["url"], accepted=True, ref=ref, title=page["title"], category=cat,
                  relevance=round(rel, 3), blocks=len(kept_blocks), depth=src["depth"], section=src["section"])
        if dd["exact"] or dd["near"]:
            self.emit("deduplication", url=src["url"], exact_removed=dd["exact"], near_removed=dd["near"],
                      novelty=round(dd["novelty"], 2))

        # ----- recursive discovery -----
        self.relevance.learn(page["headings"][:15])
        self.register_terms(extract_terms(page["headings"], page["blocks"]), rel, src["depth"])
        if src["depth"] < self.cfg.max_depth:
            cands = []
            for link in page["links"]:
                if is_skipped_domain(domain_of(link["url"])):
                    continue
                link_rel = self.relevance.text_score(f"{link['anchor']} {link['context']}")
                if link_rel < self.cfg.min_snippet_relevance and not (link["ref"] and rel >= 0.5 and len(link["anchor"]) > 15):
                    continue
                lcat, lcred = classify_source(link["url"])
                cands.append({
                    "url": link["url"], "title": link["anchor"], "snippet": link["context"], "query": f"link from {src['url']}",
                    "section": src["section"], "subtopic": src["subtopic"], "depth": src["depth"] + 1, "parent": src["url"],
                    "date": None, "cat": lcat, "cred": lcred, "rel": link_rel,
                    "score": self.score_candidate(link_rel, lcred, src["depth"] + 1, domain_of(link["url"]), rel, link["ref"])})
            cands.sort(key=lambda c: c["score"], reverse=True)
            added = self.add_candidates(cands[: self.cfg.max_links_per_page])
            if added:
                self.emit("recursion", kind="links", url=src["url"], depth=src["depth"] + 1, new_urls=added)
        return dd["novelty"]

    # ---- Stage 1: Reader-LM HTML -> Markdown ----------------------------------------
    async def convert_chunk(self, chunk: str) -> tuple[str, bool]:
        plain_len = len(_squash(BeautifulSoup(chunk, "lxml").get_text(" ", strip=True)))
        try:
            out = await self.app.ollama.chat(self.cfg.ollama_model, [
                {"role": "system", "content": READER_SYSTEM}, {"role": "user", "content": chunk}])
        except LLMError as exc:
            log.warning("Reader-LM call failed: %s", exc)
            return html_chunk_to_markdown(chunk), False
        out = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", out.strip())
        ratio = len(out) / max(1, plain_len)
        if not out or ratio < 0.4 or ratio > 3.0 or looks_loopy(out) or re.search(r"</?(html|body|div|span)\b", out):
            return html_chunk_to_markdown(chunk), False
        return out, True

    async def process_sources(self) -> None:
        self.set_phase("processing")
        todo = self.db.query("SELECT id, ref, url, blocks_json FROM sources WHERE job_id=? AND status='kept' AND md IS NULL ORDER BY ref", (self.id,))
        total, fallbacks = len(todo), 0
        for i, src in enumerate(todo, 1):
            chunks = make_chunks(json.loads(src["blocks_json"]), self.cfg.chunk_chars)
            parts = []
            for j, chunk in enumerate(chunks, 1):
                self.emit("llm_processing", stage="source_processing", ref=src["ref"], url=src["url"], source=f"{i}/{total}",
                          chunk=f"{j}/{len(chunks)}", model=self.cfg.ollama_model)
                md, used_llm = await self.convert_chunk(chunk)
                fallbacks += 0 if used_llm else 1
                parts.append(md)
            self.db.execute("UPDATE sources SET md=? WHERE id=?", ("\n\n".join(parts), src["id"]))
        if total:
            self.emit("llm_processing", stage="source_processing_done", sources=total, deterministic_fallback_chunks=fallbacks)

    # ---- Stage 2-4: organise + synthesise + stream Markdown --------------------------
    @staticmethod
    def _item_text(item: dict) -> str:
        text = re.sub(r"(?m)^#{1,6}\s+(.*)$", r"**\1**", item["text"])  # keep source headings out of the outline
        return f"### [S{item['ref']}] {item['title']} ({item['domain']})\n{text}\n\n" if item.get("ref") else text + "\n\n"

    def pack_items(self, items: list[dict]) -> list[str]:
        budget = self.cfg.synth_max_chars
        packs, cur = [], ""
        for item in items:
            header_len = len(self._item_text({**item, "text": ""}))
            for part in self._split_item_text(item["text"], budget - header_len - 20):
                piece = self._item_text({**item, "text": part})
                if cur and len(cur) + len(piece) > budget:
                    packs.append(cur)
                    cur = ""
                cur += piece
        if cur:
            packs.append(cur)
        return packs

    @staticmethod
    def _split_item_text(text: str, limit: int) -> list[str]:
        limit = max(limit, 500)
        if len(text) <= limit:
            return [text]
        parts, cur = [], ""
        for para in re.split(r"\n{2,}", text):
            for piece in ([para] if len(para) <= limit else _split_text(para, limit)):
                if cur and len(cur) + len(piece) + 2 > limit:
                    parts.append(cur)
                    cur = ""
                cur = f"{cur}\n\n{piece}" if cur else piece
        if cur:
            parts.append(cur)
        return parts

    async def reduce_packs(self, section: str, packs: list[str]) -> list[str]:
        """Hierarchical reduction when the evidence needs too many packs for one section."""
        level = 0
        while len(packs) > self.cfg.synth_max_packs and level < 3:
            level += 1
            before = sum(len(p) for p in packs)
            reduced = []
            for i, pack in enumerate(packs, 1):
                self.emit("llm_processing", stage="hierarchical_reduce", section=section, level=level, part=f"{i}/{len(packs)}")
                try:
                    out = await self.app.ollama.chat(self.synth_model, [
                        {"role": "system", "content": REDUCE_SYSTEM.format(section=section, topic=self.topic)},
                        {"role": "user", "content": pack}])
                except LLMError:
                    out = ""
                ok, _ = grounding_check(out, pack) if out else (False, "")
                reduced.append(out.strip() if ok and len(out) < len(pack) else pack)
            packs = self.pack_items([{"text": r} for r in reduced])
            if sum(len(p) for p in packs) >= before:
                break
        return packs

    async def stream_section(self, section: str, items: list[dict], urls: dict[int, str]) -> AsyncIterator[str]:
        if not items:
            yield "_No relevant sources were collected for this section._\n\n"
            return
        packs = await self.reduce_packs(section, self.pack_items(items))
        for i, pack in enumerate(packs, 1):
            self.emit("llm_processing", stage="synthesis", section=section, part=f"{i}/{len(packs)}", model=self.synth_model)
            linker, guard, raw, failed = CitationLinker(urls), HeadingGuard(), "", False
            try:
                async for piece in self.app.ollama.stream(self.synth_model, [
                        {"role": "system", "content": SYNTH_SYSTEM.format(section=section, topic=self.topic)},
                        {"role": "user", "content": f"Evidence:\n\n{pack}\nWrite the section now."}]):
                    raw += piece
                    out = linker.feed(guard.feed(piece))
                    if out:
                        yield out
                    if len(raw) % 400 < len(piece) and looks_loopy(raw[-1500:]):
                        failed = True
                        break
                tail = linker.feed(guard.flush()) + linker.flush()
                if tail:
                    yield tail
            except LLMError as exc:
                log.warning("synthesis failed for %s: %s", section, exc)
                failed = True
            ok, why = grounding_check(raw, pack) if raw and not failed else (False, "generation failed")
            if not ok:
                # Never leave the user with an ungrounded or empty section: append the verbatim evidence.
                yield (f"\n\n> **Note:** the model output for this part did not pass grounding checks ({why}); "
                       f"the collected evidence is reproduced below.\n\n")
                body = re.sub(r"\[S(\d+)\]", lambda m: f"[[{m.group(1)}]]({urls[int(m.group(1))]})" if int(m.group(1)) in urls else m.group(0), pack)
                yield demote_headings(body)
            yield "\n\n"
        refs = sorted({it["ref"] for it in items if it.get("ref")})
        yield "**Sources:** " + ", ".join(f"[{r}]({urls[r]})" for r in refs if r in urls) + "\n\n"

    SECTION_LEXICON = {
        "intro": ("introduction", "overview", "definition", "refers to", "is a ", "known as", "defined as"),
        "fundamentals": ("history", "evolution", "origin", "concept", "terminology", "principle", "basics", "fundamental", "founded", "early"),
        "technical": ("architecture", "algorithm", "implementation", "module", "stack", "protocol", "pipeline", "specification",
                      "component", "design", "equation", "model", "sensor", "software", "hardware", "mechanism"),
        "applications": ("application", "use case", "deployed", "industry", "used in", "used for", "case study", "real-world",
                         "commercial", "adoption", "deployment", "customers"),
        "advantages": ("advantage", "benefit", "limitation", "drawback", "pros", "cons", "trade-off", "tradeoff", "strength", "weakness"),
        "research": ("study", "research", "experiment", "results", "findings", "paper", "survey", "evaluation", "benchmark",
                     "researchers", "trial", "dataset"),
        "comparison": ("compared", "comparison", "versus", " vs ", "alternative", "difference", "unlike", "whereas", "outperform"),
        "challenges": ("challenge", "risk", "problem", "safety", "regulation", "ethical", "cost", "barrier", "difficult", "fail",
                       "unsolved", "adverse", "hard in"),
        "future": ("future", "roadmap", "emerging", "expected", "next generation", "trend", "forecast", "outlook", "upcoming", "by 20"),
    }

    def classify_paragraph(self, text: str, prior: str) -> str:
        low = text.lower()
        scores = {k: sum(low.count(w) for w in words) + (1.0 if k == prior else 0.0) for k, words in self.SECTION_LEXICON.items()}
        best = max(scores, key=lambda k: scores[k])
        return best if scores[best] >= 1.0 else prior

    def organize(self, sources: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        """Stage 2: group Reader-LM Markdown by subtopic. Dimension pages are split paragraph-by-paragraph; pages found
        for a discovered subtopic stay together under that subtopic. Nothing is dropped, only regrouped."""
        sections: dict[str, dict[int, dict]] = defaultdict(dict)
        details: dict[str, list[dict]] = defaultdict(list)
        for s in sources:
            base = {"ref": s["ref"], "title": s["title"], "domain": s["domain"]}
            if s["section"] == "detail" or s["section"] not in self.SECTION_LEXICON:
                details[s["subtopic"]].append({**base, "text": s["md"] or ""})
                continue
            pending_headings: list[str] = []
            for para in re.split(r"\n{2,}", s["md"] or ""):
                para = para.strip()
                if not para:
                    continue
                if re.match(r"#{1,6}\s", para):
                    pending_headings.append(para)
                    continue
                key = self.classify_paragraph(" ".join(pending_headings[-1:] + [para]), s["section"])
                entry = sections[key].setdefault(s["ref"], {**base, "text": ""})
                entry["text"] += ("\n\n".join(pending_headings) + "\n\n" if pending_headings else "") + para + "\n\n"
                pending_headings = []
        return {k: list(v.values()) for k, v in sections.items()}, details

    def extract_facts(self, sources: list[dict], limit: int = 25) -> list[tuple[float, str, int]]:
        pattern = re.compile(
            r"(%|\b(?:19|20)\d{2}\b|[$€£]\s?\d|\b\d+(?:[.,]\d+)?\s?(?:km|m|cm|mm|kg|g|mph|kph|km/h|hz|ghz|mhz|gb|mb|tb|kw|kwh|"
            r"million|billion|thousand|trillion|years?|hours?|percent|times)\b)", re.I)
        seen, found = set(), []
        for s in sources:
            for b in json.loads(s["blocks_json"] or "[]"):
                if b["t"] in HEADING_TAGS:
                    continue
                for sent in re.split(r"(?<=[.!?])\s+", b["x"]):
                    if 40 <= len(sent) <= 320 and pattern.search(sent):
                        key = re.sub(r"\W+", " ", sent.lower()).strip()
                        if key not in seen:
                            seen.add(key)
                            found.append((self.relevance.text_score(sent) + 0.3 * s["credibility"], sent.strip(), s["ref"]))
        return sorted(found, key=lambda f: f[0], reverse=True)[:limit]

    async def generate(self) -> str:
        self.set_phase("generation")
        self.emit("generation_started", topic=self.topic)
        sources = self.db.query("SELECT * FROM sources WHERE job_id=? AND status='kept' ORDER BY ref", (self.id,))
        urls = {s["ref"]: s["url"] for s in sources}
        if not sources:
            raise RuntimeError("No relevant sources could be collected for this topic; nothing to report.")
        by_section, detail_subtopics = self.organize(sources)
        self.emit("deduplication", stage="organized", sections={k: len(v) for k, v in by_section.items()},
                  subtopics=list(detail_subtopics))
        md_parts: list[str] = []

        async def out(text: str) -> None:
            md_parts.append(text)
            self.emit("token", text=text)

        await out(f"# {self.topic}\n\n")
        number = 0
        for key in REPORT_ORDER:
            number += 1
            title = SECTION_TITLES[key]
            await out(f"## {number}. {title}\n\n")
            if key in MODEL_SECTIONS:
                async for piece in self.stream_section(title, by_section.get(key, []), urls):
                    await out(piece)
            elif key == "detail":
                subtopics = detail_subtopics
                if not subtopics:
                    await out("_No additional subtopics were discovered within the research budget._\n\n")
                for sub, items in sorted(subtopics.items(), key=lambda kv: -len(kv[1])):
                    await out(f"### {sub}\n\n")
                    async for piece in self.stream_section(sub, items, urls):
                        await out(piece)
            elif key == "facts":
                facts = self.extract_facts(sources)
                await out("\n".join(f"- {sent} [[{ref}]]({urls[ref]})" for _, sent, ref in facts) + "\n\n" if facts
                          else "_No quantitative statements were found in the collected sources._\n\n")
            elif key == "gaps":
                await out(self.knowledge_gaps(sources, by_section))
            elif key == "conclusion":
                await out(self.conclusion(sources, by_section))
            elif key == "references":
                lines = []
                for s in sources:
                    meta = ", ".join(x for x in (s["domain"], s["category"], f"published {s['pub_date']}" if s["pub_date"] else "",
                                                 f"retrieved {s['retrieved_at'][:10]}") if x)
                    lines.append(f"{s['ref']}. [{(s['title'] or s['url']).replace('[', '(').replace(']', ')')}]({s['url']}) — {meta}")
                await out("\n".join(lines) + "\n")
        return "".join(md_parts)

    def knowledge_gaps(self, sources: list[dict], by_section: dict[str, list[dict]]) -> str:
        lines = []
        thin = [SECTION_TITLES[k] for k in DIMENSIONS if len(by_section.get(k, [])) < 2]
        if thin:
            lines.append("- **Thinly covered sections** (fewer than two sources): " + ", ".join(thin) + ".")
        rows = self.db.query("SELECT reason, COUNT(*) AS n FROM sources WHERE job_id=? AND status IN ('failed','rejected') GROUP BY reason ORDER BY n DESC", (self.id,))
        if rows:
            lines.append("- **Sources not used:** " + "; ".join(f"{r['reason'].replace('_', ' ')} ({r['n']})" for r in rows) + ".")
        unexplored = self.db.one("SELECT COUNT(*) AS n FROM sources WHERE job_id=? AND status='queued'", (self.id,))["n"]
        if unexplored:
            lines.append(f"- **{unexplored} discovered sources were not fetched** because the research budget ended first.")
        lines.append("- **Conflicting claims:** the system does not automatically detect contradictions. Non-duplicate claims from "
                     "different sources are all retained with their source tags, so disagreements must be checked by comparing the cited sources.")
        lines.append("- **Unsupported content:** PDFs, paywalled, login-gated, and robots-restricted pages are skipped by design.")
        return "\n".join(lines) + "\n\n"

    def conclusion(self, sources: list[dict], by_section: dict[str, list[dict]]) -> str:
        cats = Counter(s["category"] for s in sources)
        subs = sorted({s["subtopic"] for s in sources if s["section"] == "detail"})  # discovered subtopics
        st = self.stats()
        text = (f"This report on **{self.topic}** consolidates {len(sources)} sources "
                f"({', '.join(f'{n} {c}' for c, n in cats.most_common())}) gathered through {st['queries']} search queries and "
                f"recursive link and term discovery. ")
        if subs:
            text += "Subtopics examined beyond the core overview: " + ", ".join(subs[:15]) + ". "
        text += ("Every statement above is attributed to the numbered sources listed in the References section; "
                 "see *Knowledge Gaps* for what remains unverified or uncovered.")
        return text + "\n\n"


# =====================================================================================
# 12. JOB MANAGEMENT HELPERS
# =====================================================================================

OVERRIDABLE = {"max_depth", "max_urls", "max_pages", "max_queries", "max_duration"}


def create_job(app: AppState, topic: str, overrides: dict) -> str:
    job_id = uuid.uuid4().hex
    cfg = {k: v for k, v in overrides.items() if k in OVERRIDABLE and v is not None}
    now = utcnow()
    app.db.execute("INSERT INTO jobs (id, topic, config_json, status, phase, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                   (job_id, _squash(topic), json.dumps(cfg), "queued", "queued", now, now))
    return job_id


def start_job(app: AppState, job_id: str) -> JobRuntime:
    rt = JobRuntime(job_id)
    app.runtimes[job_id] = rt
    rt.task = asyncio.create_task(Orchestrator(app, job_id, rt).run())
    return rt


# =====================================================================================
# 13. FASTAPI APPLICATION
# =====================================================================================

class ResearchRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=300)
    max_depth: Optional[int] = Field(None, ge=0, le=6)
    max_urls: Optional[int] = Field(None, ge=1, le=2000)
    max_pages: Optional[int] = Field(None, ge=1, le=500)
    max_queries: Optional[int] = Field(None, ge=1, le=200)
    max_duration: Optional[int] = Field(None, ge=30, le=86400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = AppState(Settings.from_env())
    state.db.execute("UPDATE jobs SET status='interrupted' WHERE status IN ('running','queued')")
    app.state.deep = state
    yield
    for rt in state.runtimes.values():
        if rt.task and not rt.task.done():
            rt.task.cancel()
    await asyncio.sleep(0.2)
    await state.close()


app = FastAPI(title="DEEPSEARCH", description="Recursive web research engine (local Ollama)", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _env("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


def _state(request: Request) -> AppState:
    return request.app.state.deep


def _job_or_404(state: AppState, job_id: str) -> dict:
    job = state.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "Research job not found.")
    return job


def _running(state: AppState, job_id: str) -> bool:
    rt = state.runtimes.get(job_id)
    return bool(rt and rt.task and not rt.task.done())


@app.get("/api/health")
async def health(request: Request):
    state = _state(request)
    ollama = await state.ollama.health()
    return {"status": "online" if ollama["reachable"] and not ollama["missing"] else "degraded", "service": "DEEPSEARCH",
            "ollama": ollama, "model": state.settings.ollama_model, "search_providers": state.search.providers}


@app.post("/api/research", status_code=202)
async def start_research(body: ResearchRequest, request: Request):
    state = _state(request)
    if not state.search.providers:
        raise HTTPException(503, "No search provider configured.")
    job_id = create_job(state, body.topic, body.model_dump(exclude={"topic"}))
    start_job(state, job_id)
    return {"job_id": job_id, "status": "queued", "stream_url": f"/api/research/{job_id}/stream"}


@app.get("/api/research")
async def list_research(request: Request):
    rows = _state(request).db.query("SELECT id, topic, status, phase, created_at, updated_at FROM jobs ORDER BY created_at DESC LIMIT 100")
    return {"jobs": rows}


@app.get("/api/research/{job_id}")
async def research_status(job_id: str, request: Request):
    state = _state(request)
    job = _job_or_404(state, job_id)
    counts = {r["status"]: r["n"] for r in state.db.query(
        "SELECT status, COUNT(*) AS n FROM sources WHERE job_id=? GROUP BY status", (job_id,))}
    return {
        "job_id": job_id, "topic": job["topic"], "status": job["status"], "phase": job["phase"], "error": job["error"],
        "created_at": job["created_at"], "updated_at": job["updated_at"], "config": json.loads(job["config_json"] or "{}"),
        "sources": counts, "queries": state.db.one("SELECT COUNT(*) AS n FROM queries WHERE job_id=?", (job_id,))["n"],
        "stats": json.loads(job["stats_json"]) if job["stats_json"] else None, "markdown": job["markdown"],
    }


@app.get("/api/research/{job_id}/sources")
async def research_sources(job_id: str, request: Request, status: Optional[str] = None):
    state = _state(request)
    _job_or_404(state, job_id)
    sql = ("SELECT ref AS source_id, url, title, domain, author, pub_date AS publication_date, retrieved_at, category, "
           "relevance, credibility, section, subtopic, depth AS recursion_depth, status AS extraction_status, reason, "
           "parent_url, dup_blocks AS duplicate_blocks_removed FROM sources WHERE job_id=?")
    params: tuple = (job_id,)
    if status:
        sql += " AND status=?"
        params += (status,)
    return {"job_id": job_id, "sources": state.db.query(sql + " ORDER BY id", params)}


@app.post("/api/research/{job_id}/cancel")
async def cancel_research(job_id: str, request: Request):
    state = _state(request)
    _job_or_404(state, job_id)
    if not _running(state, job_id):
        raise HTTPException(409, "Job is not running.")
    rt = state.runtimes[job_id]
    rt.user_cancelled = True
    rt.task.cancel()
    return {"job_id": job_id, "status": "cancelling"}


@app.post("/api/research/{job_id}/resume", status_code=202)
async def resume_research(job_id: str, request: Request):
    state = _state(request)
    job = _job_or_404(state, job_id)
    if _running(state, job_id):
        raise HTTPException(409, "Job is already running.")
    if job["status"] == "completed":
        raise HTTPException(409, "Job is already completed.")
    start_job(state, job_id)
    return {"job_id": job_id, "status": "resuming", "stream_url": f"/api/research/{job_id}/stream"}


def _sse(event_id: int, type_: str, data: dict) -> str:
    return f"id: {event_id}\nevent: {type_}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/research/{job_id}/stream")
async def stream_research(job_id: str, request: Request):
    """SSE stream. Progress events and Markdown `token` events are separate event types."""
    state = _state(request)
    job = _job_or_404(state, job_id)
    rt = state.runtimes.get(job_id)
    start = int(request.headers.get("last-event-id", "0") or 0) if rt else 0

    async def gen() -> AsyncIterator[str]:
        if rt is None:  # no live runtime (e.g. after a restart): replay persisted state
            yield _sse(1, "research_started", {"type": "research_started", "job_id": job_id, "topic": job["topic"]})
            if job["status"] == "completed":
                yield _sse(2, "token", {"type": "token", "text": job["markdown"] or ""})
                yield _sse(3, "done", {"type": "done", "status": "completed", "job_id": job_id})
            elif job["status"] == "failed":
                yield _sse(2, "error", {"type": "error", "message": job["error"] or "failed"})
            else:
                yield _sse(2, "done", {"type": "done", "status": job["status"], "job_id": job_id,
                                       "hint": "POST /api/research/{id}/resume to continue"})
            return
        i, idle = start, 0.0
        while True:
            while i < len(rt.events):
                type_, data = rt.events[i]
                i += 1
                yield _sse(i, type_, data)
                if type_ in ("done", "error"):
                    return
            if rt.finished and i >= len(rt.events):
                return
            if await request.is_disconnected():
                return  # job keeps running; reconnect with Last-Event-ID to continue
            await asyncio.sleep(0.1)
            idle += 0.1
            if idle >= 15:
                idle = 0.0
                yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


# =====================================================================================
# 14. COMMAND LINE
# =====================================================================================

async def _cli_research(args: argparse.Namespace) -> int:
    state = AppState(Settings.from_env())
    try:
        job_id = create_job(state, args.topic, {"max_depth": args.depth, "max_urls": args.urls, "max_pages": args.pages,
                                                "max_queries": args.queries, "max_duration": args.duration})
        rt = start_job(state, job_id)
        shown, markdown = 0, []
        while True:
            while shown < len(rt.events):
                type_, data = rt.events[shown]
                shown += 1
                if type_ == "token":
                    markdown.append(data["text"])
                    if not args.output:
                        sys.stdout.write(data["text"])
                        sys.stdout.flush()
                else:
                    detail = {k: v for k, v in data.items() if k not in ("type", "ts", "results", "queries", "research_questions")}
                    print(f"[{type_}] {json.dumps(detail, ensure_ascii=False)[:220]}", file=sys.stderr)
                if type_ == "error":
                    return 1
            if rt.finished and shown >= len(rt.events):
                break
            await asyncio.sleep(0.1)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write("".join(markdown))
            print(f"Report written to {args.output}", file=sys.stderr)
        return 0
    finally:
        await state.close()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="DEEPSEARCH recursive web research engine")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the FastAPI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    research = sub.add_parser("research", help="run one research job and print the Markdown")
    research.add_argument("topic")
    research.add_argument("--depth", type=int)
    research.add_argument("--urls", type=int)
    research.add_argument("--pages", type=int)
    research.add_argument("--queries", type=int)
    research.add_argument("--duration", type=int)
    research.add_argument("-o", "--output", help="write Markdown to this file instead of stdout")
    args = parser.parse_args()
    if args.cmd == "research":
        sys.exit(asyncio.run(_cli_research(args)))
    import uvicorn

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()