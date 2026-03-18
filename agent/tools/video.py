"""
Proton9 — Video Analysis Tool

Extracts metadata + sampled keyframes from a local video file and summarizes it.
Uses ffprobe/ffmpeg if available.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from tools.base import BaseTool, ToolResult


class VideoAnalyzeTool(BaseTool):
    name = "video_analyze"
    description = (
        "Analyze a local video file by extracting metadata and keyframes, then summarize content. "
        "Useful for meeting/video recap, timeline notes, and action item extraction."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to local video file (.mp4, .mov, .mkv, .webm, etc.)"},
            "sample_seconds": {
                "type": "number",
                "description": "Seconds between sampled keyframes (default: 10)",
                "default": 10,
            },
            "max_frames": {
                "type": "integer",
                "description": "Maximum keyframes to extract (default: 8)",
                "default": 8,
            },
            "question": {
                "type": "string",
                "description": "Optional user question/focus for the summary",
            },
            "transcript_path": {
                "type": "string",
                "description": "Optional subtitle/transcript text file path to improve spoken-content summary",
            },
            "output_json_path": {
                "type": "string",
                "description": "Optional path to save analysis JSON",
            },
        },
        "required": ["path"],
    }

    def __init__(self, llm=None, default_cwd: str = "."):
        self.llm = llm
        self.default_cwd = os.path.abspath(default_cwd)

    def execute(
        self,
        path: str,
        sample_seconds: float = 10,
        max_frames: int = 8,
        question: str = "",
        transcript_path: str = "",
        output_json_path: str = "",
        **kwargs,
    ) -> ToolResult:
        try:
            sample_seconds = max(float(sample_seconds or 10), 1.0)
            max_frames = max(int(max_frames or 8), 1)

            video_path = os.path.abspath(path if os.path.isabs(path) else os.path.join(self.default_cwd, path))
            if not os.path.exists(video_path):
                return ToolResult(success=False, output="", error=f"Video file not found: {video_path}")
            if not os.path.isfile(video_path):
                return ToolResult(success=False, output="", error=f"Path is not a file: {video_path}")

            metadata, metadata_warning = self._probe_video(video_path)

            run_tag = f"{Path(video_path).stem}_{int(time.time())}"
            frame_dir = os.path.join(self.default_cwd, ".video_cache", run_tag)
            os.makedirs(frame_dir, exist_ok=True)

            frames, frame_warning = self._extract_frames(
                video_path=video_path,
                frame_dir=frame_dir,
                sample_seconds=sample_seconds,
                max_frames=max_frames,
            )

            transcript_text = self._read_transcript_text(transcript_path)

            summary = ""
            if self.llm and frames:
                summary = self._summarize_with_llm(
                    metadata=metadata,
                    frame_paths=frames,
                    question=question or "",
                    transcript_text=transcript_text,
                )
            elif frames:
                summary = (
                    "Keyframes extracted successfully, but no LLM instance available for multimodal summarization."
                )
            else:
                summary = "No keyframes extracted. Returned metadata-only analysis."

            payload = {
                "video_path": video_path,
                "metadata": metadata,
                "summary": summary,
                "question": question or "",
                "sample_seconds": sample_seconds,
                "max_frames": max_frames,
                "frame_count": len(frames),
                "frames": frames,
                "transcript_path": transcript_path or "",
                "transcript_char_count": len(transcript_text),
                "warnings": [w for w in (metadata_warning, frame_warning) if w],
            }

            if output_json_path:
                output_path = os.path.abspath(
                    output_json_path
                    if os.path.isabs(output_json_path)
                    else os.path.join(self.default_cwd, output_json_path)
                )
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                payload["saved_to"] = output_path

            return ToolResult(success=True, output=json.dumps(payload, indent=2, ensure_ascii=False))
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _probe_video(self, video_path: str):
        ffprobe_bin = shutil.which("ffprobe")
        cmd = []
        if ffprobe_bin:
            cmd = [
                ffprobe_bin,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                video_path,
            ]
        try:
            if cmd:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                if res.returncode == 0:
                    data = json.loads(res.stdout or "{}")

                    format_obj = data.get("format", {}) or {}
                    streams = data.get("streams", []) or []
                    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {}) or {}
                    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {}) or {}

                    metadata = {
                        "file_size_bytes": int(float(format_obj.get("size", 0) or 0)),
                        "duration_seconds": float(format_obj.get("duration", 0) or 0),
                        "format_name": format_obj.get("format_name", ""),
                        "video_codec": video_stream.get("codec_name", ""),
                        "width": int(video_stream.get("width", 0) or 0),
                        "height": int(video_stream.get("height", 0) or 0),
                        "fps_raw": video_stream.get("r_frame_rate", ""),
                        "audio_codec": audio_stream.get("codec_name", ""),
                        "has_audio": bool(audio_stream),
                    }
                    return metadata, ""
                warning = f"ffprobe failed: {res.stderr.strip()[:300]}"
            else:
                warning = "ffprobe not found on PATH"

            ffmpeg_bin = self._resolve_ffmpeg_binary()
            if not ffmpeg_bin:
                return {
                    "file_size_bytes": os.path.getsize(video_path),
                    "extension": Path(video_path).suffix.lower(),
                }, warning
            fallback_meta, fallback_warn = self._probe_with_ffmpeg(video_path, ffmpeg_bin)
            combined_warn = f"{warning}; {fallback_warn}" if fallback_warn else warning
            return fallback_meta, combined_warn
        except Exception as e:
            return {
                "file_size_bytes": os.path.getsize(video_path),
                "extension": Path(video_path).suffix.lower(),
            }, f"ffprobe unavailable or failed: {e}"

    def _extract_frames(self, video_path: str, frame_dir: str, sample_seconds: float, max_frames: int):
        ffmpeg_bin = self._resolve_ffmpeg_binary()
        if not ffmpeg_bin:
            return [], "ffmpeg unavailable: not found on PATH and no bundled fallback found"
        fps_expr = f"1/{sample_seconds}"
        frame_pattern = os.path.join(frame_dir, "frame_%03d.jpg")
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            video_path,
            "-vf",
            f"fps={fps_expr}",
            "-frames:v",
            str(max_frames),
            frame_pattern,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                return [], f"ffmpeg frame extraction failed: {res.stderr.strip()[:300]}"
            frames = sorted(str(p) for p in Path(frame_dir).glob("frame_*.jpg"))
            if not frames:
                return [], "ffmpeg ran but no frames were produced"
            return frames, ""
        except Exception as e:
            return [], f"ffmpeg unavailable or failed: {e}"

    def _resolve_ffmpeg_binary(self) -> str:
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            return ffmpeg_bin
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return ""

    def _probe_with_ffmpeg(self, video_path: str, ffmpeg_bin: str):
        cmd = [ffmpeg_bin, "-i", video_path]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            text = (res.stderr or "") + "\n" + (res.stdout or "")

            duration_seconds = 0.0
            width, height = 0, 0
            fps_raw = ""
            video_codec = ""
            audio_codec = ""

            m_duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
            if m_duration:
                hours = int(m_duration.group(1))
                mins = int(m_duration.group(2))
                secs = float(m_duration.group(3))
                duration_seconds = hours * 3600 + mins * 60 + secs

            m_video = re.search(r"Video:\s*([^,\s]+).*?(\d{2,5})x(\d{2,5})", text, re.IGNORECASE)
            if m_video:
                video_codec = m_video.group(1).strip()
                width = int(m_video.group(2))
                height = int(m_video.group(3))
            m_fps = re.search(r"(\d+(?:\.\d+)?)\s*fps", text, re.IGNORECASE)
            if m_fps:
                fps_raw = m_fps.group(1)

            m_audio = re.search(r"Audio:\s*([^,\s]+)", text, re.IGNORECASE)
            if m_audio:
                audio_codec = m_audio.group(1).strip()

            metadata = {
                "file_size_bytes": os.path.getsize(video_path),
                "duration_seconds": duration_seconds,
                "format_name": Path(video_path).suffix.lower().lstrip("."),
                "video_codec": video_codec,
                "width": width,
                "height": height,
                "fps_raw": fps_raw,
                "audio_codec": audio_codec,
                "has_audio": bool(audio_codec),
            }
            return metadata, "metadata derived via ffmpeg fallback parser"
        except Exception as e:
            return {
                "file_size_bytes": os.path.getsize(video_path),
                "extension": Path(video_path).suffix.lower(),
            }, f"ffmpeg metadata fallback failed: {e}"

    def _read_transcript_text(self, transcript_path: str) -> str:
        if not transcript_path:
            return ""
        try:
            path = os.path.abspath(
                transcript_path if os.path.isabs(transcript_path) else os.path.join(self.default_cwd, transcript_path)
            )
            if not os.path.exists(path) or not os.path.isfile(path):
                return ""
            return Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

    def _summarize_with_llm(self, metadata: dict, frame_paths: list[str], question: str, transcript_text: str):
        prompt = (
            "Analyze this video from sampled keyframes and metadata.\n"
            "If transcript is provided, prioritize spoken content for the summary and timeline.\n"
            "If evidence is missing, state uncertainty explicitly and avoid guessing.\n"
            f"Metadata: {json.dumps(metadata)}\n"
        )
        if transcript_text:
            prompt += (
                "Transcript excerpt:\n"
                f"{transcript_text[:12000]}\n"
            )
        if question:
            prompt += f"Focus question: {question}\n"
        prompt += (
            "Output format:\n"
            "1) Summary (4-8 sentences, include key decisions/details)\n"
            "2) Timeline bullets with concrete points\n"
            "3) Action items / follow-ups\n"
        )
        resp = self.llm.call(messages=[{"role": "user", "content": prompt}], images=frame_paths)
        return (resp.text or "").strip()
