<!-- # DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data. -->

# DECISIONS.md

1. **Indexes.** I created a unique index on emp_code in employees, so ___. On attendance_logs I created a unique index on (emp_code, date) so ___, plus (date desc, emp_code) so ___, and (status, date, emp_code) so ___. I also added (emp_code, punch_in) for ___ and (department, joined_on) for ___. I considered an index on ___ but rejected it because ___.

2. **Punch-in race.** Both requests ___. The unique index lets ___ succeed, and the other one gets ___ which I turn into ___. I don't check first because ___.

3. **Ties.** The leaderboard uses ___ to rank, so tied employees ___ and the next rank is ___. Because limit filters on rank, a tie at the cutoff returns ___ rows.

4. **Headcount.** The department summary starts from the ___ collection, not the logs. After the lookup, the unwind keeps employees with no logs because ___, so they count as ___.

5. **100x data.** I would change ___ because ___.