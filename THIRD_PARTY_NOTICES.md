# Third-party skills

Some skills in `.claude/skills/` are copied unchanged from two MIT-licensed repos. Their own text is kept as is so they can be updated by copying again; how this repo uses them, and where its rules override them, is in `docs/agents/skills.md`.

| Skills | Source | Commit | Licence |
| --- | --- | --- | --- |
| grilling, grill-me, to-spec, to-tickets, tdd, codebase-design, triage | [mattpocock/skills](https://github.com/mattpocock/skills) | `5863806` (12 Nov 2026) | MIT, Copyright (c) 2026 Matt Pocock |
| file-pr, babysit-pr, leaving-pr-comments, full-stop, full-send, takeover, modernize | [t3dotgg/tstack](https://github.com/t3dotgg/tstack) | `11e37a7` (11 Nov 2026) | MIT, Copyright (c) 2026 Theo Browne |

Not copied: Pocock's `code-review` (its two-axis method is folded into `reviewer-code` instead, so it doesn't shadow Claude Code's own `/code-review`), `implement-spec` (the factory orchestrator does that job), and tstack's `sol-loop` and `council` (they need models from other providers, which this factory doesn't run).

Both licences:

```
MIT License

Copyright (c) 2026 Matt Pocock
Copyright (c) 2026 Theo Browne

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
