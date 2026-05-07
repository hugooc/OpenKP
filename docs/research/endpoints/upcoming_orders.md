# Upcoming orders endpoint

Source HARs:
- `docs/research/captures/kp-capture-noreply.har`, 2026-05-06 (homepage list-context call).
- `docs/research/captures/kp-capture-various-with-phi.har`, 2026-05-06 (drill-in call with `selectedOrderID`).

**Response bodies were stripped from both HARs by Chrome's HAR export.**
URLs, request bodies, and response sizes are intact. Future work needs a
fresh capture with bodies preserved.

## What this is

The "You have new instructions to review for your requested POTASSIUM" /
"View instructions" card on the MyChart homepage. Pending lab, imaging, or
procedure orders that the doctor has placed but the patient hasn't completed
yet — with patient prep instructions attached.

This is a **new data class** OpenKP doesn't yet expose. It pairs with
`list_lab_results` / `read_lab_result` (results you've already received) but
sits at the opposite end of the lifecycle: orders the doctor placed that are
awaiting your action.

Per the homepage UI ("View all (5)"), Hugo has 5 such pending orders today.

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| Get one upcoming order's details/instructions | `POST /mychartcn/api/upcoming-orders/GetUpcomingOrders` body `{selectedOrderID, PageNonce}` | 🔴 Mapped, body stripped. ~11.2 KB response. |
| List of upcoming orders (badge count + summary) | unknown — likely embedded in `MixedItemFeed` | 🔴 Not yet captured. |

Page route: `/mychartcn/app/upcoming-orders?ordid=<encrypted-orderID>`.

## `POST /mychartcn/api/upcoming-orders/GetUpcomingOrders`

This is a **per-order detail call**, not a list call. The body carries one
`selectedOrderID`, so we need a separate listing source to enumerate orders.

**Request body:**

```json
{
  "selectedOrderID": "WP-24...",
  "PageNonce": "<32-char hex>"
}
```

The `selectedOrderID` is a long URL-encoded encrypted Epic handle (`WP-24...`
prefix followed by ~150 chars of percent-encoded base64-ish payload) — the
same opaque ID that appears in the page route's `?ordid=` parameter.

**Response:** ~11.2 KB JSON. Shape unknown (body stripped). Likely contains
order type, ordering provider, instructions HTML, prep requirements, expected
location, expiration date.

**Headers:** standard `/mychartcn/` family — CSRF token + PageNonce body
field, same contract as the messages endpoints. See `messages.md` "Auth /
anti-forgery" for the two-token dance.

## Listing surface — open question

The homepage shows 5 pending orders without ever calling `GetUpcomingOrders`
five times, so the list must be embedded in one of:

- `MixedItemFeed` — the homepage feed assembly endpoint (likely returns
  rendered HTML or a feed-item-shaped JSON of mixed cards).
- A dedicated `GetUpcomingOrdersList` we haven't captured.

The "View all (5)" link probably leads to a dedicated list view. Capture
that page click in a future HAR.

## Tool candidates

- `list_upcoming_orders()` — pending labs/imaging/procedures with summary.
  Pairs with `list_lab_results` for full lifecycle visibility.
- `read_upcoming_order_instructions(order_id)` — fetch the patient prep
  instructions (potential PDF download via the same `LoadReportContent`
  infrastructure used by visit notes / AVS).

Both useful. Strong candidates for the next read tool after we have bodies
mapped. Higher value than `reply_to_message` because new data class > yet
another action surface, and the underlying instructions are exactly the kind
of thing patients lose track of (Hugo's POTASSIUM card has been sitting
unread).

## Capture next

Re-open `/mychartcn/app/upcoming-orders` in DevTools with **Preserve log on**
and **response bodies enabled** (Network tab → settings → uncheck "limit
captured data"). Click "View all (5)" to land on the list view, then click
into one order. We need:

1. The list endpoint and its response body.
2. The `GetUpcomingOrders` detail body for one order.
3. Whether instructions are a separate `LoadReportContent` call or embedded
   in the detail response.
