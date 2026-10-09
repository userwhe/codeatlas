import { describe, expect, it } from "vitest";

import { formatCount } from "./format";

describe("formatCount", () => {
  it("uses the singular for one item", () => {
    expect(formatCount(1)).toBe("1 item");
  });

  it("uses the plural for several items", () => {
    expect(formatCount(3)).toBe("3 items");
  });
});
