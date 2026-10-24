"""GCP Vertex AI provider for Claude models.

Uses the Anthropic SDK's built-in AsyncAnthropicVertex client, which has the
same messages.create() API as the standard client.  Auth uses GCP Application
Default Credentials (``gcloud auth application-default login``).

Install: pip install "anthropic[vertex]" google-cloud-aiplatform
"""

import json
import logging
import time
from pathlib import Path

from anthropic import AsyncAnthropicVertex

from config import get_settings
from llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Vertex model IDs differ from direct API (e.g. "claude-sonnet-4-6" not
# "claude-sonnet-4-20250514").  Global endpoint pricing = same as direct API;
# regional endpoints have a 10 % premium.
VERTEX_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0}
REGIONAL_MULTIPLIER = 1.1  # 10 % premium for non-global endpoints
PRICING_CACHE_PATH = Path("data/vertex_pricing_cache.json")
PRICING_CACHE_TTL = 86400  # 24 h


def _fetch_billing_pricing() -> dict[str, dict[str, float]] | None:
    """Fetch Claude pricing from GCP Cloud Billing API.

    Returns ``{model: {input: $/Mtok, output: $/Mtok}}`` or *None* on failure.
    """
    # Check file cache first
    if PRICING_CACHE_PATH.exists():
        try:
            cached = json.loads(PRICING_CACHE_PATH.read_text())
            if time.time() - cached.get("ts", 0) < PRICING_CACHE_TTL:
                logger.debug("Using cached Vertex pricing (%s)", PRICING_CACHE_PATH)
                return cached.get("pricing")
        except (json.JSONDecodeError, OSError):
            pass

    try:
        from google.cloud import billing_v1  # type: ignore[attr-defined]
    except ImportError:
        logger.debug("google-cloud-billing not installed; skipping pricing fetch")
        return None

    try:
        client = billing_v1.CloudCatalogClient()

        # Find the Vertex AI service
        vertex_service = None
        for svc in client.list_services():
            if "Vertex AI" in (svc.display_name or ""):
                vertex_service = svc.name
                break
        if not vertex_service:
            logger.debug("Vertex AI service not found in Cloud Billing catalog")
            return None

        # Scan SKUs for Claude models
        pricing: dict[str, dict[str, float]] = {}
        for sku in client.list_skus(parent=vertex_service):
            desc = (sku.description or "").lower()
            if "claude" not in desc:
                continue

            # Determine model name from description
            model_name = None
            for known in VERTEX_PRICING:
                if known.replace("-", " ") in desc or known.replace("-", "") in desc:
                    model_name = known
                    break
            if not model_name:
                continue

            # Determine input vs output from category / description
            is_output = "output" in desc
            direction = "output" if is_output else "input"

            # Extract price per unit
            if sku.pricing_info and sku.pricing_info[0].pricing_expression:
                rates = sku.pricing_info[0].pricing_expression.tiered_rates
                if rates:
                    unit_price = rates[0].unit_price
                    price_usd = unit_price.units + unit_price.nanos / 1e9
                    # Billing API prices are per 1K chars; convert to per 1M tokens (~4 chars/token)
                    price_per_mtok = price_usd * 1000 * 4
                    pricing.setdefault(model_name, {})
                    pricing[model_name][direction] = round(price_per_mtok, 2)

        if pricing:
            # Cache to disk
            PRICING_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            PRICING_CACHE_PATH.write_text(json.dumps({"ts": time.time(), "pricing": pricing}))
            logger.info("Fetched Vertex pricing from Cloud Billing API: %s", list(pricing.keys()))
            return pricing

    except Exception:
        logger.debug("Cloud Billing API pricing fetch failed", exc_info=True)

    return None


class VertexProvider(LLMProvider):
    """Claude via GCP Vertex AI."""

    def __init__(self):
        settings = get_settings()
        if not settings.vertex_project_id:
            raise ValueError(
                "vertex_project_id is required. Set VERTEX_PROJECT_ID env var "
                "or pass --vertex-project."
            )
        self.client = AsyncAnthropicVertex(
            project_id=settings.vertex_project_id,
            region=settings.vertex_region,
        )
        self.model = settings.vertex_model
        self._region = settings.vertex_region
        self._input_price_override = settings.vertex_input_price
        self._output_price_override = settings.vertex_output_price
        self.total_input_tokens = 0
        self.total_output_tokens = 0

        # Resolve pricing
        self._pricing = self._resolve_pricing()

    def _resolve_pricing(self) -> dict[str, float]:
        """Determine input/output pricing for the current model.

        Priority: config overrides > Cloud Billing API > hardcoded fallback.
        """
        if self._input_price_override > 0 and self._output_price_override > 0:
            return {
                "input": self._input_price_override,
                "output": self._output_price_override,
            }

        # Try Cloud Billing API
        billing = _fetch_billing_pricing()
        if billing and self.model in billing:
            p = billing[self.model]
            if "input" in p and "output" in p:
                return p

        # Hardcoded fallback
        p = VERTEX_PRICING.get(self.model, DEFAULT_PRICING).copy()

        # Apply regional multiplier
        if self._region != "global":
            p["input"] = round(p["input"] * REGIONAL_MULTIPLIER, 2)
            p["output"] = round(p["output"] * REGIONAL_MULTIPLIER, 2)

        return p

    async def analyze(self, prompt: str, context: str = "") -> str:
        messages = []
        if context:
            messages.append({"role": "user", "content": f"Context:\n{context}\n\n{prompt}"})
        else:
            messages.append({"role": "user", "content": prompt})

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=messages,
        )
        self.total_input_tokens += response.usage.input_tokens
        self.total_output_tokens += response.usage.output_tokens
        logger.debug(
            "Vertex tokens: in=%d, out=%d",
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return response.content[0].text

    async def generate_report(self, prompt: str) -> str:
        return await self.analyze(prompt)

    def estimate_cost(
        self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> float:
        cost = (input_tokens / 1_000_000) * self._pricing["input"]
        cost += (output_tokens / 1_000_000) * self._pricing["output"]
        return cost

    @property
    def session_cost(self) -> float:
        return self.estimate_cost(self.total_input_tokens, self.total_output_tokens)

    @property
    def supports_tool_use(self) -> bool:
        return True

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 4096,
    ) -> dict:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=messages,
            tools=tools,
        )
        self.total_input_tokens += response.usage.input_tokens
        self.total_output_tokens += response.usage.output_tokens
        logger.debug(
            "Vertex tool-call tokens: in=%d, out=%d",
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return {
            "content": response.content,
            "stop_reason": response.stop_reason,
            "usage": {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        }
