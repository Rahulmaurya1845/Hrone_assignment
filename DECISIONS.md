# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, 409 on duplicates), `joined_on` and (`department`, `joined_on`) for headcount. `attendance_logs`: unique (`emp_code`, `date`) for one record per day, punch-in safety and the monthly and trend queries; (`date` desc, `emp_code`) for the list and leaderboard; (`emp_code`, `punch_in` desc) to find the record to punch out. I rejected an index on `status`: five values filter almost nothing and slow writes.

2. **Punch-in race.** Both requests pass validation and compute the same date, then call `insert_one`. The unique (`emp_code`, `date`) index lets MongoDB accept one. The other gets `DuplicateKeyError`, which I turn into 409.

3. **Ties.** `$rank` gives tied people the same rank. I apply `limit` after ranking, so everyone tied at a rank within the limit is returned, even if that is more than `limit` rows.

4. **Headcount.** The summary starts from `employees` (joined on or before month end), joins the month's logs with `$lookup`, and `$unwind` keeps employees with none. Distinct `emp_code`s are counted.

5. **100x data.** I would store daily department totals, updated on each punch or correction, instead of joining per employee, and use keyset paging instead of `skip`.