import { describe, it, expect } from "vitest";
import { isIsoCalendarDate } from "./dateValidation";

// Property 19: the date validator accepts exactly ISO calendar dates.
// Validates: Requirements 5.8
//
// Table-driven, near-exhaustive cases covering: valid dates, wrong shapes,
// impossible dates, and the leap-year Feb 29 boundary.

describe("isIsoCalendarDate — valid calendar dates accepted", () => {
  const valid = [
    "2023-01-01",
    "2023-12-31",
    "2000-02-29", // 2000 is a leap year (divisible by 400)
    "2020-02-29", // ordinary leap year
    "1999-11-30",
    "2023-04-30",
    "2023-06-15",
    "0001-01-01",
    "9999-12-31",
    "2023-02-28",
    "2023-08-31",
  ];
  for (const s of valid) {
    it(`accepts ${s}`, () => {
      expect(isIsoCalendarDate(s)).toBe(true);
    });
  }
});

describe("isIsoCalendarDate — wrong shapes rejected", () => {
  const wrongShape = [
    "2023", // year only
    "2023-1-1", // unpadded month/day
    "2023/01/01", // wrong separator
    "abcd-ef-gh", // non-digits
    "", // empty
    "2023-01", // missing day
    "23-01-01", // two-digit year
    "2023-01-01T00:00:00", // datetime, extra content
    " 2023-01-01", // leading whitespace
    "2023-01-01 ", // trailing whitespace
    "2023-013-01", // three-digit month
    "2023-01-001", // three-digit day
    "20230101", // no separators
    "2023-01-1", // unpadded day only
  ];
  for (const s of wrongShape) {
    it(`rejects ${JSON.stringify(s)}`, () => {
      expect(isIsoCalendarDate(s)).toBe(false);
    });
  }
});

describe("isIsoCalendarDate — impossible dates rejected", () => {
  const impossible = [
    "2023-02-30", // Feb never has 30
    "2023-13-01", // month 13
    "2023-00-10", // month 0
    "2023-01-32", // day 32
    "2023-01-00", // day 0
    "2023-04-31", // April has 30
    "2023-06-31", // June has 30
    "2023-11-31", // November has 30
    "2023-12-32", // day 32
  ];
  for (const s of impossible) {
    it(`rejects ${s}`, () => {
      expect(isIsoCalendarDate(s)).toBe(false);
    });
  }
});

describe("isIsoCalendarDate — leap-year Feb 29 boundary", () => {
  it("accepts 2020-02-29 (leap year)", () => {
    expect(isIsoCalendarDate("2020-02-29")).toBe(true);
  });
  it("rejects 2021-02-29 (non-leap year)", () => {
    expect(isIsoCalendarDate("2021-02-29")).toBe(false);
  });
  it("accepts 2000-02-29 (divisible by 400)", () => {
    expect(isIsoCalendarDate("2000-02-29")).toBe(true);
  });
  it("rejects 1900-02-29 (century not divisible by 400)", () => {
    expect(isIsoCalendarDate("1900-02-29")).toBe(false);
  });
});
