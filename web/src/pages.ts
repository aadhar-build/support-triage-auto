// Pure renderers: JSON from the Go API in, HTML strings out. Every interpolated
// value goes through escapeHtml; the text copies src/triage/admin.py.
import { escapeHtml as e } from "./escape";
import { pyStr, rate, usd, type Proportion } from "./format";

const PER_RUN_CAP = "Per-run cap: $2.50 (#10)";
const PLACEHOLDER_NOTE = "placeholder prices, not real spend; API-equivalent dollars";
const SPEND_SOURCE = "Estimated from run manifests; #10's spend log is not read yet.";
const NO_METRICS = "no usable metrics file";
const HEADLINES: [string, string, string][] = [
  ["escalation", "f2", "Escalation F2"],
  ["escalation", "tpr", "Escalation TPR"],
  ["escalation", "tnr", "Escalation TNR"],
  ["escalation", "precision", "Escalation precision"],
  ["escalation", "f1", "Escalation F1"],
  ["escalation", "fnr", "Missed escalation rate"],
  ["escalation", "fpr", "False escalation rate"],
  ["action", "accuracy", "Action accuracy"],
  ["intent", "accuracy", "Intent accuracy"],
];

export interface Headline extends Proportion {
  label: string;
}

export interface OverviewData {
  run_count: number;
  ok_count: number;
  latest: null | { name: string; kind: string; created_at: string; headlines: Headline[] | null };
  spend: null | {
    this_run_usd: number | null;
    month: string;
    month_total_usd: number;
    month_runs: number;
    month_no_cost: number;
    monthly_cap_usd: number | null;
    placeholder_prices: boolean;
  };
}

export interface RunRow {
  name: string;
  status: string;
  kind?: string;
  created_at?: string;
  cost?: { estimated_usd: number | null; cost_per_1000_usd: number | null; prices_are_placeholders: boolean };
  has_metrics?: boolean;
}

export interface RunMetricsData {
  name: string;
  status: string;
  metrics: Record<string, Record<string, Proportion>> | null;
  reason?: string;
}

function page(title: string, body: string): string {
  const nav = '<nav><a href="/">Overview</a><a href="/runs">Runs</a></nav>';
  return `${nav}<h1>${e(title)}</h1>${body}`;
}

function table(headers: string[], rows: string[]): string {
  const head = headers.map((h) => `<th>${e(h)}</th>`).join("");
  return `<table><tr>${head}</tr>${rows.join("")}</table>`;
}

function rateRows(rows: Headline[]): string {
  return table(
    ["Rate", "Value"],
    rows.map((h) => `<tr><td>${e(h.label)}</td><td>${rate(h)}</td></tr>`),
  );
}

export function renderOverview(data: OverviewData): string {
  const { latest, spend } = data;
  if (latest === null || spend === null) {
    const body = data.run_count === 0 ? "<p>No runs yet.</p>" : "<p>No run has a valid manifest.</p>";
    return page("Overview", body);
  }
  const parts = [
    `<section><h2>Latest run</h2><ul><li>name: ${e(latest.name)}</li><li>kind: ${e(latest.kind)}</li>` +
      `<li>created_at: ${e(latest.created_at)}</li></ul>`,
    latest.headlines === null ? "<p>No metrics for this run.</p>" : rateRows(latest.headlines),
    "</section>",
  ];
  const thisRun = spend.this_run_usd === null
    ? "No cost recorded for this run."
    : `This run: ${usd(spend.this_run_usd)} estimated`;
  const k = spend.month_runs;
  const monthLine = `This month: ${usd(spend.month_total_usd)} estimated across ${k} run${k === 1 ? "" : "s"}` +
    ` (${spend.month_no_cost} with no cost recorded)`;
  const capLine = spend.monthly_cap_usd === null ? "Monthly cap: not set" : `Monthly cap: ${usd(spend.monthly_cap_usd)}`;
  parts.push(
    "<section><h2>Spend</h2><ul>",
    `<li>${e(thisRun)} &#183; ${e(PER_RUN_CAP)}</li>`,
    `<li>${e(monthLine)} &#183; ${e(capLine)}</li></ul>`,
  );
  if (spend.placeholder_prices === true) parts.push(`<p class="note">${e(PLACEHOLDER_NOTE)}</p>`);
  parts.push(`<p class="note">${e(SPEND_SOURCE)}</p></section>`);
  return page("Overview", parts.join(""));
}

export function renderRuns(data: { runs: RunRow[] }): string {
  if (data.runs.length === 0) return page("Runs", "<p>No runs yet.</p>");
  const rows = data.runs.map((r) => {
    if (r.status !== "ok") {
      return `<tr><td>${e(r.name)}</td><td colspan="4">${e(r.status)}</td></tr>`;
    }
    const cost = r.cost?.estimated_usd ?? null;
    const costText = cost === null ? "no cost recorded" : `${usd(cost)} estimated`;
    const name = r.has_metrics === true
      ? `<a href="${e("/runs/" + encodeURIComponent(r.name))}">${e(r.name)}</a>`
      : e(r.name);
    const has = r.has_metrics === true ? "yes" : `no (${NO_METRICS})`;
    return `<tr><td>${name}</td><td>${e(pyStr(r.kind))}</td><td>${e(pyStr(r.created_at))}</td>` +
      `<td>${e(costText)}</td><td>${e(has)}</td></tr>`;
  });
  return page("Runs", table(["Run", "Kind", "created_at", "Estimated cost", "Has metrics"], rows));
}

export function renderRunMetrics(data: RunMetricsData): string {
  const m = data.metrics;
  if (m === null) {
    return page("Runs", `<p>${e(data.name)}: metrics not available (${e(NO_METRICS)}).</p>`);
  }
  const rows = HEADLINES.map(([block, key, label]) => ({ label, ...(m[block]?.[key] ?? {}) }));
  return page("Runs", `<section><h2>${e(data.name)}</h2>${rateRows(rows)}</section>`);
}

export function renderNotFound(): string {
  return page("Runs", "<p>Run not found.</p>");
}

export function renderError(): string {
  return page("Error", `<p class="error">${e("Could not load data from the console API.")}</p>`);
}
