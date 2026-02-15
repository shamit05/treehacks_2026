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
from pathlib import Path

from openai import AsyncOpenAI

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
# Model-adaptive parameters
# ---------------------------------------------------------------------------

def _model_params(model: str, max_tokens: int) -> dict:
    """
    Return API kwargs adapted for the model.
    Some models (gpt-5-mini, o-series) require max_completion_tokens
    instead of max_tokens, and only support temperature=1.
    """
    # Models that need the new-style parameters
    new_style = any(tag in model.lower() for tag in ["gpt-5", "o1", "o3", "o4"])
    if new_style:
        return {"max_completion_tokens": max_tokens, "temperature": 1}
    else:
        return {"max_tokens": max_tokens, "temperature": 0.1}


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
    """
    async with _llm_semaphore:
        for attempt in range(1 + MAX_RETRIES):
            try:
                print(f"[agent] rid={request_id} {label} attempt={attempt + 1} model={model}")
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **params,
                    response_format={"type": "json_object"},
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
) -> dict:
    """
    Given a zoomed crop with OmniParser-detected elements, ask the LLM
    to pick which element_id(s) match the target.
    Returns dict with element_ids (list[int]), confidence, label.
    """
    client = _get_client()

    prompt = _OMNI_REFINE_PROMPT_TEMPLATE
    prompt = prompt.replace("{{INSTRUCTION}}", instruction)
    prompt = prompt.replace("{{TARGET_LABEL}}", target_label or "")
    prompt = prompt.replace("{{ELEMENTS_CONTEXT}}", elements_context)

    crop_b64 = base64.b64encode(crop_image_bytes).decode("utf-8")

    # Use the fast model for refine — simple element-picking task
    model = os.getenv("OPENAI_NEXT_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o"))
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
