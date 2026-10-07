# DVR Helper (beta)

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that makes series-rule DVR recordings behave, and optionally pushes finished recordings into Jellyfin with a description and artwork.

> **Status: beta 0.1.0.** It runs on a real setup, but a lot is still being worked out
> (see [Known limitations](#known-limitations)). Expect rough edges and breaking changes.
> Tested with Dispatcharr 0.31.0 and Jellyfin 12.1.0.

It works from the DVR **series rules you already have** in Dispatcharr. There is nothing to re-enter.

## What it does

| Feature | What happens |
|---|---|
| **Dedupe** | Dispatcharr books a series-rule airing once per channel that carries it. DVR Helper keeps one and removes the others. |
| **Reruns** | A later airing of something already recorded or booked is not recorded. For team-vs-team games (`Team A at Team B`), the same game within 24 hours is a rerun, however the title is worded (`Mammoth at Devils` and `NHL Hockey : Utah Mammoth at New Jersey Devils` are the same game) — teams don't play at the same venue back to back, and `A at B` is not `B at A`. Any other programme with an identical title within 12 hours is a rerun too. Different episodes of a series are kept. |
| **Airing ledger** | The plugin remembers each recorded airing in a small SQLite file, so deleting a recording (or its file) in Dispatcharr or Jellyfin doesn't make a replay look new. See [Airing ledger](#airing-ledger). |
| **Already handled** | If you stopped a recording, or it completed, the guide may keep listing the airing for hours and Dispatcharr re-books it. DVR Helper cancels the re-booking. |
| **Failover** | If a recording dies because its stream died, a new recording starts on another channel airing the same programme, until the original end time. It is a separate file, not stitched into the first. |
| **Start delay** | Series-rule recordings booked ahead of time start N minutes late (default 5). Dispatcharr's own offset only supports starting earlier. |
| **Jellyfin metadata** | When a recording finishes, the plugin finds it in Jellyfin and writes the name, description and date. See below for artwork. |

### Artwork

Artwork is looked up on the public internet from the programme title only. **It never uses logo/poster URLs from your playlist or guide**, so nothing private is copied into Jellyfin or published.

* **Sports matchups** (`League: Away at Home`, `vs`, `@`): the two teams' badges from [TheSportsDB](https://www.thesportsdb.com/) are placed side by side on a 16:9 card (needs Pillow, which Dispatcharr ships). The teams' full names are taken from the programme description, because TheSportsDB's free key only searches full names.
* **TV shows**: poster, and episode still and summary when the guide has season/episode numbers, from [TVMaze](https://www.tvmaze.com/).
* If nothing matches, the recording gets text metadata only.

## Install

### From Dispatcharr (plugin repo)

1. In Dispatcharr go to *Plugins* and add a repository with this URL:

   ```
   https://raw.githubusercontent.com/adamef93/dvr-helper/main/manifest.json
   ```
2. Install **DVR Helper** from that repo, enable it and open its settings. The repo is unsigned, so Dispatcharr will show it as unverified.
3. **Restart Dispatcharr** after installing or updating.

### Manually

The plugin must be installed in a folder named **`dvr_helper`** (Dispatcharr uses the folder name as the plugin key).

1. Copy the `dvr_helper/` folder into Dispatcharr's plugins directory (`/data/plugins/` inside the container, i.e. the `plugins` folder of your Dispatcharr data volume on the host), **or** zip the folder and use *Plugins → Import*.
2. In Dispatcharr, open *Plugins*, find **DVR Helper**, enable it and open its settings.
3. **Restart Dispatcharr** after installing or updating. Event hooks run in the Celery workers, which only pick up new plugin code when they start.

```sh
cd dvr-helper && zip -r dvr_helper.zip dvr_helper
```

## Settings

| Setting | Default | Notes |
|---|---|---|
| Enable dedupe | on | Cross-channel dedupe, same-channel duplicates, reruns. |
| Enable failover | on | |
| Preferred channel (tvg_id contains) | blank | Prefer channels whose guide id contains this when keeping/choosing; otherwise the lowest channel number wins. |
| Same-airing start window (minutes) | 15 | Airings starting within this window of each other are one airing. |
| Skip failover if less than N minutes remain | 10 | |
| Max channels to try per airing | 3 | |
| Start recordings N minutes late | 5 | `0` disables. Only applies to recordings booked before their start. |
| Don't re-record airings already stopped/completed | on | |
| Skip game reruns within N hours | 24 | Same `Team A at Team B` game as an earlier recording. `0` disables. |
| Skip other reruns within N hours | 12 | Identical title. `0` disables. See limitations for shows with no episode data. |
| Jellyfin host:port | blank | e.g. `jellyfin:8096`. Blank disables Jellyfin features. Must be reachable **from the Dispatcharr container**. |
| Jellyfin API key | blank | Create one in Jellyfin → Dashboard → API Keys. Stored in plain text in the plugin settings (Dispatcharr's settings form has no password field). |
| Recordings folder as Jellyfin sees it | `/downloads/recordings` | The Jellyfin-side path of Dispatcharr's recordings folder. A Jellyfin library must include it. |
| Fetch posters/artwork from public sources | on | |
| TheSportsDB API key | `123` | The public free test key. |
| Remember recorded airings for N days | 14 | How long the ledger keeps an airing. Keep it longer than the rerun windows. `0` keeps entries forever. |
| Also fail over for rules pinned to a channel | off | |

Actions (buttons): *Apply start delay*, *Run dedupe* (also sweeps upcoming reruns), *Retry failover*, *Clean up now*, *Update ledger*, *Forget entry*, *Push metadata*, *Run guard*. Most run automatically from Dispatcharr events (`recording_start`, `recording_end`, `epg_refresh`).

## Airing ledger

Dispatcharr forgets a recording as soon as it is deleted, so without help a replay of a game you already watched (and deleted) would be recorded again. DVR Helper keeps its own small ledger instead: one row per recorded airing (title, programme time, status, size). The rerun and already-handled checks read it alongside Dispatcharr's recordings, so **deleting a recording, or only its file, from Dispatcharr or Jellyfin leaves the ledger entry in place**.

* **Where:** `dvr_helper/ledger.db` in Dispatcharr's data folder (`/data/dvr_helper/ledger.db` inside the container). It's a plain SQLite file, deliberately outside the plugin folder so plugin updates don't touch it and outside Dispatcharr's own database so upgrades don't either. It isn't part of Dispatcharr's database backups.
* **What goes in:** an airing is remembered when its recording starts, refreshed when it ends, and kept if the recording is deleted after it holds content. A recording cancelled while still recording, or one that captured nothing, is not remembered.
* **Pruning:** entries older than the retention setting (14 days by default) are removed during the guide-refresh sweep.
* **Forgetting one:** the *Forget entry* action (pass `recording_id`) removes an airing so a replay of it can be recorded again.
* The plugin never touches Jellyfin for this; it only looks at its own ledger and Dispatcharr's recordings.

## Privacy

* Programme/team names are sent to TheSportsDB and TVMaze to find artwork. Nothing else leaves your machine except calls to your own Jellyfin.
* No playlist, guide or channel-logo URLs are read or copied.
* The Jellyfin API key sits in the plugin's settings in Dispatcharr's database.

## Known limitations

This is a beta. Things to know:

* **The ledger only knows what the plugin has seen.** Recordings made before it was installed are added the first time it runs (on the next guide refresh or *Update ledger*). Anything deleted before that is not remembered.
* **Booking-time hook is unreliable.** Dispatcharr books series-rule recordings in a Celery worker where the plugin's database hook doesn't reliably fire. The plugin therefore also runs its guards when a recording **starts** and after each **EPG refresh**. A duplicate or rerun can briefly appear as scheduled, or even start, before it is removed.
* **Reruns are matched on title / team names only.** A rerun titled very differently won't match. Daily shows with no episode data that air within the rerun window of themselves are treated as reruns; lower the window if that bites.
* **Sports artwork depends on TheSportsDB** having both teams. Otherwise you get text only. TVMaze matches by title alone, so two shows with the same name may get the wrong poster.
* **Failover needs the other channel to be mapped to the guide** (it looks for the same programme on another channel). A dead stream with no alternative stays dead.
* **The start delay doesn't apply** to recordings booked after their scheduled start (e.g. a game already in progress).
* **Plugin code is cached per worker.** After updating the plugin, restart Dispatcharr; web workers may otherwise keep running old code.
* **The automatic Jellyfin push after a recording ends is lightly tested.** The *Push metadata* button is better tested.
* Only the series-rule flow is handled. Manually scheduled one-off recordings are mostly left alone, but a manual re-record of an airing you already stopped or completed can be cancelled if it matches a series rule (turn off "already handled" if you want that).

## Development

Single file: `dvr_helper/plugin.py`. Dispatcharr loads it as a legacy-style plugin (`Plugin` class with `fields` and `actions`); `plugin.json` is the manifest.

### Releasing

1. Bump `version` in `dvr_helper/plugin.json` and in `Plugin.version`, and update `CHANGELOG.md`.
2. Build the zip (`dvr_helper/` at its root) and create the GitHub release with that zip attached.
3. Run `python3 scripts/make_manifest.py dist/dvr_helper-<version>.zip <commit>` using the exact zip you attached, and commit `manifest.json` and `metadata/`.

## License

Public domain, released under [The Unlicense](LICENSE).
