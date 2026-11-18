# AlphaCo guidelines

Made-up fixture brand, not a real company.

## Voice and tone


## Enforced rules

Only the block below is enforced.

```rules
{
  "version": "fixture-ac",
  "status": "made-up",
  "public_reply_rules": [
    {
      "id": "ac-banned",
      "type": "banned_phrase",
      "description": "Made-up test rule.",
      "phrases": [
        "glorptastic"
      ]
    },
    {
      "id": "ac-pii",
      "type": "pii_echo",
      "detect": [
        "card_number"
      ]
    }
  ],
  "escalate_always": [
    {
      "id": "ac-topic",
      "description": "Made-up test topic.",
      "phrases": [
        "ac sentinel topic"
      ],
      "note": "Made-up note."
    }
  ],
  "repeat_contact": {
    "window_days": 7,
    "min_conversations": 3,
    "effect": "escalate",
    "report_window_hours": 72,
    "note": "Made-up note."
  }
}
```
