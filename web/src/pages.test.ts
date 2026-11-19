import { describe, expect, it } from "vitest";
import type { Proportion } from "./format";
import { renderOverview, renderRunMetrics, renderRuns, renderNotFound } from "./pages";

const EVIL = `<img src=x onerror=alert(1)>&"'`;
const EVIL_ESC = "&lt;img src=x onerror=alert(1)&gt;&amp;&quot;&#39;";

const prop = (value: number | null, n: number): Proportion => ({ value, n, ci: [0.25, 0.75], directional: false });
const LABELS = [
  "Escalation F2", "Escalation TPR", "Escalation TNR", "Escalation precision", "Escalation F1",
  "Missed escalation rate", "False escalation rate", "Action accuracy", "Intent accuracy",
];
const headlines = LABELS.map((label, i) => ({ label, value: i / 10, n: 10, ci: null, directional: i === 0 }));

function overview(over: Record<string, unknown> = {}) {
  return {
    run_count: 2,
    ok_count: 2,
    latest: { name: "run-b", kind: "synthetic", created_at: "2026-10-03T00:00:00Z", headlines },
    spend: {
      this_run_usd: 0.35, month: "2026-10", month_total_usd: 0.35, month_runs: 2, month_no_cost: 0,
      monthly_cap_usd: 30, placeholder_prices: true,
    },
    ...over,
  };
}

const metrics = {
  escalation: { f2: prop(0.5, 4), tpr: prop(0.5, 4), tnr: prop(0.5, 4), precision: prop(0.5, 4),
    f1: prop(0.5, 4), fnr: prop(0.5, 4), fpr: prop(0.5, 4) },
  action: { accuracy: prop(0.03125, 8) },
  intent: { accuracy: prop(null, 0) },
};

describe("renderOverview", () => {
  it("empty states", () => {
    expect(renderOverview({ run_count: 0, ok_count: 0, latest: null, spend: null })).toContain("<p>No runs yet.</p>");
    const none = renderOverview({ run_count: 2, ok_count: 0, latest: null, spend: null });
    expect(none).toContain("<p>No run has a valid manifest.</p>");
    expect(none).not.toContain("No runs yet.");
  });

  it("latest run and the rates table", () => {
    const html = renderOverview(overview());
    expect(html).toContain("<li>name: run-b</li><li>kind: synthetic</li><li>created_at: 2026-10-03T00:00:00Z</li>");
    expect(html).toContain("<tr><th>Rate</th><th>Value</th></tr>");
    expect(html).toContain('<tr><td>Escalation F2</td><td>0.0000 &#183; n=10 &#183; no interval <span class="badge">directional</span></td></tr>');
    expect(html).toContain("<tr><td>Intent accuracy</td><td>0.8000 &#183; n=10 &#183; no interval</td></tr>");
    expect(html).not.toContain("No metrics for this run.");
  });

  it("no metrics for the latest run", () => {
    const html = renderOverview(overview({ latest: { name: "a", kind: "synthetic", created_at: "x", headlines: null } }));
    expect(html).toContain("<p>No metrics for this run.</p>");
    expect(html).not.toContain("<th>Rate</th>");
  });

  it("spend lines, plural", () => {
    const html = renderOverview(overview());
    expect(html).toContain("<li>This run: $0.35 estimated &#183; Per-run cap: $2.50 (#10)</li>");
    expect(html).toContain("<li>This month: $0.35 estimated across 2 runs (0 with no cost recorded) &#183; Monthly cap: $30.00</li>");
    expect(html).toContain('<p class="note">placeholder prices, not real spend; API-equivalent dollars</p>');
    expect(html).toContain('<p class="note">Estimated from run manifests; #10&#39;s spend log is not read yet.</p>');
  });

  it("spend lines, one run, no cost, no cap, no placeholder note", () => {
    const html = renderOverview(overview({
      spend: { this_run_usd: null, month: "2026-10", month_total_usd: 0, month_runs: 1, month_no_cost: 1,
        monthly_cap_usd: null, placeholder_prices: false },
    }));
    expect(html).toContain("<li>No cost recorded for this run. &#183; Per-run cap: $2.50 (#10)</li>");
    expect(html).toContain("across 1 run (1 with no cost recorded) &#183; Monthly cap: not set</li>");
    expect(html).not.toContain("placeholder prices");
    expect(html).toContain("spend log is not read yet.");
  });
});

