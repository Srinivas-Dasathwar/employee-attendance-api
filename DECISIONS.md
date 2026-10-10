# DECISIONS.md

1. **Indexes.** On `employees` I created a unique index on `emp_code`, so duplicate employee codes are prevented, and `(department, joined_on)` for the headcount lookup. On `attendance_logs` I created a unique index on `(emp_code, date)`, so each employee has only one record per day, `(date DESC, emp_code)` to match the list sort order, `(status, date, emp_code)` for the status filter, and `(emp_code, punch_in)` so punch-out can find an employee's latest record. I rejected an index on `punch_out` because no query filters on it and it would only add write overhead.

2. **Punch-in race.** Both requests try to insert a record for the same employee and date. The unique index lets only one succeed, and the other gets a duplicate-key error, which I return as `409 Conflict`. I don't check first, because both requests could pass the check before either inserts.

3. **Ties.** The leaderboard ranks employees by total late minutes using `$rank`, so tied employees share a rank and the next rank is skipped (1, 2, 2, 4). The limit filters on rank, so a tie at the cutoff returns more rows than the limit.

4. **Headcount.** The department summary starts from the `employees` collection, not the logs. The unwind uses `preserveNullAndEmptyArrays: true`, so employees with no logs stay in and count toward headcount.

5. **100x data.** I would pre-aggregate monthly totals for the leaderboard and department summary, because right now they scan every log in the month on each request.