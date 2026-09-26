// Pure date validation shared by the References_View edit form (task 12.2).
//
// `isIsoCalendarDate` is the client-side mirror of the backend's ISO-date
// predicate (Property 19 / Req 5.8). It accepts a string iff it is a valid
// `YYYY-MM-DD` calendar date: the shape must be exactly four digits, a dash,
// two digits, a dash, two digits, AND the day must be a real day for that
// month and year (so `2023-02-30`, `2023-13-01`, `2023-00-10`, `2023-01-32`
// are all rejected, and leap years are respected).

const ISO_DATE_SHAPE = /^(\d{4})-(\d{2})-(\d{2})$/;

// A year is a leap year under the Gregorian calendar iff it is divisible by 4,
// except centuries which must be divisible by 400.
function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

function daysInMonth(year: number, month: number): number {
  // month is 1-12 here.
  switch (month) {
    case 1:
    case 3:
    case 5:
    case 7:
    case 8:
    case 10:
    case 12:
      return 31;
    case 4:
    case 6:
    case 9:
    case 11:
      return 30;
    case 2:
      return isLeapYear(year) ? 29 : 28;
    default:
      return 0;
  }
}

// Returns true iff `s` is a valid YYYY-MM-DD calendar date.
export function isIsoCalendarDate(s: string): boolean {
  const m = ISO_DATE_SHAPE.exec(s);
  if (!m) return false;
  const year = Number(m[1]);
  const month = Number(m[2]);
  const day = Number(m[3]);
  if (month < 1 || month > 12) return false;
  const maxDay = daysInMonth(year, month);
  if (day < 1 || day > maxDay) return false;
  return true;
}
