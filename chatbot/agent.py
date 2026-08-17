"""The agent loop.

1. Load conversation history for this channel identity.
2. Append the incoming message.
3. Call the model with the system prompt, the history and the tool schemas.
4. No tool calls -> that is the reply; save and send.
5. Tool calls -> execute all of them, append the results, go back to 3.
6. Cap the loop.

All of a turn's tool calls are executed together rather than one per round
trip, because "the black hoodie in L and the olive polo in M" is one message
with two products and should resolve in one pass.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from backend.config import settings
from chatbot import messages as msg
from chatbot import session as session_store
from chatbot.prompt import build_system_prompt
from chatbot.providers import LLMProvider, ProviderError, get_provider
from chatbot.tools.base import REGISTRY, ToolContext, call_tool, load_all, tool_specs

log = logging.getLogger("wanas.agent")

load_all()

# Customer-facing failure text. The customer never sees a stack trace and
# staff always see the real error.
RATE_LIMITED = "الضغط عالي شوية، ابعتلي تاني بعد دقيقة."
GENERIC_FAILURE = "حصلت مشكلة عندنا دلوقتي. جرب تبعت تاني بعد شوية وأنا موجود."
LOOP_EXHAUSTED = "معلش خدت وقت في ده. ممكن توضحلي طلبك في جملة واحدة؟"


#: Any local path the model might echo out of a tool result. The prompt tells
#: it never to write one; this is the guarantee behind the instruction, since
#: a pasted "data/images/..." is meaningless to a customer and leaks the
#: server's layout.
_PATH_LEAK = re.compile(r"\S*data[/\\](?:images|size-charts|inbound)[/\\]\S*")


def strip_paths(text: str) -> tuple[str, bool]:
    cleaned = _PATH_LEAK.sub("", text or "")
    if cleaned == text:
        return text, False
    # Collapse the whitespace the removal left behind.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True


#: A tool the model named as if it had called it, written out as text instead
#: of a real function call -- `request_human(reason='unclear', summary='...')`
#: as the actual reply. The prompt forbids it; this is the guarantee behind
#: the instruction, built from the live tool registry so it never drifts from
#: what the model can actually call.
def _tool_call_leak_pattern() -> re.Pattern:
    names = sorted(REGISTRY, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\s*\([^)]{0,400}\)")


_TOOL_CALL_LEAK = _tool_call_leak_pattern()


def strip_tool_leaks(text: str) -> tuple[str, bool]:
    cleaned = _TOOL_CALL_LEAK.sub("", text or "")
    if cleaned == text:
        return text, False
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True


#: `**bold**`, `__bold__` and `#`/`##` headings -- Markdown the model reaches
#: for out of habit that makes a WhatsApp reply read like a generated
#: document instead of someone typing. Kept to the handful of forms that are
#: unambiguous to strip; a bare hyphen or asterisk is left alone; it could be
#: a real part of the sentence.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)


def strip_markdown(text: str) -> tuple[str, bool]:
    cleaned = _MD_BOLD.sub(lambda m: m.group(1) or m.group(2) or "", text or "")
    cleaned = _MD_HEADING.sub("", cleaned)
    if cleaned == text:
        return text, False
    return cleaned.strip(), True


def _sent_images(history: list[dict]) -> set[str]:
    """Every image already delivered earlier in this conversation.

    Read from the `attachments` the agent stamped onto its own past replies
    (see `messages.assistant`), not re-derived from tool results -- a tool can
    return more photos than the image policy actually sent, and only what was
    actually sent may never be re-sent uninvited.
    """
    seen: set[str] = set()
    for message in history:
        if message.get("role") == msg.ASSISTANT:
            seen.update(message.get("attachments") or [])
    return seen


@dataclass
class AgentReply:
    text: str
    attachments: list[str] = field(default_factory=list)
    #: Tool names called this turn, in order. Logged, and what the tests and
    #: the chat harness read.
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None


def run_turn(
    db: Session,
    channel: str,
    external_id: str,
    text: str,
    *,
    provider: LLMProvider | None = None,
) -> AgentReply:
    provider = provider or get_provider()
    specs = tool_specs()
    system_prompt = build_system_prompt()

    history = session_store.load(db, channel, external_id)
    sent_images = _sent_images(history)
    history.append(msg.user(text))

    # `history` is the same list object the loop below appends to, so the
    # tool layer's duplicate-call cache and image de-dup both see this turn's
    # own calls as they happen, not only what was already saved.
    ctx = ToolContext(session=db, channel=channel, external_id=external_id, sent_images=sent_images, history=history)
    called: list[str] = []

    for _turn in range(settings.tool_loop_cap):
        try:
            reply = provider.generate(system_prompt, history, specs)
        except ProviderError as exc:
            # A rate limit is transient and worth telling the customer about.
            # An auth or configuration failure is a deployment problem that no
            # customer message will fix, so it gets a generic apology and a
            # loud log entry.
            log.error("provider %s failed (%s): %s", provider.name, exc.kind, exc)
            text_out = RATE_LIMITED if exc.kind == "rate_limit" else GENERIC_FAILURE
            if settings.chatbot_debug:
                text_out = f"{text_out}\n[debug] {exc.kind}: {exc}"
            session_store.save(db, channel, external_id, history)
            return AgentReply(text=text_out, error=exc.kind, tool_calls=called)
        except Exception as exc:
            # Anything the provider did not classify -- a bug in translation, a
            # library raising something unexpected. It must still be logged
            # with a traceback *and* still answer the customer: an exception
            # escaping here means the WhatsApp adapter sends nothing at all,
            # which is a silent failure from the customer's side even though
            # the server logged it.
            log.exception("unexpected failure in provider %s", provider.name)
            text_out = GENERIC_FAILURE
            if settings.chatbot_debug:
                text_out = f"{text_out}\n[debug] {type(exc).__name__}: {exc}"
            session_store.save(db, channel, external_id, history)
            return AgentReply(text=text_out, error="provider_crash", tool_calls=called)

        if not reply.tool_calls:
            if not reply.text:
                # Usually a token limit or a content filter, and invisible
                # otherwise.
                log.warning(
                    "empty model reply for %s/%s (finish_reason=%s)", channel, external_id, reply.finish_reason
                )
            text_out, path_leaked = strip_paths(reply.text)
            text_out, tool_leaked = strip_tool_leaks(text_out)
            text_out, _ = strip_markdown(text_out)
            if path_leaked or tool_leaked:
                # Worth logging: it means the prompt or the tool descriptions
                # have drifted, and it is the earliest signal of that. A tool
                # call written out as text instead of made for real is worse
                # -- the customer would have read raw function syntax.
                log.warning(
                    "stripped a %s from a reply to %s/%s",
                    "tool call" if tool_leaked else "file path",
                    channel,
                    external_id,
                )
            history.append(msg.assistant(text_out, signature=reply.signature, attachments=ctx.attachments))
            session_store.save(db, channel, external_id, history)
            return AgentReply(
                text=text_out or GENERIC_FAILURE, attachments=ctx.attachments, tool_calls=called
            )

        # The signatures ride along in the history and are handed straight
        # back to the provider on the next pass. Nothing here reads them --
        # the history is stored as JSON, so an opaque string survives the
        # database round-trip untouched, which is what makes a second tool
        # call in the same conversation work.
        history.append(msg.assistant(reply.text, reply.tool_calls, signature=reply.signature))

        results = []
        for call in reply.tool_calls:
            name = call.get("name", "")
            called.append(name)
            content = call_tool(ctx, name, call.get("arguments"))
            log.info("tool %s(%s) -> %s", name, call.get("arguments"), list(content)[:4])
            results.append(msg.tool_result(call.get("id", name), name, content))
        history.append(msg.tool_results(results))

    # A model stuck calling tools without ever replying must hit a ceiling and
    # return something graceful, not spin.
    log.warning("tool loop cap hit for %s/%s after %s calls", channel, external_id, len(called))
    history.append(msg.assistant(LOOP_EXHAUSTED, attachments=ctx.attachments))
    session_store.save(db, channel, external_id, history)
    return AgentReply(
        text=LOOP_EXHAUSTED, attachments=ctx.attachments, tool_calls=called, error="loop_cap"
    )
