# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `health()` | Always returns 200, never checks MongoDB | Stop MongoDB, /health still 200 (contract: 503) | Ping MongoDB, return 503 on failure |
| 2 | `punch_in` | Unknown employee: `find_one` returns None and is used anyway | POST punch-in with EMP9999 gives 500, not 404 | Check for None, raise 404 |
| 3 | `punch_in` | Check-then-insert race, no unique index | Parallel punch-ins give two 201s | Unique index on (emp_code, date), catch DuplicateKeyError and return 409 |
| 4 | `punch_in` | Response leaks MongoDB `_id` as "id" | Response contains "id": "6ac9d6a9..." | Removed; response built by `record_out` |
| 5 | `punch_in` | punch_in returned as naive datetime string with microseconds, not epoch ms truncated to seconds | Response shows "2026-10-10T11:39:45.085329" | UTC-aware datetimes, truncated to seconds, epoch ms at the boundary |
| 6 | `punch_in` | Uses the server's local timezone for date and lateness, ignores the overnight rule | Run on a UTC server near midnight IST, wrong date | Explicit IST conversion and R1 in `attendance_date` |
| 7 | `PunchInIn.status` | Free string, any value accepted | status "BANANA" is stored | Literal of PRESENT, WFH, ON_DUTY (422 otherwise); epoch range validated too |
| 8 | `compute_late_minutes` | Floors to minutes before comparing, so 09:40:30 counts as not late; uses the punch-in's own date, wrong for overnight shifts | 09:40:30 on a 09:30 shift returns 0 instead of 10 | Compare seconds > 600, then floor; use the attendance date's shift start |
| 9 | `compute_work_hours` | Python `round()` is not half-up; microseconds not truncated | 9.125 hours rounds to 9.12 instead of 9.13 | Whole seconds, Decimal with ROUND_HALF_UP |
| 10 | `compute_overtime` | No 30-minute minimum; end always on the punch date, wrong for overnight shifts | Punch-out 5 minutes after shift end gives 5, not 0 | Added the minimum; shift end via `shift_bounds` (+1 day if overnight) |
| 11 | `create_employee` | Check-then-insert race, no unique index; `created_at` naive local time and returned as datetime string | Parallel POSTs with the same emp_code give two 201s | Unique index, catch DuplicateKeyError for 409; UTC and epoch ms |
| 12 | `EmployeeIn` | No validation (emp_code format, email, dates, shift_start == shift_end) | `joined_on: "banana"` returns 201 | Patterns, lengths and validators, 422 on failure |
| 13 | `list_employees` | skip is `page * page_size` (first page skipped), `total` ignores the filter, no sort, no page_size limit | Page 1 returns the second page; total same with or without department | Fixed skip, count with filter, sort by emp_code, validated paging |
| 14 | `list_attendance` | Loads every matching document, sorts in Python, no index; sort has no emp_code tiebreaker | Slow and memory heavy at 100k records | Sort, skip and limit in MongoDB with index (date desc, emp_code asc) |
| 15 | `list_attendance` | No validation of dates, status, or paging | `?date_from=banana` or `?page=0` returns 200 | Validated dates and status, `Query(ge=1, le=100)` |
| 16 | `list_attendance` | Returns `id` and raw datetimes; missing late_minutes / half_day / history on legacy records not defaulted | Items contain "id" and ISO datetime strings | `record_out` strips `_id`, converts to ms, fills defaults |

## Looked at and not a defect
- The `IST` constant (UTC+05:30) is correct; India has no daylight saving.
- Default shift times of 09:30 and 18:30 match the contract.
- Returning 201 for punch-in and employee creation is correct.