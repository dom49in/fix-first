-- Run in the Zetaris SQL Editor, one statement at a time. Replaces fixfirst.us_holidays so it keeps only NATIONWIDE holidays
-- (the first registration also kept state-level days: 68 rows instead of about 44, which would wrongly remove working days).
DROP VIEW fixfirst.us_holidays;

CREATE SCHEMASTORE VIEW us_holidays WITH CONTAINER fixfirst AS
SELECT `date` AS holiday_date, name AS holiday_name FROM fixfirst_ext.holidays_2023 WHERE CAST(`global` AS STRING) = 'true'
UNION ALL
SELECT `date` AS holiday_date, name AS holiday_name FROM fixfirst_ext.holidays_2024 WHERE CAST(`global` AS STRING) = 'true'
UNION ALL
SELECT `date` AS holiday_date, name AS holiday_name FROM fixfirst_ext.holidays_2025 WHERE CAST(`global` AS STRING) = 'true'
UNION ALL
SELECT `date` AS holiday_date, name AS holiday_name FROM fixfirst_ext.holidays_2026 WHERE CAST(`global` AS STRING) = 'true';

-- Check: expect about 11 per year, so roughly 44 rows, and as many distinct dates as rows
SELECT COUNT(1) AS n, COUNT(DISTINCT holiday_date) AS d FROM fixfirst.us_holidays;
-- And look at what was kept
SELECT holiday_date, holiday_name FROM fixfirst.us_holidays ORDER BY holiday_date;
