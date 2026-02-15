# app/services/agent.py
# Owner: Eng 3 (Agent Pipeline)
#
# AI model integration for generating step plans.
# Sends screenshot + goal to a multimodal model, parses structured JSON response.
# Includes retry on invalid JSON and robust error handling.

import asyncio
import base64
import json
import os
import re
import tempfile
import time
from pathlib import Path

from openai import AsyncOpenAI

# Native Gemini SDK — used for file upload + pre-cached image generation.
# Falls back to OpenAI-compatible endpoint when unavailable.
try:
    import google.generativeai as genai
    _genai_available = True
except ImportError:
    genai = None
    _genai_available = False

from app.schemas.step_plan import (
    BBoxNorm,
    ImageSize,
    NextStepResponse,
    OmniPlanResponse,
    RefineResponse,
    SoMStepPlan,
    StepPlan,
)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------
_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "plan_prompt.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text()
_REFINE_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "refine_prompt.txt"
_REFINE_PROMPT_TEMPLATE = _REFINE_PROMPT_PATH.read_text()

_SOM_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "som_plan_prompt.txt"
_SOM_PROMPT_TEMPLATE = _SOM_PROMPT_PATH.read_text()

_REFINE_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "refine_prompt.txt"
_REFINE_PROMPT_TEMPLATE = _REFINE_PROMPT_PATH.read_text()

_SOM_REFINE_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "som_refine_prompt.txt"
_SOM_REFINE_PROMPT_TEMPLATE = _SOM_REFINE_PROMPT_PATH.read_text()

_OMNI_PLAN_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "omniparser_plan_prompt.txt"
_OMNI_PLAN_PROMPT_TEMPLATE = _OMNI_PLAN_PROMPT_PATH.read_text()

_OMNI_REFINE_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "omniparser_refine_prompt.txt"
_OMNI_REFINE_PROMPT_TEMPLATE = _OMNI_REFINE_PROMPT_PATH.read_text()

_GEMINI_PLAN_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "gemini_plan_prompt.txt"
_GEMINI_PLAN_PROMPT_TEMPLATE = _GEMINI_PLAN_PROMPT_PATH.read_text()

_GEMINI_NEXT_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "gemini_next_prompt.txt"
_GEMINI_NEXT_PROMPT_TEMPLATE = _GEMINI_NEXT_PROMPT_PATH.read_text()

