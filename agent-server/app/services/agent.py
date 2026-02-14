# app/services/agent.py
# Owner: Eng 3 (Agent Server)
#
# AI model integration for generating step plans.
# Sends screenshot + goal to a multimodal model and parses the structured JSON response.

import base64
import json
import os
from pathlib import Path

from openai import AsyncOpenAI

from app.schemas.step_plan import StepPlan

# Load prompt template
_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "plan_prompt.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text()

# OpenAI client (initialized lazily)
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


async def generate_plan(
    goal: str,
    image_size_json: str,
    screenshot_bytes: bytes,
    learning_profile: str | None = None,
    app_context: str | None = None,
    session_summary: str | None = None,
    request_id: str = "",
) -> StepPlan:
    """
    Call the multimodal model with the screenshot and goal,
    parse the response as a StepPlan.
    """
    client = _get_client()

    # Build the prompt from template
    prompt = _PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{IMAGE_SIZE_JSON}}", image_size_json)
    prompt = prompt.replace("{{LEARNING_PROFILE_TEXT}}", learning_profile or "default")
    prompt = prompt.replace("{{APP_CONTEXT_JSON}}", app_context or "{}")
    prompt = prompt.replace("{{SESSION_SUMMARY}}", session_summary or "none")

    # Encode screenshot as base64 for the vision API
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    print(f"[agent] request_id={request_id} sending to model, goal={goal!r}")

    response = await client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        messages=[
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
        ],
        max_tokens=2000,
        temperature=0.1,
    )

    raw_text = response.choices[0].message.content or ""
    print(f"[agent] request_id={request_id} raw response length={len(raw_text)}")

    # Strip markdown code fences if present
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = cleaned.index("\n")
        cleaned = cleaned[first_newline + 1 :]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # Parse and validate
    plan_dict = json.loads(cleaned)
    plan = StepPlan.model_validate(plan_dict)

    print(f"[agent] request_id={request_id} plan validated, {len(plan.steps)} steps")
    return plan
