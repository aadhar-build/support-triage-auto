# Labelling guide v1 (DRAFT for Aadhar's approval, 18 Nov 2026)

One conversation gets three labels: **intent**, **action** and **should escalate**. Label from the customer's turns and the thread so far, not from what the brand later did. When two labels fit, the rules below say which wins.

## Action: what support should do next

| Action | Use when | Example (made up) |
| --- | --- | --- |
| **answer** | A public reply can fully help, with no more detail from the customer and no specialist. Praise, general questions, known outages with a status to share. | "Love the new app update!" · "Is the store open on Sunday?" |
| **ask** | We need details before we can help (order number, location, device), usually by moving to DM. | "My parcel hasn't arrived." · "I was charged twice." |
| **route** | A specialist team has to act: account access, technical faults, a specific department. | "Can't log in, the code never arrives." · "App crashes on launch since the update." |
| **escalate** | A person must look now, whatever the intent. Any case on the escalate list in `policies/rules.json`. | "I've been hacked and there are charges I didn't make." · "My lawyer will hear about this." |

Order of precedence: **escalate** beats everything; then **route** for account access and technical faults; then **ask**; **answer** only when nothing more is needed.

## Should escalate

True exactly when the action is **escalate**. It's a separate column so the eval can count missed and false escalations.

## Intent: what the customer wants (frozen 18 Nov: 7 plus other)

| Intent | Covers | Not this |
| --- | --- | --- |
| delivery | Orders, parcels, shipping, tracking, missed or late delivery | A refund for a lost parcel is still delivery; the refund demand makes the action escalate |
| billing | Charges, refunds, invoices, payments, prices, subscriptions | Fraud on a hacked account is account_access |
| account_access | Log-in, passwords, 2FA codes, locked or hacked accounts | A broken app with a working log-in is outage_technical |
| outage_technical | Service down, app or site errors, crashes, connection problems | |
| complaint | Unhappy with service or staff, with no more specific intent | A late parcel with angry words is delivery |
| praise | Thanks, compliments | Sarcastic "thanks" is a complaint or the specific intent |
| other | Anything else: questions, store hours, spam, non-support chatter | |

## Edge cases

- **Sarcasm:** label what the customer means. "Great, third missed delivery, love it" is delivery, action ask.
- **Several issues:** label the one the customer most wants fixed, usually the last unresolved one.
- **Missing context:** if the last turn only makes sense with earlier turns, use them.
- **Not English:** label the intent if you can tell; action **route** (to a team that speaks it), unless it's on the escalate list.
- **Card number or password posted in public:** escalate, so a person can get it hidden. (Proposed; confirm.)
