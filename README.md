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
| **Reruns** | A later airing of something already recorded or booked within N hours (default 12) is not recorded. Matched by title, or by the two teams for sports matchups (so `Devils at Islanders` and `NHL Hockey : New Jersey Devils at New York Islanders` count as the same game). Different episodes of a series are kept. |
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
| Skip reruns within N hours | 12 | `0` disables. See limitations for shows with no episode data. |
| Jellyfin host:port | blank | e.g. `jellyfin:8096`. Blank disables Jellyfin features. Must be reachable **from the Dispatcharr container**. |
| Jellyfin API key | blank | Create one in Jellyfin → Dashboard → API Keys. Stored in plain text in the plugin settings (Dispatcharr's settings form has no password field). |
| Recordings folder as Jellyfin sees it | `/downloads/recordings` | The Jellyfin-side path of Dispatcharr's recordings folder. A Jellyfin library must include it. |
| Fetch posters/artwork from public sources | on | |
| TheSportsDB API key | `123` | The public free test key. |
| Also fail over for rules pinned to a channel | off | |

Actions (buttons): *Apply start delay*, *Run dedupe* (also sweeps upcoming reruns), *Retry failover*, *Clean up now*, *Push metadata*, *Run guard*. Most run automatically from Dispatcharr events (`recording_start`, `recording_end`, `epg_refresh`).

## Privacy

* Programme/team names are sent to TheSportsDB and TVMaze to find artwork. Nothing else leaves your machine except calls to your own Jellyfin.
* No playlist, guide or channel-logo URLs are read or copied.
* The Jellyfin API key sits in the plugin's settings in Dispatcharr's database.

## Known limitations

This is a beta. Things to know:

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
