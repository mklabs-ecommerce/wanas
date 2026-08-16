"""The neutral message format -- the contract between agent and provider.

Three shapes, and only three:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [ToolCall, ...]}
    {"role": "tool_results", "results": [ToolResult, ...]}

A ToolCall is {"id", "name", "arguments", "signature"?}; a ToolResult is
{"id", "name", "content"}.

`signature` exists because some models return an opaque blob with each tool
call that has to be echoed back in the next request or the whole thing is
refused. It is carried through history untouched and means nothing here --
absorbing that kind of difference inside the provider layer is the point of
having one.

The agent only ever speaks this. Adding a provider means writing one class and
changing one config value; no other file changes.
"""

from __future__ import annotations

USER = "user"
ASSISTANT = "assistant"
TOOL_RESULTS = "tool_results"


def user(text: str) -> dict:
    return {"role": USER, "content": text}


def assistant(
    text: str = "",
    tool_calls: list[dict] | None = None,
    signature: str | None = None,
    attachments: list[str] | None = None,
) -> dict:
    message: dict = {"role": ASSISTANT, "content": text or ""}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if signature:
        message["signature"] = signature
    if attachments:
        # Images actually sent with this reply, kept in history (not sent to
        # the provider -- every translation layer ignores unknown keys) so a
        # later turn can tell what the customer has already been shown and
        # never resend it uninvited.
        message["attachments"] = list(attachments)
    return message


def tool_call(call_id: str, name: str, arguments: dict, signature: str | None = None) -> dict:
    call = {"id": call_id, "name": name, "arguments": arguments or {}}
    if signature:
        call["signature"] = signature
    return call


def tool_results(results: list[dict]) -> dict:
    return {"role": TOOL_RESULTS, "results": results}


def tool_result(call_id: str, name: str, content: dict) -> dict:
    return {"id": call_id, "name": name, "content": content}


def is_user(message: dict) -> bool:
    return message.get("role") == USER


def has_tool_calls(message: dict) -> bool:
    return bool(message.get("tool_calls"))
