"""
LLMPlanner — R5: integrates an LLM as an order-intake / planning agent.

The LLM receives a natural-language production goal and returns a structured
JSON production plan.  The plan is validated before being dispatched to the
agent system.  The LLM has NO direct Modbus access — it only produces JSON
that the SourceAgents act on.

Default model: claude-haiku-4-5-20251001 (fast, cheap, structurally capable).
Falls back to Ollama local models if ANTHROPIC_API_KEY is not set.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON schema for LLM output validation
# ---------------------------------------------------------------------------

_SCHEMA_DESCRIPTION = """
{
  "orders": [
    {"type": <int 1 or 2>, "count": <positive int>},
    ...
  ],
  "total_parts": <sum of counts>,
  "reasoning": "<brief explanation of how you read the request>"
}
"""

_SYSTEM_PROMPT = """You are a production planner for a crane-based manufacturing cell.

The cell contains:
- Source1: generates Type-1 parts (plan: Process1 → Sink)
- Source2: generates Type-2 parts (plan: Process2 → Process1 → Sink)
- Process1, Process2: two processing stations
- Sink: completed parts exit here

Your task: parse the user's production request and output a JSON production plan.

RULES:
1. Output ONLY valid JSON — no markdown, no extra text.
2. "type" must be 1 or 2.
3. "count" must be a positive integer.
4. "total_parts" must equal the sum of all counts.
5. If the request is ambiguous, make a reasonable interpretation and explain in "reasoning".
6. If the request is impossible (e.g. negative count), return {"error": "<reason>"}.

OUTPUT FORMAT:
""" + _SCHEMA_DESCRIPTION


class LLMPlanner:
    """Parses natural-language production orders into structured JSON plans."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001",
                 max_retries: int = 3):
        self.model       = model
        self.max_retries = max_retries
        self._client     = None
        self._use_ollama = True
        self._init_client()

    # ------------------------------------------------------------------
    # Client initialisation
    # ------------------------------------------------------------------

    def _init_client(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=api_key)
                logger.info("[LLM] Using Anthropic API — model: %s", self.model)
            except ImportError:
                logger.warning("[LLM] anthropic package not installed; trying Ollama")
                self._try_ollama()
        else:
            logger.info("[LLM] No ANTHROPIC_API_KEY — trying Ollama")
            self._try_ollama()

    def _try_ollama(self):
        try:
            import requests  # noqa: F401
            self._use_ollama = True
            logger.info("[LLM] Using Ollama local server")
        except ImportError:
            logger.warning("[LLM] requests not installed; Ollama unavailable")

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def plan(self, user_input: str) -> Optional[dict]:
        """
        Convert a natural-language order into a structured production plan.

        Returns a validated dict like:
          {"orders": [{"type": 1, "count": 3}, {"type": 2, "count": 2}],
           "total_parts": 5, "reasoning": "..."}

        Returns None if LLM is unavailable or all retries fail.
        """
        print(f"\n[LLM Planner] Input: '{user_input}'")

        for attempt in range(1, self.max_retries + 1):
            raw = self._call_llm(user_input)
            if raw is None:
                logger.error("[LLM] No response on attempt %d", attempt)
                continue

            print(f"[LLM Planner] Raw output (attempt {attempt}):\n{raw}\n")

            plan = self._parse_and_validate(raw)
            if plan is not None:
                logger.info("[LLM] Valid plan: %s", plan)
                return plan

            logger.warning("[LLM] Invalid output on attempt %d, retrying...", attempt)

        logger.error("[LLM] All %d attempts failed — falling back to None", self.max_retries)
        return None

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _call_llm(self, user_input: str) -> Optional[str]:
        if self._client is not None:
            return self._call_anthropic(user_input)
        if self._use_ollama:
            return self._call_ollama(user_input)
        # No backend available: return a hand-written plan for demo purposes
        return self._fallback_parse(user_input)

    def _call_anthropic(self, user_input: str) -> Optional[str]:
        try:
            import anthropic
            response = self._client.messages.create(
                model=self.model,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_input}],
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.error("[LLM] Anthropic error: %s", e)
            return None

    def _call_ollama(self, user_input: str,
                     ollama_url: str = "http://localhost:11435/api/generate",
                     model: str = "llama3") -> Optional[str]:
        try:
            import requests
            payload = {
                "model": model,
                "prompt": _SYSTEM_PROMPT + "\n\nUser request: " + user_input,
                "stream": False,
            }
            resp = requests.post(ollama_url, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
        except Exception as e:
            logger.error("[LLM] Ollama error: %s", e)
            return None

    def _fallback_parse(self, user_input: str) -> str:
        """
        Minimal regex-free parser used when no LLM backend is available.
        Demonstrates the system still works — report must mention this fallback.
        """
        import re
        orders = []
        for match in re.finditer(r"(\d+)\s*type[-\s]?(\d)", user_input, re.I):
            count = int(match.group(1))
            ptype = int(match.group(2))
            orders.append({"type": ptype, "count": count})

        if not orders:
            orders = [{"type": 1, "count": 1}]

        total = sum(o["count"] for o in orders)
        return json.dumps({
            "orders": orders,
            "total_parts": total,
            "reasoning": "Parsed locally (no LLM backend available).",
        })

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _parse_and_validate(self, raw: str) -> Optional[dict]:
        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("[LLM] JSON parse error: %s", e)
            return None

        if "error" in data:
            logger.warning("[LLM] LLM reported error: %s", data["error"])
            return None

        orders = data.get("orders")
        if not isinstance(orders, list) or len(orders) == 0:
            logger.warning("[LLM] Missing or empty 'orders'")
            return None

        for o in orders:
            if not isinstance(o.get("type"), int) or o["type"] not in (1, 2):
                logger.warning("[LLM] Invalid type: %s", o)
                return None
            if not isinstance(o.get("count"), int) or o["count"] < 1:
                logger.warning("[LLM] Invalid count: %s", o)
                return None

        expected_total = sum(o["count"] for o in orders)
        if data.get("total_parts") != expected_total:
            data["total_parts"] = expected_total   # fix silently

        return data
