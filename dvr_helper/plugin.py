"""DVR Helper (beta) - a Dispatcharr plugin that makes series-rule DVR behave.

Works off the DVR series rules already defined in Dispatcharr; there is nothing
to re-enter here.

* Dedupe: an airing carried on several channels is booked once per channel by
  Dispatcharr. Keep one.
* Reruns: skip later airings of a programme that is already recorded or booked
  (matched by title, or by the two teams for sports matchups).
* Already handled: do not re-book an airing you stopped or that completed while
  the guide still lists it.
* Failover: when a recording dies because its stream died, record the rest from
  another channel airing the same programme, as a separate file.
* Start delay: begin recordings a few minutes after the scheduled start.
* Jellyfin metadata: when a recording finishes, write its description and
  artwork (looked up from public sources, never from playlist/guide URLs) into
  Jellyfin.
"""

import base64
import logging
import os
import re
import threading
import unicodedata
import time
from datetime import timedelta

from django.db.models import Q
from django.db.models.signals import post_save
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

PLUGIN_KEY = "dvr_helper"  # must match the folder name the plugin is installed under
SIGNAL_UID = "dvr_helper_signals"

DEFAULTS = {
    "enable_dedupe": True,
    "enable_failover": True,
    "prefer_tvg_substring": "",
    "window_minutes": 15,
    "min_remaining_minutes": 10,
    "max_failovers": 3,
    "failover_pinned_rules": False,
    "start_delay_minutes": 5,
    "skip_handled_airings": True,
    "rerun_window_hours": 12,
    "jellyfin_host": "",
    "jellyfin_api_key": "",
    "jellyfin_recordings_path": "/downloads/recordings",
    "fetch_images": True,
    "sportsdb_api_key": "123",
}
RECORDINGS_ROOT = "/data/recordings"
FINAL_STATUSES = ("completed", "stopped", "interrupted")

# interrupted_reason prefixes that mean the stream died (worth failing over).
FAILOVER_REASONS = ("no_stream_data", "ffmpeg_outage_window_exhausted")
DEAD_STATUSES = ("interrupted", "stopped")
# An airing with one of these already needs no new recording, even if the guide
# keeps listing it (e.g. a game that ended early but is scheduled for hours more).
HANDLED_STATUSES = ("stopped", "completed")


# --------------------------------------------------------------------------
# Settings and series rules
# --------------------------------------------------------------------------

def _settings():
    """Return merged settings if the plugin is enabled, else None."""
    from apps.plugins.models import PluginConfig

    cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
    if not cfg or not cfg.enabled:
        return None
    return _merge(cfg.settings)


def _merge(raw):
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in (raw or {}).items() if v not in (None, "")})
    return merged


def _rules():
    from core.models import CoreSettings

    return CoreSettings.get_dvr_series_rules()


def _rule_label(rule):
    return (rule.get("title") or rule.get("description") or "(untitled rule)").strip()


def _rule_is_pinned(rule):
    return bool(str(rule.get("tvg_id") or "").strip()) or rule.get("channel_id") not in (None, "")


def _rule_q(rule):
    """Q over ProgramData for a series rule, using the same semantics as the
    rule evaluator (title/description modes, AND/OR/quotes, tvg_id)."""
    from apps.epg.query_utils import parse_text_query

    q = Q()
    title = (rule.get("title") or "").strip()
    title_mode = (rule.get("title_mode") or "exact").lower()
    if title:
        if title_mode == "exact":
            q &= Q(title__iexact=title)
        else:
            q &= parse_text_query(
                "title", title, use_regex=title_mode == "regex", whole_words=title_mode == "search"
            )
    desc = (rule.get("description") or "").strip()
    desc_mode = (rule.get("description_mode") or "contains").lower()
    if desc:
        q &= parse_text_query(
            "description", desc, use_regex=desc_mode == "regex", whole_words=desc_mode == "search"
        )
    tvg = str(rule.get("tvg_id") or "").strip()
    if tvg:
        q &= Q(tvg_id=tvg)
    return q


# --------------------------------------------------------------------------
# Recording helpers
# --------------------------------------------------------------------------

def _cp(rec):
    return rec.custom_properties or {}


def _snap(rec):
    return _cp(rec).get("program") or {}


def _program_row(rec):
    """The current EPG row for a recording's programme snapshot, if any."""
    from apps.epg.models import ProgramData

    snap = _snap(rec)
    row = ProgramData.objects.filter(pk=snap["id"]).first() if snap.get("id") else None
    if row is not None and row.tvg_id == snap.get("tvg_id"):
        return row
    start, end = parse_datetime(snap.get("start_time") or ""), parse_datetime(snap.get("end_time") or "")
    if snap.get("tvg_id") and start and end:
        return ProgramData.objects.filter(tvg_id=snap["tvg_id"], start_time=start, end_time=end).first()
    return None


def _matched_rules(rec, rules):
    """Indexes of series rules the recording's programme matches."""
    from apps.epg.models import ProgramData

    row = _program_row(rec)
    if row is None:
        return set()
    hits = set()
    for i, rule in enumerate(rules):
        try:
            if ProgramData.objects.filter(pk=row.pk).filter(_rule_q(rule)).exists():
                hits.add(i)
        except Exception:
            logger.exception("dvr_helper: could not evaluate rule %r", _rule_label(rule))
    return hits


def _identity(snap):
    if snap.get("season") is not None and snap.get("episode") is not None:
        return f"s{snap['season']}e{snap['episode']}"
    if snap.get("onscreen_episode"):
        return str(snap["onscreen_episode"]).strip().lower()
    if snap.get("sub_title"):
        return str(snap["sub_title"]).strip().lower()
    return None


