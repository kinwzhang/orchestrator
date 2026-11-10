# Watch dashboard: system-local activity timestamps

## User requirement

The watch dashboard must display Last activity in the operating system's local timezone, rather than presenting the stored UTC timestamp directly. This is a future dashboard change; do not modify the active model-preferences-cli run or its approved inputs to implement it.

## Behavior

- Convert each timezone-aware activity instant to the system-local timezone when rendering. Respect the local offset applicable to that instant, including daylight-saving transitions where applicable. Do not hardcode Asia/Singapore or a fixed UTC+08 offset.
- Display a readable date and time with a numeric UTC offset, for example 2026-10-31 18:38:41 +08:00 for the instant 2026-10-31T10:38:41.981586+00:00 on a system configured for Singapore. Label the column or dashboard to make the local-time interpretation clear.
- Keep persisted journal/state timestamps and machine evidence in their existing UTC representation. Rendering must not rewrite evidence or affect chronological ordering, duration calculations, locks or lifecycle behavior.
- Absent activity remains blank or has a clear unavailable marker. Malformed or timezone-naive legacy values must not crash watch or silently be treated as a confirmed UTC instant; define and document an honest fallback.
- Use the current system timezone when the dashboard is started or restarted. Supporting a timezone change during an already-running dashboard is not required for this change.

## Acceptance

Verify equivalent instants render correctly under UTC and a non-UTC system timezone, including an offset transition where supported. Cover absent, malformed and timezone-naive values. Confirm rendering does not mutate original state or timestamps. Use deterministic fixtures and supported platform mechanisms rather than assuming Unix-only timezone APIs. Record Fedora and Windows evidence separately; Linux-only tests do not establish native Windows verification.

## Delivery

Implement through the future approved dashboard assignment. Preserve the running feature, existing approvals and live evidence. No live agent calls are needed to verify timestamp formatting.
