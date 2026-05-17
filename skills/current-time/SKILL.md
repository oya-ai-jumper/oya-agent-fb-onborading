---
name: current-time
display_name: "Time & Date Utilities"
description: "Get current time, convert timezones, calculate date differences, and more"
category: general
icon: clock
skill_type: tool
catalog_type: core
tool_schema:
  name: time_utils
  description: "Comprehensive time and date utilities"
  parameters:
    type: object
    properties:
      action:
        type: string
        description: "Action to perform"
        enum: [current_time, convert_time, relative_time, get_timestamp, days_in_month, get_week_year, date_diff, date_add, is_business_day, list_timezones]
      timezone:
        type: string
        description: "IANA timezone (e.g., America/New_York, Asia/Tokyo). Default: UTC"
      source_timezone:
        type: string
        description: "Source timezone for convert_time"
      target_timezone:
        type: string
        description: "Target timezone for convert_time"
      time:
        type: string
        description: "ISO 8601 datetime string (e.g., 2026-03-19T14:30:00Z)"
      date:
        type: string
        description: "Date string in YYYY-MM-DD format"
      date2:
        type: string
        description: "Second date for date_diff"
      duration:
        type: string
        description: "Duration for date_add: +30d, -2M, +1y, +3h, +15m (M=months, m=minutes)"
      output_format:
        type: string
        description: "Python strftime format string for output"
      region:
        type: string
        description: "Filter timezones by region (e.g., America, Asia, Europe)"
    required: [action]
---
# Time & Date Utilities

Comprehensive time and date operations for agents.

## Actions

- **current_time** — Get current time with full breakdown (year, month, day, day_of_week, quarter, week, unix timestamp, DST status). Answers "what time/year/month/day is it?"
- **convert_time** — Convert datetime between timezones with offset calculation.
- **relative_time** — Human-readable relative time ("2 days ago", "in 3 hours").
- **get_timestamp** — Convert ISO datetime to Unix timestamp (ms and seconds).
- **days_in_month** — Days in a month, first/last day, leap year check.
- **get_week_year** — ISO week number, day of year, quarter, days remaining.
- **date_diff** — Difference between two dates in days, weeks, months, years, hours, and business days.
- **date_add** — Add/subtract duration from a date (+30d, -2M, +1y, +3h, +15m). M=months, m=minutes.
- **is_business_day** — Weekday/weekend check with next/previous business day (no holiday awareness).
- **list_timezones** — List IANA timezones, optionally filtered by region.

All timezones use IANA identifiers (e.g., `America/New_York`, `Asia/Tokyo`, `Europe/London`). Default timezone is UTC.
