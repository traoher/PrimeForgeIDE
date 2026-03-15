"""
Proton9 — Video Fetch Tool

Downloads a remote video (e.g. YouTube URL) to a local file for subsequent analysis.
Requires yt-dlp to be installed and available on PATH.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from tools.base import BaseTool, ToolResult


class VideoFetchTool(BaseTool):
    name = "video_fetch"
    description = (
        "Download a video URL to a local file using yt-dlp. "
        "Use this before video_analyze when user provides a URL."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Video URL (e.g., YouTube link)"},
            "output_dir": {
                "type": "string",
                "description": "Directory to save downloaded video (default: .video_cache/downloads)",
            },
            "format": {
                "type": "string",
                "description": "yt-dlp format selector (default: mp4 fallback)",
            },
            "include_subtitles": {
                "type": "boolean",
                "description": "Attempt to download subtitles/auto-captions and extract transcript text (default: true)",
                "default": True,
            },
        },
        "required": ["url"],
    }

    def __init__(self, default_cwd: str = "."):
        self.default_cwd = os.path.abspath(default_cwd)

    def execute(
        self,
        url: str,
        output_dir: str = "",
        format: str = "",
        include_subtitles: bool = True,
        **kwargs,
    ) -> ToolResult:
        try:
            if not url or not isinstance(url, str):
                return ToolResult(success=False, output="", error="url is required")
            if not re.match(r"^https?://", url.strip(), re.IGNORECASE):
                return ToolResult(success=False, output="", error=f"Invalid URL: {url}")

            out_dir = output_dir.strip() if output_dir else ".video_cache/downloads"
            abs_out_dir = os.path.abspath(out_dir if os.path.isabs(out_dir) else os.path.join(self.default_cwd, out_dir))
            os.makedirs(abs_out_dir, exist_ok=True)

            # Check yt-dlp availability via module first, then CLI.
            use_module = False
            try:
                check = subprocess.run(
                    [sys.executable, "-m", "yt_dlp", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if check.returncode == 0:
                    use_module = True
                else:
                    check = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=10)
            except FileNotFoundError:
                return ToolResult(
                    success=False,
                    output="",
                    error="yt-dlp is not installed or not on PATH. Install yt-dlp and retry.",
                )
            if check.returncode != 0:
                return ToolResult(
                    success=False,
                    output="",
                    error="yt-dlp is not available on PATH. Install yt-dlp and retry.",
                )

            run_tag = int(time.time())
            # Let yt-dlp choose extension while keeping deterministic filename base.
            output_template = str(Path(abs_out_dir) / f"video_{run_tag}.%(ext)s")
            # Prefer progressive video to avoid ffmpeg merge requirement.
            fmt = format.strip() if format else "b[ext=mp4]/b"

            base_cmd = [sys.executable, "-m", "yt_dlp"] if use_module else ["yt-dlp"]
            base_cmd += [
                "-f",
                fmt,
                "--no-playlist",
                "--extractor-args",
                "youtube:player_client=android",
                "-o",
                output_template,
            ]
            cmd = list(base_cmd)
            if include_subtitles:
                cmd += [
                    "--write-subs",
                    "--write-auto-subs",
                    "--sub-langs",
                    "en.*,en",
                    "--sub-format",
                    "vtt/srt/best",
                ]
            cmd += [url]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except FileNotFoundError:
                return ToolResult(
                    success=False,
                    output="",
                    error="yt-dlp is not installed or not on PATH. Install yt-dlp and retry.",
                )
            subtitle_warning = ""
            if res.returncode != 0 and include_subtitles:
                # Fallback: download video without subtitle flags.
                cmd_no_subs = list(base_cmd) + [url]
                try:
                    res_no_subs = subprocess.run(cmd_no_subs, capture_output=True, text=True, timeout=600)
                except FileNotFoundError:
                    return ToolResult(
                        success=False,
                        output="",
                        error="yt-dlp is not installed or not on PATH. Install yt-dlp and retry.",
                    )
                if res_no_subs.returncode == 0:
                    subtitle_warning = (
                        "Subtitle download failed; proceeding with video-only download. "
                        f"Reason: {(res.stderr or '').strip()[:300]}"
                    )
                    res = res_no_subs
                else:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"yt-dlp failed (code {res_no_subs.returncode}): {res_no_subs.stderr.strip()[:600]}",
                    )
            elif res.returncode != 0:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"yt-dlp failed (code {res.returncode}): {res.stderr.strip()[:600]}",
                )

            # Find newest file in output dir matching the run tag.
            candidates = sorted(Path(abs_out_dir).glob(f"video_{run_tag}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                return ToolResult(success=False, output="", error="Download reported success but no output file found.")

            out_file = str(candidates[0].resolve())
            if Path(out_file).suffix.lower() in {".m4a", ".mp3", ".aac", ".wav", ".flac", ".ogg"}:
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        "Download resolved to audio-only media. Retry with a video-capable format "
                        "(for example: format='b[ext=mp4]/b')."
                    ),
                )

            subtitle_path = ""
            transcript_path = ""
            transcript_text = ""
            if include_subtitles:
                sub_candidates = sorted(
                    list(Path(abs_out_dir).glob(f"video_{run_tag}*.vtt"))
                    + list(Path(abs_out_dir).glob(f"video_{run_tag}*.srt")),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if sub_candidates:
                    subtitle_path = str(sub_candidates[0].resolve())
                    transcript_text = self._subtitle_to_text(subtitle_path)
                    if transcript_text:
                        transcript_file = Path(abs_out_dir) / f"video_{run_tag}.transcript.txt"
                        transcript_file.write_text(transcript_text, encoding="utf-8")
                        transcript_path = str(transcript_file.resolve())
            payload = {
                "url": url,
                "local_path": out_file,
                "output_dir": abs_out_dir,
                "format": fmt,
                "yt_dlp_version": (check.stdout or "").strip(),
                "subtitle_path": subtitle_path,
                "transcript_path": transcript_path,
                "transcript_char_count": len(transcript_text),
                "transcript_preview": (transcript_text[:400] + "...") if len(transcript_text) > 400 else transcript_text,
                "warning": subtitle_warning,
            }
            return ToolResult(success=True, output=json.dumps(payload, indent=2))
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="Video download timed out.")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _subtitle_to_text(self, subtitle_path: str) -> str:
        try:
            raw = Path(subtitle_path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

        lines = raw.splitlines()
        cleaned = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if s.upper() == "WEBVTT":
                continue
            if re.match(r"^\d+$", s):
                continue
            if "-->" in s:
                continue
            s = re.sub(r"<[^>]+>", " ", s)
            s = re.sub(r"\[[^\]]+\]", " ", s)
            s = re.sub(r"\s+", " ", s).strip()
            if not s:
                continue
            if cleaned and cleaned[-1] == s:
                continue
            cleaned.append(s)
        return "\n".join(cleaned).strip()
