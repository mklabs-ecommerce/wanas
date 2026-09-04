# WhatsApp message templates

The copy to submit in Meta Business Manager, and why it reads the way it does.

Meta refuses free-form, business-initiated text more than 24 hours after the
customer's last message. Inside that window every automatic message goes as
itself; outside it, only a pre-approved template can reopen the conversation.
With no template approved the message is **not sent at all** -- the line is
written into the transcript marked undelivered and a staff alert says to phone
the customer. See `docs/OPERATIONS.md` and
`domain/services/notifications.py::record_status_push`.

## Two rules that shape every line below

**No variables.** `WhatsAppClient.send_template` posts
`{"name": ..., "language": {"code": "ar"}}` with no `components`. A template
carrying `{{1}}` passes Meta's review and then fails at *send* time, because
the parameters it declares never arrive. So the approved copy has to stand on
its own with no order number, no product name and no total in it.

**So the template is a door, not the message.** One approved name covers all
four order statuses, and it cannot say which one happened. Its job is to get
the customer to reply -- a reply reopens the 24-hour window, and from that
moment the bot answers with the real detail, which is already sitting in the
transcript. Every line below therefore ends by asking for a reply. Writing
them as though they were the announcement is the mistake to avoid: a customer
told "your order shipped" with no tracking and no way to ask is worse served
than one invited into a conversation.

Quick-reply buttons are safe to add and are the better door -- one tap instead
of typing. `assistant/channels/whatsapp.py` already ingests a button press
(`message_type == "button"`) as an ordinary inbound message, so a tap opens
the window and starts a turn exactly as typing would.

## The five templates

Names are lowercase with underscores, language `ar`. Submit each as **Custom**
in the category given; Meta may recategorise, which changes what it costs, not
whether it works.

---

### 1. `wanas_order_update` -> `WHATSAPP_TEMPLATE_ORDER_UPDATE`

**Category: Utility.** Covers packed, shipped, delivered and cancelled -- one
name for all four, which is exactly why it names none of them.

```
في تحديث على طلبك من وناس ✅

رد على الرسالة دي وهنقولك الطلب وصل لفين وكل التفاصيل على طول.
```

Optional quick-reply button: `طلبي وصل فين؟`

---

### 2. `wanas_feedback_request` -> `WHATSAPP_TEMPLATE_FEEDBACK_REQUEST`

**Category: Utility.** Sent after a parcel is marked delivered.

```
طلبك من وناس وصلك ✅

تقيّم تجربتك معانا من 1 لـ 5؟ ولو عندك أي ملاحظة اكتبها في ردك — بتفرق معانا فعلاً.
```

---

### 3. `wanas_order_confirmation` -> `WHATSAPP_TEMPLATE_ORDER_CONFIRMATION`

**Category: Utility.** The safety net, not the normal path: a confirmation
fires seconds after the customer confirms, so the window is almost always
open and the full itemised text goes as itself. This is what gets sent if
that send is ever refused.

```
وصلنا طلبك واتأكد ✅

رد على الرسالة دي وهنبعتلك تفاصيل الطلب والإجمالي وموعد الوصول.
```

---

### 4. `wanas_back_in_stock` -> `WHATSAPP_TEMPLATE_BACK_IN_STOCK`

**Category: Marketing.** A waitlisted item is available again. It cannot name
the product, so it leans on the fact that the customer asked to be told.

```
خبر حلو 🖤

القطعة اللي كنت مستنيها رجعت متوفرة تاني في وناس.

رد على الرسالة دي وهنظبطلك المقاس واللون قبل ما تخلص تاني.
```

Optional quick-reply button: `عايز أطلبها`

---

### 5. `wanas_abandoned_cart` -> `WHATSAPP_TEMPLATE_ABANDONED_CART`

**Category: Marketing.** This one is sent *by definition* to somebody who went
quiet, so it is the template that matters most -- without it the nudge
essentially never reaches anybody.

```
لسه طلبك مستنيك في السلة 🛒

رد على الرسالة دي ونكمّل الأوردر في دقيقة، ولو محتاج مساعدة في المقاس أو اللون احنا معاك.
```

Optional quick-reply button: `كمّل طلبي`

## After approval

Set the five approved names, then redeploy:

```
WHATSAPP_TEMPLATE_ORDER_UPDATE=wanas_order_update
WHATSAPP_TEMPLATE_FEEDBACK_REQUEST=wanas_feedback_request
WHATSAPP_TEMPLATE_ORDER_CONFIRMATION=wanas_order_confirmation
WHATSAPP_TEMPLATE_BACK_IN_STOCK=wanas_back_in_stock
WHATSAPP_TEMPLATE_ABANDONED_CART=wanas_abandoned_cart
WHATSAPP_TEMPLATE_LANGUAGE=ar
```

A name set here that Meta has *not* approved is worse than a blank one: the
send is attempted, refused, and the customer gets nothing -- where a blank
name at least raises the staff alert without spending the attempt. Set each
one only once its template shows **Approved**.

## If you want the status in the message itself

The limit is the client, not Meta. Naming the status would need one approved
template per status (`wanas_order_shipped`, `wanas_order_delivered`, ...) or
one template with body variables plus `components` support in
`WhatsAppClient.send_template` -- the docstring there says as much. Worth
doing once the templates above are approved and the door is open at all;
not worth blocking on now.
