"""
app.py — Modal app entry point for OverlayGuide Voice Assistant.

Adapted from the ragbot's app.py:
  - App name: overlay-voice-assistant
  - No ChromaDB, no RAG preload
  - run_bot imports from server.bot.overlay_guide_bot
  - No frontend static files (Mac client connects directly)
  - Adds /ws/{session_id} WebSocket for control channel (screenshots + plans)
  - Keeps: /offer endpoint, modal.Dict signaling, SmallWebRTCConnection

Architecture:
  Mac client                    ASGI app (this file)           Bot container
  ──────────                    ────────────────────           ─────────────
  POST /offer  ───────────────→ creates session Dict  ───────→ spawns run_bot()
  ←─── SDP answer ────────────  via ephemeral Dict    ←──────  WebRTC answer

  WS /ws/{session_id} ────────→ relays screenshots    ───────→ AgentServerProcessor
                                to session Dict                reads Dict

  ←─── StepPlan JSON ─────────  polls session Dict    ←──────  writes plan to Dict
"""

import asyncio
import json
import os
import time
import uuid

import modal

from server import SERVICE_REGIONS

APP_NAME = "overlay-voice-assistant"

app = modal.App(APP_NAME)

# Container image for the Pipecat bot pipeline
bot_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "ffmpeg")
    .uv_pip_install(
        "pipecat-ai[webrtc,openai,silero,local-smart-turn,noisereduce,soundfile]==0.0.92",
        "websocket-client",
        "aiohttp",
        "fastapi[standard]",
    )
    .add_local_dir("server", remote_path="/root/server")
)

MINUTES = 60  # seconds

with bot_image.imports():
    from loguru import logger
    from pipecat.transports.smallwebrtc.connection import (
        IceServer,
        SmallWebRTCConnection,
    )

    from server.bot.overlay_guide_bot import run_bot


@app.cls(
    image=bot_image,
    timeout=30 * MINUTES,
    region=SERVICE_REGIONS,
    enable_memory_snapshot=True,
    max_inputs=1,
    # min_containers=1,  # uncomment to keep warm for testing
)
class OverlayVoiceAssistant:

    @modal.enter(snap=True)
    def load(self):
        """No heavy preloading needed (unlike ragbot's ChromaDB)."""
        pass

    @modal.method()
    async def run_bot(self, signal_dict: modal.Dict):
        """Launch the bot pipeline with WebRTC connection.

        Args:
            signal_dict: Ephemeral Dict containing offer, ice_servers,
                         and session_dict_name for cross-container comms.
        """
        try:
            start = time.time()

            offer = await signal_dict.get.aio("offer")
            ice_servers_raw = await signal_dict.get.aio("ice_servers")
            session_dict_name = await signal_dict.get.aio("session_dict_name")
            agent_server_url = await signal_dict.get.aio("agent_server_url")

            ice_servers = [IceServer(**s) for s in ice_servers_raw]
            logger.info(
                f"Got signaling data in {time.time() - start:.3f}s, "
                f"session={session_dict_name}"
            )

            # Initialize WebRTC
            webrtc_connection = SmallWebRTCConnection(ice_servers)
            await webrtc_connection.initialize(
                sdp=offer["sdp"], type=offer["type"]
            )

            @webrtc_connection.event_handler("closed")
            async def handle_disconnected(conn: SmallWebRTCConnection):
                logger.info("WebRTC connection closed.")

            # Send SDP answer back through the signal Dict
            answer = webrtc_connection.get_answer()
            await signal_dict.put.aio("answer", answer)
            logger.info(f"SDP answer sent in {time.time() - start:.3f}s")

            # Run the bot pipeline
            await run_bot(
                webrtc_connection=webrtc_connection,
                session_dict_name=session_dict_name,
                agent_server_url=agent_server_url or "http://localhost:8000",
            )

        except Exception as e:
            logger.error(f"Bot pipeline failed: {e}")
            raise RuntimeError(f"Failed to start bot pipeline: {e}")

    @modal.method()
    def ping(self):
        return "pong"


# ---------------------------------------------------------------------------
# ASGI frontend — /offer + /ws/{session_id}
# ---------------------------------------------------------------------------

frontend_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi==0.115.12")
    .add_local_dir("server", remote_path="/root/server")
)

with frontend_image.imports():
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware

_ICE_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]

# Default agent-server URL — override via AGENT_SERVER_URL env var
_AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", "http://localhost:8000")


