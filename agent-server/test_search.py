#!/usr/bin/env python3
"""
Modular tests for the Bright Data web search integration.

Tests each layer independently so you can pinpoint failures fast:
  1. Bright Data SERP API connectivity
  2. HTML → text extraction
  3. LLM search-query generation (uses project's configured provider)
  4. Full search_for_goal pipeline
  5. In-memory store persistence (simulates /plan → /next flow)
  6. End-to-end /plan with search via the running server

Usage:
    # Make sure .env is loaded (script loads it automatically).
    cd agent-server && source .venv/bin/activate

    # Run ALL tests:
    python test_search.py

    # Run a SINGLE test by name:
    python test_search.py brightdata
    python test_search.py html
    python test_search.py queries
    python test_search.py pipeline
    python test_search.py store
    python test_search.py server
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # load .env so API keys are available

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
SKIP = "\033[93m⊘ SKIP\033[0m"
HEADER = "\033[1;36m"
RESET = "\033[0m"

results: list[tuple[str, str, str]] = []  # (name, status, detail)


def record(name: str, passed: bool, detail: str = ""):
    status = PASS if passed else FAIL
    results.append((name, status, detail))
    print(f"  {status}  {name}")
    if detail:
        for line in detail.split("\n"):
            print(f"         {line}")


def record_skip(name: str, reason: str):
    results.append((name, SKIP, reason))
    print(f"  {SKIP}  {name}  ({reason})")


def section(title: str):
    print(f"\n{HEADER}{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}{RESET}\n")


def take_screenshot() -> tuple[bytes, int, int]:
    """Capture screen. Returns (png_bytes, w, h)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name
    subprocess.run(
        ["screencapture", "-x", "-C", "-m", tmp_path],
        capture_output=True, timeout=10,
    )
    png_bytes = Path(tmp_path).read_bytes()
    Path(tmp_path).unlink()
    return png_bytes, 1920, 1080


# ===================================================================
# TEST 1: Bright Data SERP API — raw connectivity
# ===================================================================
async def test_brightdata_api():
    """Call Bright Data SERP API directly with a simple query."""
    section("Test 1: Bright Data SERP API connectivity")

    api_key = os.getenv("BRIGHTDATA_API_KEY")
    if not api_key:
        record_skip("brightdata_api_key_present", "BRIGHTDATA_API_KEY not set in .env")
        return

    record("brightdata_api_key_present", True, f"Key: {api_key[:8]}...{api_key[-4:]}")

    import httpx
    import urllib.parse

    query = "how to open System Settings on macOS"
    url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "zone": "serp_api1",
        "url": url,
        "format": "raw",
    }

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.brightdata.com/request",
                json=data,
                headers=headers,
            )
        elapsed = round((time.time() - start) * 1000)

        record(
            "brightdata_http_status",
            resp.status_code == 200,
            f"Status: {resp.status_code} ({elapsed}ms)",
        )

        if resp.status_code == 200:
            raw = resp.text
            record(
                "brightdata_response_has_content",
                len(raw) > 100,
                f"Response body: {len(raw):,} chars",
            )
            # SERP API returns JSON even with format=raw; verify structure
            try:
                body = resp.json()
                is_json = isinstance(body, dict) and "organic" in body
            except Exception:
                is_json = False
            record(
                "brightdata_response_is_serp_json",
                is_json,
                f"Parseable as SERP JSON with 'organic' key: {is_json}",
            )
        else:
            record("brightdata_response_has_content", False, f"Error: {resp.text[:300]}")

    except Exception as e:
        elapsed = round((time.time() - start) * 1000)
        record("brightdata_http_request", False, f"{type(e).__name__}: {e} ({elapsed}ms)")


# ===================================================================
# TEST 2: HTML → plain text extraction
# ===================================================================
def test_html_extraction():
    """Verify the HTML stripping helper works correctly."""
    section("Test 2: HTML → text extraction")

    from app.services.search import _html_to_text

    # Simple tags
    result = _html_to_text("<p>Hello <b>world</b></p>")
    record(
        "html_strips_basic_tags",
        "Hello" in result and "world" in result and "<" not in result,
        f"Result: {result!r}",
    )

    # HTML entities
    result = _html_to_text("&amp; &lt; &gt; &quot;")
    record(
        "html_decodes_entities",
        "&" in result and "<" in result and ">" in result,
        f"Result: {result!r}",
    )

    # Whitespace collapsing
    result = _html_to_text("<div>  lots   of   \n\n  space  </div>")
    record(
        "html_collapses_whitespace",
        "  " not in result and "\n" not in result,
        f"Result: {result!r}",
    )

    # Empty input
    result = _html_to_text("")
    record("html_handles_empty", result == "", f"Result: {result!r}")


