"""
AgentServerProcessor — the bridge between the voice pipeline and the existing
agent-server.  This is the only new custom code in the voice integration.

Pipeline position:  STT → AgentServerProcessor → TTS

On receiving a TranscriptionFrame (user's spoken goal):
  1. Request a screenshot from the Mac client (via Modal Dict → ASGI WS relay).
  2. Wait for the Mac client to upload the screenshot (stored in Modal Dict).
  3. POST /plan to the agent-server with the goal + screenshot.
  4. Push each step instruction as a TextFrame → TTS speaks it.
  5. Store the full StepPlan JSON in Modal Dict → ASGI relays to Mac overlay.

Cross-container communication:
  The bot runs in a separate Modal container from the ASGI frontend.
  All data exchange with the Mac client goes through a named Modal Dict:
    - "screenshot_requested" (bool): bot sets True → ASGI relays to Mac
    - "screenshot_data" (str): Mac sends screenshot → ASGI stores base64 here
    - "screenshot_size" (dict): {"w": int, "h": int}
    - "latest_plan" (str): bot stores StepPlan JSON → ASGI relays to Mac
"""

import asyncio
import base64
import json
import os
import time

import aiohttp
from loguru import logger

from pipecat.frames.frames import (
    Frame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import modal


class AgentServerProcessor(FrameProcessor):
    """Pipecat FrameProcessor that bridges STT transcriptions to the
    existing agent-server /plan endpoint and outputs instructions for TTS.

    Uses a named Modal Dict for cross-container communication with the
    ASGI frontend (which relays to/from the Mac client).
    """

    def __init__(
        self,
        session_dict_name: str,
        agent_server_url: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._dict_name = session_dict_name
        self._session_dict = modal.Dict.from_name(session_dict_name)
        self._agent_url = (
            agent_server_url
            or os.environ.get("AGENT_SERVER_URL", "http://localhost:8000")
        ).rstrip("/")
        self._http: aiohttp.ClientSession | None = None

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            )
        return self._http

    async def cleanup(self):
        if self._http and not self._http.closed:
            await self._http.close()
        await super().cleanup()

    # ------------------------------------------------------------------
    # Frame processing
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            goal = frame.text.strip()
            logger.info(f"[AgentServerProcessor] transcription: {goal!r}")

            if not goal:
                await self.push_frame(
                    TextFrame("I didn't catch that. Could you repeat your goal?")
                )
                return

            # 1. Request a fresh screenshot from the Mac client
            await self._session_dict.put.aio("screenshot_requested", True)

            # 2. Wait for the Mac client to upload a screenshot
            screenshot_b64 = await self._wait_for_screenshot(timeout=10.0)
            if screenshot_b64 is None:
                await self.push_frame(
                    TextFrame(
                        "I need to see your screen to help. "
                        "Please make sure the overlay app is running."
                    )
                )
                return

            screenshot_bytes = base64.b64decode(screenshot_b64)
            try:
                image_size = await self._session_dict.get.aio("screenshot_size")
            except Exception:
                image_size = {"w": 1512, "h": 982}

            # 3. Call agent-server /plan
            try:
                plan = await self._call_plan(goal, screenshot_bytes, image_size)
            except Exception as e:
                logger.error(f"[AgentServerProcessor] /plan failed: {e}")
                await self.push_frame(
                    TextFrame(
                        "I had trouble creating a plan. "
                        "Could you be more specific about what you want to do?"
                    )
                )
                return

            # 4. Store plan in Dict → ASGI relays to Mac overlay
            try:
                await self._session_dict.put.aio(
                    "latest_plan", json.dumps(plan)
                )
            except Exception as e:
                logger.error(f"[AgentServerProcessor] failed to store plan: {e}")

            # 5. Speak each step instruction via TTS
            steps = plan.get("steps", [])
            if not steps:
                await self.push_frame(
                    TextFrame(
                        "I couldn't figure out steps for that goal. "
                        "Could you try rephrasing?"
                    )
                )
                return

            for i, step in enumerate(steps, 1):
                instruction = step.get("instruction", "")
                if instruction:
                    await self.push_frame(
                        TextFrame(f"Step {i}: {instruction}")
                    )

        else:
            # Pass through all non-transcription frames unchanged
            await self.push_frame(frame, direction)

    # ------------------------------------------------------------------
    # Screenshot polling
    # ------------------------------------------------------------------

    async def _wait_for_screenshot(self, timeout: float = 10.0) -> str | None:
        """Poll the Modal Dict for a screenshot uploaded by the Mac client.
        Returns base64-encoded PNG string or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = await self._session_dict.get.aio("screenshot_data")
                if data:
                    logger.info(
                        f"[AgentServerProcessor] got screenshot ({len(data)} chars b64)"
                    )
                    # Clear the flag so we don't re-read stale data on next turn
                    await self._session_dict.put.aio("screenshot_data", "")
                    return data
            except Exception:
                pass
            await asyncio.sleep(0.15)
        logger.warning("[AgentServerProcessor] screenshot timeout")
        return None

    # ------------------------------------------------------------------
    # Agent-server HTTP call
    # ------------------------------------------------------------------

    async def _call_plan(
        self, goal: str, screenshot: bytes, image_size: dict
    ) -> dict:
        """POST /plan to the agent-server with the goal + screenshot."""
        session = await self._get_http()
        url = f"{self._agent_url}/plan"

        data = aiohttp.FormData()
        data.add_field("goal", goal)
        data.add_field("image_size", json.dumps(image_size))
        data.add_field(
            "screenshot",
            screenshot,
            filename="screenshot.png",
            content_type="image/png",
        )

        t0 = time.perf_counter()
        logger.info(f"[AgentServerProcessor] POST {url} goal={goal!r}")

        async with session.post(url, data=data) as resp:
            elapsed = time.perf_counter() - t0
            logger.info(
                f"[AgentServerProcessor] /plan → {resp.status} in {elapsed:.2f}s"
            )

            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"Agent-server returned {resp.status}: {body[:500]}"
                )

            plan = await resp.json()
            logger.info(
                f"[AgentServerProcessor] plan has "
                f"{len(plan.get('steps', []))} steps"
            )
            return plan
