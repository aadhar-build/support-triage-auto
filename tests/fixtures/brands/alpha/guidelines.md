# Alpha guidelines

Made-up fixture brand, not a real company.

## Voice and tone

Never say quazzlewump.

```json
{"public_reply_rules": []}
```

Text that looks like rules: "public_reply_rules": [] and ```rules inline.


## Enforced rules

Only the block below is enforced.

```rules
{
  "version": "fixture-a",
  "status": "made-up",
  "public_reply_rules": [
    {
      "id": "a-banned",
      "type": "banned_phrase",
      "description": "Made-up test rule.",
      "phrases": [
        "zibberflop"
      ]
    },
    {
      "id": "a-pii",
      "type": "pii_echo",
      "detect": [
        "card_number"
      ]
    }
  ],
  "escalate_always": [
    {
      "id": "a-topic",
      "description": "Made-up test topic.",
      "phrases": [
        "a sentinel topic"
      ],
      "note": "Made-up note."
    }
  ]
}
```