# ---------------------------------------------------------------------------
# LLM client (lazy singleton) — supports Gemini, OpenAI, and OpenRouter
# ---------------------------------------------------------------------------
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """
    Create the LLM client.  Priority order:

    1. Gemini (best vision): set GEMINI_API_KEY.
       Uses Google's OpenAI-compatible endpoint.
       Model names: gemini-2.5-pro, gemini-2.5-flash, etc.

    2. OpenAI: set OPENAI_API_KEY.

    3. OpenRouter: set OPENROUTER_API_KEY.
       Model names: openai/gpt-4o, anthropic/claude-sonnet-4, etc.
    """
    global _client
    if _client is not None:
        return _client

    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")

    if gemini_key:
        _client = AsyncOpenAI(
            api_key=gemini_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        model = os.getenv("OPENAI_MODEL", "gemini-2.5-pro")
        print(f"[agent] Using Gemini via OpenAI-compat endpoint (model={model})")
    elif openai_key:
        _client = AsyncOpenAI(api_key=openai_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4o")
        print(f"[agent] Using direct OpenAI (model={model})")
    elif openrouter_key:
        _client = AsyncOpenAI(
            api_key=openrouter_key,
            base_url="https://openrouter.ai/api/v1",
        )
        model = os.getenv("OPENAI_MODEL", "openai/gpt-4o")
        print(f"[agent] Using OpenRouter (model={model})")
    else:
        raise AgentError(
            "No API key found. Set GEMINI_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY in .env"
        )
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
# Model-adaptive parameters
# ---------------------------------------------------------------------------

def _model_params(model: str, max_tokens: int) -> dict:
    """
    Return API kwargs adapted for the model.
    Handles OpenAI o-series, OpenRouter model slugs, etc.
    """
    m = model.lower()
    # Models that need the new-style parameters (o-series, gpt-5)
    new_style = any(tag in m for tag in ["gpt-5", "o1", "o3", "o4"])
    if new_style:
        return {"max_completion_tokens": max_tokens, "temperature": 1}
    else:
        return {"max_tokens": max_tokens, "temperature": 0.1}


def _supports_json_mode(model: str) -> bool:
    """Check if the model supports response_format: json_object."""
    m = model.lower()
    # OpenAI models that support it
    if any(tag in m for tag in ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo", "gpt-5"]):
        return True
    # OpenRouter prefixed OpenAI models
    if "openai/" in m:
        return True
    # Google Gemini supports it
    if "gemini" in m:
        return True
    # Anthropic Claude does NOT support response_format
    if "claude" in m or "anthropic/" in m:
        return False
    # Default: try it (OpenRouter will error if unsupported)
    return True


# ---------------------------------------------------------------------------
# Rate limit handling
# ---------------------------------------------------------------------------
MAX_RETRIES = 2  # retry twice (for rate limits + invalid JSON)

# Global semaphore to serialize LLM calls and avoid piling up rate-limited requests
_llm_semaphore = asyncio.Semaphore(1)


async def _call_llm_with_backoff(client, model: str, messages: list, params: dict, request_id: str, label: str):
    """
    Call the LLM with rate-limit-aware retry and backoff.
    Uses a semaphore to avoid piling up concurrent requests.
    Automatically handles response_format support per model.
    """
    # Only use json_object mode for models that support it
    extra_kwargs = {}
    if _supports_json_mode(model):
        extra_kwargs["response_format"] = {"type": "json_object"}

    async with _llm_semaphore:
        for attempt in range(1 + MAX_RETRIES):
            try:
                print(f"[agent] rid={request_id} {label} attempt={attempt + 1} model={model}")
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **params,
                    **extra_kwargs,
                )
                raw_text = response.choices[0].message.content or ""
                print(f"[agent] rid={request_id} {label} response length={len(raw_text)}")
                return raw_text
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "rate_limit" in error_str.lower():
                    # Extract retry-after if available, default to 25s
                    wait = 25
                    import re as _re
                    match = _re.search(r"try again in (\d+\.?\d*)s", error_str)
                    if match:
                        wait = float(match.group(1)) + 2  # add buffer
                    print(f"[agent] rid={request_id} {label} rate limited, waiting {wait:.0f}s...")
                    await asyncio.sleep(wait)
                elif attempt < MAX_RETRIES:
                    print(f"[agent] rid={request_id} {label} attempt={attempt + 1} error: {e}")
                    await asyncio.sleep(1)
                else:
                    raise
        raise AgentError(f"{label} failed after {MAX_RETRIES + 1} attempts")


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------


async def generate_plan(
    goal: str,
    image_size: ImageSize,
    screenshot_with_markers_bytes: bytes,
    markers_json: str,
    learning_profile: str | None = None,
    app_context: str | None = None,
    session_summary: str | None = None,
    request_id: str = "",
) -> StepPlan:
    """
    Call the multimodal model with the marked screenshot + marker metadata and goal,
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
    prompt = prompt.replace("{{MARKERS_JSON}}", markers_json)

    # Encode marked screenshot
    screenshot_b64 = base64.b64encode(screenshot_with_markers_bytes).decode("utf-8")

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
                **_model_params(model, 2000),
                response_format={"type": "json_object"},
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


async def generate_refined_bbox(
    goal: str,
    step_id: str,
    instruction: str,
    crop_rect_full_norm_json: str,
    crop_image_bytes: bytes,
    session_summary: str | None = None,
    request_id: str = "",
) -> BBoxNorm:
    """Generate a tight bbox in crop-normalized coordinates."""
    client = _get_client()

    prompt = _REFINE_PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{STEP_ID}}", step_id)
    prompt = prompt.replace("{{INSTRUCTION}}", instruction)
    prompt = prompt.replace("{{CROP_RECT_FULL_NORM_JSON}}", crop_rect_full_norm_json)
    prompt = prompt.replace("{{SESSION_SUMMARY}}", session_summary or "none")

    crop_b64 = base64.b64encode(crop_image_bytes).decode("utf-8")
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{crop_b64}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    last_error: Exception | None = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            print(f"[agent] rid={request_id} refine attempt={attempt + 1} step_id={step_id}")
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                **_model_params(model, 500),
                response_format={"type": "json_object"},
            )
            raw_text = response.choices[0].message.content or ""
            bbox_dict = _extract_json(raw_text)
            bbox = BBoxNorm.model_validate(bbox_dict)
            print(f"[agent] rid={request_id} refine validated")
            return bbox
        except AgentError:
            raise
        except json.JSONDecodeError as e:
            last_error = AgentError(f"Invalid JSON from model on refine: {e}")
            print(f"[agent] rid={request_id} refine attempt={attempt + 1} JSON error: {e}")
        except Exception as e:
            last_error = AgentError(f"Refine model call failed: {type(e).__name__}: {e}")
            print(f"[agent] rid={request_id} refine attempt={attempt + 1} error: {type(e).__name__}: {e}")

    raise last_error or AgentError("Unknown refine failure")


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
                **_model_params(model, 2000),
                response_format={"type": "json_object"},
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


# ---------------------------------------------------------------------------
# Next-step generation (per-step re-screenshot flow)
# ---------------------------------------------------------------------------
_NEXT_STEP_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "next_step_prompt.txt"
_NEXT_STEP_PROMPT_TEMPLATE = _NEXT_STEP_PROMPT_PATH.read_text()


async def generate_next_step(
    goal: str,
    image_size: ImageSize,
    screenshot_bytes: bytes,
    completed_steps: str,
    total_steps: int,
    learning_profile: str | None = None,
    app_context: str | None = None,
    request_id: str = "",
    elements_context: str = "",
) -> NextStepResponse:
    """
    Given a fresh screenshot after the user completed a step, identify
    the next 1-2 actions. Returns a NextStepResponse with status
    ("continue", "done", "retry") and optional steps.
    If elements_context is provided, the screenshot has YOLO-annotated boxes.
    """
    client = _get_client()

    image_size_json = json.dumps({"w": image_size.w, "h": image_size.h})
    prompt = _NEXT_STEP_PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{IMAGE_SIZE_JSON}}", image_size_json)
    prompt = prompt.replace("{{COMPLETED_STEPS_JSON}}", completed_steps)
    prompt = prompt.replace("{{TOTAL_STEPS}}", str(total_steps))
    prompt = prompt.replace("{{LEARNING_PROFILE_TEXT}}", learning_profile or "default")
    prompt = prompt.replace("{{APP_CONTEXT_JSON}}", app_context or "{}")
    prompt = prompt.replace("{{ELEMENTS_CONTEXT}}", elements_context)

    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    model = os.getenv("OPENAI_NEXT_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o"))
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

    raw_text = await _call_llm_with_backoff(
        client, model, messages, _model_params(model, 1000),
        request_id, "next-step"
    )
    plan_dict = _extract_json(raw_text)
    result = NextStepResponse.model_validate(plan_dict)
    print(f"[agent] rid={request_id} next-step validated: status={result.status} steps={len(result.steps)} message={result.message!r}")
    return result

    raise last_error or AgentError("Unknown next-step failure")


# ---------------------------------------------------------------------------
# SoM plan generation (marker-based)
# ---------------------------------------------------------------------------


async def generate_som_plan(
    goal: str,
    image_size: ImageSize,
    screenshot_bytes: bytes,
    markers_json: str | None = None,
    learning_profile: str | None = None,
    app_context: str | None = None,
    session_summary: str | None = None,
    request_id: str = "",
) -> SoMStepPlan:
    """
    Call the multimodal model with a marked-up screenshot.
    The model reads marker IDs from the image and selects them.
    markers_json is no longer embedded in the prompt (too many tokens).
    """
    client = _get_client()

    image_size_json = json.dumps({"w": image_size.w, "h": image_size.h})
    prompt = _SOM_PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{IMAGE_SIZE_JSON}}", image_size_json)

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
            print(f"[agent] rid={request_id} som-plan attempt={attempt + 1} model={model} goal={goal!r}")

            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                **_model_params(model, 2000),
                response_format={"type": "json_object"},
            )

            raw_text = response.choices[0].message.content or ""
            print(f"[agent] rid={request_id} som-plan raw response length={len(raw_text)}")

            plan_dict = _extract_json(raw_text)
            plan = SoMStepPlan.model_validate(plan_dict)

            print(f"[agent] rid={request_id} som-plan validated: {len(plan.steps)} steps")
            return plan

        except AgentError:
            raise
        except json.JSONDecodeError as e:
            last_error = AgentError(f"Invalid JSON from SoM model: {e}")
            print(f"[agent] rid={request_id} som-plan attempt={attempt + 1} JSON parse error: {e}")
        except Exception as e:
            last_error = AgentError(f"SoM model call failed: {type(e).__name__}: {e}")
            print(f"[agent] rid={request_id} som-plan attempt={attempt + 1} error: {type(e).__name__}: {e}")

    raise last_error or AgentError("Unknown SoM plan failure")


# ---------------------------------------------------------------------------
# Refinement generation (bbox in crop)
# ---------------------------------------------------------------------------


async def generate_refine(
    instruction: str,
    target_label: str,
    crop_image_bytes: bytes,
    request_id: str = "",
) -> RefineResponse:
    """
    Given a cropped screenshot region, ask the model for a tight bounding box
    around the target element. Returns bbox in crop-normalized coordinates.
    """
    client = _get_client()

    prompt = _REFINE_PROMPT_TEMPLATE
    prompt = prompt.replace("{{INSTRUCTION}}", instruction)
    prompt = prompt.replace("{{TARGET_LABEL}}", target_label or "")

    crop_b64 = base64.b64encode(crop_image_bytes).decode("utf-8")

    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{crop_b64}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    last_error: Exception | None = None

    for attempt in range(1 + MAX_RETRIES):
        try:
            print(f"[agent] rid={request_id} refine attempt={attempt + 1} model={model}")

            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                **_model_params(model, 500),
                response_format={"type": "json_object"},
            )

            raw_text = response.choices[0].message.content or ""
            print(f"[agent] rid={request_id} refine raw response length={len(raw_text)}")

            bbox_dict = _extract_json(raw_text)
            bbox = RefineResponse.model_validate(bbox_dict)

            print(f"[agent] rid={request_id} refine validated: ({bbox.x:.3f},{bbox.y:.3f},{bbox.w:.3f},{bbox.h:.3f}) conf={bbox.confidence}")
            return bbox

        except AgentError:
            raise
        except json.JSONDecodeError as e:
            last_error = AgentError(f"Invalid JSON from refine model: {e}")
            print(f"[agent] rid={request_id} refine attempt={attempt + 1} JSON parse error: {e}")
        except Exception as e:
            last_error = AgentError(f"Refine model call failed: {type(e).__name__}: {e}")
            print(f"[agent] rid={request_id} refine attempt={attempt + 1} error: {type(e).__name__}: {e}")

    raise last_error or AgentError("Unknown refine failure")


# ---------------------------------------------------------------------------
# Second-pass SoM refinement (marker selection on zoomed crop)
# ---------------------------------------------------------------------------


async def generate_som_refine(
    instruction: str,
    target_label: str,
    crop_image_bytes: bytes,
    request_id: str = "",
) -> dict:
    """
    Second-pass SoM: given a zoomed crop with dense markers,
    ask the model to select all markers covering the target element.
    Returns a dict with marker_ids (list), confidence, label.
    """
    client = _get_client()

    prompt = _SOM_REFINE_PROMPT_TEMPLATE
    prompt = prompt.replace("{{INSTRUCTION}}", instruction)
    prompt = prompt.replace("{{TARGET_LABEL}}", target_label or "")

    crop_b64 = base64.b64encode(crop_image_bytes).decode("utf-8")

    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{crop_b64}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    last_error: Exception | None = None

    for attempt in range(1 + MAX_RETRIES):
        try:
            print(f"[agent] rid={request_id} som-refine attempt={attempt + 1} model={model}")

            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                **_model_params(model, 500),
                response_format={"type": "json_object"},
            )

            raw_text = response.choices[0].message.content or ""
            print(f"[agent] rid={request_id} som-refine raw response length={len(raw_text)}")

            result = _extract_json(raw_text)

            # Normalize to always have marker_ids as a list
            if "marker_ids" in result and isinstance(result["marker_ids"], list):
                ids = result["marker_ids"]
            elif "marker_id" in result:
                ids = [result["marker_id"]]
                result["marker_ids"] = ids
            else:
                raise AgentError("som-refine response missing marker_ids")

            print(f"[agent] rid={request_id} som-refine picked marker_ids={ids} conf={result.get('confidence')} label={result.get('label')!r}")
            return result

        except AgentError:
            raise
        except json.JSONDecodeError as e:
            last_error = AgentError(f"Invalid JSON from som-refine model: {e}")
            print(f"[agent] rid={request_id} som-refine attempt={attempt + 1} JSON parse error: {e}")
        except Exception as e:
            last_error = AgentError(f"SoM-refine model call failed: {type(e).__name__}: {e}")
            print(f"[agent] rid={request_id} som-refine attempt={attempt + 1} error: {type(e).__name__}: {e}")

    raise last_error or AgentError("Unknown som-refine failure")


# ---------------------------------------------------------------------------
# OmniParser-based plan generation (element ID selection)
# ---------------------------------------------------------------------------


async def generate_omniparser_plan(
    goal: str,
    image_size: ImageSize,
    annotated_screenshot_bytes: bytes,
    elements_context: str,
    learning_profile: str | None = None,
    app_context: str | None = None,
    session_summary: str | None = None,
    request_id: str = "",
) -> OmniPlanResponse:
    """
    Given an OmniParser-annotated screenshot and a structured element list,
    ask the LLM to select which element IDs correspond to each step.
    Returns an OmniPlanResponse with element_ids per step.
    """
    client = _get_client()

    image_size_json = json.dumps({"w": image_size.w, "h": image_size.h})
    prompt = _OMNI_PLAN_PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{IMAGE_SIZE_JSON}}", image_size_json)
    prompt = prompt.replace("{{ELEMENTS_CONTEXT}}", elements_context)
    prompt = prompt.replace("{{LEARNING_PROFILE_TEXT}}", learning_profile or "default")
    prompt = prompt.replace("{{APP_CONTEXT_JSON}}", app_context or "{}")
    prompt = prompt.replace("{{SESSION_SUMMARY}}", session_summary or "none")

    screenshot_b64 = base64.b64encode(annotated_screenshot_bytes).decode("utf-8")

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

    raw_text = await _call_llm_with_backoff(
        client, model, messages, _model_params(model, 2000),
        request_id, "omni-plan"
    )
    plan_dict = _extract_json(raw_text)
    plan = OmniPlanResponse.model_validate(plan_dict)
    print(f"[agent] rid={request_id} omni-plan validated: {len(plan.steps)} steps")
    return plan


# ---------------------------------------------------------------------------
# OmniParser refine: pick element from a zoomed crop
# ---------------------------------------------------------------------------


async def generate_omniparser_refine(
    instruction: str,
    target_label: str,
    crop_image_bytes: bytes,
    elements_context: str,
    request_id: str = "",
    raw_crop_bytes: bytes | None = None,
) -> dict:
    """
    Given a zoomed crop with OmniParser-detected elements, ask the LLM
    to pick which element_id(s) match the target.
    Sends both raw crop (for reading text) and annotated crop (for box numbers).
    Returns dict with element_ids (list[int]), confidence, label.
    """
    client = _get_client()

    prompt = _OMNI_REFINE_PROMPT_TEMPLATE
    prompt = prompt.replace("{{INSTRUCTION}}", instruction)
    prompt = prompt.replace("{{TARGET_LABEL}}", target_label or "")
    prompt = prompt.replace("{{ELEMENTS_CONTEXT}}", elements_context)

    # Send the ANNOTATED crop (with numbered boxes) so the LLM can see
    # which element_id maps to which box, plus the raw crop so it can
    # read actual text labels on the UI underneath.
    annotated_b64 = base64.b64encode(crop_image_bytes).decode("utf-8")
    content: list[dict] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{annotated_b64}", "detail": "high"}},
    ]
    # Also send the raw (unannotated) crop so the LLM can read text beneath the boxes
    if raw_crop_bytes:
        raw_b64 = base64.b64encode(raw_crop_bytes).decode("utf-8")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{raw_b64}", "detail": "high"}}
        )

    # Use the main model for refine — accuracy matters here
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    messages = [{"role": "user", "content": content}]

    raw_text = await _call_llm_with_backoff(
        client, model, messages, _model_params(model, 500),
        request_id, "omni-refine"
    )
    result = _extract_json(raw_text)

    # Normalize element_ids
    if "element_ids" in result and isinstance(result["element_ids"], list):
        ids = result["element_ids"]
    elif "element_id" in result:
        ids = [result["element_id"]]
        result["element_ids"] = ids
    else:
        raise AgentError("omni-refine response missing element_ids")

    print(f"[agent] rid={request_id} omni-refine picked element_ids={ids} conf={result.get('confidence')} label={result.get('label')!r}")
    return result


# ---------------------------------------------------------------------------
# Gemini one-shot: full screenshot → YOLO elements → single LLM call → plan
# ---------------------------------------------------------------------------


async def generate_gemini_plan(
    goal: str,
    annotated_screenshot_bytes: bytes,
    raw_screenshot_bytes: bytes,
    elements_context: str,
    request_id: str = "",
    search_context: str = "",
) -> dict:
    """
    One-shot plan generation optimized for Gemini's native vision + GUI grounding.
    Sends both annotated (numbered boxes) and raw screenshot.
    Returns dict with steps, each containing box_2d [ymin, xmin, ymax, xmax] on 0-1000 scale.
    """
    client = _get_client()

    prompt = _GEMINI_PLAN_PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{ELEMENTS_CONTEXT}}", elements_context or "(no elements detected)")
    prompt = prompt.replace("{{SEARCH_CONTEXT}}", search_context or "none")

    # Send annotated screenshot (numbered boxes) + raw screenshot (readable text)
    annotated_b64 = base64.b64encode(annotated_screenshot_bytes).decode("utf-8")
    raw_b64 = base64.b64encode(raw_screenshot_bytes).decode("utf-8")

    content: list[dict] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{annotated_b64}", "detail": "high"}},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{raw_b64}", "detail": "high"}},
    ]

    model = os.getenv("OPENAI_MODEL", "gemini-3-pro-preview")
    messages = [{"role": "user", "content": content}]

    raw_text = await _call_llm_with_backoff(
        client, model, messages, _model_params(model, 2000),
        request_id, "gemini-plan"
    )
    result = _extract_json(raw_text)
    print(f"[agent] rid={request_id} gemini-plan: {len(result.get('steps', []))} steps")
    # Log full raw response for debugging element selection
    print(f"[agent] rid={request_id} gemini-plan raw response:\n{raw_text[:3000]}")
    return result


async def generate_gemini_plan_stream(
    goal: str,
    annotated_screenshot_bytes: bytes,
    raw_screenshot_bytes: bytes,
    elements_context: str,
    request_id: str = "",
    search_context: str = "",
):
    """
    Streaming version of generate_gemini_plan.
    Yields partial results as they arrive:
      1. {"type": "instruction", "text": "..."} — as soon as instruction is found
      2. {"type": "plan", "data": {...}} — full parsed JSON when complete
    """
    client = _get_client()

    prompt = _GEMINI_PLAN_PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{ELEMENTS_CONTEXT}}", elements_context or "(no elements detected)")
    prompt = prompt.replace("{{SEARCH_CONTEXT}}", search_context or "none")

    annotated_b64 = base64.b64encode(annotated_screenshot_bytes).decode("utf-8")
    raw_b64 = base64.b64encode(raw_screenshot_bytes).decode("utf-8")

    content: list[dict] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{annotated_b64}", "detail": "high"}},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{raw_b64}", "detail": "high"}},
    ]

    model = os.getenv("OPENAI_MODEL", "gemini-3-pro-preview")
    params = _model_params(model, 2000)

    extra_kwargs = {}
    if _supports_json_mode(model):
        extra_kwargs["response_format"] = {"type": "json_object"}

    print(f"[agent] rid={request_id} gemini-plan-stream model={model}")

    buffer = ""
    instruction_sent = False

    async with _llm_semaphore:
        stream = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            stream=True,
            **params,
            **extra_kwargs,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                buffer += delta.content

                # Try to extract instruction early from partial JSON
                if not instruction_sent and '"instruction"' in buffer:
                    import re
                    m = re.search(r'"instruction"\s*:\s*"([^"]*)"', buffer)
                    if m:
                        instruction_sent = True
                        yield {"type": "instruction", "text": m.group(1)}

    # Full response complete — parse and yield (with retry on bad JSON)
    print(f"[agent] rid={request_id} gemini-plan-stream complete, length={len(buffer)}")
    try:
        result = _extract_json(buffer)
        yield {"type": "plan", "data": result}
    except AgentError as parse_err:
        print(f"[agent] rid={request_id} gemini-plan-stream JSON parse failed: {parse_err}, retrying non-streaming...")
        # Retry once with a fresh non-streaming call
        try:
            result = await generate_gemini_plan(
                goal=goal,
                annotated_screenshot_bytes=annotated_screenshot_bytes,
                raw_screenshot_bytes=raw_screenshot_bytes,
                elements_context=elements_context,
                request_id=request_id + "-retry",
                search_context=search_context,
            )
            # generate_gemini_plan returns a dict (already parsed)
            yield {"type": "plan", "data": result}
        except Exception as retry_err:
            print(f"[agent] rid={request_id} gemini-plan-stream retry also failed: {retry_err}")
            raise parse_err


# ---------------------------------------------------------------------------
# Gemini one-shot next step
# ---------------------------------------------------------------------------


async def generate_gemini_next(
    goal: str,
    annotated_screenshot_bytes: bytes,
    raw_screenshot_bytes: bytes,
    elements_context: str,
    completed_steps_summary: str,
    num_completed: int,
    total_steps: int,
    request_id: str = "",
    search_context: str = "",
) -> dict:
    """
    Given a fresh screenshot after the user completed a step, ask Gemini
    what to do next (continue / done / retry).
    """
    client = _get_client()

    prompt = _GEMINI_NEXT_PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{SEARCH_CONTEXT}}", search_context or "none")
    prompt = prompt.replace("{{NUM_COMPLETED}}", str(num_completed))
    prompt = prompt.replace("{{TOTAL_STEPS}}", str(total_steps))
    prompt = prompt.replace("{{COMPLETED_STEPS}}", completed_steps_summary or "none yet")
    prompt = prompt.replace("{{ELEMENTS_CONTEXT}}", elements_context or "(no elements detected)")

    annotated_b64 = base64.b64encode(annotated_screenshot_bytes).decode("utf-8")
    raw_b64 = base64.b64encode(raw_screenshot_bytes).decode("utf-8")

    content: list[dict] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{annotated_b64}", "detail": "high"}},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{raw_b64}", "detail": "high"}},
    ]

    model = os.getenv("OPENAI_MODEL", "gemini-3-pro-preview")
    messages = [{"role": "user", "content": content}]

    raw_text = await _call_llm_with_backoff(
        client, model, messages, _model_params(model, 2000),
        request_id, "gemini-next"
    )
    result = _extract_json(raw_text)
    print(f"[agent] rid={request_id} gemini-next: status={result.get('status')} steps={len(result.get('steps', []))}")
    # Log full raw response for debugging element selection
    print(f"[agent] rid={request_id} gemini-next raw response:\n{raw_text[:3000]}")
    return result


# ---------------------------------------------------------------------------
# Native Gemini SDK: file upload + generation with pre-uploaded images
# ---------------------------------------------------------------------------

_genai_configured = False


def _ensure_genai():
    """Configure the native Gemini SDK (lazy, once)."""
    global _genai_configured
    if not _genai_available:
        raise RuntimeError("google-generativeai not installed")
    if _genai_configured:
        return
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=key)
    _genai_configured = True
    print("[agent] Gemini native SDK configured")


def is_native_genai_available() -> bool:
    """Check if native Gemini file upload is available."""
    return _genai_available and bool(os.getenv("GEMINI_API_KEY"))


def _wait_for_file_active(f):
    """Poll a Gemini file until it's done processing."""
    while f.state.name == "PROCESSING":
        time.sleep(0.3)
        f = genai.get_file(f.name)
    return f


def upload_images_to_gemini(
    annotated_bytes: bytes,
    raw_bytes: bytes,
) -> tuple:
    """
    Upload annotated + raw screenshots to Gemini File API.
    Files persist on Google's servers for 48h — no re-upload needed.
    Returns (annotated_file, raw_file) objects usable in generate_content().
    """
    _ensure_genai()

    # Write to temp files (SDK needs file paths)
    paths = []
    try:
        for label, data in [("annotated", annotated_bytes), ("raw", raw_bytes)]:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(data)
            tmp.close()
            paths.append(tmp.name)

        annotated_file = genai.upload_file(paths[0], mime_type="image/png")
        raw_file = genai.upload_file(paths[1], mime_type="image/png")

        # Poll until processing completes (usually instant for images)
        annotated_file = _wait_for_file_active(annotated_file)
        raw_file = _wait_for_file_active(raw_file)

        print(f"[agent] Uploaded to Gemini: annotated={annotated_file.name} raw={raw_file.name}")
        return annotated_file, raw_file

    finally:
        for p in paths:
            try:
                os.unlink(p)
            except Exception:
                pass


async def generate_gemini_plan_with_files_stream(
    goal: str,
    annotated_file,
    raw_file,
    elements_context: str,
    request_id: str = "",
    search_context: str = "",
):
    """
    Streaming plan generation using pre-uploaded Gemini files.
    Since images are already on Google's servers, this skips the
    ~3-8MB base64 upload and starts generating immediately.
    Yields same events as generate_gemini_plan_stream.
    """
    _ensure_genai()

    prompt = _GEMINI_PLAN_PROMPT_TEMPLATE
    prompt = prompt.replace("{{GOAL}}", goal)
    prompt = prompt.replace("{{ELEMENTS_CONTEXT}}", elements_context or "(no elements detected)")
    prompt = prompt.replace("{{SEARCH_CONTEXT}}", search_context or "none")

    model_name = os.getenv("OPENAI_MODEL", "gemini-3-flash-preview")
    model = genai.GenerativeModel(model_name)

    print(f"[agent] rid={request_id} gemini-plan-files-stream model={model_name}")

    contents = [prompt, annotated_file, raw_file]

    buffer = ""
    instruction_sent = False

    async with _llm_semaphore:
        response = await model.generate_content_async(
            contents,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                max_output_tokens=2000,
                temperature=0.1,
            ),
            stream=True,
        )

        async for chunk in response:
            text = ""
            try:
                text = chunk.text or ""
            except Exception:
                pass
            if text:
                buffer += text

                # Try to extract instruction early from partial JSON
                if not instruction_sent and '"instruction"' in buffer:
                    m = re.search(r'"instruction"\s*:\s*"([^"]*)"', buffer)
                    if m:
                        instruction_sent = True
                        yield {"type": "instruction", "text": m.group(1)}

    # Full response complete — parse and yield (with retry on bad JSON)
    print(f"[agent] rid={request_id} gemini-plan-files-stream complete, length={len(buffer)}")
    print(f"[agent] rid={request_id} raw buffer: {buffer[:500]!r}")
    try:
        result = _extract_json(buffer)
        yield {"type": "plan", "data": result}
    except AgentError as parse_err:
        print(f"[agent] rid={request_id} gemini-plan-files-stream JSON parse failed: {parse_err}, retrying...")
        # Retry once using the same files + non-streaming native call
        try:
            retry_response = await model.generate_content_async(
                contents,
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                    max_output_tokens=2000,
                    temperature=0.1,
                ),
            )
            retry_text = retry_response.text or ""
            print(f"[agent] rid={request_id} retry response length={len(retry_text)}")
            result = _extract_json(retry_text)
            yield {"type": "plan", "data": result}
        except Exception as retry_err:
            print(f"[agent] rid={request_id} retry also failed: {retry_err}")
            raise parse_err