def _same_airing(a, b):
    ia, ib = _identity(_snap(a)), _identity(_snap(b))
    return ia is None or ib is None or ia == ib


def _rank(rec, prefer):
    tvg = _snap(rec).get("tvg_id") or ""
    number = getattr(rec.channel, "channel_number", None)
    return (
        _cp(rec).get("status") != "recording",
        (prefer not in tvg) if prefer else True,
        number if number is not None else float("inf"),
        rec.channel_id,
    )


def _siblings(rec, cfg, rules, my_rules):
    """Live recordings of the same airing on other channels, via a shared rule."""
    from apps.channels.models import Recording

    window = timedelta(minutes=float(cfg["window_minutes"]))
    qs = (
        Recording.objects.select_related("channel")
        .filter(start_time__gte=rec.start_time - window, start_time__lte=rec.start_time + window)
        .exclude(pk=rec.pk)
        .exclude(channel_id=rec.channel_id)
    )
    out = []
    for r in qs:
        if _cp(r).get("status") in DEAD_STATUSES or not _same_airing(rec, r):
            continue
        if _matched_rules(r, rules) & my_rules:
            out.append(r)
    return out


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------

def _dedupe_group(rec, cfg, rules):
    """Keep one recording among rec and its same-airing siblings; return deleted ids."""
    my_rules = _matched_rules(rec, rules)
    if not my_rules:
        return []
    sibs = _siblings(rec, cfg, rules, my_rules)
    if not sibs:
        return []
    group = [rec] + sibs
    keep = min(group, key=lambda r: _rank(r, cfg["prefer_tvg_substring"]))
    now = timezone.now()
    removed = []
    for r in group:
        if r.pk == keep.pk:
            continue
        # Never touch a recording that is already running or has started.
        if _cp(r).get("status") == "recording" or (r.pk != rec.pk and r.start_time <= now):
            continue
        rid = r.pk
        r.delete()
        removed.append(rid)
        logger.info("dvr_helper: removed duplicate recording %s (kept %s)", rid, keep.pk)
    return removed


def _delay_start(rec, cfg, rules):
    """Push an upcoming series-rule recording's start back by start_delay_minutes.

    Saving a new start_time makes Dispatcharr revoke and reschedule the task."""
    delay = float(cfg["start_delay_minutes"] or 0)
    cp = _cp(rec)
    if delay <= 0 or cp.get("start_delayed") or cp.get("failover_of"):
        return False
    if cp.get("status") not in (None, "", "scheduled") or rec.start_time <= timezone.now():
        return False
    if not _matched_rules(rec, rules):
        return False
    new_start = rec.start_time + timedelta(minutes=delay)
    if new_start >= rec.end_time:
        return False
    cp["start_delayed"] = delay
    rec.custom_properties = cp
    rec.start_time = new_start
    rec.save(update_fields=["start_time", "custom_properties"])
    logger.info("dvr_helper: recording %s start delayed %s min to %s", rec.pk, delay, new_start)
    return True


def _drop_same_channel_duplicate(rec, cfg, rules):
    """Delete a newly created recording if the same airing is already booked or
    running on the same channel (Dispatcharr's own check misses started ones)."""
    from apps.channels.models import Recording

    my_rules = _matched_rules(rec, rules)
    if not my_rules:
        return False
    window = timedelta(minutes=float(cfg["window_minutes"]))
    # Match on end_time: Dispatcharr rewrites start_time on restart recovery.
    qs = (
        Recording.objects.filter(
            channel_id=rec.channel_id,
            end_time__gte=rec.end_time - window,
            end_time__lte=rec.end_time + window,
            pk__lt=rec.pk,
        )
        .filter(end_time__gt=timezone.now())
    )
    for other in qs:
        if _cp(other).get("status") in DEAD_STATUSES or not _same_airing(rec, other):
            continue
        if _matched_rules(other, rules) & my_rules:
            rid = rec.pk
            rec.delete()
            logger.info("dvr_helper: removed same-channel duplicate recording %s (kept %s)", rid, other.pk)
            return True
    return False


