"""Gemini and Groq calls for question drafting, cross-checking and embeddings.
Only this module talks to the model APIs. Every call has a hard timeout;
quota refusals become QuotaExhausted (refills pause), everything else
ProviderFailure. API keys never appear in logs or errors."""

import logging
from dataclasses import dataclass

import httpx

from exams.errors import ProviderFailure, QuotaExhausted

logger = logging.getLogger("webnest.exams.llm")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_RATE_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "retry-after",
)
EMBEDDING_DIMENSIONS = 768
MAX_EMBED_BATCH = 50


@dataclass(frozen=True)
class LlmReplyData:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    rate_limits: dict | None


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    try:
        return max(1, int(float(value))) if value else None
    except ValueError:
        return None


class ExamLlmClient:
    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    def available_providers(self) -> list[str]:
        providers = []
        if (self._settings.gemini_api_key or "").strip():
            providers.append("gemini")
        if (self._settings.groq_api_key or "").strip():
            providers.append("groq")
        return providers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._settings.llm_timeout_seconds, transport=self._transport)

    async def _post(self, provider: str, url: str, payload: dict, headers: dict) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderFailure(provider, f"{provider} request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderFailure(provider, f"{provider} request failed: {type(exc).__name__}") from exc
        if response.status_code == 429:
            raise QuotaExhausted(provider, f"{provider} quota or rate limit reached", _retry_after(response))
        body = ""
        if response.status_code >= 400:
            try:
                body = response.text[:300]
            except Exception:
                body = ""
            if "RESOURCE_EXHAUSTED" in body:
                raise QuotaExhausted(provider, f"{provider} quota exhausted", _retry_after(response))
            raise ProviderFailure(provider, f"{provider} returned HTTP {response.status_code}")
        return response

    async def complete_json(
        self, provider: str, system_prompt: str, user_message: str, temperature: float, max_tokens: int
    ) -> LlmReplyData:
        if provider == "gemini":
            return await self._gemini(system_prompt, user_message, temperature, max_tokens)
        if provider == "groq":
            return await self._groq(system_prompt, user_message, temperature, max_tokens)
        raise ProviderFailure(provider, f"unknown provider {provider}")

    async def _gemini(self, system_prompt: str, user_message: str, temperature: float, max_tokens: int) -> LlmReplyData:
        model = self._settings.gemini_model
        payload = {
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        headers = {"x-goog-api-key": self._settings.gemini_api_key, "Content-Type": "application/json"}
        response = await self._post("gemini", f"{GEMINI_BASE}/{model}:generateContent", payload, headers)
        try:
            data = response.json()
            candidate = data["candidates"][0]
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
            usage = data.get("usageMetadata") or {}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderFailure("gemini", "gemini returned an unexpected response shape") from exc
        if not text.strip():
            raise ProviderFailure("gemini", "gemini returned an empty response")
        return LlmReplyData(
            text=text,
            provider="gemini",
            model=model,
            prompt_tokens=_int(usage.get("promptTokenCount")),
            completion_tokens=_int(usage.get("candidatesTokenCount")) + _int(usage.get("thoughtsTokenCount")),
            rate_limits=None,
        )

    async def _groq(self, system_prompt: str, user_message: str, temperature: float, max_tokens: int) -> LlmReplyData:
        model = self._settings.groq_model
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": min(max_tokens, self._settings.groq_max_output_tokens),
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._settings.groq_api_key}"}
        response = await self._post("groq", GROQ_URL, payload, headers)
        rate_limits = {h: response.headers[h] for h in GROQ_RATE_HEADERS if h in response.headers} or None
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderFailure("groq", "groq returned an unexpected response shape") from exc
        if not text.strip():
            raise ProviderFailure("groq", "groq returned an empty response")
        return LlmReplyData(
            text=text,
            provider="groq",
            model=model,
            prompt_tokens=_int(usage.get("prompt_tokens")),
            completion_tokens=_int(usage.get("completion_tokens")),
            rate_limits=rate_limits,
        )


class GeminiEmbedder:
    """768-dimension embeddings of prompt + correct option, one batch request."""

    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    def available(self) -> bool:
        return bool((self._settings.gemini_api_key or "").strip())

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        if not texts:
            return [], 0
        if not self.available():
            raise ProviderFailure("gemini", "embeddings need a Gemini API key")
        model = self._settings.exam_embedding_model
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_EMBED_BATCH):
            chunk = texts[start : start + MAX_EMBED_BATCH]
            payload = {
                "requests": [
                    {
                        "model": f"models/{model}",
                        "content": {"parts": [{"text": text[:8000]}]},
                        "outputDimensionality": EMBEDDING_DIMENSIONS,
                    }
                    for text in chunk
                ]
            }
            headers = {"x-goog-api-key": self._settings.gemini_api_key, "Content-Type": "application/json"}
            try:
                async with httpx.AsyncClient(timeout=self._settings.llm_timeout_seconds, transport=self._transport) as client:
                    response = await client.post(f"{GEMINI_BASE}/{model}:batchEmbedContents", json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise ProviderFailure("gemini", f"embedding request failed: {type(exc).__name__}") from exc
            if response.status_code == 429:
                raise QuotaExhausted("gemini", "gemini embedding quota reached", _retry_after(response))
            if response.status_code >= 400:
                raise ProviderFailure("gemini", f"embedding returned HTTP {response.status_code}")
            try:
                embeddings = response.json()["embeddings"]
                chunk_vectors = [[float(x) for x in item["values"]] for item in embeddings]
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderFailure("gemini", "embedding response had an unexpected shape") from exc
            if len(chunk_vectors) != len(chunk) or any(len(v) != EMBEDDING_DIMENSIONS for v in chunk_vectors):
                raise ProviderFailure("gemini", "embedding response had the wrong size")
            vectors.extend(chunk_vectors)
        # The batch endpoint reports no usage; estimate ~4 characters per token.
        return vectors, sum(len(t) for t in texts) // 4
