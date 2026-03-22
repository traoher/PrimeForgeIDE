"""
Proton9 — LLM Gateway

Multi-provider LLM interface. Phase 1 = Gemini only.
Handles multimodal input (text + images) and streaming.
"""

import os
import json
import base64
import time
import random
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")


class LLMResponse:
    """Structured response from LLM."""
    def __init__(self, text: str, tool_call: dict = None, usage: dict = None,
                 actual_provider: str = None, actual_model: str = None):
        self.text = text
        self.tool_call = tool_call  # {"name": str, "arguments": dict}
        self.usage = usage or {}
        self.actual_provider = actual_provider  # Which provider actually answered
        self.actual_model = actual_model  # Which model actually answered

    def __repr__(self):
        if self.tool_call:
            return f"LLMResponse(tool={self.tool_call['name']})"
        return f"LLMResponse(text={self.text[:80]}...)"


class GeminiProvider:
    """Google Gemini API provider with function calling."""

    def __init__(self, model: str = None):
        from google import genai
        
        # Load all available GEMINI_API_KEYs from environment
        self.api_keys = []
        for k, v in os.environ.items():
            if k.startswith("GEMINI_API_KEY") and v.strip():
                self.api_keys.append(v.strip())
                
        if not self.api_keys:
            raise ValueError("No GEMINI_API_KEY found in .env")

        self.current_key_idx = 0
        self.client = genai.Client(api_key=self.api_keys[self.current_key_idx])
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        
        # Define fallback models to try if the primary hits a quota (429) or internal error (500)
        # Prioritize newest generations first, then drop down to older generations.
        fallback_models = [
            self.model,
            "gemini-2.5-flash"
        ]
        # Preserve order, remove duplicates
        self.models_to_try = list(dict.fromkeys(fallback_models))
        
        self._genai = genai

    def _is_hard_quota_error(self, error_str: str) -> bool:
        text = (error_str or "").lower()
        return (
            "spending cap" in text
            or "billing" in text
            or "resource_exhausted" in text
            or "exceeded its spending cap" in text
            or "insufficient quota" in text
        )

    def call(self, messages: list, tools: list = None, images: list = None) -> LLMResponse:
        """
        Call Gemini with messages, optional tools (function declarations), and optional images.

        Args:
            messages: List of {"role": "user"|"model", "content": str}
            tools:    List of tool definitions (JSON schema format)
            images:   List of image bytes or file paths
        """
        from google.genai import types

        # Build contents
        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else msg["role"]
            parts = [types.Part.from_text(text=msg["content"])]

            # Attach images to the first user message
            if msg == messages[-1] and images:
                for img in images:
                    if isinstance(img, (str, Path)) and os.path.exists(str(img)):
                        with open(str(img), "rb") as f:
                            img_bytes = f.read()
                        mime = self._guess_mime(str(img))
                        parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
                    elif isinstance(img, bytes):
                        parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))

            contents.append(types.Content(role=role, parts=parts))

        # Build tool declarations
        gemini_tools = None
        if tools:
            func_declarations = []
            for tool in tools:
                # Convert our tool schema to Gemini function declaration
                func_declarations.append(types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool["description"],
                    parameters=tool.get("parameters", {}),
                ))
            gemini_tools = [types.Tool(function_declarations=func_declarations)]

        # Call the API with automatic fallback
        config = types.GenerateContentConfig(
            tools=gemini_tools,
            temperature=0.2,
        )

        response = None
        last_error = None

        # Try models in fallback order
        for current_model in self.models_to_try:
            # For each model, try available API keys
            keys_tried = 0
            while keys_tried < len(self.api_keys):
                try:
                    # Delay slightly to prevent slamming the API when looping
                    if keys_tried > 0:
                        time.sleep(2)
                        
                    response = self.client.models.generate_content(
                        model=current_model,
                        contents=contents,
                        config=config,
                    )
                    
                    # Succeeded on a fallback model? Stick to it.
                    if current_model != self.model:
                        print(f"\n  [LLM FALLBACK] Successfully switched to {current_model}")
                        self.model = current_model
                    break # Break out of the API key loop on success
                    
                except Exception as e:
                    error_str = str(e).lower()
                    
                    # 429 = Quota or Rate Limit. 400 = "API key not valid" (if someone put a bad key in .env). 403 = Forbidden.
                    is_quota = "429" in error_str or "quota" in error_str or "exhausted" in error_str or "400" in error_str or "403" in error_str
                    is_server_error = "500" in error_str or "internal error" in error_str or "503" in error_str
                    is_hard_quota = self._is_hard_quota_error(error_str)

                    if is_quota or is_server_error:
                        print(f"\n  [LLM GATEWAY] {current_model} failed with API Key #{self.current_key_idx + 1} (Quota/Bad/Server Error: {e}).")
                        last_error = e
                        keys_tried += 1
                        
                        # Rotate to the next API key
                        if keys_tried < len(self.api_keys):
                            self.current_key_idx = (self.current_key_idx + 1) % len(self.api_keys)
                            print(f"  [LLM GATEWAY] Rotating to API Key #{self.current_key_idx + 1}...")
                            self.client = self._genai.Client(api_key=self.api_keys[self.current_key_idx])
                        else:
                            if is_hard_quota:
                                print(f"  [LLM FALLBACK] Hard quota exhaustion for {current_model}. Skipping cooldown and trying next fallback immediately...")
                            else:
                                print(f"  [LLM FALLBACK] Overloaded on all {len(self.api_keys)} API keys for {current_model}. Pausing for 60s...")
                                time.sleep(60)
                            # After cooldown, or immediately for hard quota exhaustion, try the next fallback model.
                            break
                    else:
                        raise  # Raise immediately if it's a token context length error type of 400

            if response:
                break # Break out of the model loop if we got a response

        if not response:
            raise last_error or RuntimeError("All LLM fallback models and API keys failed.")

        # Parse response
        candidate = response.candidates[0]
        text_parts = []
        tool_call = None

        if candidate.content and getattr(candidate.content, "parts", None):
            for part in candidate.content.parts:
                if part.text:
                    text_parts.append(part.text)
                elif getattr(part, "function_call", None):
                    fc = part.function_call
                    tool_call = {
                        "name": fc.name,
                        "arguments": dict(fc.args) if fc.args else {},
                    }
        elif getattr(candidate, "finish_reason", None):
            text_parts.append(f"[LLM Generation Stopped. Finish Reason: {candidate.finish_reason}]")
        else:
            text_parts.append("[Empty Response from LLM]")

        usage = {}
        if response.usage_metadata:
            usage = {
                "input_tokens": getattr(response.usage_metadata, "prompt_token_count", 0) or 0,
                "output_tokens": getattr(response.usage_metadata, "candidates_token_count", 0) or 0,
            }

        return LLMResponse(
            text="\n".join(text_parts),
            tool_call=tool_call,
            usage=usage,
        )

    def stream_call(self, messages: list, tools: list = None, images: list = None,
                    on_token: callable = None) -> LLMResponse:
        """Streaming version of call(). Yields token chunks via on_token callback.
        Falls back to regular call() if streaming fails or tools are used."""
        # Tool calls can't easily be streamed (need full response), fall back
        if tools:
            return self.call(messages, tools=tools, images=images)

        from google.genai import types

        # Build contents (same as call())
        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else msg["role"]
            parts = [types.Part.from_text(text=msg["content"])]
            if msg == messages[-1] and images:
                for img in images:
                    if isinstance(img, (str, Path)) and os.path.exists(str(img)):
                        with open(str(img), "rb") as f:
                            img_bytes = f.read()
                        mime = self._guess_mime(str(img))
                        parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
                    elif isinstance(img, bytes):
                        parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))
            contents.append(types.Content(role=role, parts=parts))

        config = types.GenerateContentConfig(temperature=0.2)

        try:
            text_parts = []
            for chunk in self.client.models.generate_content_stream(
                model=self.model, contents=contents, config=config,
            ):
                if chunk.text:
                    text_parts.append(chunk.text)
                    if on_token:
                        on_token(chunk.text)

            usage = {}
            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                usage = {
                    "input_tokens": getattr(chunk.usage_metadata, "prompt_token_count", 0) or 0,
                    "output_tokens": getattr(chunk.usage_metadata, "candidates_token_count", 0) or 0,
                }

            return LLMResponse(text="".join(text_parts), usage=usage)
        except Exception:
            # Fall back to non-streaming on any error
            return self.call(messages, tools=tools, images=images)

    def _guess_mime(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(ext, "image/png")


class OpenAIProvider:
    """OpenAI API Provider (also supports DeepSeek, Qwen via base_url overrides)."""

    def __init__(self, model: str = None):
        import openai
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")
        
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set in .env")
        
        # Determine fallback models based on base_url heuristics
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")
        
        if base_url and "deepseek" in base_url.lower():
            fallback_models = [self.model, "deepseek-reasoner", "deepseek-chat"]
        else:
            fallback_models = [self.model, "gpt-4o", "chatgpt-4o-latest", "gpt-4o-mini"]
            
        self.models_to_try = list(dict.fromkeys(fallback_models))
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)

    def call(self, messages: list, tools: list = None, images: list = None) -> LLMResponse:
        def build_oai_messages(use_images: bool) -> list:
            oai_messages = []
            for msg in messages:
                role = "assistant" if msg["role"] == "model" else msg["role"]
                content = msg["content"]

                if use_images and msg == messages[-1] and images:
                    content_list = [{"type": "text", "text": content}]
                    for img in images:
                        if isinstance(img, (str, Path)) and os.path.exists(str(img)):
                            with open(str(img), "rb") as f:
                                img_bytes = f.read()
                            mime = self._guess_mime(str(img))
                        elif isinstance(img, bytes):
                            img_bytes = img
                            mime = "image/png"
                        else:
                            continue

                        b64_data = base64.b64encode(img_bytes).decode("utf-8")
                        content_list.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64_data}"},
                            }
                        )
                    oai_messages.append({"role": role, "content": content_list})
                else:
                    oai_messages.append({"role": role, "content": content})
            return oai_messages

        oai_tools = None
        if tools:
            oai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    }
                }
                for tool in tools
            ]

        response = None
        last_error = None
        
        for current_model in self.models_to_try:
            tried_text_only_fallback = False
            try:
                oai_messages = build_oai_messages(use_images=bool(images))
                kwargs = {
                    "model": current_model,
                    "messages": oai_messages,
                }
                if oai_tools:
                    kwargs["tools"] = oai_tools
                    kwargs["tool_choice"] = "auto"
                
                # Model specific quirks
                if "o1" not in current_model and "o3" not in current_model and "reasoner" not in current_model:
                    kwargs["temperature"] = 0.2
                    
                response = self.client.chat.completions.create(**kwargs)
                
                if current_model != self.model:
                    print(f"\n  [LLM FALLBACK] Successfully switched to {current_model}")
                    self.model = current_model
                break
            except Exception as e:
                error_str = str(e).lower()
                is_quota = "429" in error_str or "quota" in error_str or "rate limit" in error_str or "insufficient" in error_str
                is_server_error = "500" in error_str or "503" in error_str or "internal error" in error_str or "server error" in error_str
                is_multimodal_payload_error = (
                    ("failed to deserialize the json body" in error_str)
                    or ("unknown variant `input_text`" in error_str)
                    or ("unknown variant 'input_text'" in error_str)
                    or ("expected `text`" in error_str)
                    or ("expected 'text'" in error_str)
                    or ("invalid chat format" in error_str)
                    or ("content type" in error_str and "image_url" in error_str)
                )

                if images and is_multimodal_payload_error and not tried_text_only_fallback:
                    tried_text_only_fallback = True
                    print(
                        f"\n  [LLM] {current_model} rejected multimodal payload; retrying text-only mode.",
                        flush=True,
                    )
                    try:
                        kwargs = {
                            "model": current_model,
                            "messages": build_oai_messages(use_images=False),
                        }
                        if oai_tools:
                            kwargs["tools"] = oai_tools
                            kwargs["tool_choice"] = "auto"
                        if "o1" not in current_model and "o3" not in current_model and "reasoner" not in current_model:
                            kwargs["temperature"] = 0.2

                        response = self.client.chat.completions.create(**kwargs)
                        if current_model != self.model:
                            print(f"\n  [LLM FALLBACK] Successfully switched to {current_model}")
                            self.model = current_model
                        break
                    except Exception as e2:
                        e = e2
                        error_str = str(e).lower()
                        is_quota = (
                            "429" in error_str
                            or "quota" in error_str
                            or "rate limit" in error_str
                            or "insufficient" in error_str
                        )
                        is_server_error = (
                            "500" in error_str
                            or "503" in error_str
                            or "internal error" in error_str
                            or "server error" in error_str
                        )
                
                if is_quota or is_server_error:
                    print(f"\n  [LLM FALLBACK] {current_model} failed (Quota/Server Error: {e}). Trying next...")
                    last_error = e
                    time.sleep(2)
                    continue
                else:
                    raise
                    
        if not response:
            raise last_error or RuntimeError("All OpenAI fallback models failed.")

        choice = response.choices[0]
        msg = choice.message
        
        text_parts = [msg.content] if msg.content else []
        tool_call_dict = None
        
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            tool_call_dict = {
                "name": tc.function.name,
                "arguments": args
            }
            
        return LLMResponse(
            text="\n".join(text_parts),
            tool_call=tool_call_dict,
            usage={
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            }
        )

    def _guess_mime(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "image/png")

    def stream_call(self, messages: list, tools: list = None, images: list = None, on_token: callable = None) -> LLMResponse:
        """
        Feature J: Token-by-token streaming via OpenAI-compatible API.
        
        Calls on_token(text_delta) for each token as it arrives.
        Returns the complete LLMResponse at the end.
        """
        oai_messages = []
        for msg in messages:
            role = "assistant" if msg["role"] == "model" else msg["role"]
            oai_messages.append({"role": role, "content": msg["content"]})

        oai_tools = None
        if tools:
            oai_tools = [
                {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                for t in tools
            ]

        kwargs = {"model": self.model, "messages": oai_messages, "stream": True,
                  "stream_options": {"include_usage": True}}
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"
        if "o1" not in self.model and "o3" not in self.model and "reasoner" not in self.model:
            kwargs["temperature"] = 0.2

        full_text = []
        tool_call_dict = None
        tool_name = ""
        tool_args_str = ""
        stream_usage = {"input_tokens": 0, "output_tokens": 0}

        try:
            stream = self.client.chat.completions.create(**kwargs)
            for chunk in stream:
                # Capture usage from final chunk (sent when stream_options.include_usage=True)
                if hasattr(chunk, "usage") and chunk.usage:
                    stream_usage = {
                        "input_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                        "output_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                    }
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # Text streaming
                if delta.content:
                    full_text.append(delta.content)
                    if on_token:
                        try:
                            on_token(delta.content)
                        except Exception:
                            pass

                # Tool call streaming (accumulate name + args)
                if delta.tool_calls:
                    tc = delta.tool_calls[0]
                    if tc.function:
                        if tc.function.name:
                            tool_name = tc.function.name
                        if tc.function.arguments:
                            tool_args_str += tc.function.arguments

            # Parse accumulated tool call
            if tool_name:
                try:
                    args = json.loads(tool_args_str) if tool_args_str else {}
                except json.JSONDecodeError:
                    args = {}
                tool_call_dict = {"name": tool_name, "arguments": args}

            # Fallback: estimate from chars if API didn't return usage
            if stream_usage["input_tokens"] == 0 and stream_usage["output_tokens"] == 0:
                total_chars = sum(len(t) for t in full_text)
                stream_usage["output_tokens"] = total_chars // 4  # ~4 chars/token estimate

            return LLMResponse(
                text="".join(full_text),
                tool_call=tool_call_dict,
                usage=stream_usage,
            )
        except Exception:
            # Fallback to non-streaming
            return self.call(messages, tools=tools, images=images)


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek API provider via OpenAI-compatible endpoint."""

    def __init__(self, model: str = None):
        import openai
        api_key = os.getenv("DEEPSEEK_API_KEY")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not set in .env")

        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        fallback_models = [self.model, "deepseek-chat", "deepseek-reasoner"]
        self.models_to_try = list(dict.fromkeys(fallback_models))
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)


class AnthropicProvider:
    """Anthropic Claude API Provider."""

    def __init__(self, model: str = None):
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in .env")
        
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        fallback_models = [self.model, "claude-sonnet-4-20250514", "claude-3-7-sonnet-latest", "claude-3-5-haiku-latest"]
        self.models_to_try = list(dict.fromkeys(fallback_models))
        self.client = anthropic.Anthropic(api_key=api_key)

    def call(self, messages: list, tools: list = None, images: list = None) -> LLMResponse:
        system_prompt = ""
        anthropic_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_prompt += msg["content"] + "\n"
                continue
                
            role = "assistant" if msg["role"] == "model" else msg["role"]
            content = msg["content"]
            
            if msg == messages[-1] and images:
                content_list = [{"type": "text", "text": content}]
                for img in images:
                    if isinstance(img, (str, Path)) and os.path.exists(str(img)):
                        with open(str(img), "rb") as f:
                            img_bytes = f.read()
                        mime = self._guess_mime(str(img))
                    elif isinstance(img, bytes):
                        img_bytes = img
                        mime = "image/png"
                    else:
                        continue
                        
                    content_list.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.b64encode(img_bytes).decode('utf-8')
                        }
                    })
                anthropic_messages.append({"role": role, "content": content_list})
            else:
                anthropic_messages.append({"role": role, "content": content})

        anthropic_tools = None
        if tools:
            anthropic_tools = [{"name": tool["name"], "description": tool.get("description", ""), "input_schema": tool.get("parameters", {})} for tool in tools]

        response = None
        last_error = None
        
        for current_model in self.models_to_try:
            try:
                kwargs = {
                    "model": current_model,
                    "max_tokens": 8192,
                    "messages": anthropic_messages,
                    "system": system_prompt.strip(),
                    "temperature": 0.2,
                }
                if anthropic_tools:
                    kwargs["tools"] = anthropic_tools

                response = self.client.messages.create(**kwargs)
                
                if current_model != self.model:
                    print(f"\n  [LLM FALLBACK] Anthropic switched to {current_model}")
                    self.model = current_model
                break
            except Exception as e:
                error_str = str(e).lower()
                is_quota = "429" in error_str or "overloaded" in error_str or "rate limit" in error_str
                is_server_error = "500" in error_str or "503" in error_str or "internal error" in error_str
                is_auth = "401" in error_str or "authentication" in error_str or "deprecated" in error_str
                if is_quota or is_server_error or is_auth:
                    print(f"\n  [LLM] Anthropic {current_model} failed: {e}. Trying next model...")
                    last_error = e
                    time.sleep(1)
                    continue
                else:
                    raise
                    
        if not response:
            raise last_error or RuntimeError("All Anthropic fallback models failed.")

        text_parts = []
        tool_call_dict = None
        
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_call_dict = {"name": block.name, "arguments": block.input}
                
        return LLMResponse(
            text="\n".join(text_parts),
            tool_call=tool_call_dict,
            usage={
                "input_tokens": response.usage.input_tokens if response.usage else 0,
                "output_tokens": response.usage.output_tokens if response.usage else 0,
            }
        )

    def _guess_mime(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "image/png")


class TokenTracker:
    """Sliding window token tracker for TPM limits."""
    def __init__(self, limit_tpm: int = 950000):
        # Default Google AI Studio limit is 1M. Setting 950k as a safe buffer.
        self.limit_tpm = limit_tpm
        self.history = []  # List of tuples: (timestamp, tokens_used)

    def add_tokens(self, tokens: int):
        self.history.append((time.time(), tokens))

    def get_current_tpm(self) -> int:
        now = time.time()
        # Keep only records from the last 60 seconds
        self.history = [(ts, tk) for ts, tk in self.history if now - ts <= 60]
        return sum(tk for _, tk in self.history)

    def wait_if_needed(self, estimated_tokens: int = 0, max_wait_seconds: int = 120):
        """Pause execution if adding estimated_tokens would exceed the TPM limit."""
        start_wait = time.time()
        while True:
            current_tpm = self.get_current_tpm()
            if current_tpm + estimated_tokens < self.limit_tpm:
                break

            waited = time.time() - start_wait
            if waited >= max_wait_seconds:
                raise TimeoutError(
                    f"Token throttle wait exceeded {max_wait_seconds}s "
                    f"(current_tpm={current_tpm}, estimated={estimated_tokens}, limit={self.limit_tpm})"
                )
            
            # If we're over the limit, wait a few seconds and check again
            print(f"\n  [TOKEN THROTTLE] Approaching TPM Limit ({current_tpm} + {estimated_tokens} > {self.limit_tpm}). Pausing...")
            time.sleep(5)


class TokenUsageLedger:
    """Persistent cumulative token usage ledger (day/week/month), per-model."""

    def __init__(self, ledger_path: str):
        self.ledger_path = os.path.abspath(ledger_path)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)

    def _default_data(self) -> dict:
        return {
            "schema_version": 2,
            "updated_at": "",
            "day": {},
            "week": {},
            "month": {},
            "lifetime": {"_total": {"input_tokens": 0, "output_tokens": 0, "calls": 0}},
        }

    def _load_unlocked(self) -> dict:
        if not os.path.exists(self.ledger_path):
            return self._default_data()
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return self._default_data()
            return data
        except Exception:
            # Corrupt/partial file should not break agent execution.
            return self._default_data()

    def _save_unlocked(self, data: dict):
        temp_path = f"{self.ledger_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, self.ledger_path)

    def _add_bucket_usage(self, buckets: dict, key: str, model: str, input_tokens: int, output_tokens: int):
        """Add usage to a time bucket, both per-model and _total."""
        period = buckets.setdefault(key, {"_total": {"input_tokens": 0, "output_tokens": 0, "calls": 0}})
        # Migrate legacy flat format (schema v1) to nested
        if "input_tokens" in period and "_total" not in period:
            period["_total"] = {"input_tokens": period.pop("input_tokens"), "output_tokens": period.pop("output_tokens"), "calls": period.pop("calls", 0)}
        total = period.setdefault("_total", {"input_tokens": 0, "output_tokens": 0, "calls": 0})
        total["input_tokens"] += int(input_tokens)
        total["output_tokens"] += int(output_tokens)
        total["calls"] += 1
        model_entry = period.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
        model_entry["input_tokens"] += int(input_tokens)
        model_entry["output_tokens"] += int(output_tokens)
        model_entry["calls"] += 1

    def record_usage(self, input_tokens: int, output_tokens: int, model: str = "_unknown"):
        now = datetime.now()
        day_key = now.strftime("%Y-%m-%d")
        iso = now.isocalendar()
        week_key = f"{iso.year}-W{iso.week:02d}"
        month_key = now.strftime("%Y-%m")

        with self._lock:
            data = self._load_unlocked()
            day = data.setdefault("day", {})
            week = data.setdefault("week", {})
            month = data.setdefault("month", {})

            self._add_bucket_usage(day, day_key, model, input_tokens, output_tokens)
            self._add_bucket_usage(week, week_key, model, input_tokens, output_tokens)
            self._add_bucket_usage(month, month_key, model, input_tokens, output_tokens)

            # Lifetime bucket (flat, no time key)
            lifetime = data.setdefault("lifetime", {"_total": {"input_tokens": 0, "output_tokens": 0, "calls": 0}})
            if "input_tokens" in lifetime and "_total" not in lifetime:
                lifetime["_total"] = {"input_tokens": lifetime.pop("input_tokens"), "output_tokens": lifetime.pop("output_tokens"), "calls": lifetime.pop("calls", 0)}
            lt_total = lifetime.setdefault("_total", {"input_tokens": 0, "output_tokens": 0, "calls": 0})
            lt_total["input_tokens"] += int(input_tokens)
            lt_total["output_tokens"] += int(output_tokens)
            lt_total["calls"] += 1
            lt_model = lifetime.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
            lt_model["input_tokens"] += int(input_tokens)
            lt_model["output_tokens"] += int(output_tokens)
            lt_model["calls"] += 1

            data["updated_at"] = now.isoformat()
            self._save_unlocked(data)

    def get_cumulative(self) -> dict:
        now = datetime.now()
        day_key = now.strftime("%Y-%m-%d")
        iso = now.isocalendar()
        week_key = f"{iso.year}-W{iso.week:02d}"
        month_key = now.strftime("%Y-%m")

        def with_total(bucket: dict) -> dict:
            """Extract _total aggregate; backward-compatible with flat format."""
            if isinstance(bucket, dict) and "_total" in bucket:
                raw = bucket["_total"]
            else:
                raw = bucket
            in_tokens = int(raw.get("input_tokens", 0))
            out_tokens = int(raw.get("output_tokens", 0))
            calls = int(raw.get("calls", 0))
            return {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "total_tokens": in_tokens + out_tokens,
                "calls": calls,
            }

        def per_model(bucket: dict) -> dict:
            """Extract per-model breakdown from a bucket."""
            if not isinstance(bucket, dict):
                return {}
            return {k: with_total({"_total": v}) for k, v in bucket.items() if k != "_total" and isinstance(v, dict)}

        with self._lock:
            data = self._load_unlocked()
            day_bucket = (data.get("day") or {}).get(day_key, {})
            week_bucket = (data.get("week") or {}).get(week_key, {})
            month_bucket = (data.get("month") or {}).get(month_key, {})
            lifetime_bucket = data.get("lifetime") or {}
            return {
                "day_key": day_key,
                "week_key": week_key,
                "month_key": month_key,
                "day": with_total(day_bucket),
                "week": with_total(week_bucket),
                "month": with_total(month_bucket),
                "lifetime": with_total(lifetime_bucket),
                "by_model": {
                    "day": per_model(day_bucket),
                    "week": per_model(week_bucket),
                    "month": per_model(month_bucket),
                    "lifetime": per_model(lifetime_bucket),
                },
            }


class LLMGateway:
    """
    Provider-agnostic LLM gateway.
    Supports Gemini, OpenAI (DeepSeek, Qwen), and Anthropic models interchangeably.
    """

    FALLBACK_ORDER = ["gemini", "deepseek", "openai", "anthropic"]

    def __init__(
        self,
        provider: str = "gemini",
        model: str = None,
        call_timeout_seconds: int | float | None = None,
        throttle_max_wait_seconds: int | float | None = None,
        transient_retries: int | None = None,
        transient_retry_base_seconds: int | float | None = None,
        pricing: dict | None = None,
        fallback_provider: str | None = None,
    ):
        self.provider_name = provider.lower()
        self.provider = self._make_provider(self.provider_name, model)

        # Multi-provider failover chain: primary fails → try all others in order
        # ⚠️ CRITICAL — Do NOT simplify to a single fallback. The chain is what
        # makes the agent resilient when a provider is down or rate-limited.
        self._configured_fallback = (fallback_provider or "").lower() or None
        self._fallback_chain: list[str] = []  # Built lazily, excludes primary
        self._fallback_providers: dict = {}  # Lazy-initialized provider instances

        # Build fallback chain: configured fallback first, then FALLBACK_ORDER
        seen = {self.provider_name}
        if self._configured_fallback and self._configured_fallback not in seen:
            self._fallback_chain.append(self._configured_fallback)
            seen.add(self._configured_fallback)
        for fb in self.FALLBACK_ORDER:
            if fb not in seen:
                self._fallback_chain.append(fb)
                seen.add(fb)

        # Legacy compat
        self.fallback_provider_name = self._fallback_chain[0] if self._fallback_chain else None
        self.fallback_provider = None

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.session_call_count = 0
        self.session_id = str(uuid.uuid4())
        self.pricing = {
            "input_usd_per_million": float((pricing or {}).get("input_usd_per_million", 0.0) or 0.0),
            "output_usd_per_million": float((pricing or {}).get("output_usd_per_million", 0.0) or 0.0),
        }
        
        # TPM Tracker (Limits are often ~1M for Gemini Flash)
        self.token_tracker = TokenTracker(limit_tpm=950000)
        env_ledger_path = os.getenv("Proton9_TOKEN_LEDGER_PATH")
        default_ledger_path = Path(__file__).parent.parent / "logs" / "token_usage_ledger.json"
        self.token_ledger = TokenUsageLedger(str(env_ledger_path or default_ledger_path))
        env_timeout = os.getenv("Proton9_LLM_CALL_TIMEOUT_SECONDS")
        if call_timeout_seconds is not None:
            self.call_timeout_seconds = float(call_timeout_seconds)
        elif env_timeout:
            self.call_timeout_seconds = float(env_timeout)
        else:
            self.call_timeout_seconds = 75.0
        env_throttle_wait = os.getenv("Proton9_TOKEN_THROTTLE_MAX_WAIT_SECONDS")
        if throttle_max_wait_seconds is not None:
            self.throttle_max_wait_seconds = float(throttle_max_wait_seconds)
        elif env_throttle_wait:
            self.throttle_max_wait_seconds = float(env_throttle_wait)
        else:
            self.throttle_max_wait_seconds = 120.0
        env_retry_count = os.getenv("Proton9_LLM_TRANSIENT_RETRIES")
        if transient_retries is not None:
            self.transient_retries = int(transient_retries)
        else:
            self.transient_retries = int(env_retry_count) if env_retry_count else 2
        env_retry_base = os.getenv("Proton9_LLM_TRANSIENT_RETRY_BASE_SECONDS")
        if transient_retry_base_seconds is not None:
            self.transient_retry_base_seconds = float(transient_retry_base_seconds)
        else:
            self.transient_retry_base_seconds = float(env_retry_base) if env_retry_base else 1.5

    @staticmethod
    def _make_provider(name: str, model: str = None):
        name = name.lower()
        if name == "gemini":
            return GeminiProvider(model=model)
        if name == "deepseek":
            return DeepSeekProvider(model=model)
        if name == "openai":
            return OpenAIProvider(model=model)
        if name == "anthropic":
            return AnthropicProvider(model=model)
        raise ValueError(f"Unsupported provider: {name}")

    def _get_fallback_provider(self):
        """Legacy compat — returns first fallback provider."""
        return self._get_provider_for_fallback(self.fallback_provider_name) if self.fallback_provider_name else None

    def _get_provider_for_fallback(self, name: str):
        """Lazy-init a fallback provider by name. Returns None on init failure."""
        if not name:
            return None
        if name in self._fallback_providers:
            return self._fallback_providers[name]
        try:
            provider = self._make_provider(name)
            self._fallback_providers[name] = provider
            print(f"  [LLM] Fallback provider initialized: {name}", flush=True)
            return provider
        except Exception as e:
            print(f"  [LLM] Cannot init fallback provider {name}: {e}", flush=True)
            self._fallback_providers[name] = None  # Don't retry failed inits
            return None

    def _estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> dict:
        in_rate = float(self.pricing.get("input_usd_per_million", 0.0) or 0.0)
        out_rate = float(self.pricing.get("output_usd_per_million", 0.0) or 0.0)
        input_cost = (max(int(input_tokens), 0) / 1_000_000.0) * in_rate
        output_cost = (max(int(output_tokens), 0) / 1_000_000.0) * out_rate
        total_cost = input_cost + output_cost
        return {
            "input_cost_usd": round(input_cost, 6),
            "output_cost_usd": round(output_cost, 6),
            "total_cost_usd": round(total_cost, 6),
        }

    # Provider-specific characters-per-token ratios (empirically derived)
    _CHARS_PER_TOKEN = {
        "deepseek": 3.3,
        "openai": 3.3,
        "anthropic": 3.5,
        "gemini": 3.5,
    }

    def count_tokens(self, text: str) -> int:
        """
        Estimate token count using provider-specific char-to-token ratio.
        
        More accurate than the naive chars//4 estimate:
        - GPT/DeepSeek: ~3.3 chars/token (BPE tokenizer)
        - Gemini/Anthropic: ~3.5 chars/token
        """
        if not text:
            return 0
        ratio = self._CHARS_PER_TOKEN.get(self.provider_name, 3.5)
        return max(1, int(len(text) / ratio))

    def _call_provider_with_timeout(self, messages: list, tools: list = None, images: list = None, provider=None, timeout_override: float = None) -> LLMResponse:
        """Execute provider.call with a hard timeout to prevent indefinite hangs."""
        use_provider = provider or self.provider
        timeout = timeout_override or self.call_timeout_seconds
        if not timeout or timeout <= 0:
            return use_provider.call(messages, tools=tools, images=images)

        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def worker():
            try:
                result_queue.put(("ok", use_provider.call(messages, tools=tools, images=images)))
            except Exception as exc:
                result_queue.put(("error", exc))

        thread = threading.Thread(target=worker, daemon=True, name="Proton9-llm-call")
        thread.start()

        try:
            status, payload = result_queue.get(timeout=timeout)
        except queue.Empty:
            model_name = getattr(use_provider, "model", "unknown")
            provider_label = getattr(use_provider, "provider_name", None) or self.provider_name
            raise TimeoutError(
                f"LLM call timeout after {timeout:.1f}s "
                f"(provider={provider_label}, model={model_name})"
            )

        if status == "error":
            raise payload
        return payload

    def _is_transient_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        transient_markers = [
            "503",
            "502",
            "504",
            "service unavailable",
            "unavailable",
            "internal error",
            "server error",
            "temporarily unavailable",
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "econnreset",
            "etimedout",
            # ⚠️ CRITICAL — Do NOT remove the rate limit markers below.
            # Without them, 429/TPM errors crash the agent instead of retrying.
            "429",
            "rate limit",
            "rate_limit",
            "quota",
            "resource exhausted",
            "too many requests",
            "tokens per minute",
        ]
        return any(marker in text for marker in transient_markers)

    def _error_kind(self, exc: Exception) -> str:
        """Classify retry-relevant error family."""
        if isinstance(exc, TimeoutError):
            return "timeout"
        text = str(exc).lower()
        if "timeout" in text or "timed out" in text or "etimedout" in text:
            return "timeout"
        # Hard quota = spending cap exhausted, billing issues — waiting won't help
        if any(marker in text for marker in ("spending cap", "billing", "insufficient quota")):
            return "hard_quota"
        # ⚠️ CRITICAL — Do NOT remove this rate_limit classification.
        # It enables the escalating 30s/60s/90s backoff in _call_with_retries.
        if any(marker in text for marker in ("429", "rate limit", "rate_limit", "quota", "resource exhausted", "too many requests", "tokens per minute")):
            return "rate_limit"
        if any(marker in text for marker in ("503", "502", "504", "service unavailable", "server error", "internal error")):
            return "server"
        if any(marker in text for marker in ("connection reset", "connection aborted", "broken pipe", "econnreset")):
            return "network"
        return "other"

    def _call_with_retries(self, messages, tools=None, images=None, provider=None, provider_label="?") -> LLMResponse | None:
        """Try calling a provider with retry logic. Returns None if all retries exhausted."""
        use_provider = provider or self.provider
        base_retries = max(int(self.transient_retries), 0)
        attempts = base_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return self._call_provider_with_timeout(messages, tools=tools, images=images, provider=use_provider)
            except Exception as e:
                kind = self._error_kind(e)

                # Hard quota (spending cap) — skip ALL retries, fall through to next provider immediately
                if kind == "hard_quota":
                    print(f"  [LLM] {provider_label} hard quota exhausted (spending cap/billing): {e}. Skipping retries.", flush=True)
                    return None

                # ⚠️ CRITICAL — Do NOT remove this rate limit handler.
                # Without it, 429/TPM errors crash the agent immediately.
                # The 30s/60s/90s backoff matches the DeepSeek TPM reset window.
                if kind == "rate_limit":
                    rate_limit_attempts = max(attempts, 3)
                    if attempt >= rate_limit_attempts:
                        print(f"  [LLM] {provider_label} rate limit exhausted after {attempt} attempts: {e}", flush=True)
                        return None
                    sleep_s = 30.0 * attempt
                    print(
                        f"  [LLM] Rate limit hit (attempt {attempt}/{rate_limit_attempts}): {e}. "
                        f"Waiting {sleep_s:.0f}s for TPM window...",
                        flush=True,
                    )
                    time.sleep(sleep_s)
                    continue

                effective_retries = min(base_retries, 1) if kind == "timeout" else base_retries
                effective_attempts = effective_retries + 1
                if attempt >= effective_attempts or not self._is_transient_error(e):
                    print(
                        f"  [LLM] {provider_label} failed ({kind}): {e}",
                        flush=True,
                    )
                    return None
                sleep_s = (self.transient_retry_base_seconds * (2 ** (attempt - 1))) + random.uniform(0.0, 0.4)
                print(
                    f"  [LLM] Transient {kind} error on attempt {attempt}/{effective_attempts}: {e}. "
                    f"Retrying in {sleep_s:.1f}s...",
                    flush=True,
                )
                time.sleep(sleep_s)
        return None

    def call(self, messages: list, tools: list = None, images: list = None,
             on_token: callable = None) -> LLMResponse:
        """Call the configured LLM provider, with automatic fallback on timeout/server error.
        If on_token is provided, streams the response token-by-token via the callback."""
        model_name = getattr(self.provider, "model", "unknown")
        call_started = time.time()

        estimated_prompt_tokens = self.count_tokens(
            "".join(m.get("content", "") for m in messages)
        )
        current_tpm = self.token_tracker.get_current_tpm()
        print(
            f"\n  [LLM] Preflight {self.provider_name}:{model_name} | "
            f"msgs={len(messages)} | est_tokens={estimated_prompt_tokens} | current_tpm={current_tpm}",
            flush=True,
        )
        self.token_tracker.wait_if_needed(
            estimated_tokens=estimated_prompt_tokens,
            max_wait_seconds=self.throttle_max_wait_seconds,
        )
        print(
            f"\n  [LLM] Calling {self.provider_name}:{model_name} | "
            f"msgs={len(messages)} | est_tokens={estimated_prompt_tokens}",
            flush=True,
        )

        # Streaming path: if on_token callback is provided and provider supports it
        if on_token and hasattr(self.provider, "stream_call"):
            try:
                response = self.provider.stream_call(
                    messages, tools=tools, images=images, on_token=on_token,
                )
                if response is not None:
                    # Track usage same as regular path
                    in_tokens = response.usage.get("input_tokens", 0)
                    out_tokens = response.usage.get("output_tokens", 0)
                    self.total_input_tokens += in_tokens
                    self.total_output_tokens += out_tokens
                    self.session_call_count += 1
                    self.token_tracker.add_tokens(in_tokens + out_tokens)
                    try:
                        self.token_ledger.record_usage(in_tokens, out_tokens, model=model_name)
                    except Exception:
                        pass
                    elapsed = time.time() - call_started
                    print(f"  [LLM] Streamed in {elapsed:.2f}s | in={in_tokens} out={out_tokens}", flush=True)
                    return response
            except Exception as e:
                print(f"  [LLM] Streaming failed ({e}), falling back to regular call", flush=True)

        responded_provider = self.provider_name
        responded_model = model_name

        response = self._call_with_retries(
            messages, tools=tools, images=images,
            provider=self.provider, provider_label=self.provider_name,
        )

        # Multi-provider cascading fallback
        # ⚠️ CRITICAL — Do NOT reduce to a single fallback. The cascading chain
        # ensures the agent survives even when multiple providers are down.
        if response is None:
            for fb_name in self._fallback_chain:
                fb = self._get_provider_for_fallback(fb_name)
                if fb is None:
                    continue
                fb_model = getattr(fb, "model", "unknown")
                print(
                    f"  [LLM] Falling back to {fb_name}:{fb_model}",
                    flush=True,
                )
                response = self._call_with_retries(
                    messages, tools=tools, images=images,
                    provider=fb, provider_label=fb_name,
                )
                if response is not None:
                    responded_provider = fb_name
                    responded_model = fb_model
                    print(f"  [LLM] Fallback {fb_name} succeeded", flush=True)
                    break

        if response is None:
            tried = [self.provider_name] + self._fallback_chain
            raise RuntimeError(f"LLM call failed on ALL providers: {', '.join(tried)}")

        # Tag response with actual provider/model for transparent tracking
        response.actual_provider = responded_provider
        response.actual_model = responded_model

        # Track usage — use ACTUAL model, not configured one
        in_tokens = response.usage.get("input_tokens", 0)
        out_tokens = response.usage.get("output_tokens", 0)
        
        self.total_input_tokens += in_tokens
        self.total_output_tokens += out_tokens
        self.session_call_count += 1
        
        # Add actual usage to the sliding window tracker
        self.token_tracker.add_tokens(in_tokens + out_tokens)
        try:
            self.token_ledger.record_usage(in_tokens, out_tokens, model=responded_model)
        except Exception as e:
            print(f"  [LLM] Warning: failed to record token ledger: {e}", flush=True)
        elapsed = time.time() - call_started
        print(f"  [LLM] Response in {elapsed:.2f}s | in={in_tokens} out={out_tokens}", flush=True)

        return response

    def get_usage_summary(self) -> dict:
        """Return cumulative token usage."""
        cumulative = self.token_ledger.get_cumulative()
        session_cost = self._estimate_cost_usd(self.total_input_tokens, self.total_output_tokens)
        day_cost = self._estimate_cost_usd(
            cumulative.get("day", {}).get("input_tokens", 0),
            cumulative.get("day", {}).get("output_tokens", 0),
        )
        week_cost = self._estimate_cost_usd(
            cumulative.get("week", {}).get("input_tokens", 0),
            cumulative.get("week", {}).get("output_tokens", 0),
        )
        month_cost = self._estimate_cost_usd(
            cumulative.get("month", {}).get("input_tokens", 0),
            cumulative.get("month", {}).get("output_tokens", 0),
        )
        lifetime_cost = self._estimate_cost_usd(
            cumulative.get("lifetime", {}).get("input_tokens", 0),
            cumulative.get("lifetime", {}).get("output_tokens", 0),
        )
        session = {
            "session_id": self.session_id,
            "calls": self.session_call_count,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            **session_cost,
        }
        cumulative["day"].update(day_cost)
        cumulative["week"].update(week_cost)
        cumulative["month"].update(month_cost)
        cumulative["lifetime"].update(lifetime_cost)
        return {
            "provider": self.provider_name,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "current_session": session,
            "cumulative": cumulative,
            "pricing": {
                "input_usd_per_million": self.pricing.get("input_usd_per_million", 0.0),
                "output_usd_per_million": self.pricing.get("output_usd_per_million", 0.0),
            },
        }
