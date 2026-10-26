-- Register the two WEB-API data sources in Zetaris as REST tables. The agent never calls these websites itself:
-- it queries the views fixfirst.us_holidays and fixfirst.weather_daily through Zetaris, like every other table.
-- Run by  python scripts/load_to_zetaris.py --register-apis   or paste block by block into the Zetaris SQL Editor.
--
-- STATUS: written from Zetaris's REST-table pattern that worked for project 1 (Frankfurter exchange rates). These endpoints
-- have NOT been confirmed on a live Zetaris instance. If a step fails, the agent still runs: the pre-flight check marks the
-- table "degraded" and the answer says holidays / weather were not applied.
-- Both APIs are free and need no key: Nager.Date (public holidays) and Open-Meteo (historical weather).
-- Years 2023-2026 cover the historical sample (FY2023-24) and the active extract (FY2026).

-- 0. Logical database for external tables
CREATE LIGHTNING DATABASE fixfirst_ext DESCRIBE BY "fixfirst_external_web_api_tables";

-- 1. HOLIDAYS: each yearly call returns a top-level array of {date, localName, name, countryCode, global, ...}.
--    Only nationwide holidays (global = true) are kept: the API also lists state-level days that DC does not observe.
CREATE LIGHTNING REST TABLE holidays_2023 FROM fixfirst_ext REQUEST(
    endpoint "https://date.nager.at/api/v3/PublicHolidays/2023/US",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CACHE TABLE fixfirst_ext.holidays_2023;

CREATE LIGHTNING REST TABLE holidays_2024 FROM fixfirst_ext REQUEST(
    endpoint "https://date.nager.at/api/v3/PublicHolidays/2024/US",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CACHE TABLE fixfirst_ext.holidays_2024;

CREATE LIGHTNING REST TABLE holidays_2025 FROM fixfirst_ext REQUEST(
    endpoint "https://date.nager.at/api/v3/PublicHolidays/2025/US",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CACHE TABLE fixfirst_ext.holidays_2025;

CREATE LIGHTNING REST TABLE holidays_2026 FROM fixfirst_ext REQUEST(
    endpoint "https://date.nager.at/api/v3/PublicHolidays/2026/US",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CACHE TABLE fixfirst_ext.holidays_2026;

CREATE SCHEMASTORE VIEW us_holidays WITH CONTAINER fixfirst AS
SELECT `date` AS holiday_date, name AS holiday_name FROM fixfirst_ext.holidays_2023 WHERE CAST(`global` AS STRING) = 'true'
UNION ALL
SELECT `date` AS holiday_date, name AS holiday_name FROM fixfirst_ext.holidays_2024 WHERE CAST(`global` AS STRING) = 'true'
UNION ALL
SELECT `date` AS holiday_date, name AS holiday_name FROM fixfirst_ext.holidays_2025 WHERE CAST(`global` AS STRING) = 'true'
UNION ALL
SELECT `date` AS holiday_date, name AS holiday_name FROM fixfirst_ext.holidays_2026 WHERE CAST(`global` AS STRING) = 'true';

-- 2. WEATHER: Open-Meteo returns {"daily": {"time": [...], "precipitation_sum": [...], ...}} (parallel arrays),
--    so the view zips the arrays and explodes them into one row per day. Washington DC, 1 Jan 2023 - 10 Sep 2026.
CREATE LIGHTNING REST TABLE weather_raw FROM fixfirst_ext REQUEST(
    endpoint "https://archive-api.open-meteo.com/v1/archive?latitude=38.9072&longitude=-77.0369&start_date=2023-01-01&end_date=2026-09-10&daily=precipitation_sum,wind_speed_10m_max,temperature_2m_max,temperature_2m_min&timezone=America%2FNew_York",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
) BODY ();

CACHE TABLE fixfirst_ext.weather_raw;

CREATE SCHEMASTORE VIEW weather_daily WITH CONTAINER fixfirst AS
SELECT z.time AS weather_date,
       CAST(z.precipitation_sum AS DOUBLE) AS precip_mm,
       CAST(z.wind_speed_10m_max AS DOUBLE) AS wind_max_kmh,
       CAST(z.temperature_2m_max AS DOUBLE) AS temp_max_c,
       CAST(z.temperature_2m_min AS DOUBLE) AS temp_min_c
FROM fixfirst_ext.weather_raw
LATERAL VIEW explode(arrays_zip(daily.time, daily.precipitation_sum, daily.wind_speed_10m_max,
                                daily.temperature_2m_max, daily.temperature_2m_min)) t AS z;

-- If the weather view returns no rows or an error about field names, try the index form instead (replace the view):
-- CREATE SCHEMASTORE VIEW weather_daily WITH CONTAINER fixfirst AS
-- SELECT daily.time[i] AS weather_date, CAST(daily.precipitation_sum[i] AS DOUBLE) AS precip_mm,
--        CAST(daily.wind_speed_10m_max[i] AS DOUBLE) AS wind_max_kmh, CAST(daily.temperature_2m_max[i] AS DOUBLE) AS temp_max_c,
--        CAST(daily.temperature_2m_min[i] AS DOUBLE) AS temp_min_c
-- FROM fixfirst_ext.weather_raw LATERAL VIEW posexplode(daily.time) p AS i, d;

-- 3. Verify
SELECT COUNT(1) AS n FROM fixfirst.us_holidays;
SELECT COUNT(1) AS n FROM fixfirst.weather_daily;

-- Teardown (run in Zetaris, not by the app):
-- DROP VIEW fixfirst.us_holidays; DROP VIEW fixfirst.weather_daily;
