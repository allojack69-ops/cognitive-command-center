import json
import os
import re
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Blueprint, g, redirect, render_template, request, session, url_for
from sqlalchemy import func, select

from db import Event, Run, SessionLocal, log_event

bp = Blueprint("universe_analytics", __name__, template_folder="templates")

EVENT_TYPE = "site_pageview_v1"
VISITOR_COOKIE = "ul_vid"
VISIT_COOKIE = "ul_visit"
VISITOR_MAX_AGE = 60 * 60 * 24 * 730
VISIT_MAX_AGE = 60 * 30
ANALYTICS_TZ = os.getenv("ANALYTICS_TZ", "Europe/Prague")

BOT_RE = re.compile(
    r"(bot|crawler|spider|slurp|bingpreview|facebookexternalhit|"
    r"whatsapp|telegrambot|discordbot|linkedinbot|preview)",
    re.I,
)

EXCLUDED_PREFIXES = (
    "/static/",
    "/admin",
    "/api/",
    "/health",
)

EXCLUDED_SUFFIXES = (
    ".csv",
    ".json",
    ".txt",
    ".xml",
    ".ico",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".svg",
)


def _clip(value, n=180):
    value = (value or "").strip()
    return value[:n] if value else None


def _referrer_host():
    raw = request.referrer or ""
    try:
        host = (urlparse(raw).hostname or "").lower()
    except Exception:
        host = ""
    # Internal navigation is not an acquisition source.
    current_host = (request.host or "").split(":", 1)[0].lower()
    if not host or host == current_host:
        return None
    return _clip(host, 160)


def _should_track():
    if request.method != "GET":
        return False
    if session.get("admin"):
        return False
    path = request.path or "/"
    if any(path.startswith(x) for x in EXCLUDED_PREFIXES):
        return False
    if any(path.lower().endswith(x) for x in EXCLUDED_SUFFIXES):
        return False
    ua = request.headers.get("User-Agent", "")
    if BOT_RE.search(ua):
        return False
    # Track navigational/html requests, not fetches for machine endpoints.
    accept = request.headers.get("Accept", "")
    if accept and "text/html" not in accept and "*/*" not in accept:
        return False
    return True


@bp.before_app_request
def track_pageview():
    if not _should_track():
        return

    visitor_id = request.cookies.get(VISITOR_COOKIE)
    visit_id = request.cookies.get(VISIT_COOKIE)

    if not visitor_id:
        visitor_id = "V-" + secrets.token_hex(10)
        g.analytics_set_visitor = visitor_id

    if not visit_id:
        visit_id = "S-" + secrets.token_hex(10)

    # Refresh the 30-minute visit cookie on every tracked navigation.
    g.analytics_set_visit = visit_id

    payload = {
        "path": _clip(request.path, 320) or "/",
        "endpoint": _clip(request.endpoint, 160),
        "referrer_host": _referrer_host(),
        "utm_source": _clip(request.args.get("utm_source"), 120),
        "utm_medium": _clip(request.args.get("utm_medium"), 120),
        "utm_campaign": _clip(request.args.get("utm_campaign"), 160),
        "participant_id": _clip(session.get("participant_id"), 80),
    }

    # Analytics must never break a user-facing page.
    try:
        with SessionLocal() as db:
            log_event(
                db,
                EVENT_TYPE,
                run_id=visit_id,
                participant_id=visitor_id,
                payload=payload,
            )
            db.commit()
    except Exception:
        pass


@bp.after_app_request
def analytics_cookies(response):
    secure = request.is_secure or os.getenv("COOKIE_SECURE", "1") == "1"

    visitor_id = getattr(g, "analytics_set_visitor", None)
    if visitor_id:
        response.set_cookie(
            VISITOR_COOKIE,
            visitor_id,
            max_age=VISITOR_MAX_AGE,
            httponly=True,
            secure=secure,
            samesite="Lax",
        )

    visit_id = getattr(g, "analytics_set_visit", None)
    if visit_id:
        response.set_cookie(
            VISIT_COOKIE,
            visit_id,
            max_age=VISIT_MAX_AGE,
            httponly=True,
            secure=secure,
            samesite="Lax",
        )

    return response


def _admin_guard():
    if not session.get("admin"):
        return redirect(url_for("admin_login", next=request.path))
    return None


def _event_payload(event):
    try:
        return json.loads(event.payload_json or "{}")
    except Exception:
        return {}


def _local_date(dt, tz):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


