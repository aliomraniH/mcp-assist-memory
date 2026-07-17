-- Surface attribution: every tool_events row records which auth surface
-- (dashboard token label: web | desktop-cli | chatgpt | cursor) the call came
-- through, resolved by the HTTP auth gate from the presented token. Additive
-- and nullable — rows from non-HTTP transports (tests, direct calls) stay NULL.
ALTER TABLE tool_events ADD COLUMN IF NOT EXISTS source_surface text;
CREATE INDEX IF NOT EXISTS tool_events_surface_ts ON tool_events (source_surface, ts);
