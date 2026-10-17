import { describe, expect, it } from "vitest";
import { formatFileSize, formatStorageBytes } from "./format";

describe("binary storage units", () => {
  it.each([
    [0, "0 B"], [1, "1 B"], [1023, "1,023 B"],
    [1024, "1 KiB"], [1024 ** 2, "1 MiB"], [1024 ** 3, "1 GiB"],
    [1024 ** 4, "1 TiB"], [2199023255552, "2 TiB"],
    [50 * 1024 ** 3, "50 GiB"], [335002, "327.2 KiB"],
    [1e12, "931.32 GiB"], [1024 ** 5, "1 PiB"],
  ])("formats %s bytes as %s", (bytes, expected) => {
    expect(formatStorageBytes(bytes)).toBe(expected);
  });
  it.each([null, undefined, NaN, Infinity, -1])("keeps unavailable capacity distinct from zero (%s)", value => {
    expect(formatStorageBytes(value)).toBe("Not measured");
  });
  it("uses binary labels for shared file sizes", () => {
    expect(formatFileSize(1024)).toBe("1 KiB");
    expect(formatFileSize(2199023255552)).toBe("2 TiB");
  });
});
