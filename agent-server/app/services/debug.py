# app/services/debug.py
# Owner: Eng 3 (Agent Pipeline)
#
# Per-request debug output session.
# Saves images, prompts, LLM responses, and metadata at each pipeline step
# into a structured folder for post-hoc analysis.
#
# Output directory: /tmp/og_debug/<request_id>/
# Each file is prefixed with a step number for ordering.

import json
import os
import time
from pathlib import Path

DEBUG_ROOT = Path("/tmp/og_debug")


class DebugSession:
    """
    Collects debug output for a single request.
    Creates a folder /tmp/og_debug/<request_id>/ and saves files into it.

    Usage:
        dbg = DebugSession("my-request-id", goal="Open System Settings")
        dbg.save_image("original_screenshot", screenshot_bytes)
        dbg.save_text("yolo_elements", elements_context)
        dbg.save_json("gemini_response", {"steps": [...]})
        dbg.save_image("final_overlay", overlay_bytes)
    """

    def __init__(self, request_id: str, goal: str = "", endpoint: str = "plan"):
        self.request_id = request_id
        self.goal = goal
        self.endpoint = endpoint
        self._step = 0
        self._start_time = time.time()

        # Create output directory
        self.dir = DEBUG_ROOT / request_id
        self.dir.mkdir(parents=True, exist_ok=True)

        # Write session metadata
        self._write_file("00_session.txt", (
            f"request_id: {request_id}\n"
            f"endpoint: {endpoint}\n"
            f"goal: {goal}\n"
            f"timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        ))

    def _next_prefix(self) -> str:
        self._step += 1
        return f"{self._step:02d}"

    def _write_file(self, filename: str, content: str | bytes):
        """Write a file to the debug directory."""
        path = self.dir / filename
        try:
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content)
        except Exception as e:
            print(f"[debug] failed to save {path}: {e}")

    def save_image(self, label: str, data: bytes, info: str = ""):
        """Save an image (PNG) with a step number prefix."""
        prefix = self._next_prefix()
        filename = f"{prefix}_{label}.png"
        self._write_file(filename, data)
        size_kb = len(data) / 1024
        print(f"[debug] rid={self.request_id} saved {filename} ({size_kb:.0f}KB) {info}")

    def save_text(self, label: str, text: str, info: str = ""):
        """Save a text file with a step number prefix."""
        prefix = self._next_prefix()
        filename = f"{prefix}_{label}.txt"
        self._write_file(filename, text)
        print(f"[debug] rid={self.request_id} saved {filename} ({len(text)} chars) {info}")

    def save_json(self, label: str, data: dict | list, info: str = ""):
        """Save a JSON file with a step number prefix."""
        prefix = self._next_prefix()
        filename = f"{prefix}_{label}.json"
        try:
            text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        except Exception:
            text = str(data)
        self._write_file(filename, text)
        print(f"[debug] rid={self.request_id} saved {filename} {info}")

    def save_prompt_and_response(
        self, label: str, prompt: str, response: str,
        model: str = "", info: str = "",
    ):
        """Save both the prompt and raw LLM response as a single text file."""
        prefix = self._next_prefix()
        filename = f"{prefix}_{label}.txt"
        elapsed = time.time() - self._start_time
        content = (
            f"{'='*80}\n"
            f"  {label.upper()}\n"
            f"  model: {model}\n"
            f"  elapsed: {elapsed:.1f}s since request start\n"
            f"  response length: {len(response)} chars\n"
            f"{'='*80}\n\n"
            f"--- PROMPT ---\n\n"
            f"{prompt}\n\n"
            f"--- RAW RESPONSE ---\n\n"
            f"{response}\n"
        )
        self._write_file(filename, content)
        print(f"[debug] rid={self.request_id} saved {filename} "
              f"(prompt={len(prompt)}, response={len(response)}) {info}")

    def save_step_resolution(
        self, step_id: str, step_data: dict,
        resolved_bbox: tuple, verification_result: dict | None = None,
    ):
        """Save the full resolution trace for a single step."""
        prefix = self._next_prefix()
        filename = f"{prefix}_step_{step_id}_resolution.json"
        data = {
            "step_id": step_id,
            "gemini_output": step_data,
            "resolved_bbox": {
                "x": resolved_bbox[0],
                "y": resolved_bbox[1],
                "w": resolved_bbox[2],
                "h": resolved_bbox[3],
            },
            "verification": verification_result,
        }
        try:
            text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        except Exception:
            text = str(data)
        self._write_file(filename, text)

    def finalize(self, plan_json: dict | None = None):
        """Write the final output and a summary."""
        elapsed = time.time() - self._start_time
        summary = (
            f"request_id: {self.request_id}\n"
            f"endpoint: {self.endpoint}\n"
            f"goal: {self.goal}\n"
            f"total_time: {elapsed:.1f}s\n"
            f"total_steps_saved: {self._step}\n"
            f"output_dir: {self.dir}\n"
        )
        if plan_json:
            steps = plan_json.get("steps", [])
            summary += f"plan_steps: {len(steps)}\n"
            for i, s in enumerate(steps):
                targets = s.get("targets", [{}])
                t = targets[0] if targets else {}
                summary += (
                    f"  step {i+1}: {s.get('instruction', '')[:60]}\n"
                    f"    bbox: ({t.get('x',0):.3f}, {t.get('y',0):.3f}, "
                    f"{t.get('w',0):.3f}, {t.get('h',0):.3f})\n"
                    f"    label: {t.get('label')}\n"
                    f"    confidence: {t.get('confidence')}\n"
                )
        self._write_file("99_summary.txt", summary)
        if plan_json:
            self._write_file("99_final_plan.json",
                             json.dumps(plan_json, indent=2, ensure_ascii=False, default=str))
        print(f"[debug] rid={self.request_id} session finalized -> {self.dir} ({self._step} files, {elapsed:.1f}s)")
