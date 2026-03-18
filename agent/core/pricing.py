"""
Proton9 — Pricing Manager

Maintains a cached pricing table for LLM models with auto-refresh logic.
Prices are stored in logs/pricing.json and refreshed monthly or on staleness.
"""

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path


# Default pricing (USD per 1M tokens) — manually curated baseline
DEFAULT_PRICING = {
    # ── Gemini 3.x Preview ──
    "gemini-3.1-pro-preview":              {"input": 1.25,  "output": 10.00, "thinking_output": 10.00},
    "gemini-3.1-pro-preview-customtools":   {"input": 1.25,  "output": 10.00, "thinking_output": 10.00},
    "gemini-3.1-flash-lite-preview":        {"input": 0.075, "output": 0.30},
    "gemini-3.1-flash-image-preview":       {"input": 0.10,  "output": 0.40},
    "gemini-3-pro-preview":                {"input": 1.25,  "output": 10.00, "thinking_output": 10.00},
    "gemini-3-flash-preview":              {"input": 0.15,  "output": 0.60,  "thinking_output": 3.50},
    "gemini-3-pro-image-preview":          {"input": 0.15,  "output": 0.60},
    # ── Gemini 2.5 ──
    "gemini-2.5-flash":                    {"input": 0.15,  "output": 0.60,  "thinking_output": 3.50},
    "gemini-2.5-pro":                      {"input": 1.25,  "output": 10.00, "thinking_output": 10.00},
    "gemini-2.5-flash-lite":               {"input": 0.075, "output": 0.30},
    "gemini-2.5-flash-lite-preview-09-2025": {"input": 0.075, "output": 0.30},
    "gemini-2.5-flash-image":              {"input": 0.10,  "output": 0.40},
    "gemini-2.5-computer-use-preview-10-2025": {"input": 1.25, "output": 10.00},
    # ── Gemini 2.0 ──
    "gemini-2.0-flash":                    {"input": 0.10,  "output": 0.40},
    "gemini-2.0-flash-001":                {"input": 0.10,  "output": 0.40},
    "gemini-2.0-flash-lite":               {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash-lite-001":           {"input": 0.075, "output": 0.30},
    # ── Gemini aliases (same pricing as their base model) ──
    "gemini-flash-latest":                 {"input": 0.15,  "output": 0.60,  "thinking_output": 3.50},
    "gemini-flash-lite-latest":            {"input": 0.075, "output": 0.30},
    "gemini-pro-latest":                   {"input": 1.25,  "output": 10.00, "thinking_output": 10.00},
    # ── Gemini 1.5 (legacy) ──
    "gemini-1.5-flash":                    {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro":                      {"input": 1.25,  "output": 5.00},
    # ── DeepSeek ──
    "deepseek-chat":                       {"input": 0.27,  "output": 1.10,  "cache_hit": 0.07},
    "deepseek-reasoner":                   {"input": 0.55,  "output": 2.19,  "cache_hit": 0.14},
    # ── OpenAI ──
    "gpt-4o":                              {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":                         {"input": 0.15,  "output": 0.60},
    "gpt-4.1":                             {"input": 2.00,  "output": 8.00},
    "gpt-4.1-mini":                        {"input": 0.40,  "output": 1.60},
    "gpt-4.1-nano":                        {"input": 0.10,  "output": 0.40},
    "o3-mini":                             {"input": 1.10,  "output": 4.40},
    # ── Anthropic ──
    "claude-sonnet-4-20250514":            {"input": 3.00,  "output": 15.00},
    "claude-3.7-sonnet":                   {"input": 3.00,  "output": 15.00},
    "claude-3.5-sonnet":                   {"input": 3.00,  "output": 15.00},
    "claude-3.5-haiku":                    {"input": 0.80,  "output": 4.00},
}


class PricingManager:
    """
    Manages LLM pricing data with persistence and staleness-based refresh.

    Pricing is cached in a JSON file with a `last_checked` timestamp.
    Refresh logic:
      - If last_checked > 31 days ago → refresh on current launch
      - If today is 1st of month and not checked this month → refresh
      - Otherwise → use cached prices
    """

    def __init__(self, pricing_path: str = None):
        if pricing_path is None:
            pricing_path = str(Path(__file__).parent.parent / "logs" / "pricing.json")
        self.pricing_path = os.path.abspath(pricing_path)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.pricing_path), exist_ok=True)
        self._data = self._load()

    def _default_data(self) -> dict:
        return {
            "schema_version": 1,
            "last_checked": "",
            "models": dict(DEFAULT_PRICING),
        }

    def _load(self) -> dict:
        if not os.path.exists(self.pricing_path):
            return self._default_data()
        try:
            with open(self.pricing_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "models" not in data:
                return self._default_data()
            return data
        except Exception:
            return self._default_data()

    def _save(self):
        with self._lock:
            temp_path = f"{self.pricing_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, self.pricing_path)

    def should_refresh(self) -> bool:
        """Check if pricing data is stale and needs refreshing."""
        last_checked_str = self._data.get("last_checked", "")
        if not last_checked_str:
            return True

        try:
            last_checked = datetime.fromisoformat(last_checked_str)
        except (ValueError, TypeError):
            return True

        now = datetime.now()

        # If > 31 days since last check → refresh
        if (now - last_checked) > timedelta(days=31):
            return True

        # If today is the 1st and we haven't checked this month → refresh
        if now.day == 1 and last_checked.month != now.month:
            return True

        return False

    def refresh(self):
        """
        Refresh pricing data.

        Currently merges defaults (which ship with code updates).
        Future: HTTP fetch from official pricing pages.
        """
        print("  [PRICING] Refreshing pricing table...")

        # Merge defaults with any existing custom entries
        models = self._data.get("models", {})
        for model, prices in DEFAULT_PRICING.items():
            models[model] = prices  # Defaults override stale values

        self._data["models"] = models
        self._data["last_checked"] = datetime.now().isoformat()
        self._save()
        print(f"  [PRICING] Updated {len(models)} models (last_checked={self._data['last_checked'][:10]})")

    def check_and_refresh(self):
        """Check staleness and refresh if needed. Called on server startup."""
        if self.should_refresh():
            self.refresh()
        else:
            last = self._data.get("last_checked", "")[:10]
            print(f"  [PRICING] Using cached prices (last_checked={last})")

    def get_pricing(self) -> dict:
        """Return the full pricing data for sending to the GUI."""
        return {
            "models": self._data.get("models", {}),
            "last_checked": self._data.get("last_checked", ""),
        }

    def get_model_price(self, model: str) -> dict:
        """Get pricing for a specific model. Returns empty dict if unknown."""
        return self._data.get("models", {}).get(model, {})

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost in USD for a given token usage."""
        prices = self.get_model_price(model)
        if not prices:
            return 0.0
        in_rate = float(prices.get("input", 0))
        out_rate = float(prices.get("output", 0))
        cost = (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate
        return round(cost, 6)