# ===================================================================
# TEST 3: LLM search-query generation (uses project's provider)
# ===================================================================
async def test_query_generation():
    """Verify LLM produces sensible search queries from a goal."""
    section("Test 3: LLM search-query generation")

    # Check that at least one LLM key is configured
    has_key = bool(os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY"))
    if not has_key:
        record_skip("llm_key_present", "No LLM API key set (GEMINI/OPENAI/OPENROUTER)")
        return

    record("llm_key_present", True)

    from app.services.search import _generate_search_queries

    goal = "Change the wallpaper in macOS System Settings"

    start = time.time()
    queries = await _generate_search_queries(goal=goal, screenshot_bytes=None, app_context=None)
    elapsed = round((time.time() - start) * 1000)

    record(
        "query_gen_returns_list",
        isinstance(queries, list) and len(queries) >= 1,
        f"Got {len(queries)} queries ({elapsed}ms)",
    )

    record(
        "query_gen_returns_strings",
        all(isinstance(q, str) for q in queries),
        f"Queries: {queries}",
    )

    record(
        "query_gen_max_3",
        len(queries) <= 3,
        f"Count: {len(queries)} (max 3)",
    )

    # Queries should be relevant (at least one mentions wallpaper, settings, or macOS)
    any_relevant = any(
        any(kw in q.lower() for kw in ["wallpaper", "settings", "macos", "desktop", "background"])
        for q in queries
    )
    record(
        "query_gen_content_relevant",
        any_relevant,
        f"Relevance check for goal about wallpaper: {any_relevant}",
    )


# ===================================================================
# TEST 4: Full search_for_goal pipeline
# ===================================================================
async def test_search_pipeline():
    """Run the complete search pipeline: queries → Bright Data → extract → store."""
    section("Test 4: Full search_for_goal pipeline")

    bd_key = os.getenv("BRIGHTDATA_API_KEY")
    has_llm_key = bool(os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY"))
    if not bd_key or not has_llm_key:
        missing = []
        if not bd_key:
            missing.append("BRIGHTDATA_API_KEY")
        if not has_llm_key:
            missing.append("LLM API key (GEMINI/OPENAI/OPENROUTER)")
        record_skip("pipeline_keys", f"Missing: {', '.join(missing)}")
        return

    from app.services.search import search_for_goal, clear_search_context

    # Clear any previous state
    clear_search_context()

    goal = "Enable dark mode in macOS Sonoma"

    start = time.time()
    context = await search_for_goal(
        goal=goal,
        screenshot_bytes=None,
        app_context=None,
        request_id="test-pipeline",
    )
    elapsed = round((time.time() - start) * 1000)

    record(
        "pipeline_returns_string",
        isinstance(context, str),
        f"Type: {type(context).__name__}",
    )

    record(
        "pipeline_has_content",
        len(context) > 50,
        f"Context length: {len(context):,} chars ({elapsed}ms)",
    )

    # Should contain some relevant keywords
    ctx_lower = context.lower()
    has_relevance = any(kw in ctx_lower for kw in ["dark", "mode", "macos", "settings", "appearance"])
    record(
        "pipeline_content_relevant",
        has_relevance,
        f"Contains relevant keywords: {has_relevance}",
    )

    # Context should be capped
    record(
        "pipeline_context_capped",
        len(context) <= 4000,
        f"Length {len(context):,} <= 4000 cap",
    )

    # Print a snippet for manual inspection
    print(f"\n  Context preview (first 500 chars):")
    for line in context[:500].split("\n"):
        print(f"     {line}")
    print()


# ===================================================================
# TEST 5: In-memory store persistence (/plan → /next flow)
# ===================================================================
async def test_store_persistence():
    """Verify that search context stored during /plan is retrievable for /next."""
    section("Test 5: In-memory store persistence")

    from app.services.search import (
        _search_store,
        get_stored_search_context,
        clear_search_context,
    )

    # Start clean
    clear_search_context()
    record("store_starts_empty", len(_search_store) == 0)

    # Simulate what /plan does: store context
    test_goal = "Test goal for store check"
    test_context = "This is cached search context about the test goal."
    _search_store[test_goal] = test_context

    # Simulate what /next does: retrieve context
    retrieved = get_stored_search_context(test_goal)
    record(
        "store_retrieves_exact_match",
        retrieved == test_context,
        f"Stored {len(test_context)} chars, retrieved {len(retrieved)} chars",
    )

    # Different goal should return empty
    other = get_stored_search_context("completely different goal")
    record(
        "store_returns_empty_for_unknown",
        other == "",
        f"Unknown goal returned: {other!r}",
    )

    # Clear specific goal
    clear_search_context(test_goal)
    after_clear = get_stored_search_context(test_goal)
    record(
        "store_clear_specific_works",
        after_clear == "",
        f"After clear: {after_clear!r}",
    )

    # Test clear all
    _search_store["a"] = "1"
    _search_store["b"] = "2"
    clear_search_context()
    record(
        "store_clear_all_works",
        len(_search_store) == 0,
        f"Store size after clear_all: {len(_search_store)}",
    )


# ===================================================================
# TEST 6: End-to-end /plan with search via running server
# ===================================================================
def test_server_plan_with_search():
    """
    Hit the running server's /plan endpoint and verify the plan comes back.
    The server logs will show the search activity.
    """
    section("Test 6: End-to-end /plan via server (check server logs for search)")

    import httpx

    SERVER_URL = "http://localhost:8000"

    # Health check
    try:
        resp = httpx.get(f"{SERVER_URL}/health", timeout=5)
        health = resp.json()
        record(
            "server_running",
            health["status"] == "ok",
            f"mock_mode={health['mock_mode']}, model={health['model']}",
        )
        if health.get("mock_mode"):
            record_skip(
                "server_plan_with_search",
                "Server in MOCK_MODE — search won't run. "
                "Restart without MOCK_MODE to test search integration.",
            )
            return
    except httpx.ConnectError:
        record_skip(
            "server_running",
            "Server not running. Start with: uvicorn app.main:app --reload",
        )
        return

    # Take screenshot
    png_bytes, w, h = take_screenshot()
    record("screenshot_captured", len(png_bytes) > 0, f"{len(png_bytes):,} bytes")

    goal = "Open Wi-Fi settings in System Settings"

    # Send to /plan — this triggers search internally
    start = time.time()
    resp = httpx.post(
        f"{SERVER_URL}/plan",
        data={
            "goal": goal,
            "image_size": f'{{"w":{w},"h":{h}}}',
        },
        files={
            "screenshot": ("screenshot.png", png_bytes, "image/png"),
        },
        headers={"X-Request-ID": "test-search-e2e"},
        timeout=60,  # longer timeout: search + plan generation
    )
    elapsed = round((time.time() - start) * 1000)

    record(
        "plan_status_200",
        resp.status_code == 200,
        f"Status: {resp.status_code} ({elapsed}ms)",
    )

    if resp.status_code == 200:
        plan = resp.json()
        record(
            "plan_has_steps",
            len(plan.get("steps", [])) >= 1,
            f"Steps: {len(plan['steps'])}, goal: {plan['goal']!r}",
        )

        # Now test /next with the same goal — should pick up stored context
        first_step = plan["steps"][0]
        completed = json.dumps([{"id": first_step["id"], "instruction": first_step["instruction"]}])

        png_bytes2, w2, h2 = take_screenshot()
        resp2 = httpx.post(
            f"{SERVER_URL}/next",
            data={
                "goal": goal,
                "image_size": f'{{"w":{w2},"h":{h2}}}',
                "completed_steps": completed,
                "total_steps": str(len(plan["steps"])),
            },
            files={"screenshot": ("screenshot.png", png_bytes2, "image/png")},
            headers={"X-Request-ID": "test-search-e2e-next"},
            timeout=60,
        )

        record(
            "next_status_200",
            resp2.status_code == 200,
            f"Status: {resp2.status_code} (check server logs for 'using N chars of stored search context')",
        )
    else:
        print(f"    Error body: {resp.text[:400]}")


# ===================================================================
# Runner
# ===================================================================
TEST_MAP = {
    "brightdata": ("Bright Data SERP API", test_brightdata_api),
    "html": ("HTML extraction", test_html_extraction),
    "queries": ("Query generation", test_query_generation),
    "pipeline": ("Search pipeline", test_search_pipeline),
    "store": ("Store persistence", test_store_persistence),
    "server": ("Server end-to-end", test_server_plan_with_search),
}


def main():
    args = sys.argv[1:]
    print(f"\n{'='*60}")
    print("  The Cookbook — Search Integration Tests")
    print(f"{'='*60}")

    if args:
        selected = args
    else:
        selected = list(TEST_MAP.keys())

    for key in selected:
        if key not in TEST_MAP:
            print(f"\n  Unknown test: {key!r}")
            print(f"  Available: {', '.join(TEST_MAP.keys())}")
            sys.exit(1)

        label, func = TEST_MAP[key]
        if asyncio.iscoroutinefunction(func):
            asyncio.run(func())
        else:
            func()

    # Summary
    section("Summary")
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    skipped = sum(1 for _, s, _ in results if s == SKIP)
    total = len(results)

    print(f"  {passed} passed, {failed} failed, {skipped} skipped  ({total} total)\n")

    if failed > 0:
        print("  Failed tests:")
        for name, status, detail in results:
            if status == FAIL:
                print(f"    x {name}: {detail}")
        print()
        sys.exit(1)
    else:
        print("  All tests passed!\n")


if __name__ == "__main__":
    main()
