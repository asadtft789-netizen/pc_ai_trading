"""LLM-driven strategy mutation and generation for the self-evolving trader.

Uses the Claude API to analyze past performance and market regimes to
generate or evolve Python code for trading strategies.
"""

import json
import os
import re
import uuid
import logging
from typing import Optional, Dict, Any, List
import urllib.request
import anthropic
from openai import OpenAI

from config import Settings
from memory import MemoryManager
from strategy import BaseStrategy

logger = logging.getLogger(__name__)

class StrategyMutator:
    """Handles the LLM loop for creating and evolving strategies.

    Runs a unified provider chain: a primary provider (Google AI Studio) with an
    OpenRouter fallback. Both providers read and write the same SQLite memory so
    they act as one continuous mind — if the primary is down or rate-limited, the
    fallback takes over and continues exactly where the primary left off
    (same strategies, rules, reflections, and generations).
    """

    def __init__(self, settings: Settings, memory: MemoryManager):
        self.settings = settings
        self.memory = memory
        # Prefer the unified LLM key from settings (LLM_API_KEY env var), falling back to
        # ANTHROPIC_API_KEY for direct Anthropic access.
        resolved_key = settings.llm.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.has_api = bool(resolved_key and resolved_key != "your_llm_api_key_here")

        # --- Primary provider ---
        provider = (settings.llm.provider or "openai").lower()
        base_url = settings.llm.base_url or None
        self.primary_type, self.primary_client = self._build_client(
            provider, base_url, resolved_key
        )

        # --- Fallback provider (OpenRouter) ---
        fb_provider = (getattr(settings.llm, "fallback_provider", "") or "").lower()
        fb_base = getattr(settings.llm, "fallback_base_url", "") or None
        fb_key = getattr(settings.llm, "fallback_api_key", "") or ""
        self.fallback_type, self.fallback_client = self._build_client(
            fb_provider, fb_base, fb_key
        )

    def _build_client(self, provider: str, base_url: str | None, api_key: str):
        """Build a single LLM client descriptor. Returns (client_type, client)."""
        if provider == "anthropic":
            return "anthropic", anthropic.Anthropic(api_key=api_key)
        if provider == "openrouter" or (
            base_url and "openrouter.ai" in base_url
        ):
            return "openai", OpenAI(api_key=api_key, base_url=base_url or "https://openrouter.ai/api/v1")
        if base_url and "generativelanguage.googleapis.com" in base_url:
            # Google AI Studio: native generateContent endpoint (its OpenAI-compatible
            # chat endpoint returns 404 for every model).
            return "google", None
        # Default: OpenAI-compatible
        return "openai", OpenAI(api_key=api_key, base_url=base_url or "https://api.openai.com/v1")

    def _generate_prompt(self,
                         regime_label: str,
                         regime_indicators: Dict[str, Any],
                         past_reflections: List[str],
                         rules: List[str],
                         parent_strategy_code: Optional[str] = None) -> str:
        """Constructs the prompt for the LLM."""

        system_context = """
You are an expert quantitative trading researcher. Your task is to write a Python trading strategy.
The strategy MUST be a subclass of `BaseStrategy` and implement the `generate_signals(self, df: pd.DataFrame) -> pd.DataFrame` method.

Interface:
- Input `df`: Multi-index (timestamp, symbol) with columns [open, high, low, close, volume].
- Output: The same `df` with a 'signal' column (1 for Buy, -1 for Sell, 0 for Neutral).
- Avoid look-ahead bias.
- Use vectorized operations with pandas/numpy for efficiency.
- Focus on swing trading (holding periods of days to weeks).

Market Context:
Current Regime: {regime}
Indicators: {indicators}

Hard Rules (MUST ALL BE FOLLOWED, combined from all past lessons):
{rules}

Recent Reflections:
{reflections}
"""

        if parent_strategy_code:
            user_prompt = f"""
I have an existing strategy that performed as described in the reflections.
Please MUTATE this strategy to improve its performance in the current '{regime_label}' regime.
Focus on fixing the weaknesses identified in the reflections.

Parent Code:
```python
{parent_strategy_code}
```

Return ONLY the Python code for the new strategy class. Do not include explanations.
"""
        else:
            user_prompt = f"""
Please CREATE a brand new trading strategy optimized for a '{regime_label}' market regime.
The strategy should be robust and handle the volatility described in the indicators.

Return ONLY the Python code for the new strategy class. Do not include explanations.
"""

        return system_context.format(
            regime=regime_label,
            indicators=regime_indicators,
            rules="\n".join([f"- {r}" for r in rules]) if rules else "None yet — every mistake creates a new rule.",
            reflections="\n".join([f"- {r}" for r in past_reflections]) if past_reflections else "No prior reflections."
        ) + "\n\n" + user_prompt

    def generate_new_strategy(self, parent_id: Optional[str] = None) -> Optional[str]:
        """Queries the LLM to generate a new or mutated strategy."""
        if not self.has_api:
            logger.error("LLM API key not found (set LLM_API_KEY or ANTHROPIC_API_KEY).")
            return None

        regime = self.memory.get_latest_regime()
        regime_label = regime['label'] if regime else "unknown"
        regime_indicators = regime['indicators_json'] if regime else "{}"

        # Get recent reflections for this regime or overall
        reflections = []

        # Get all accumulated rules from past lessons (combined into strategy)
        rule_rows = self.memory.get_all_rules()
        rules = [row['rule_text'] for row in rule_rows]

        parent_code = None
        generation = 0
        if parent_id:
            # Fetch parent code from DB
            with self.memory._get_connection() as conn:
                row = conn.execute("SELECT code, generation FROM strategies WHERE id = ?", (parent_id,)).fetchone()
                if row:
                    parent_code = row['code']
                    generation = row['generation'] + 1

        prompt = self._generate_prompt(regime_label, regime_indicators, reflections, rules, parent_code)

        try:
            text = self._chat_complete(
                model=self.settings.llm.model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )

            code = self._extract_code(text)
            if not code:
                return None

            strategy_id = str(uuid.uuid4())
            name = f"Strategy_{strategy_id[:8]}"
            self.memory.save_strategy(strategy_id, name, code, parent_id, generation)
            return strategy_id

        except Exception as e:
            logger.error(f"Error generating strategy: {e}")
            return None

    def generate_lesson(self, strategy_id: str, trades: List[Dict[str, Any]],
                        metrics: Dict[str, Any], regime_label: str) -> Optional[str]:
        """Ask the LLM what to learn from this strategy's recent trades and metrics."""
        if not self.has_api:
            return None

        trade_lines = []
        for t in trades:
            pnl = t.get('pnl')
            pnl_str = f"pnl={pnl}" if pnl is not None else "pnl=unknown"
            trade_lines.append(
                f"{t['symbol']} {t['side']} qty={t['qty']} entry={t.get('entry_price')} exit={t.get('exit_price')} {pnl_str} status={t['status']}"
            )

        prompt = f"""
You are reviewing the completed trading session of an autonomous swing-trading bot.

Strategy ID: {strategy_id}
Market Regime: {regime_label}
Performance Metrics: {json.dumps(metrics)}

Recent Trades:
{chr(10).join(trade_lines) if trade_lines else "No trades executed."}

Based on what worked and what didn't, write exactly ONE concise trading rule (one sentence) that this strategy should follow going forward to avoid repeating mistakes. Format: "Rule: <the rule>". Return ONLY the rule sentence, no explanations.
"""

        try:
            text = self._chat_complete(
                model=self.settings.llm.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = text.strip()
            # Extract the rule sentence after "Rule:" if present
            if text.lower().startswith("rule:"):
                text = text[len("rule:"):].strip()
            return text

        except Exception as e:
            logger.error(f"Error generating lesson: {e}")
            return None

    def _chat_complete(self, model: str, max_tokens: int, messages: List[Dict[str, Any]]) -> str:
        """Send a chat completion, trying the primary provider then the OpenRouter fallback.

        Both providers share the same SQLite memory so they act as one continuous
        mind: if Google AI Studio is down or rate-limited, OpenRouter takes over and
        continues exactly where the primary left off (same strategies, rules,
        reflections, generations).
        """
        last_error: Optional[Exception] = None

        # Try primary provider
        if self.primary_type == "anthropic":
            try:
                response = self.primary_client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=messages,
                )
                return response.content[0].text
            except Exception as e:
                last_error = e
                logger.warning(f"Primary LLM (anthropic) failed: {e}")
        elif self.primary_type == "google":
            try:
                return self._google_complete(
                    self.settings.llm.model, max_tokens, messages
                )
            except Exception as e:
                last_error = e
                logger.warning(f"Primary LLM (google) failed: {e}")
        else:  # openai-compatible
            try:
                response = self.primary_client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=messages,
                )
                return response.choices[0].message.content
            except Exception as e:
                last_error = e
                logger.warning(f"Primary LLM (openai) failed: {e}")

        # Try OpenRouter fallback (same unified mind)
        fb_model = self.settings.llm.fallback_model or model
        if self.fallback_type == "openai" and self.fallback_client is not None:
            try:
                logger.info("Falling back to OpenRouter LLM (unified mind).")
                response = self.fallback_client.chat.completions.create(
                    model=fb_model,
                    max_tokens=max_tokens,
                    messages=messages,
                )
                return response.choices[0].message.content
            except Exception as e:
                last_error = e
                logger.warning(f"Fallback LLM (openrouter) failed: {e}")

        raise RuntimeError(f"All LLM providers failed; last error: {last_error}")

    def _google_complete(self, model: str, max_tokens: int, messages: List[Dict[str, Any]]) -> str:
        """Call Google AI Studio's native generateContent endpoint (OpenAI chat endpoint 404s there)."""
        base = (self.settings.llm.base_url or "").rstrip("/")
        # base_url may already carry a version path (e.g. .../v1beta/openai); strip it so we
        # build the native generateContent URL against the API host origin.
        if "/v1beta" in base:
            base = base[: base.index("/v1beta")]
        elif "/v1" in base:
            base = base[: base.index("/v1")]
        model_name = model
        if model_name.startswith("models/"):
            model_name = model_name[len("models/"):]
        url = f"{base}/v1beta/models/{model_name}:generateContent?key={self.settings.llm.api_key}"

        # Convert OpenAI-style messages to Google contents format
        contents = []
        for m in messages:
            role = m.get("role", "user")
            if role == "assistant":
                role = "model"
            contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})

        body = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.7,
            },
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        candidates = result.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"No candidates returned by Google AI Studio: {result}")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        return text

    def _extract_code(self, text: str) -> Optional[str]:
        """Extracts Python code from LLM response markdown blocks."""
        match = re.search(r"```python\s+(.*?)\s+```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # Fallback if no markdown block but looks like code
        if "class" in text and "def generate_signals" in text:
            return text.strip()
        return None
