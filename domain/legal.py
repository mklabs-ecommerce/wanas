"""The public privacy policy.

Meta requires a reachable Privacy Policy URL before a WhatsApp app can be
submitted for review, and the page has to be readable by someone who is not
logged in -- so this router is deliberately the one part of the app with no
credential, no signature check and no database access.

The text is kept here, next to the code, rather than in a CMS or a Notion page
someone can quietly edit: what it claims about where customer data goes is
only true as long as it matches `assistant/providers/openrouter.py`,
`assistant/providers/gemini.py`, `backend/integrations/shopify_client.py` and
`backend/models.py`. When one of those changes, this changes in the same
commit.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["legal"])

#: Shown on the page and sent to Meta. Bump it whenever the text below changes
#: in a way a customer would care about.
LAST_UPDATED = "23 August 2026"

CONTACT_EMAIL = "mklabsecommerce@gmail.com"

_STYLE = """
:root { color-scheme: light dark; --fg: #1a1a1a; --muted: #5c5c5c; --bg: #fff;
        --rule: #e4e4e4; --accent: #7a5cff; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #ececec; --muted: #a0a0a0; --bg: #141414; --rule: #2c2c2c;
          --accent: #9d86ff; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
             "Helvetica Neue", Arial, sans-serif;
       -webkit-text-size-adjust: 100%; }
main { max-width: 44rem; margin: 0 auto; padding: 3rem 1.25rem 5rem; }
h1 { font-size: 1.9rem; line-height: 1.2; margin: 0 0 .35rem; }
h2 { font-size: 1.12rem; margin: 2.4rem 0 .6rem; }
.updated { color: var(--muted); font-size: .875rem; margin: 0 0 2.2rem;
           padding-bottom: 1.4rem; border-bottom: 1px solid var(--rule); }
p, li { margin: 0 0 .8rem; }
ul { padding-left: 1.15rem; }
a { color: var(--accent); }
table { border-collapse: collapse; width: 100%; margin: .4rem 0 1rem;
        font-size: .94rem; display: block; overflow-x: auto; }
th, td { text-align: left; padding: .55rem .7rem; border-bottom: 1px solid var(--rule);
         vertical-align: top; }
th { font-weight: 600; white-space: nowrap; }
footer { margin-top: 3rem; padding-top: 1.4rem; border-top: 1px solid var(--rule);
         color: var(--muted); font-size: .875rem; }
"""

_BODY = f"""
<h1>Wanas Gallery — Privacy Policy</h1>
<p class="updated">Last updated {LAST_UPDATED}</p>

<p>Wanas Gallery is a streetwear brand based in Egypt. We sell through an
assistant on WhatsApp that answers questions about our products and takes
orders. This page explains what that assistant collects, who it shares data
with, and how to have your data removed.</p>

<h2>What we collect</h2>
<p>We only collect what reaches us through a conversation you start, or an
order you place:</p>
<ul>
  <li><strong>Your WhatsApp phone number and profile name</strong>, provided to
      us by WhatsApp when you message our business number.</li>
  <li><strong>The content of your messages</strong> — text, photos you send us,
      and voice notes.</li>
  <li><strong>Your conversation history</strong> with the assistant, so it can
      follow a conversation across several messages.</li>
  <li><strong>Order and delivery details</strong> you give us in order to buy
      something: name, delivery address, governorate, contact phone, optionally
      an email address, and the items in your order.</li>
  <li><strong>Feedback</strong> — a rating and any comment you leave about an
      order.</li>
</ul>
<p>We do <strong>not</strong> collect payment card details. Orders are cash on
delivery, so no card or bank information ever reaches our systems.</p>

<h2>Why we use it</h2>
<p>To answer your questions, recommend products, take and fulfil your order,
arrange delivery, handle returns and complaints, and pass a conversation to a
human colleague when the assistant cannot help. We do not sell your data, and
we do not use it for advertising.</p>

<h2>Who we share it with</h2>
<p>Your data is handled by a small number of service providers, each for one
purpose:</p>
<table>
  <tr><th>WhatsApp (Meta Platforms)</th>
      <td>Carries messages between you and us. Your use of WhatsApp is also
          governed by Meta's own privacy policy.</td></tr>
  <tr><th>OpenRouter</th>
      <td>Routes the AI model that generates the assistant's replies,
          transcribes your voice notes into text so the assistant can read
          what you said, and compares photos you send against our product
          catalogue. The relevant message content is sent through OpenRouter's
          service in order to produce these.</td></tr>
  <tr><th>Shopify</th>
      <td>Holds our product catalogue and our orders. When you place an order,
          your name, phone number, delivery address and items are stored there.</td></tr>
  <tr><th>Railway</th>
      <td>Hosts the assistant and its database.</td></tr>
</table>
<p>We may also disclose data where the law requires it. Because these providers
operate internationally, your data may be processed outside Egypt.</p>

<h2>How long we keep it</h2>
<p>Conversation history is kept only as long as it is useful for serving you and
is cleared periodically. Orders and the customer record attached to them are
kept for as long as we need them for accounting, returns and warranty purposes.
You can ask us to delete them sooner, as below.</p>

<h2>Your choices</h2>
<p>You can ask us to show you the data we hold about you, correct it, or delete
it. You can stop the assistant contacting you at any time by telling it to stop
or by blocking our number on WhatsApp.</p>
<p>To make any of these requests, email
<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a> from the address you gave
us, or include the WhatsApp number you messaged us from so we can find your
record. We will respond within 30 days. Some data may be kept where the law
requires us to, such as records of a completed sale.</p>

<h2>Security</h2>
<p>Messages reaching us from WhatsApp are verified as genuinely from Meta before
being processed, connections are encrypted in transit, and access to the
customer database is limited to staff who need it.</p>

<h2>Children</h2>
<p>Our store is not directed at children, and we do not knowingly collect data
from anyone under 18. If you believe a child has sent us information, email us
and we will remove it.</p>

<h2>Changes</h2>
<p>If we change this policy we will update the date at the top of this page.</p>

<h2>Contact</h2>
<p>Questions about this policy, or about your data:
<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.</p>

<footer>Wanas Gallery · Egypt</footer>
"""

PRIVACY_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy — Wanas Gallery</title>
<style>{_STYLE}</style>
</head>
<body><main>{_BODY}</main></body>
</html>"""


@router.get("/privacy", response_class=HTMLResponse)
def privacy() -> HTMLResponse:
    """Meta's required Privacy Policy URL. Public and unauthenticated on
    purpose -- a reviewer has to be able to read it while logged out."""
    return HTMLResponse(PRIVACY_HTML)