@app.function(image=frontend_image)
@modal.asgi_app()
@modal.concurrent(max_inputs=100)
def serve_frontend():
    """ASGI app with WebRTC signaling + control WebSocket."""

    web_app = FastAPI()

    web_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @web_app.get("/health")
    async def health():
        return {"status": "ok", "app": APP_NAME}

    # ------------------------------------------------------------------
    # POST /offer — WebRTC signaling (same flow as ragbot)
    # ------------------------------------------------------------------
    @web_app.post("/offer")
    async def offer(offer: dict):
        start = time.time()

        # Create a persistent session Dict for screenshot/plan relay
        session_id = str(uuid.uuid4())
        session_dict_name = f"overlay-session-{session_id}"
        session_dict = modal.Dict.from_name(
            session_dict_name, create_if_missing=True
        )

        # Use an ephemeral Dict for the one-time offer/answer exchange
        with modal.Dict.ephemeral() as signal_dict:
            await signal_dict.put.aio("ice_servers", _ICE_SERVERS)
            await signal_dict.put.aio("offer", offer)
            await signal_dict.put.aio("session_dict_name", session_dict_name)
            await signal_dict.put.aio("agent_server_url", _AGENT_SERVER_URL)

            logger.info(
                f"Offer stored in {time.time() - start:.3f}s, "
                f"spawning bot session={session_id}"
            )

            # Spawn the bot in its own container
            bot_call = OverlayVoiceAssistant().run_bot.spawn(signal_dict)

            # Wait for the bot to produce an SDP answer
            try:
                while True:
                    answer = await signal_dict.get.aio("answer")
                    if answer:
                        logger.info(
                            f"Answer received in {time.time() - start:.3f}s"
                        )
                        # Return session_id so the client can connect to /ws
                        return {
                            "sdp": answer["sdp"],
                            "type": answer["type"],
                            "session_id": session_id,
                        }
                    await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Error during signaling: {type(e)}: {e}")
                bot_call.cancel()
                raise e

    # ------------------------------------------------------------------
    # WS /ws/{session_id} — control channel (screenshots + plans)
    # ------------------------------------------------------------------
    @web_app.websocket("/ws/{session_id}")
    async def control_ws(websocket: WebSocket, session_id: str):
        """Bidirectional control channel between Mac client and bot.

        Mac → server:
            {"type": "screenshot", "data": "<base64 PNG>", "image_size": {"w": int, "h": int}}

        Server → Mac:
            {"type": "step_plan", "plan": {...}}
            {"type": "request_screenshot"}
        """
        session_dict_name = f"overlay-session-{session_id}"
        try:
            session_dict = modal.Dict.from_name(session_dict_name)
        except Exception:
            await websocket.close(code=4004, reason="Session not found")
            return

        await websocket.accept()
        logger.info(f"Control WS connected for session {session_id}")

        async def recv_loop():
            """Receive messages from Mac client and store in session Dict."""
            while True:
                try:
                    msg = await websocket.receive_json()
                    msg_type = msg.get("type")

                    if msg_type == "screenshot":
                        await session_dict.put.aio(
                            "screenshot_data", msg["data"]
                        )
                        await session_dict.put.aio(
                            "screenshot_size", msg.get("image_size", {"w": 1512, "h": 982})
                        )
                        logger.info(
                            f"Screenshot stored for session {session_id}"
                        )
                    else:
                        logger.warning(
                            f"Unknown message type from client: {msg_type}"
                        )

                except WebSocketDisconnect:
                    logger.info(f"Control WS disconnected: {session_id}")
                    break
                except Exception as e:
                    logger.error(f"Control WS recv error: {e}")
                    break

        async def relay_loop():
            """Poll session Dict for plans and screenshot requests, relay to Mac."""
            last_plan = None
            while True:
                try:
                    # Check for screenshot request from bot
                    try:
                        requested = await session_dict.get.aio(
                            "screenshot_requested"
                        )
                        if requested:
                            await session_dict.put.aio(
                                "screenshot_requested", False
                            )
                            await websocket.send_json(
                                {"type": "request_screenshot"}
                            )
                            logger.info("Relayed screenshot request to client")
                    except Exception:
                        pass

                    # Check for new plan from bot
                    try:
                        plan_json = await session_dict.get.aio("latest_plan")
                        if plan_json and plan_json != last_plan:
                            last_plan = plan_json
                            await websocket.send_json(
                                {
                                    "type": "step_plan",
                                    "plan": json.loads(plan_json),
                                }
                            )
                            logger.info("Relayed plan to client")
                    except Exception:
                        pass

                except Exception as e:
                    logger.error(f"Control WS relay error: {e}")
                    break

                await asyncio.sleep(0.15)  # poll interval

        try:
            await asyncio.gather(recv_loop(), relay_loop())
        except Exception:
            pass
        finally:
            logger.info(f"Control WS closing for session {session_id}")
            # Clean up session Dict
            try:
                await modal.Dict.delete.aio(session_dict_name)
            except Exception:
                pass

    return web_app


# ---------------------------------------------------------------------------
# Warm up GPU snapshots
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bot = modal.Cls.from_name(APP_NAME, "OverlayVoiceAssistant")
    num_cold_starts = 50
    for _ in range(num_cold_starts):
        start = time.time()
        bot().ping.remote()
        elapsed = time.time() - start
        print(f"Ping: {elapsed:.3f}s")
        time.sleep(10.0)
    print(f"Warmed up {num_cold_starts} cold starts")
