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

<<<<<<< Current (Your changes)
from app.schemas.step_plan import BBoxNorm, ImageSize
from app.schemas.step_plan import StepPlan
=======
from app.schemas.step_plan import (
    ImageSize,
    RefineResponse,
    SoMStepPlan,
    StepPlan,
)
>>>>>>> Incoming (Background Agent changes)

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
                max_tokens=2000,
                temperature=0.1,
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
                max_tokens=500,
                temperature=0.1,
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
                max_tokens=2000,
                temperature=0.1,
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
) -> StepPlan:
    """
    Given a fresh screenshot after the user completed a step, identify
    the next 1-2 actions. Returns a StepPlan with only those steps.
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

    last_error: Exception | None = None

    for attempt in range(1 + MAX_RETRIES):
        try:
            print(f"[agent] rid={request_id} next-step attempt={attempt + 1} model={model}")

            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=1000,
                temperature=0.1,
                response_format={"type": "json_object"},
            )

            raw_text = response.choices[0].message.content or ""
            print(f"[agent] rid={request_id} next-step response length={len(raw_text)}")

            plan_dict = _extract_json(raw_text)
            plan = StepPlan.model_validate(plan_dict)

            print(f"[agent] rid={request_id} next-step validated: {len(plan.steps)} steps")
            return plan

        except AgentError:
            raise
        except json.JSONDecodeError as e:
            last_error = AgentError(f"Invalid JSON from model on next-step: {e}")
            print(f"[agent] rid={request_id} next-step attempt={attempt + 1} JSON error: {e}")
        except Exception as e:
            last_error = AgentError(f"Next-step model call failed: {type(e).__name__}: {e}")
            print(f"[agent] rid={request_id} next-step attempt={attempt + 1} error: {e}")

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
                max_tokens=2000,
                temperature=0.1,
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
                max_tokens=500,
                temperature=0.1,
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
                max_tokens=500,
                temperature=0.1,
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