def _norm_title(title):
    """Title without the 'live' marker (superscript letters) and case/spacing noise."""
    t = "".join(c for c in (title or "") if unicodedata.category(c) != "Lm")
    t = re.sub(r"[\(\[]\s*(live|new|repeat|replay|rerun)\s*[\)\]]", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip().lower()


def _matchup_key(title):
    """('bruins', 'rangers')-style key for 'League: Away at Home' titles, else None."""
    t = _clean_title(title or "")
    if ":" in t:
        t = t.split(":", 1)[1]
    parts = MATCHUP_SEP.split(t.strip())
    if len(parts) != 2:
        return None
    teams = [re.sub(r"[^a-z0-9 ]", "", x.lower()).strip() for x in parts]
    return tuple(teams) if all(teams) else None


def _same_team(a, b):
    return a == b or a.endswith(" " + b) or b.endswith(" " + a)


def _same_programme(snap_a, snap_b):
    """Same title, or the same two teams under differently worded titles
    ('Bruins at Rangers' vs 'NHL Hockey : Boston Bruins at New York Rangers')."""
    if _norm_title(snap_a.get("title")) == _norm_title(snap_b.get("title")):
        return True
    ka, kb = _matchup_key(snap_a.get("title")), _matchup_key(snap_b.get("title"))
    if not (ka and kb):
        return False
    return any(_same_team(ka[0], x) and _same_team(ka[1], y) for x, y in (kb, kb[::-1]))


def _has_content(rec):
    """True if this recording holds (or is getting) the programme, i.e. a rerun is redundant."""
    cp = _cp(rec)
    status = cp.get("status")
    if status in (None, "", "scheduled", "recording", "completed"):
        return True
    return status in ("stopped", "interrupted") and (cp.get("bytes_written") or 0) > 0


def _drop_rerun(rec, cfg, rules):
    """Delete a recording that is a later replay of a programme already recorded
    (or booked to record earlier) within rerun_window_hours."""
    from apps.channels.models import Recording

    hours = float(cfg["rerun_window_hours"] or 0)
    if hours <= 0 or _cp(rec).get("failover_of"):
        return False
    my_rules = _matched_rules(rec, rules)
    my_snap = _snap(rec)
    my_start = parse_datetime(_snap(rec).get("start_time") or "") or rec.start_time
    if not my_rules or not my_snap.get("title"):
        return False
    span = timedelta(hours=hours)
    for other in Recording.objects.exclude(pk=rec.pk).filter(
        end_time__gte=rec.end_time - span - timedelta(hours=6),
        end_time__lte=rec.end_time + span,
    ):
        if not _has_content(other) or not _same_programme(my_snap, _snap(other)):
            continue
        o_start = parse_datetime(_snap(other).get("start_time") or "") or other.start_time
        # Only the later airing is the rerun; keep the earlier one.
        if not (timedelta(0) < my_start - o_start <= span):
            continue
        ia, ib = _identity(_snap(rec)), _identity(_snap(other))
        if ia is not None and ib is not None and ia != ib:
            continue  # different episode of the same series
        if _matched_rules(other, rules) & my_rules:
            rid = rec.pk
            rec.delete()
            logger.info(
                "dvr_helper: removed recording %s; rerun of recording %s (aired %s)",
                rid, other.pk, o_start,
            )
            return True
    return False


def _drop_handled_airing(rec, cfg, rules):
    """Delete a new series-rule recording when this airing was already recorded
    to completion or stopped by the user (on any channel)."""
    from apps.channels.models import Recording

    my_rules = _matched_rules(rec, rules)
    if not my_rules:
        return False
    window = timedelta(minutes=float(cfg["window_minutes"]))
    qs = (
        Recording.objects.filter(
            end_time__gte=rec.end_time - window,
            end_time__lte=rec.end_time + window,
        )
        .exclude(pk=rec.pk)
    )
    for other in qs:
        if _cp(other).get("status") not in HANDLED_STATUSES or not _same_airing(rec, other):
            continue
        if _matched_rules(other, rules) & my_rules:
            rid = rec.pk
            rec.delete()
            logger.info(
                "dvr_helper: removed recording %s; this airing was already %s as recording %s",
                rid, _cp(other).get("status"), other.pk,
            )
            return True
    return False


def _on_recording_saved(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        cfg = _settings()
        if not cfg:
            return
        if _cp(instance).get("failover_of"):
            return
        rules = _rules()
        if cfg["skip_handled_airings"] and _drop_handled_airing(instance, cfg, rules):
            return
        if cfg["enable_dedupe"] and _drop_rerun(instance, cfg, rules):
            return
        if cfg["enable_dedupe"] and _drop_same_channel_duplicate(instance, cfg, rules):
            return
        if cfg["enable_dedupe"]:
            _dedupe_group(instance, cfg, rules)
            if not type(instance).objects.filter(pk=instance.pk).exists():
                return
        _delay_start(instance, cfg, rules)
    except Exception:
        logger.exception("dvr_helper: dedupe hook failed")


def _connect_signal():
    from apps.channels.models import Recording

    # Disconnect first so a plugin reload swaps in the new code.
    post_save.disconnect(sender=Recording, dispatch_uid=SIGNAL_UID)
    post_save.connect(_on_recording_saved, sender=Recording, dispatch_uid=SIGNAL_UID, weak=False)


def _sweep_reruns(cfg, rules):
    """Remove upcoming (not yet started) recordings that are reruns of something earlier."""
    from apps.channels.models import Recording

    removed = []
    now = timezone.now()
    upcoming = [
        r for r in Recording.objects.filter(end_time__gt=now).order_by("start_time")
        if _cp(r).get("status") in (None, "", "scheduled") and r.start_time > now
    ]
    for rec in upcoming:
        rid = rec.pk  # Django clears pk on delete
        if Recording.objects.filter(pk=rid).exists() and _drop_rerun(rec, cfg, rules):
            removed.append(rid)
    return removed


def _dedupe_all(cfg):
    from apps.channels.models import Recording

    rules = _rules()
    removed = []
    for rec in (
        Recording.objects.select_related("channel")
        .filter(end_time__gt=timezone.now())
        .order_by("start_time")
    ):
        if not Recording.objects.filter(pk=rec.pk).exists():
            continue
        if _cp(rec).get("status") in DEAD_STATUSES:
            continue
        removed += _dedupe_group(rec, cfg, rules)
    if cfg["enable_dedupe"]:
        removed += _sweep_reruns(cfg, rules)
    return removed


# --------------------------------------------------------------------------
# Failover
# --------------------------------------------------------------------------

def _failover(recording_id, cfg):
    from apps.channels.managers import with_effective_values
    from apps.channels.models import Channel, Recording
    from apps.epg.models import ProgramData

    rec = Recording.objects.select_related("channel").filter(pk=recording_id).first()
    if rec is None:
        return {"status": "skipped", "reason": "recording gone"}
    cp = _cp(rec)
    if cp.get("status") != "interrupted":
        return {"status": "skipped", "reason": f"status={cp.get('status')}"}
    reason = cp.get("interrupted_reason") or ""
    if not reason.startswith(FAILOVER_REASONS):
        return {"status": "skipped", "reason": f"not a stream failure ({reason or 'no reason'})"}

    rules = _rules()
    matched = _matched_rules(rec, rules)
    if not cfg["failover_pinned_rules"]:
        matched = {i for i in matched if not _rule_is_pinned(rules[i])}
    if not matched:
        return {"status": "skipped", "reason": "not from a cross-channel series rule"}

    now = timezone.now()
    if rec.end_time - now < timedelta(minutes=float(cfg["min_remaining_minutes"])):
        return {"status": "skipped", "reason": "too little of the programme left"}

    tried = list(cp.get("failover_chain") or [])
    if rec.channel_id not in tried:
        tried.append(rec.channel_id)
    if len(tried) > int(cfg["max_failovers"]):
        return {"status": "skipped", "reason": "max failovers reached"}

    # Another live recording of this airing already covers it.
    for sib in _siblings(rec, cfg, rules, matched):
        if sib.end_time > now and sib.start_time <= now + timedelta(minutes=1):
            return {"status": "skipped", "reason": f"recording {sib.pk} already covers this airing"}

    orig_start = parse_datetime(_snap(rec).get("start_time") or "") or rec.start_time
    window = timedelta(minutes=float(cfg["window_minutes"]))
    rule_q = Q()
    for i in matched:
        rule_q |= _rule_q(rules[i])
    progs = list(
        ProgramData.objects.select_related("epg")
        .filter(start_time__lte=now, end_time__gt=now)
        .filter(start_time__gte=orig_start - window, start_time__lte=orig_start + window)
        .filter(rule_q)
    )
    # Skip airings that are a different episode of the same series.
    snap_identity = _identity(_snap(rec))
    progs = [
        p for p in progs
        if snap_identity is None
        or _identity({"season": (p.custom_properties or {}).get("season"),
                      "episode": (p.custom_properties or {}).get("episode"),
                      "onscreen_episode": (p.custom_properties or {}).get("onscreen_episode"),
                      "sub_title": p.sub_title}) in (None, snap_identity)
    ]
    if not progs:
        return {"status": "no_alternate", "reason": "no other channel is airing this programme"}

    by_epg = {}
    for p in progs:
        by_epg.setdefault(p.epg_id, p)
    channels = (
        with_effective_values(Channel.objects.all())
        .filter(effective_epg_data_id__in=list(by_epg))
        .exclude(pk__in=tried)
        .order_by("effective_channel_number")
    )
    prefer = cfg["prefer_tvg_substring"]
    candidates = []
    for ch in channels:
        prog = by_epg.get(ch.effective_epg_data_id)
        if prog is not None:
            candidates.append(((prefer not in (prog.tvg_id or "")) if prefer else True, ch, prog))
    if not candidates:
        return {"status": "no_alternate", "reason": "no untried channel is airing this programme"}
    candidates.sort(key=lambda c: c[0])
    _, ch, prog = candidates[0]

    pcp = prog.custom_properties or {}
    new = Recording.objects.create(
        channel=ch,
        start_time=now,
        end_time=rec.end_time,
        custom_properties={
            "program": {
                "id": prog.id,
                "tvg_id": prog.tvg_id,
                "title": prog.title,
                "sub_title": prog.sub_title,
                "description": prog.description,
                "start_time": prog.start_time.isoformat(),
                "end_time": prog.end_time.isoformat(),
                "season": pcp.get("season"),
                "episode": pcp.get("episode"),
                "onscreen_episode": pcp.get("onscreen_episode"),
            },
            "failover_of": rec.pk,
            "failover_chain": tried + [ch.pk],
        },
    )
    logger.info(
        "dvr_helper: recording %s interrupted (%s); failing over to channel %s as recording %s",
        rec.pk, reason, ch.pk, new.pk,
    )
    return {"status": "ok", "failover_recording": new.pk, "channel": ch.name}


def _failover_pending(cfg):
    """Manual run: retry failover for interrupted recordings still in progress."""
    from apps.channels.models import Recording

    results = []
    for rec in Recording.objects.filter(end_time__gt=timezone.now()).order_by("-start_time"):
        cp = _cp(rec)
        if cp.get("status") == "interrupted" and (cp.get("interrupted_reason") or "").startswith(FAILOVER_REASONS):
            # Already handled if a failover recording points back at it.
            if any(_cp(r).get("failover_of") == rec.pk for r in Recording.objects.filter(end_time__gt=timezone.now())):
                continue
            results.append({"recording": rec.pk, **_failover(rec.pk, cfg)})
    if not results:
        return {"status": "ok", "message": "No interrupted recordings need failover right now."}
    return {"status": "ok", "results": results}


def _failover_when_settled(rid, cfg):
    """Dispatcharr fires recording_end before it saves the final "interrupted"
    status, so the first look often still says "recording". Retry for a bit."""
    from django.db import close_old_connections

    result = {}
    for delay in (0, 3, 8, 20):
        time.sleep(delay)
        try:
            result = _failover(rid, cfg)
        except Exception:
            logger.exception("dvr_helper: failover check failed for recording %s", rid)
            return
        finally:
            close_old_connections()
        reason = str(result.get("reason") or "")
        if not (result.get("status") == "skipped" and reason in ("status=recording", "status=None")):
            break
    logger.info("dvr_helper: recording %s failover result: %s", rid, result)


def _guard_started(rid, cfg):
    """recording_start hook: cancel a series-rule recording that should not run.

    Dispatcharr's rule check books in a worker where the post_save hook above is
    not reliably connected, so the same guards are applied when a recording
    actually starts."""
    from apps.channels.models import Recording

    rec = Recording.objects.select_related("channel").filter(pk=rid).first()
    if rec is None or _cp(rec).get("failover_of"):
        return {"status": "skipped", "reason": "gone or failover recording"}
    rules = _rules()
    if cfg["skip_handled_airings"] and _drop_handled_airing(rec, cfg, rules):
        return {"status": "ok", "removed": rid, "why": "airing already stopped/completed"}
    if cfg["enable_dedupe"] and _drop_rerun(rec, cfg, rules):
        return {"status": "ok", "removed": rid, "why": "rerun of an earlier recording"}
    if cfg["enable_dedupe"] and _drop_same_channel_duplicate(rec, cfg, rules):
        return {"status": "ok", "removed": rid, "why": "same-channel duplicate"}
    return {"status": "skipped", "reason": "nothing to cancel"}


# --------------------------------------------------------------------------
# Jellyfin metadata (description + artwork from public sources)
# --------------------------------------------------------------------------
# Artwork never comes from the playlist or guide data (those URLs are private
# to the user); it is looked up on the public internet from the programme title.

HTTP_HEADERS = {"User-Agent": "dvr-helper/0.1.0-beta"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MATCHUP_SEP = re.compile(r"\s+(?:at|vs\.?|v\.?|@)\s+", re.I)


def _clean_title(title):
    """Programme title without the superscript 'live' marker or (Replay)-style tags."""
    t = "".join(c for c in (title or "") if unicodedata.category(c) != "Lm")
    t = re.sub(r"[\(\[]\s*(live|new|repeat|replay|rerun)\s*[\)\]]", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def _jf_base(cfg):
    host = str(cfg.get("jellyfin_host") or "").strip().rstrip("/")
    if not host:
        return None
    return host if "://" in host else "http://" + host


def _jf(cfg, method, path, **kw):
    import requests

    headers = {"Authorization": 'MediaBrowser Token="%s"' % str(cfg["jellyfin_api_key"]).strip(), **HTTP_HEADERS}
    headers.update(kw.pop("headers", {}))
    resp = requests.request(method, _jf_base(cfg) + path, headers=headers, timeout=30, **kw)
    resp.raise_for_status()
    return resp


def _get_json(url, **params):
    import requests

    resp = requests.get(url, params=params or None, headers=HTTP_HEADERS, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _download_image(url):
    import requests

    if not url or not url.lower().startswith("https://"):
        return None
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=30, stream=True)
    resp.raise_for_status()
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype not in ("image/jpeg", "image/png", "image/webp"):
        return None
    data = b""
    for chunk in resp.iter_content(65536):
        data += chunk
        if len(data) > MAX_IMAGE_BYTES:
            return None
    return data, ctype


def _tvmaze_lookup(title, season, episode):
    """Series poster (and episode still/summary when season+episode are known)."""
    show = _get_json("https://api.tvmaze.com/singlesearch/shows", q=title)
    if not show:
        return {}
    got, want = _norm_title(show.get("name")), _norm_title(title)
    if got != want and not want.startswith(got + " "):
        return {}  # fuzzy hit on an unrelated show
    out = {"source": "tvmaze", "poster": (show.get("image") or {}).get("original"),
           "overview": re.sub(r"<[^>]+>", "", show.get("summary") or "").strip() or None}
    if season is not None and episode is not None:
        ep = _get_json("https://api.tvmaze.com/shows/%s/episodebynumber" % show["id"], season=season, number=episode)
        if ep:
            out["thumb"] = (ep.get("image") or {}).get("original")
            out["episode_name"] = ep.get("name")
            if ep.get("summary"):
                out["overview"] = re.sub(r"<[^>]+>", "", ep["summary"]).strip()
    return out


def _sportsdb_team(base, name, league, hint=""):
    """Resolve a short team name ('Flyers') to a TheSportsDB team.

    The free API key only searches full names, so the full name is taken from the
    programme description ('... host Philadelphia Flyers ...') when it is there."""
    short = re.escape(name.strip())
    cands = re.findall(r"((?:[A-Z][\w.'\u2019-]*\s+){0,3}%s)\b" % short, hint or "")
    cands = sorted(set(cands), key=len, reverse=True) + [name]
    for cand in cands:
        data = _get_json(base + "/searchteams.php", t=cand) or {}
        for t in data.get("teams") or []:
            if league and str(t.get("strLeague") or "").lower() != league.lower():
                continue
            full = str(t.get("strTeam") or "").lower()
            if full == cand.lower() or full.endswith(" " + name.strip().lower()):
                return t
    return None


def _sportsdb_lookup(cfg, title, air_date, hint=""):
    """Game image + team artwork for 'League: Away at Home' style titles."""
    league = None
    t = _clean_title(title)
    if ":" in t:
        league, t = [x.strip() for x in t.split(":", 1)]
    parts = MATCHUP_SEP.split(t)
    if len(parts) != 2:
        return {}
    base = "https://www.thesportsdb.com/api/v1/json/%s" % (cfg.get("sportsdb_api_key") or "123")
    a, b = _sportsdb_team(base, parts[0], league, hint), _sportsdb_team(base, parts[1], league, hint)
    if not (a and b):
        return {}
    out = {"source": "thesportsdb", "teams": (a["strTeam"], b["strTeam"])}
    home = b if re.search(r"\sat\s|@", title, re.I) else a  # 'A at B' = B is home
    out["badges"] = (a.get("strBadge"), b.get("strBadge"))  # title order: away/first, home/second
    out["word"] = "at" if re.search(r"\sat\s|@", title, re.I) else "vs"
    out["fanart"] = home.get("strFanart1") or home.get("strBanner")
    out["badge"] = home.get("strBadge")
    out["logo"] = home.get("strLogo")
    for first, second in ((a, b), (b, a)):
        data = _get_json(base + "/searchevents.php", e="%s vs %s" % (first["strTeam"], second["strTeam"])) or {}
        for ev in data.get("event") or []:
            try:
                near = abs((parse_datetime(ev["dateEvent"] + "T00:00:00+00:00") - air_date).days) <= 1
            except Exception:
                near = False
            if near:
                out["event_thumb"] = ev.get("strThumb") or ev.get("strPoster")
                out["overview"] = ev.get("strDescriptionEN")
                return out
    return out


def _compose_matchup(url_a, url_b, word):
    """16:9 card with both team badges side by side ('away  at  home')."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    badges = []
    for url in (url_a, url_b):
        got = _download_image(url)
        if not got:
            return None
        badges.append(Image.open(BytesIO(got[0])).convert("RGBA"))
    width, height, box = 1280, 720, 470
    canvas = Image.new("RGBA", (width, height), (244, 244, 246, 255))
    for badge, cx in zip(badges, (300, 980)):
        scale = box / max(badge.width, badge.height)
        badge = badge.resize((max(1, int(badge.width * scale)), max(1, int(badge.height * scale))), Image.LANCZOS)
        canvas.alpha_composite(badge, (cx - badge.width // 2, height // 2 - badge.height // 2))
    ImageDraw.Draw(canvas).text((width // 2, height // 2), word.upper(), fill=(120, 120, 128, 255),
                                font=ImageFont.load_default(size=72), anchor="mm")
    out = BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue(), "image/jpeg"


def _jf_find_item(cfg, jf_path, wait=True):
    """Find (scan if needed) the Jellyfin item for jf_path. Returns (item_id, library_id)."""
    root = str(cfg["jellyfin_recordings_path"]).rstrip("/")
    libs = _jf(cfg, "GET", "/Library/VirtualFolders").json()
    lib = next((l for l in libs for loc in l.get("Locations", [])
                if loc.rstrip("/") == root or jf_path.startswith(loc.rstrip("/") + "/")), None)
    if lib is None:
        raise RuntimeError("no Jellyfin library covers %s" % root)

    def lookup():
        items = _jf(cfg, "GET", "/Items", params={
            "Recursive": "true", "ParentId": lib["ItemId"], "Fields": "Path",
            "IncludeItemTypes": "Episode,Movie,Video", "Limit": 5000}).json().get("Items", [])
        return next((i["Id"] for i in items if i.get("Path") == jf_path), None)

    item = lookup()
    if item or not wait:
        return item, lib["ItemId"]
    _jf(cfg, "POST", "/Items/%s/Refresh" % lib["ItemId"], params={
        "Recursive": "true", "MetadataRefreshMode": "Default", "ImageRefreshMode": "Default"})
    for _ in range(36):
        time.sleep(5)
        item = lookup()
        if item:
            return item, lib["ItemId"]
    raise RuntimeError("Jellyfin did not index %s within 3 minutes" % jf_path)


def _push_metadata(rid, cfg, force=False):
    from apps.channels.models import Recording

    if not (_jf_base(cfg) and str(cfg.get("jellyfin_api_key") or "").strip()):
        return {"status": "skipped", "reason": "Jellyfin host/API key not set"}
    rec = Recording.objects.filter(pk=rid).first()
    if rec is None:
        return {"status": "skipped", "reason": "recording gone"}
    cp = _cp(rec)
    path = cp.get("file_path") or ""
    if cp.get("status") not in FINAL_STATUSES or not path.startswith(RECORDINGS_ROOT + "/"):
        return {"status": "skipped", "reason": "not finished (status=%s)" % cp.get("status")}
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return {"status": "skipped", "reason": "no output file"}
    if cp.get("jellyfin_metadata") and not force:
        return {"status": "skipped", "reason": "already done"}

    snap = _snap(rec)
    title = _clean_title(snap.get("title") or rec.channel.name)
    air = parse_datetime(snap.get("start_time") or "") or rec.start_time
    jf_path = str(cfg["jellyfin_recordings_path"]).rstrip("/") + path[len(RECORDINGS_ROOT):]
    item_id, _ = _jf_find_item(cfg, jf_path)

    art = {}
    if cfg["fetch_images"]:
        try:
            art = _sportsdb_lookup(cfg, snap.get("title") or "", air, snap.get("description") or "") or {}
        except Exception:
            logger.exception("dvr_helper: TheSportsDB lookup failed")
        if not art:
            try:
                art = _tvmaze_lookup(title, snap.get("season"), snap.get("episode")) or {}
            except Exception:
                logger.exception("dvr_helper: TVMaze lookup failed")

    overview = (snap.get("description") or "").strip() or art.get("overview") or ""
    dto = _jf(cfg, "GET", "/Items", params={
        "Ids": item_id, "Fields": "Overview,Genres,Studios,Taglines,People,ProviderIds,PremiereDate,LockedFields"}).json()["Items"][0]
    dto["Name"] = art.get("episode_name") or title
    dto["Overview"] = overview
    dto["PremiereDate"] = air.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    dto["ProductionYear"] = air.year
    dto["LockedFields"] = sorted(set(dto.get("LockedFields") or []) | {"Name", "Overview"})
    _jf(cfg, "POST", "/Items/%s" % item_id, json=dto)

    uploaded = []
    images = []  # (kind, (bytes, content_type))
    card = None
    if art.get("badges") and all(art["badges"]):
        try:
            card = _compose_matchup(art["badges"][0], art["badges"][1], art.get("word") or "vs")
        except Exception:
            logger.exception("dvr_helper: could not build matchup image")
    if card:
        images.append(("Primary", card))
        for kind, suffix, tries in (("Backdrop", "/0", 10), ("Logo", "", 1)):  # drop art from earlier pushes
            for _ in range(tries):
                try:
                    _jf(cfg, "DELETE", "/Items/%s/Images/%s%s" % (item_id, kind, suffix))
                except Exception:
                    break
    else:
        for kind, url in (("Primary", art.get("event_thumb") or art.get("thumb") or art.get("poster")
                           or art.get("fanart") or art.get("badge")),
                          ("Backdrop", art.get("fanart")), ("Logo", art.get("logo"))):
            try:
                img = _download_image(url) if url else None
            except Exception:
                logger.exception("dvr_helper: could not download %s image for recording %s", kind, rid)
                img = None
            if img:
                images.append((kind, img))
    for kind, img in images:
        try:
            _jf(cfg, "POST", "/Items/%s/Images/%s" % (item_id, kind), data=base64.b64encode(img[0]),
                headers={"Content-Type": img[1]})
            uploaded.append(kind)
        except Exception:
            logger.exception("dvr_helper: could not upload %s image for recording %s", kind, rid)

    cp["jellyfin_metadata"] = timezone.now().isoformat()
    rec.custom_properties = cp
    rec.save(update_fields=["custom_properties"])
    result = {"status": "ok", "item": item_id, "name": dto["Name"], "overview_chars": len(overview),
              "image_source": art.get("source"), "matchup_card": bool(card), "images": uploaded}
    logger.info("dvr_helper: recording %s Jellyfin metadata: %s", rid, result)
    return result


def _metadata_when_finished(rid, cfg):
    """After recording_end: wait for the final file (remux can take minutes), then push metadata."""
    from apps.channels.models import Recording
    from django.db import close_old_connections

    for _ in range(120):  # up to ~30 minutes
        time.sleep(15)
        try:
            rec = Recording.objects.filter(pk=rid).first()
            if rec is None:
                return
            cp = _cp(rec)
            if cp.get("status") in FINAL_STATUSES and cp.get("file_path") and (
                cp.get("remux_success") is not None or cp.get("status") == "interrupted"
            ):
                _push_metadata(rid, cfg)
                return
        except Exception:
            logger.exception("dvr_helper: metadata push failed for recording %s", rid)
            return
        finally:
            close_old_connections()


# --------------------------------------------------------------------------
# Plugin entry point
# --------------------------------------------------------------------------

class Plugin:
    name = "DVR Helper"
    version = "0.1.0-beta"
    author = "adamef93"
    help_url = "https://github.com/adamef93/dvr-helper"
    description = (
        "Beta. Works with your existing DVR series rules: keeps one recording per airing, "
        "skips reruns and already-recorded games, fails over to another channel when a "
        "stream dies, optionally delays starts, and writes descriptions and artwork "
        "into Jellyfin when recordings finish."
    )

    fields = [
        {"id": "enable_dedupe", "label": "Enable dedupe", "type": "boolean",
         "default": DEFAULTS["enable_dedupe"],
         "help_text": "Remove duplicate upcoming recordings of the same airing on different channels."},
        {"id": "enable_failover", "label": "Enable failover", "type": "boolean",
         "default": DEFAULTS["enable_failover"],
         "help_text": "When a stream dies mid-programme, record the rest from another channel."},
        {"id": "prefer_tvg_substring", "label": "Preferred channel (tvg_id contains)", "type": "string",
         "default": DEFAULTS["prefer_tvg_substring"],
         "help_text": "Optional. Channels whose EPG tvg_id contains this are kept/chosen first; otherwise the lowest channel number wins."},
        {"id": "window_minutes", "label": "Same-airing start window (minutes)", "type": "number",
         "default": DEFAULTS["window_minutes"],
         "help_text": "Recordings from the same rule starting within this many minutes are treated as one airing."},
        {"id": "min_remaining_minutes", "label": "Skip failover if less than N minutes remain", "type": "number",
         "default": DEFAULTS["min_remaining_minutes"]},
        {"id": "max_failovers", "label": "Max channels to try per airing", "type": "number",
         "default": DEFAULTS["max_failovers"]},
        {"id": "start_delay_minutes", "label": "Start recordings N minutes late", "type": "number",
         "default": DEFAULTS["start_delay_minutes"],
         "help_text": "Series-rule recordings begin this many minutes after the scheduled start. 0 disables."},
        {"id": "skip_handled_airings", "label": "Don't re-record airings already stopped/completed", "type": "boolean",
         "default": DEFAULTS["skip_handled_airings"],
         "help_text": "Stops the guide re-booking a game you stopped (or that finished) while it is still listed."},
        {"id": "rerun_window_hours", "label": "Skip reruns within N hours", "type": "number",
         "default": DEFAULTS["rerun_window_hours"],
         "help_text": "A later airing with the same title as something already recorded/booked within this many hours is treated as a rerun and not recorded. Different episodes of a series are kept. 0 disables."},
        {"id": "jellyfin_host", "label": "Jellyfin host:port", "type": "string", "default": "",
         "help_text": "e.g. jellyfin:8096 or 192.168.1.10:8096. Leave blank to disable Jellyfin metadata."},
        {"id": "jellyfin_api_key", "label": "Jellyfin API key", "type": "string", "default": "",
         "help_text": "Dashboard > API Keys. Stored in this plugin's settings."},
        {"id": "jellyfin_recordings_path", "label": "Recordings folder as Jellyfin sees it", "type": "string",
         "default": DEFAULTS["jellyfin_recordings_path"],
         "help_text": "The Jellyfin-side path that corresponds to Dispatcharr's recordings folder."},
        {"id": "fetch_images", "label": "Fetch posters/artwork from public sources", "type": "boolean",
         "default": DEFAULTS["fetch_images"],
         "help_text": "TheSportsDB for sports games, TVMaze for TV shows, looked up by programme title only."},
        {"id": "sportsdb_api_key", "label": "TheSportsDB API key", "type": "string", "default": DEFAULTS["sportsdb_api_key"],
         "help_text": "Defaults to the public free test key."},
        {"id": "failover_pinned_rules", "label": "Also fail over for rules pinned to a channel", "type": "boolean",
         "default": DEFAULTS["failover_pinned_rules"],
         "help_text": "Rules with a channel/tvg_id set are normally left alone."},
    ]

    actions = [
        {"id": "delay_upcoming", "label": "Apply start delay to upcoming recordings",
         "description": "Shift upcoming series-rule recordings that have not been delayed yet.",
         "button_label": "Apply delay"},
        {"id": "dedupe_now", "label": "Dedupe upcoming recordings now",
         "description": "Apply dedupe to all upcoming recordings that came from series rules.",
         "button_label": "Run dedupe"},
        {"id": "on_recording_end", "label": "Failover on recording end",
         "description": (
             "Runs automatically when a recording ends. Pressing Run does the same check by hand: "
             "it looks for a series-rule recording whose stream died and whose programme is still "
             "airing, and records the rest from another channel."
         ),
         "button_label": "Retry failover now",
         "events": ["recording_end"]},
        {"id": "on_epg_refresh", "label": "Clean up upcoming recordings after a guide refresh",
         "description": "Runs automatically after each EPG refresh: removes duplicate and rerun bookings.",
         "button_label": "Clean up now",
         "events": ["epg_refresh"]},
        {"id": "push_metadata", "label": "Add metadata to finished recordings in Jellyfin",
         "description": "Writes the description and artwork for finished recordings that have not been done yet.",
         "button_label": "Push metadata"},
        {"id": "on_recording_start", "label": "Guard on recording start",
         "description": "Runs automatically when a recording starts: cancels it if the airing was already stopped/completed or is a same-channel duplicate.",
         "button_label": "Run guard",
         "events": ["recording_start"]},
    ]

    def __init__(self):
        try:
            _connect_signal()
        except Exception:
            logger.exception("dvr_helper: could not connect dedupe signal")

    def run(self, action, params, context):
        cfg = _merge(context.get("settings"))
        if action == "dedupe_now":
            return {"status": "ok", "removed": _dedupe_all(cfg)}
        if action == "delay_upcoming":
            from apps.channels.models import Recording

            rules = _rules()
            done = [r.pk for r in Recording.objects.filter(start_time__gt=timezone.now()).order_by("start_time")
                    if _delay_start(r, cfg, rules)]
            return {"status": "ok", "delayed": done}
        if action == "on_epg_refresh":
            if not cfg["enable_dedupe"]:
                return {"status": "skipped", "reason": "dedupe disabled"}
            removed = _dedupe_all(cfg)
            if removed:
                logger.info("dvr_helper: guide-refresh cleanup removed recordings %s", removed)
            return {"status": "ok", "removed": removed}
        if action == "push_metadata":
            from apps.channels.models import Recording

            params = params or {}
            if params.get("recording_id") is not None:
                new_path = params.get("file_path")
                if new_path:
                    # Repoint a recording whose file was moved (the API won't let clients do this).
                    new_path = os.path.normpath(str(new_path))
                    rec = Recording.objects.filter(pk=int(params["recording_id"])).first()
                    if rec is None or not new_path.startswith(RECORDINGS_ROOT + "/") or not os.path.isfile(new_path):
                        return {"status": "error", "message": "recording or file not found under %s" % RECORDINGS_ROOT}
                    cp = _cp(rec)
                    cp["file_path"], cp["file_name"] = new_path, os.path.basename(new_path)
                    rec.custom_properties = cp
                    rec.save(update_fields=["custom_properties"])
                    logger.info("dvr_helper: recording %s file path set to %s", rec.pk, new_path)
                try:
                    return _push_metadata(int(params["recording_id"]), cfg, force=bool(params.get("force")))
                except Exception as exc:
                    logger.exception("dvr_helper: metadata push failed")
                    return {"status": "error", "message": str(exc)}
            results = {}
            for rec in Recording.objects.order_by("id"):
                if _cp(rec).get("status") in FINAL_STATUSES and not _cp(rec).get("jellyfin_metadata"):
                    try:
                        results[rec.pk] = _push_metadata(rec.pk, cfg)
                    except Exception as exc:
                        results[rec.pk] = {"status": "error", "message": str(exc)}
            return {"status": "ok", "results": results}
        if action == "on_recording_start":
            payload = (params or {}).get("payload") or {}
            rid = payload.get("recording_id")
            if rid is None:
                return {"status": "skipped", "reason": "no recording_id"}
            try:
                result = _guard_started(int(rid), cfg)
            except Exception:
                logger.exception("dvr_helper: start guard failed for recording %s", rid)
                return {"status": "error"}
            logger.info("dvr_helper: recording %s start guard: %s", rid, result)
            return result
        if action == "on_recording_end":
            if not cfg["enable_failover"]:
                return {"status": "skipped", "reason": "failover disabled"}
            payload = (params or {}).get("payload") or {}
            rid = payload.get("recording_id") or (payload.get("details") or {}).get("recording_id")
            if rid is not None:
                threading.Thread(
                    target=_failover_when_settled, args=(int(rid), cfg), daemon=True
                ).start()
                if _jf_base(cfg) and str(cfg.get("jellyfin_api_key") or "").strip():
                    threading.Thread(
                        target=_metadata_when_finished, args=(int(rid), cfg), daemon=True
                    ).start()
                return {"status": "ok", "message": f"failover check scheduled for recording {rid}"}
            if payload:
                logger.warning("dvr_helper: recording_end without recording_id: %s", payload)
                return {"status": "skipped", "reason": "no recording_id"}
            return _failover_pending(cfg)
        return {"status": "error", "message": f"Unknown action: {action}"}
