# DECISIONS.md

1. **Indexes.** Employees have unique `emp_code`, plus `department` and `joined_on` indexes for filtered department and
   headcount queries. Attendance has unique `(emp_code, date)` for one-record-per-day safety, `(emp_code, punch_in)`
   for punch-out selection, `(date, emp_code)` for ordered listing, plus status/date indexes for filtering. Startup
   creates them idempotently.

2. **Punch-in race.** Both requests insert directly. MongoDB's unique `(emp_code, date)` index permits one; PyMongo raises
   `DuplicateKeyError` for the other, and the API returns 409.

3. **Leaderboard ties.** `$rank` gives ties the same competition rank. Filtering after ranking keeps all rows tied at
   the limit; final order is late minutes descending, then employee code ascending.

4. **Headcount.** The department pipeline starts with employees hired by month-end and looks up logs. It preserves
   employees with no matching logs and excludes later joiners.

5. **100x data.** I would measure workload and explain plans first, then consider sharding by employee and maintaining
   materialized daily/monthly summaries. Raw logs remain the audit source; aggregate consistency must be checked before
   analytics reads switch to summaries.