describe("renderRuns", () => {
  it("empty", () => {
    expect(renderRuns({ runs: [] })).toContain("<p>No runs yet.</p>");
  });

  it("linked, unlinked and non-ok rows", () => {
    const html = renderRuns({ runs: [
      { name: "run b", status: "ok", kind: "synthetic", created_at: "2026-10-03T00:00:00Z",
        cost: { estimated_usd: 2.5, cost_per_1000_usd: null, prices_are_placeholders: true }, has_metrics: true },
      { name: "run-c", status: "ok", kind: "gold_random", created_at: "2026-10-02T00:00:00Z",
        cost: { estimated_usd: null, cost_per_1000_usd: null, prices_are_placeholders: false }, has_metrics: false },
      { name: "run-d", status: "no manifest" },
    ] });
    expect(html).toContain("<tr><th>Run</th><th>Kind</th><th>created_at</th><th>Estimated cost</th><th>Has metrics</th></tr>");
    expect(html).toContain('<tr><td><a href="/runs/run%20b">run b</a></td><td>synthetic</td><td>2026-10-03T00:00:00Z</td><td>$2.50 estimated</td><td>yes</td></tr>');
    expect(html).toContain("<tr><td>run-c</td><td>gold_random</td><td>2026-10-02T00:00:00Z</td><td>no cost recorded</td><td>no (no usable metrics file)</td></tr>");
    expect(html).toContain('<tr><td>run-d</td><td colspan="4">no manifest</td></tr>');
    expect(html).not.toContain('href="/runs/run-c"');
  });
});

describe("renderRunMetrics", () => {
  it("nine headline rows", () => {
    const html = renderRunMetrics({ name: "run-b", status: "ok", metrics });
    expect(html).toContain("<h2>run-b</h2>");
    for (const label of LABELS) expect(html).toContain(`<tr><td>${label}</td>`);
    expect(html).toContain("<tr><td>Action accuracy</td><td>0.0312 &#183; n=8 &#183; 0.2500 to 0.7500</td></tr>");
    expect(html).toContain("<tr><td>Intent accuracy</td><td>n/a &#183; n=0 &#183; 0.2500 to 0.7500</td></tr>");
  });

  it("null metrics", () => {
    const html = renderRunMetrics({ name: "run-c", status: "ok", metrics: null, reason: "no usable metrics file" });
    expect(html).toContain("<p>run-c: metrics not available (no usable metrics file).</p>");
  });

  it("404", () => {
    expect(renderNotFound()).toContain("<p>Run not found.</p>");
  });
});

describe("every page", () => {
  const evilRun = { name: EVIL, status: "ok", kind: "synthetic", created_at: "2026-10-03T00:00:00Z",
    cost: { estimated_usd: 1, cost_per_1000_usd: null, prices_are_placeholders: false }, has_metrics: true };
  const pages = [
    renderOverview(overview({ latest: { name: EVIL, kind: "synthetic", created_at: "x", headlines } })),
    renderRuns({ runs: [evilRun, { name: EVIL, status: "no manifest" }] }),
    renderRunMetrics({ name: EVIL, status: "ok", metrics }),
    renderRunMetrics({ name: EVIL, status: "ok", metrics: null }),
    renderOverview({ run_count: 0, ok_count: 0, latest: null, spend: null }),
    renderNotFound(),
  ];

  it("escapes a hostile run name", () => {
    for (const html of pages.slice(0, 4)) {
      expect(html).toContain(EVIL_ESC);
      expect(html).not.toContain("<img");
    }
    expect(pages[1]).toContain(`href="/runs/${encodeURIComponent(EVIL).replace(/'/g, "&#39;")}"`);
  });

  it("has only Overview and Runs in the nav and no absolute URL", () => {
    for (const html of pages) {
      expect(html).toContain('<nav><a href="/">Overview</a><a href="/runs">Runs</a></nav>');
      expect(html).not.toMatch(/https?:\/\//);
    }
  });
});
