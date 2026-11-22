import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { render } from "./routes";

type Canned = Record<string, [number, unknown]>;

// fakeFetch answers only the canned API paths; anything else is a 404.
function fakeFetch(canned: Canned): typeof fetch {
  return (async (input: RequestInfo | URL) => {
    const hit = canned[String(input)];
    if (hit === undefined) return new Response("not found\n", { status: 404 });
    return new Response(JSON.stringify(hit[1]), { status: hit[0], headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

const cost = { estimated_usd: null, cost_per_1000_usd: null, prices_are_placeholders: true };
const prop = { value: 0.5, n: 4, ci: null, directional: true };
const goldMetrics = {
  escalation: { f2: prop, tpr: prop, tnr: prop, precision: prop, f1: prop, fnr: prop, fpr: prop },
  action: { accuracy: prop },
  intent: { accuracy: prop },
};

const api: Canned = {
  "/api/runs": [200, { runs: [
    { name: "syn", status: "ok", kind: "synthetic", created_at: "2026-10-03T00:00:00Z", cost, has_metrics: false },
    { name: "gold", status: "ok", kind: "gold_random", created_at: "2026-10-02T00:00:00Z", cost, has_metrics: true },
  ] }],
  "/api/runs/syn/metrics": [200, { name: "syn", status: "ok", metrics: null, reason: "no usable metrics file" }],
  "/api/runs/syn/threads": [200, { name: "syn", status: "ok", threads: [
    { thread_id: "9007199254740993", action: "escalate", reason_codes: ["escalate_always"] },
  ] }],
  "/api/runs/syn/threads/9007199254740993": [200, {
    name: "syn", thread_id: "9007199254740993", reason_codes: ["escalate_always"], topic_id: ["t-zorblax"],
    final: { intent: "complaint", urgency: "normal", action: "escalate", confidence: 0.7 },
    decided_by: { source: "escalate_always", model_id: null },
  }],
  "/api/runs/gold/metrics": [200, { name: "gold", status: "ok", metrics: goldMetrics }],
};

describe("render", () => {
  it("reaches the handoff card from Runs for a synthetic run with no metrics", async () => {
    const fetchFn = fakeFetch(api);
    expect(await render("/runs", fetchFn)).toContain('<a href="/runs/syn">syn</a>');
    const run = await render("/runs/syn", fetchFn);
    expect(run).toContain("<p>syn: metrics not available (no usable metrics file).</p>");
    expect(run).toContain('<a href="/runs/syn/threads">Threads</a>');
    const list = await render("/runs/syn/threads", fetchFn);
    expect(list).toContain('<a href="/runs/syn/threads/9007199254740993">9007199254740993</a>');
    const card = await render("/runs/syn/threads/9007199254740993", fetchFn);
    expect(card).toContain("<h2>Why it escalated</h2>");
    expect(card).toContain("<li>Matched an always-escalate topic: t-zorblax</li>");
    expect(card).toContain("<p>Decided by: Always-escalate policy</p>");
  });

  it("omits the Threads link when the threads API answers 404", async () => {
    const run = await render("/runs/gold", fakeFetch(api));
    expect(run).toContain("<h2>gold</h2>");
    expect(run).not.toContain("Threads");
  });

  it("shows the error page when the threads API fails otherwise", async () => {
    const fetchFn = fakeFetch({ ...api, "/api/runs/gold/threads": [500, "internal error"] });
    await expect(render("/runs/gold", fetchFn)).rejects.toBeInstanceOf(ApiError);
  });

  it("tells a missing run from a missing thread", async () => {
    const fetchFn = fakeFetch(api);
    expect(await render("/runs/nope", fetchFn)).toContain("<p>Run not found.</p>");
    expect(await render("/runs/nope/threads", fetchFn)).toContain("<p>Run not found.</p>");
    const thread = await render("/runs/syn/threads/42", fetchFn);
    expect(thread).toContain("<p>Thread not found.</p>");
    expect(thread).not.toContain("Run not found.");
  });
});
