import { describe, expect, it } from "vitest";
import { fixed4, num4, rate, usd } from "./format";

describe("fixed4 matches Python %.4f", () => {
  it.each([
    [0.03125, "0.0312"],
    [0.09375, "0.0938"],
    [0.00375, "0.0037"],
    [0.6640828206858921, "0.6641"],
    [1, "1.0000"],
    [0, "0.0000"],
    [-0.03125, "-0.0312"],
    [12345.5, "12345.5000"],
  ])("fixed4(%s) === %s", (v, want) => {
    expect(fixed4(v)).toBe(want);
  });
});

describe("num4", () => {
  it("null is n/a", () => expect(num4(null)).toBe("n/a"));
  it("number is fixed4", () => expect(num4(0.09375)).toBe("0.0938"));
});

describe("usd", () => {
  it.each([
    [2.5, "$2.50"],
    [0.35, "$0.35"],
    [123, "$123.00"],
    [0.0123, "$0.0123"],
    [0, "$0.00"],
    [0.001, "$0.001"],
  ])("usd(%s) === %s", (v, want) => {
    expect(usd(v)).toBe(want);
  });
});

describe("rate", () => {
  it("ci null gives no interval", () => {
    expect(rate({ value: 0.5, n: 4, ci: null })).toBe("0.5000 &#183; n=4 &#183; no interval");
  });
  it("ci gives an interval", () => {
    expect(rate({ value: 0.5, n: 4, ci: [0.03125, 1] })).toBe(
      "0.5000 &#183; n=4 &#183; 0.0312 to 1.0000",
    );
  });
  it("directional true gives the badge", () => {
    expect(rate({ value: null, n: 2, ci: null, directional: true })).toBe(
      'n/a &#183; n=2 &#183; no interval <span class="badge">directional</span>',
    );
  });
  it("directional must be exactly true", () => {
    expect(rate({ value: 0, n: 0, ci: null, directional: "yes" })).not.toContain("badge");
  });
});
