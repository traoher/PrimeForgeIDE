"""
Proton9 — Image Generation Tool

Uses Gemini's image generation model to create images from text prompts.
"""

import os
import base64
import time
from pathlib import Path
from tools.base import BaseTool, ToolResult


class ImageGenerateTool(BaseTool):
    name = "image_generate"
    description = "Generate an image from a text prompt using Gemini's image model. The image is saved to disk and the path is returned. Use this for creating illustrations, diagrams, UI mockups, logos, or any visual content."
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Detailed text description of the image to generate. Be specific about content, style, colors, and composition.",
            },
            "filename": {
                "type": "string",
                "description": "Output filename (without directory). Defaults to 'generated_image.png'.",
            },
        },
        "required": ["prompt"],
    }

    def execute(self, prompt: str, filename: str = None, **kwargs) -> ToolResult:
        try:
            from google import genai
        except ImportError:
            return ToolResult(success=False, output="", error="google-genai package not installed")

        # Get API key
        api_key = None
        for k, v in os.environ.items():
            if k.startswith("GEMINI_API_KEY") and v.strip():
                api_key = v.strip()
                break
        if not api_key:
            return ToolResult(success=False, output="", error="No GEMINI_API_KEY found in environment")

        # Determine output path
        if not filename:
            filename = f"generated_{int(time.time())}.png"
        if not filename.endswith(('.png', '.jpg', '.jpeg', '.webp')):
            filename += '.png'

        # Use working directory from context if available
        working_dir = kwargs.get("working_dir", os.getcwd())
        output_path = os.path.join(working_dir, filename)

        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-2.0-flash-exp",
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    response_modalities=["image", "text"],
                ),
            )

            # Extract image from response parts
            image_saved = False
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data is not None:
                    image_data = part.inline_data.data
                    mime = part.inline_data.mime_type or "image/png"
                    # Determine extension from mime
                    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
                    ext = ext_map.get(mime, ".png")
                    if not output_path.endswith(ext):
                        output_path = output_path.rsplit(".", 1)[0] + ext

                    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                    with open(output_path, "wb") as f:
                        f.write(image_data)
                    image_saved = True
                    break

            if image_saved:
                size_kb = os.path.getsize(output_path) / 1024
                return ToolResult(
                    success=True,
                    output=f"Image generated and saved to: {output_path} ({size_kb:.1f} KB)\nPrompt: {prompt[:100]}"
                )
            else:
                # Check if there's text response (model may have refused)
                text_parts = [p.text for p in response.candidates[0].content.parts if hasattr(p, 'text') and p.text]
                if text_parts:
                    return ToolResult(success=False, output="", error=f"Model returned text instead of image: {text_parts[0][:200]}")
                return ToolResult(success=False, output="", error="No image data in response")

        except Exception as e:
            return ToolResult(success=False, output="", error=f"Image generation failed: {str(e)[:300]}")