def _analytics_snapshot(days=30):
    tz = ZoneInfo(ANALYTICS_TZ)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    today = now_local.date()
    start_date = today - timedelta(days=max(days - 1, 0))

    with SessionLocal() as db:
        total_pageviews = db.scalar(
            select(func.count()).select_from(Event).where(Event.event_type == EVENT_TYPE)
        ) or 0
        total_visitors = db.scalar(
            select(func.count(func.distinct(Event.participant_id))).where(
                Event.event_type == EVENT_TYPE,
                Event.participant_id.is_not(None),
            )
        ) or 0
        total_visits = db.scalar(
            select(func.count(func.distinct(Event.run_id))).where(
                Event.event_type == EVENT_TYPE,
                Event.run_id.is_not(None),
            )
        ) or 0

        events = db.scalars(
            select(Event)
            .where(Event.event_type == EVENT_TYPE)
            .order_by(Event.created_at.asc())
        ).all()

        runs = db.scalars(
            select(Run).order_by(Run.created_at.asc())
        ).all()

    daily = defaultdict(lambda: {
        "visitors": set(),
        "visits": set(),
        "pageviews": 0,
        "runs_started": 0,
        "runs_completed": 0,
    })
    pages = Counter()
    referrers = Counter()
    campaigns = Counter()

    for e in events:
        day = _local_date(e.created_at, tz)
        if day is None:
            continue
        payload = _event_payload(e)
        path = payload.get("path") or "/"
        referrer = payload.get("referrer_host")
        utm_source = payload.get("utm_source")
        utm_campaign = payload.get("utm_campaign")

        daily[day]["pageviews"] += 1
        if e.participant_id:
            daily[day]["visitors"].add(e.participant_id)
        if e.run_id:
            daily[day]["visits"].add(e.run_id)

        pages[path] += 1
        if referrer:
            referrers[referrer] += 1
        if utm_source:
            label = utm_source
            if utm_campaign:
                label += f" / {utm_campaign}"
            campaigns[label] += 1

    for r in runs:
        created_day = _local_date(r.created_at, tz)
        completed_day = _local_date(r.completed_at, tz) if r.completed_at else None
        if created_day:
            daily[created_day]["runs_started"] += 1
        if completed_day:
            daily[completed_day]["runs_completed"] += 1

    rows = []
    cursor = start_date
    while cursor <= today:
        d = daily[cursor]
        rows.append({
            "date": cursor.isoformat(),
            "visitors": len(d["visitors"]),
            "visits": len(d["visits"]),
            "pageviews": d["pageviews"],
            "runs_started": d["runs_started"],
            "runs_completed": d["runs_completed"],
        })
        cursor += timedelta(days=1)

    rows.reverse()
    today_row = rows[0] if rows else {
        "date": today.isoformat(),
        "visitors": 0,
        "visits": 0,
        "pageviews": 0,
        "runs_started": 0,
        "runs_completed": 0,
    }

    def window(n):
        chosen = rows[:n]
        return {
            "visitors_sum_daily": sum(x["visitors"] for x in chosen),
            "visits": sum(x["visits"] for x in chosen),
            "pageviews": sum(x["pageviews"] for x in chosen),
            "runs_started": sum(x["runs_started"] for x in chosen),
            "runs_completed": sum(x["runs_completed"] for x in chosen),
        }

    return {
        "timezone": ANALYTICS_TZ,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "all_time": {
            "unique_browsers": int(total_visitors),
            "visits": int(total_visits),
            "pageviews": int(total_pageviews),
        },
        "today": today_row,
        "last_7_days": window(7),
        "last_30_days": window(30),
        "daily": rows,
        "top_pages": [{"path": k, "pageviews": v} for k, v in pages.most_common(15)],
        "top_referrers": [{"source": k, "pageviews": v} for k, v in referrers.most_common(15)],
        "utm_sources": [{"source": k, "pageviews": v} for k, v in campaigns.most_common(15)],
        "definitions": {
            "unique_browsers": "First-party anonymous browser cookie; clearing cookies creates a new browser ID.",
            "visit": "30-minute rolling first-party visit cookie.",
            "pageview": "Tracked human-like GET navigation; admin/API/static/bot traffic excluded.",
            "privacy": "No IP address and no full user-agent are stored by this analytics module.",
        },
    }


@bp.get("/admin/analytics")
def dashboard():
    guard = _admin_guard()
    if guard:
        return guard
    data = _analytics_snapshot(30)
    return render_template("analytics.html", data=data)


@bp.get("/admin/analytics.json")
def dashboard_json():
    guard = _admin_guard()
    if guard:
        return guard
    return _analytics_snapshot(90)
