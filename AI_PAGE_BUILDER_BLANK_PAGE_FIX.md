# AI Page Builder — generated page downloads/renders blank

**Status:** Fixed on the backend. No frontend change required for this one.
**Affects:** `services/llm_provider.py`, `services/html_pipeline.py`, `core/config.py`

## Symptom

A generation completes successfully (`200`, no error shown), but the returned
`html` renders as a blank page when opened — the `<title>` shows in the
browser tab, but the body is empty or the page is visibly broken.

## Root cause

This was three separate problems that only show up together:

**1. Gemini was silently truncating output.**
`gemini-flash-latest` currently resolves to a "thinking" model. Before it
writes any visible HTML, it spends part of its `maxOutputTokens` budget (and
tens of seconds of latency) on hidden reasoning. For a CSS-heavy page this
was enough to hit the token cap (`finishReason: "MAX_TOKENS"`) before the
model finished writing the document — confirmed by reproducing the exact
failing prompt directly against the Gemini API.

**2. The backend didn't detect the truncation.**
`extract_html_document()` looked for a closing `</html>` but, if it wasn't
found, fell through and used the text anyway instead of treating it as an
error. A cut-off document was accepted as "successful."

**3. The sanitizer silently dropped everything after the cut-off point.**
Because the truncated output cut off mid-`<style>` block, the `<style>` tag
was never closed. Python's stdlib `html.parser` — which the sanitizer is
built on — has a specific (and easy to miss) behavior: if it enters a
`<style>`/`<script>` block and never finds the matching closing tag before
the input ends, it **silently discards everything from that point on**
instead of raising an error or flushing the buffered content. So all the
real page content (nav, hero section, menu, everything) after the opening
`<style>` tag vanished with no error raised anywhere.

The watermark footer then got appended to whatever tiny fragment was left
(usually just the `<head>` and an empty/opening `<style>`), which is exactly
the broken markup seen in the downloaded file.

## Fix

Four changes, all on the backend:

1. **`extract_html_document()` now hard-fails on truncation.** If no closing
   `</html>` is found, it raises `MalformedOutputError` instead of
   continuing. This turns a silent blank-page bug into a clean `503`
   response (`generation_unavailable`), so the user gets a clear "try again"
   instead of a broken page saved to their history. This is a structural
   safety net regardless of *why* the output got cut off.

2. **Disabled Gemini's hidden thinking** (`thinkingConfig.thinkingBudget: 0`
   in the Gemini API payload). We don't need chain-of-thought reasoning for
   HTML generation — this frees the entire token budget for the actual
   output and removes the extra latency the reasoning step was adding.

3. **Split the output token budget per provider** instead of one shared
   value:
   - Gemini: `16384` (well under its real 65536 cap, generous for a full styled page)
   - Groq: `8000` — Groq's `llama-3.3-70b-versatile` supports up to 32768
     completion tokens in theory, but the account's actual tokens-per-minute
     budget is what binds in practice: Groq rejects a request outright
     (`413`) if the *requested* `max_tokens` alone could exceed the
     remaining TPM budget, regardless of how much the response would
     actually use. The old shared value of 16384 tripped this every time
     Gemini failed over to Groq.

4. **Raised `LLM_TIMEOUT_SECONDS` from 20s to 45s.** A fully styled page
   with real CSS/JS routinely takes longer than 20s to generate; the old
   timeout was cutting off genuinely-in-progress (not hung) requests.

## Testing

Reproduced end-to-end against the real Gemini/Groq APIs and Supabase with
the exact prompt that produced the blank page report ("Honey & Hearth
Bakery, artisanal breads and handcrafted pastries, warm earthy tones"):

- Confirmed the raw Gemini output was cut off mid-CSS with
  `finishReason: "MAX_TOKENS"`, and that the sanitizer was silently
  dropping everything after the unclosed `<style>` tag — reproduced the
  exact reported bug before any fix.
- After the fix, a full generation (via the Groq fallback path, since
  Gemini's free-tier daily quota was exhausted by this testing) produced a
  complete, well-formed document: starts with `<!DOCTYPE html>`, ends with
  `</body>\n</html>`, single correctly-placed watermark, full real page
  content intact.
- The Gemini-specific change (`thinkingBudget: 0`) is applied but wasn't
  re-verified against a live Gemini call in this session, since the daily
  free-tier quota (20 requests/day) was used up during investigation. It
  will be exercised naturally the next time a generation runs after quota
  resets — worth a spot-check then.

## Not needed for this fix

No frontend changes. The earlier timeout fix
(`AI_PAGE_BUILDER_TIMEOUT_FIX.md`) is still required separately — that one
was about the browser aborting slow-but-successful requests, this one was
about the backend returning a broken document. Both were real bugs hit in
the same testing session but are unrelated to each other.
