import logging

import httpx

from core.config import Settings

logger = logging.getLogger("webnest.llm")

GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class ProviderError(Exception):
    """Raised when a provider call fails for a reason other than rate limiting."""


class RateLimitError(ProviderError):
    """Raised when a provider rejects the call due to quota/rate limits (HTTP 429)."""


class GenerationFailedError(Exception):
    """Raised when every configured provider failed to produce output."""


class LLMProvider:
    """Gemini-primary, Groq-fallback HTML generation. A provider that isn't
    configured (missing API key) is skipped rather than attempted and failed."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def generate_text(self, system_prompt: str, user_message: str) -> tuple[str, str]:
        """Alias for generate_html - the underlying call is plain text-in/text-out
        (Gemini/Groq chat completion), the "_html" name just reflects its first
        caller. Used by non-HTML text generation (e.g. blog post JSON)."""
        return await self.generate_html(system_prompt, user_message)

    async def generate_html(self, system_prompt: str, user_message: str) -> tuple[str, str]:
        if self._settings.gemini_api_key:
            try:
                return await self._call_gemini(system_prompt, user_message), "gemini"
            except (RateLimitError, ProviderError) as exc:
                logger.warning("Gemini generation failed, falling back to Groq: %s", exc)

        if self._settings.groq_api_key:
            try:
                return await self._call_groq(system_prompt, user_message), "groq"
            except (RateLimitError, ProviderError) as exc:
                logger.warning("Groq generation failed: %s", exc)

        raise GenerationFailedError("All configured LLM providers failed to generate a response")

    async def _call_gemini(self, system_prompt: str, user_message: str) -> str:
        url = GEMINI_URL_TEMPLATE.format(model=self._settings.gemini_model)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "maxOutputTokens": self._settings.gemini_max_output_tokens,
                # NOTE: thinkingConfig.thinkingBudget=0 previously lived here to
                # skip hidden reasoning tokens/latency, but whatever model
                # "gemini-flash-latest" currently resolves to now hard-rejects
                # thinkingBudget=0 specifically with a bare HTTP 400 "Request
                # contains an invalid argument" (confirmed by testing every other
                # value - including -1/dynamic - works fine). Omitting the field
                # entirely falls back to the model's default thinking behavior,
                # which costs a bit more latency/tokens but actually succeeds.
            },
        }
        headers = {"x-goog-api-key": self._settings.gemini_api_key, "Content-Type": "application/json"}
        response = await self._post(url, payload, headers, provider="Gemini")

        try:
            parts = response.json()["candidates"][0]["content"]["parts"]
            text = "".join(part["text"] for part in parts if "text" in part and not part.get("thought"))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("Gemini returned an unexpected response shape") from exc
        if not text:
            raise ProviderError("Gemini returned an empty response")
        return text

    async def _call_groq(self, system_prompt: str, user_message: str) -> str:
        payload = {
            "model": self._settings.groq_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": self._settings.groq_max_output_tokens,
        }
        headers = {"Authorization": f"Bearer {self._settings.groq_api_key}"}
        response = await self._post(GROQ_URL, payload, headers, provider="Groq")

        try:
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("Groq returned an unexpected response shape") from exc

    async def _post(self, url: str, payload: dict, headers: dict, provider: str) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._settings.llm_timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{provider} request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{provider} request failed: {exc}") from exc

        if response.status_code == 429:
            raise RateLimitError(f"{provider} rate limit exceeded")
        if response.status_code >= 400:
            raise ProviderError(f"{provider} returned HTTP {response.status_code}: {response.text[:300]}")
        return response
