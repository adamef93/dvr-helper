# Changelog

## Unreleased

* Rerun check for team-vs-team games now uses a separate 24-hour window (new setting) and respects home/away order (`A at B` is not `B at A`). Previously a replay more than 12 hours after the live game was missed.
* Fixed rerun and already-handled checks being skipped once the earlier airing's guide row had been purged, which is the normal state a few hours after it airs.
* The already-handled guard now also requires the same programme or matchup.
* *Run guard* accepts `dry_run` and returns a trace of why a recording would or wouldn't be treated as a rerun.

## 0.1.0-beta

First public beta.

* Cross-channel and same-channel dedupe of series-rule recordings.
* Skip reruns (title or matchup based) and re-bookings of airings already stopped/completed.
* Failover to another channel when a recording's stream dies.
* Optional start delay for series-rule recordings.
* Jellyfin metadata push (name, description, date) and artwork from TheSportsDB (two-team card) and TVMaze, never from playlist/guide URLs.
* Runs on `recording_start`, `recording_end` and `epg_refresh` events, plus manual buttons.
