# app/services/agent.py
# Owner: Eng 3 (Agent Pipeline)
#
# AI model integration for generating step plans.
# Sends screenshot + goal to a multimodal model, parses structured JSON response.
# Includes retry on invalid JSON and robust error handling.

import base64
import json
import os
import re
from pathlib import Path

from openai import AsyncOpenAI

from app.schemas.step_plan import ImageSize, StepPlan

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------
_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "plan_prompt.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text()

# ---------------------------------------------------------------------------
# OpenAI client (lazy singleton)
# ---------------------------------------------------------------------------
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise AgentError("OPENAI_API_KEY environment variable is not set")
        _client = AsyncOpenAI(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Custom errors
# ---------------------------------------------------------------------------
class AgentError(Exception):
    """Raised when the agent service fails."""
    pass


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _extract_json(raw: str) -> dict:
    """
    Extract a JSON object from a model response that may be wrapped in
    markdown code fences, have leading/trailing text, etc.
    """
    raw = raw.strip()

    # Try direct parse first (best case)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting from code fences
    match = _CODE_FENCE_RE.search(raw)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding the first { ... } block
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            return json.loads(raw[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass

    raise AgentError(f"Could not extract valid JSON from model response (length={len(raw)})")


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------
MAX_RETRIES = 1  # retry once on invalid JSON


async def generate_plan(
    goal: str,
    image_size: ImageSize,
    screenshot_bytes: bytes,
    learning_profile: str | None = None,
    app_context: str | None = None,
    session_summary: str | None = None,
    request_id: str = "",
) -> StepPlan:
    """
    Call the multimodal model with the screenshot and goal,
    parse and validate the response as a StepPlan.

    Retries once if the model returns invalid JSON.
    """
    client = _get_client()

    # Build prompt from template
    image_size_json = json.dumps({"w": image_size.w, "h": image_size.h})
    prompt = _PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{IMAGE_SIZE_JSON}}", image_size_json)
    prompt = prompt.replace("{{LEARNING_PROFILE_TEXT}}", learning_profile or "default")
    prompt = prompt.replace("{{APP_CONTEXT_JSON}}", app_context or "{}")
    prompt = prompt.replace("{{SESSION_SUMMARY}}", session_summary or "none")

    # Encode screenshot
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    last_error: Exception | None = None

    for attempt in range(1 + MAX_RETRIES):
        try:
            print(f"[agent] rid={request_id} attempt={attempt + 1} model={model} goal={goal!r}")

            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=2000,
                temperature=0.1,
            )

            raw_text = response.choices[0].message.content or ""
            print(f"[agent] rid={request_id} raw response length={len(raw_text)}")

            # Extract and validate JSON
            plan_dict = _extract_json(raw_text)
            plan = StepPlan.model_validate(plan_dict)

            print(f"[agent] rid={request_id} plan validated: {len(plan.steps)} steps")
            return plan

        except AgentError:
            raise  # don't retry config errors
        except json.JSONDecodeError as e:
            last_error = AgentError(f"Invalid JSON from model: {e}")
            print(f"[agent] rid={request_id} attempt={attempt + 1} JSON parse error: {e}")
        except Exception as e:
            last_error = AgentError(f"Model call failed: {type(e).__name__}: {e}")
            print(f"[agent] rid={request_id} attempt={attempt + 1} error: {type(e).__name__}: {e}")

    raise last_error or AgentError("Unknown agent failure")


# ---------------------------------------------------------------------------
# Replan generation
# ---------------------------------------------------------------------------
_REPLAN_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "replan_prompt.txt"
_REPLAN_PROMPT_TEMPLATE = _REPLAN_PROMPT_PATH.read_text()


async def generate_replan(
    goal: str,
    image_size: ImageSize,
    screenshot_bytes: bytes,
    current_step_id: str,
    learning_profile: str | None = None,
    app_context: str | None = None,
    session_summary: str | None = None,
    request_id: str = "",
) -> StepPlan:
    """
    Generate a revised plan when the user is stuck on a step.
    Uses a different prompt template that includes the stuck step context.
    """
    client = _get_client()

    image_size_json = json.dumps({"w": image_size.w, "h": image_size.h})
    prompt = _REPLAN_PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{IMAGE_SIZE_JSON}}", image_size_json)
    prompt = prompt.replace("{{CURRENT_STEP_ID}}", current_step_id)
    prompt = prompt.replace("{{LEARNING_PROFILE_TEXT}}", learning_profile or "default")
    prompt = prompt.replace("{{APP_CONTEXT_JSON}}", app_context or "{}")
    prompt = prompt.replace("{{SESSION_SUMMARY}}", session_summary or "none")

    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    last_error: Exception | None = None

    for attempt in range(1 + MAX_RETRIES):
        try:
            print(f"[agent] rid={request_id} replan attempt={attempt + 1} stuck_at={current_step_id}")

            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=2000,
                temperature=0.1,
            )

            raw_text = response.choices[0].message.content or ""
            plan_dict = _extract_json(raw_text)
            plan = StepPlan.model_validate(plan_dict)

            print(f"[agent] rid={request_id} replan validated: {len(plan.steps)} steps")
            return plan

        except AgentError:
            raise
        except json.JSONDecodeError as e:
            last_error = AgentError(f"Invalid JSON from model on replan: {e}")
            print(f"[agent] rid={request_id} replan attempt={attempt + 1} JSON error: {e}")
        except Exception as e:
            last_error = AgentError(f"Replan model call failed: {type(e).__name__}: {e}")
            print(f"[agent] rid={request_id} replan attempt={attempt + 1} error: {e}")

    raise last_error or AgentError("Unknown replan failure")
