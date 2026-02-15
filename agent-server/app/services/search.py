# app/services/search.py
# Owner: Eng 3 (Agent Pipeline)
#
# Web search service using Bright Data SERP API.
# Generates search queries from the user's goal + screenshot,
# fetches results, extracts useful text, and stores it
# in-memory so successive /next calls can reuse the context.

import base64
import html
import json
import os
import re
import urllib.parse
from typing import Optional

import httpx
from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# In-memory store: goal -> search context string
# Persists across requests within the same server process.
# ---------------------------------------------------------------------------
_search_store: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BRIGHTDATA_API_URL = "https://api.brightdata.com/request"
BRIGHTDATA_ZONE = "serp_api1"
SEARCH_TIMEOUT = 30.0  # seconds per Bright Data request
MAX_CONTEXT_CHARS = 4000  # cap stored context size


# ---------------------------------------------------------------------------
# HTML → plain text helper
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _html_to_text(raw_html: str) -> str:
    """Strip HTML tags and decode entities to produce readable plain text."""
    text = _TAG_RE.sub(" ", raw_html)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Query generation via OpenAI
# ---------------------------------------------------------------------------
async def _generate_search_queries(
    goal: str,
    screenshot_bytes: bytes | None = None,
    app_context: str | None = None,
) -> list[str]:
    """
    Use a lightweight OpenAI model to produce 1-3 concise Google search
    queries from the goal (and optionally the screenshot for context).
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        # Fallback: just use the goal itself
        return [goal]

    client = AsyncOpenAI(api_key=api_key)

    prompt_text = (
        "Given the following user goal for a macOS application, generate 1-3 "
        "concise Google search queries that would help find step-by-step "
        "instructions, documentation, or relevant how-to guides.\n\n"
        f"Goal: {goal}\n"
        f"App context: {app_context or 'unknown'}\n\n"
        "Output ONLY a JSON array of query strings. Example: "
        '["how to enable dark mode in Photoshop", "Photoshop preferences panel"]\n'
        "No other text."
    )

    content: list[dict] = [{"type": "text", "text": prompt_text}]

    # Optionally include screenshot at low detail for context
    if screenshot_bytes:
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{screenshot_b64}",
                    "detail": "low",
                },
            }
        )

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",  # cheap + fast for query generation
            messages=[{"role": "user", "content": content}],
            max_tokens=200,
            temperature=0.3,
        )

        raw = response.choices[0].message.content or "[]"
        # Strip any markdown fences
        raw = raw.strip().strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

        queries = json.loads(raw)
        if isinstance(queries, list):
            return [q for q in queries if isinstance(q, str)][:1]
    except Exception as e:
        print(f"[search] query generation failed: {type(e).__name__}: {e}")

    # Fallback
    return [goal]


# ---------------------------------------------------------------------------
# Bright Data SERP API call
# ---------------------------------------------------------------------------
async def _search_brightdata(query: str, request_id: str = "") -> dict | None:
    """Execute a single SERP search via Bright Data."""
    api_key = os.getenv("BRIGHTDATA_API_KEY")
    if not api_key:
        print("[search] BRIGHTDATA_API_KEY not set, skipping search")
        return None

    encoded_query = urllib.parse.quote_plus(query)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "zone": BRIGHTDATA_ZONE,
        "url": f"https://www.google.com/search?q={encoded_query}",
        "format": "raw",
    }

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
            response = await client.post(
                BRIGHTDATA_API_URL, json=data, headers=headers
            )
            if response.status_code == 200:
                print(
                    f"[search] rid={request_id} query={query!r} "
                    f"got {len(response.text)} chars"
                )
                # Bright Data SERP returns JSON even with format=raw
                try:
                    json_data = response.json()
                except Exception:
                    json_data = None
                return {"query": query, "json_data": json_data, "raw_text": response.text}
            else:
                print(
                    f"[search] rid={request_id} Bright Data error "
                    f"{response.status_code}: {response.text[:200]}"
                )
                return None
    except httpx.TimeoutException:
        print(f"[search] rid={request_id} timeout for query={query!r}")
        return None
    except Exception as e:
        print(f"[search] rid={request_id} error: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Result processing
# ---------------------------------------------------------------------------
def _extract_search_context(results: list[dict]) -> str:
    """
    Convert Bright Data SERP results into a concise text summary
    suitable for injection into an LLM prompt.
    Tries structured JSON first, falls back to raw text.
    """
    parts: list[str] = []
    for r in results:
        query = r["query"]
        json_data = r.get("json_data")
        snippet_parts: list[str] = []

        # Try structured JSON extraction first
        if isinstance(json_data, dict) and "organic" in json_data:
            for item in json_data["organic"][:5]:
                title = item.get("title", "")
                snippet = item.get("description", item.get("snippet", ""))
                if title and snippet:
                    snippet_parts.append(f"• {title}: {snippet}")
                elif title:
                    snippet_parts.append(f"• {title}")

        if snippet_parts:
            parts.append(f"[Search: {query}]\n" + "\n".join(snippet_parts))
        elif r.get("raw_text"):
            # Fallback: strip HTML and use raw text
            text = _html_to_text(r["raw_text"])[:1500]
            parts.append(f"[Search: {query}]\n{text}")

    combined = "\n\n".join(parts)
    # Cap total size
    return combined[:MAX_CONTEXT_CHARS]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def search_for_goal(
    goal: str,
    screenshot_bytes: bytes | None = None,
    app_context: str | None = None,
    request_id: str = "",
) -> str:
    """
    Search the web for information related to the user's goal.

    1. Uses OpenAI to generate targeted search queries from the goal
       (optionally using the screenshot for additional context).
    2. Calls the Bright Data SERP API for each query.
    3. Extracts clean text from the HTML results.
    4. Stores the context in-memory keyed by goal for reuse in /next calls.
    5. Returns the search context string.
    """
    # Skip if no Bright Data key
    if not os.getenv("BRIGHTDATA_API_KEY"):
        print(f"[search] rid={request_id} no BRIGHTDATA_API_KEY, skipping")
        return ""

    print(f"[search] rid={request_id} starting search for goal={goal!r}")

    # Step 1: Generate queries
    queries = await _generate_search_queries(goal, screenshot_bytes, app_context)
    print(f"[search] rid={request_id} queries: {queries}")

    # Step 2: Execute searches in parallel
    import asyncio

    tasks = [_search_brightdata(q, request_id) for q in queries]
    raw_results = await asyncio.gather(*tasks)

    # Filter out None results
    valid_results = [r for r in raw_results if r is not None]

    if not valid_results:
        print(f"[search] rid={request_id} no results returned")
        return ""

    # Step 3: Extract and clean
    context = _extract_search_context(valid_results)

    # Step 4: Store for later /next calls
    _search_store[goal] = context
    print(
        f"[search] rid={request_id} stored {len(context)} chars "
        f"of search context ({len(valid_results)} queries succeeded)"
    )

    return context


def get_stored_search_context(goal: str) -> str:
    """Retrieve previously stored search context for a goal."""
    return _search_store.get(goal, "")


def clear_search_context(goal: str | None = None):
    """Clear stored search context (for a specific goal or all)."""
    if goal:
        _search_store.pop(goal, None)
    else:
        _search_store.clear()
