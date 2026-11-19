// Number formatting that matches the Python console byte for byte.
import { escapeHtml } from "./escape";

// fixed4 is Python's "%.4f": the exact binary value of v, rounded half to even
// at the fourth decimal. Number.prototype.toFixed is not used because it can
// differ from Python on exact ties, so the double is expanded with BigInt.
export function fixed4(v: number): string {
  if (Number.isNaN(v)) return "nan";
  if (!Number.isFinite(v)) return v > 0 ? "inf" : "-inf";
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, v);
  const bits = view.getBigUint64(0);
  const negative = bits >> 63n === 1n;
  const expBits = Number((bits >> 52n) & 0x7ffn);
  let mant = bits & 0xfffffffffffffn;
  let exp: number;
  if (expBits === 0) {
    exp = -1074;
  } else {
    mant |= 1n << 52n;
    exp = expBits - 1075;
  }
  // |v| = mant * 2^exp; scaled = |v| * 10^4, rounded half to even.
  let scaled: bigint;
  const num = mant * 10000n;
  if (exp >= 0) {
    scaled = num << BigInt(exp);
  } else {
    const den = 1n << BigInt(-exp);
    const q = num / den;
    const twice = (num % den) * 2n;
    scaled = twice > den || (twice === den && q % 2n === 1n) ? q + 1n : q;
  }
  const digits = scaled.toString().padStart(5, "0");
  const text = digits.slice(0, -4) + "." + digits.slice(-4);
  return negative ? "-" + text : text;
}

export function num4(v: number | null | undefined): string {
  return v === null || v === undefined ? "n/a" : fixed4(v);
}

// usd is Python's _usd: "%.4f" with trailing zeros cut down to two decimals.
export function usd(v: number): string {
  let text = fixed4(v);
  while (text.endsWith("0") && (text.split(".")[1] ?? "").length > 2) {
    text = text.slice(0, -1);
  }
  return "$" + text;
}

// pyStr is Python's str() for the JSON values a metrics file can hold.
export function pyStr(v: unknown): string {
  if (v === null || v === undefined) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  return String(v);
}

export interface Proportion {
  value?: number | null;
  n?: unknown;
  ci?: [number | null, number | null] | null;
  directional?: unknown;
}

// rate is Python's _rate. It returns HTML: every value is escaped.
export function rate(p: Proportion): string {
  const ci = p.ci ?? null;
  const interval = ci === null ? "no interval" : `${num4(ci[0])} to ${num4(ci[1])}`;
  const badge = p.directional === true ? ' <span class="badge">directional</span>' : "";
  return `${escapeHtml(num4(p.value))} &#183; n=${escapeHtml(pyStr(p.n))} &#183; ${escapeHtml(interval)}${badge}`;
}
