"""Provider-owned replay payloads; never reinterpret signed reasoning as prose."""
from copy import deepcopy


def has_reasoning(messages):
    return any(m.get("_provider_history") or "reasoning_content" in m or any(
        isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
    ) for m in messages)


def prepare_messages(messages, provider):
    """Only send native metadata back to its originating provider adapter."""
    keep = getattr(provider, "retain_thinking", True)
    name = type(provider).__name__
    result = []
    for original in messages:
        msg = deepcopy(original)
        payload = msg.pop("_provider_history", None)
        if keep and payload and payload["provider"] == name:
            msg["_provider_history"] = payload
        if not keep or not getattr(provider, "supports_reasoning_history", False):
            msg.pop("reasoning_content", None)
        # Signed Anthropic content is only valid for Anthropic adapters.
        if isinstance(msg.get("content"), list) and name not in ("ClaudeProvider", "VertexProvider"):
            msg["content"] = [b for b in msg["content"] if not isinstance(b, dict)
                              or b.get("type") not in ("thinking", "redacted_thinking")]
        if not keep and isinstance(msg.get("content"), list):
            blocks = msg["content"]
            if not any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
                msg["content"] = [b for b in blocks if not isinstance(b, dict)
                                  or b.get("type") not in ("thinking", "redacted_thinking")]
        result.append(msg)
    return result


def reasoning_text(message):
    text = message.get("reasoning_content", "")
    data = message.get("_provider_history", {}).get("data", {})
    chunks = [text] if text else []
    for key in ("thinking", "reasoning", "reasoning_content"):
        value = data.get(key)
        if isinstance(value, str) and value and value not in chunks:
            chunks.append(value)
    for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
        if isinstance(block, dict) and block.get("type") == "thinking":
            chunks.append(block.get("thinking", ""))
    for part in data.get("parts", []):
        if part.get("thought") and part.get("text"):
            chunks.append(part["text"])
    return "\n".join(chunks)
