"""Safe, deterministic diagnostics for OpenAI-compatible LLM endpoints."""

from __future__ import annotations

import json
from urllib.parse import urlparse


def validate_endpoint(api_url: str) -> tuple[bool, str]:
    value = (api_url or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return False, "The API URL is malformed."
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "Enter a complete URL beginning with http:// or https://."
    if parsed.username or parsed.password:
        return False, "Do not place credentials inside the API URL."
    return True, ""


def _content_preview(value) -> str:
    """Extract a short display preview from common chat content shapes."""
    if isinstance(value, str):
        return value.strip()[:80]
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(part.strip() for part in parts if part.strip())[:80]


def parse_chat_completion_response(payload) -> tuple[bool, str]:
    """Validate connectivity without requiring non-empty assistant text.

    Reasoning and tool-capable models may return a valid ``chat.completion``
    with ``message.content`` set to null when a tiny test output budget is
    exhausted before visible text.  Connectivity is established by the
    OpenAI-compatible choice/message envelope; visible content is optional.
    """
    if not isinstance(payload, dict):
        return False, ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False, ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return False, ""

    message = choice.get("message")
    if isinstance(message, dict):
        preview = _content_preview(message.get("content"))
        if not preview:
            preview = _content_preview(
                message.get("reasoning_content") or message.get("reasoning")
            )
        return True, preview

    if "text" in choice:
        return True, _content_preview(choice.get("text"))
    if isinstance(choice.get("delta"), dict):
        return True, _content_preview(choice["delta"].get("content"))

    object_type = str(payload.get("object") or "")
    if object_type in {"chat.completion", "chat.completion.chunk"} and (
        "finish_reason" in choice or "logprobs" in choice
    ):
        return True, ""
    return False, ""


def _body_message(body: str) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text[:300]
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "")[:300]
        if error:
            return str(error)[:300]
        return str(parsed.get("message") or "")[:300]
    return str(parsed)[:300]


def diagnose_http_failure(status_code: int, body: str = "") -> dict:
    upstream = _body_message(body)
    mapping = {
        400: (
            "invalid_request",
            "The endpoint rejected the request format.",
            [
                "Confirm that this is an OpenAI-compatible /chat/completions endpoint.",
                "Check whether the selected model accepts chat-completions requests.",
            ],
        ),
        401: (
            "invalid_api_key",
            "Authentication failed.",
            [
                "Check that the API key is complete and belongs to this provider.",
                "Remove accidental spaces before or after the key.",
            ],
        ),
        403: (
            "permission_denied",
            "The key or account is not permitted to use this resource.",
            [
                "Check account credits, model access, region policy, and organization permissions.",
                "Confirm that the endpoint and API key come from the same provider.",
            ],
        ),
        404: (
            "endpoint_or_model_not_found",
            "The endpoint path or model name was not found.",
            [
                "Use the provider's complete /v1/chat/completions URL.",
                "Copy the exact model identifier from the provider's model list.",
            ],
        ),
        408: (
            "upstream_timeout",
            "The provider timed out before completing the request.",
            ["Retry once, then check provider status and network stability."],
        ),
        409: (
            "request_conflict",
            "The provider reported a request conflict.",
            ["Retry the request and check the provider's concurrency rules."],
        ),
        422: (
            "unsupported_parameters",
            "The model does not accept one or more request parameters.",
            [
                "Confirm OpenAI chat-completions compatibility.",
                "Try a standard chat model exposed by the same provider.",
            ],
        ),
        429: (
            "rate_limited",
            "The provider is rate limiting this account.",
            [
                "Wait briefly and retry.",
                "Reduce parallel requests or add another valid key from the same provider.",
            ],
        ),
    }
    if status_code in mapping:
        code, summary, suggestions = mapping[status_code]
    elif 500 <= status_code <= 599:
        code, summary, suggestions = (
            "provider_unavailable",
            "The provider returned a server-side error.",
            [
                "Retry after a short delay.",
                "Check the provider status page or try a different model on the same endpoint.",
            ],
        )
    else:
        code, summary, suggestions = (
            "http_error",
            f"The provider returned HTTP {status_code}.",
            ["Review the endpoint, model identifier, key, and provider documentation."],
        )
    return {
        "code": code,
        "summary": summary,
        "suggestions": suggestions,
        "upstream_message": upstream,
    }


def diagnose_exception(exc: Exception) -> dict:
    name = type(exc).__name__.lower()
    message = str(exc)
    lowered = message.lower()
    if "timeout" in name or "timed out" in lowered:
        code, summary, suggestions = (
            "timeout",
            "The connection timed out.",
            [
                "Check whether the service is running and reachable.",
                "Verify proxy, firewall, VPN, and provider availability.",
            ],
        )
    elif "ssl" in name or "certificate" in lowered:
        code, summary, suggestions = (
            "tls_error",
            "TLS certificate verification failed.",
            [
                "Check the system clock and the endpoint certificate.",
                "Do not disable TLS verification; fix the certificate or proxy instead.",
            ],
        )
    elif any(token in lowered for token in ("name resolution", "getaddrinfo", "dns")):
        code, summary, suggestions = (
            "dns_error",
            "The provider hostname could not be resolved.",
            ["Check the URL spelling, DNS settings, proxy, and internet connection."],
        )
    elif "connection refused" in lowered:
        code, summary, suggestions = (
            "connection_refused",
            "The target host refused the connection.",
            [
                "For a local endpoint, start the local proxy or model service.",
                "Confirm the host and port in the API URL.",
            ],
        )
    else:
        code, summary, suggestions = (
            "network_error",
            "PathCopilot could not complete the network request.",
            ["Check the URL, provider status, proxy, firewall, and internet connection."],
        )
    return {
        "code": code,
        "summary": summary,
        "suggestions": suggestions,
        "upstream_message": message[:300],
    }
