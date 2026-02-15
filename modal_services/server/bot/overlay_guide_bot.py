"""
overlay_guide_bot.py — Pipecat pipeline for OverlayGuide voice assistant.

Adapted from the ragbot's moe_and_dal_bot.py but with a much simpler pipeline:
  Transport (WebRTC audio) → STT (Parakeet) → AgentServerProcessor → TTS (Kokoro) → Transport

No RAG, no ChromaDB, no LLM context, no video/avatar.
The "brain" is the existing agent-server called via HTTP from AgentServerProcessor.
"""

import sys
from loguru import logger

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask

from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor

from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.base_transport import TransportParams

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams

from pipecat.frames.frames import TextFrame

from .services.modal_services import ModalTunnelManager
from .services.modal_parakeet_service import ModalParakeetSegmentedSTTService
from .services.modal_kokoro_service import ModalKokoroTTSService
from .processors.agent_server_processor import AgentServerProcessor

try:
    logger.remove(0)
    logger.add(sys.stderr, level="DEBUG")
except ValueError:
    pass

_AUDIO_INPUT_SAMPLE_RATE = 16000
_AUDIO_OUTPUT_SAMPLE_RATE = 24000


async def run_bot(
    webrtc_connection: SmallWebRTCConnection,
    session_dict_name: str,
    agent_server_url: str = "http://localhost:8000",
):
    """Main bot execution function.

    Sets up and runs the Pipecat pipeline:
      WebRTC audio in → Parakeet STT → AgentServerProcessor → Kokoro TTS → WebRTC audio out

    Args:
        webrtc_connection: The SmallWebRTC connection from the /offer endpoint.
        session_dict_name: Name of the Modal Dict used for cross-container
            communication (screenshots, plans) with the ASGI frontend.
        agent_server_url: URL of the existing agent-server (default localhost:8000).
    """

    # ------------------------------------------------------------------
    # Spawn Modal GPU services (STT + TTS)
    # ------------------------------------------------------------------
    parakeet_tunnel = ModalTunnelManager(
        app_name="parakeet-transcription",
        cls_name="Transcriber",
    )

    kokoro_tunnel = ModalTunnelManager(
        app_name="kokoro-tts",
        cls_name="KokoroTTS",
    )

    # ------------------------------------------------------------------
    # Transport (WebRTC, audio only)
    # ------------------------------------------------------------------
    transport_params = TransportParams(
        audio_in_enabled=True,
        audio_in_sample_rate=_AUDIO_INPUT_SAMPLE_RATE,
        audio_out_enabled=True,
        audio_out_sample_rate=_AUDIO_OUTPUT_SAMPLE_RATE,
        video_out_enabled=False,
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(stop_secs=0.2)
        ),
        turn_analyzer=LocalSmartTurnAnalyzerV3(
            params=SmartTurnParams()
        ),
    )

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=transport_params,
    )

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------
    stt = ModalParakeetSegmentedSTTService(
        modal_tunnel_manager=parakeet_tunnel,
    )

    tts = ModalKokoroTTSService(
        modal_tunnel_manager=kokoro_tunnel,
        voice="am_puck",
        speed=1.35,
    )

    agent_processor = AgentServerProcessor(
        session_dict_name=session_dict_name,
        agent_server_url=agent_server_url,
    )

    # RTVI events for Pipecat client UI
    rtvi = RTVIProcessor()

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------
    pipeline = Pipeline(
        processors=[
            transport.input(),
            rtvi,
            stt,
            agent_processor,
            tts,
            transport.output(),
        ],
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
        observers=[RTVIObserver(rtvi)],
    )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        logger.info("Client ready — sending greeting.")
        await rtvi.set_bot_ready()
        # Speak an initial greeting
        await task.queue_frame(
            TextFrame(
                "Hi! I'm your OverlayGuide assistant. "
                "Tell me what you'd like to do on your screen, "
                "and I'll walk you through it step by step."
            )
        )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected.")
        await task.cancel()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected.")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    runner = PipelineRunner()
    await runner.run(task)

    # Cleanup
    await agent_processor.cleanup()
    logger.info("Pipeline finished.")
