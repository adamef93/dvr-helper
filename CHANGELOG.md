# Changelog

## 0.1.0-beta

First public beta.

* Cross-channel and same-channel dedupe of series-rule recordings.
* Skip reruns (title or matchup based) and re-bookings of airings already stopped/completed.
* Failover to another channel when a recording's stream dies.
* Optional start delay for series-rule recordings.
* Jellyfin metadata push (name, description, date) and artwork from TheSportsDB (two-team card) and TVMaze, never from playlist/guide URLs.
* Runs on `recording_start`, `recording_end` and `epg_refresh` events, plus manual buttons.
