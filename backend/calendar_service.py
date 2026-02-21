"""
Google Calendar event creation for the Voice Scheduling Agent.
Loads credentials from env or file, builds Calendar API client.
Supports timed events (meetings) with configurable duration and all-day events.
"""

import json
import logging
import os
from datetime import datetime, timedelta

from dateutil.tz import gettz
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Scope for Calendar API
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _get_credentials():
    """Load credentials: first from GOOGLE_CREDENTIALS_JSON env, else from credentials.json."""
    json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if json_str:
        try:
            info = json.loads(json_str)
            logger.info("Loaded Google credentials from GOOGLE_CREDENTIALS_JSON env")
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        except json.JSONDecodeError as e:
            logger.exception("Failed to parse GOOGLE_CREDENTIALS_JSON: %s", e)
            raise
    path = os.path.join(os.path.dirname(__file__), "credentials.json")
    if os.path.isfile(path):
        logger.info("Loading Google credentials from credentials.json")
        return Credentials.from_service_account_file(path, scopes=SCOPES)
    raise FileNotFoundError(
        "No Google credentials: set GOOGLE_CREDENTIALS_JSON or place credentials.json in backend/"
    )


def _get_calendar_id():
    """Calendar ID from env; required for create_event."""
    cal_id = os.environ.get("CALENDAR_ID", "").strip()
    if not cal_id:
        raise ValueError("CALENDAR_ID environment variable is not set")
    return cal_id


def _build_service():
    """Build and return Calendar API v3 service (cached per process by caller if desired)."""
    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds)


def create_event(
    summary: str,
    description: str,
    timezone: str = "Asia/Kolkata",
    *,
    start_datetime_str: str | None = None,
    date: str | None = None,
    duration_minutes: int = 30,
) -> dict:
    """
    Create a calendar event: either timed (meeting) or all-day.

    For timed events, pass start_datetime_str (and optionally duration_minutes).
    For all-day events, pass date (YYYY-MM-DD).

    Args:
        summary: Event/meeting title.
        description: e.g. "Meeting scheduled by Voice Assistant for {name}".
        timezone: IANA timezone (default Asia/Kolkata); used for timed events only.
        start_datetime_str: ISO format YYYY-MM-DDTHH:MM:SS for timed events.
        date: YYYY-MM-DD for all-day events. Mutually exclusive with start_datetime_str.
        duration_minutes: Length of timed event in minutes (default 30). Ignored for all-day.

    Returns:
        {"success": True, "event_link": url} or {"success": False, "error": str}.
    """
    if start_datetime_str and date:
        return {"success": False, "error": "Provide start_datetime_str OR date, not both"}
    if not start_datetime_str and not date:
        return {"success": False, "error": "Provide start_datetime_str or date"}

    if date:
        return _create_all_day_event(summary=summary, description=description, date=date)
    return _create_timed_event(
        summary=summary,
        description=description,
        start_datetime_str=start_datetime_str,
        timezone=timezone,
        duration_minutes=duration_minutes,
    )


def _create_timed_event(
    summary: str,
    description: str,
    start_datetime_str: str,
    timezone: str,
    duration_minutes: int = 30,
) -> dict:
    """Create a timed calendar event (meeting) with configurable duration."""
    logger.info(
        "create_event (timed) called: summary=%r, start_datetime_str=%r, duration_minutes=%d, timezone=%s",
        summary,
        start_datetime_str,
        duration_minutes,
        timezone,
    )
    try:
        s = start_datetime_str.strip().replace("Z", "")[:19]
        start_dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_iso, "timeZone": timezone},
            "end": {"dateTime": end_iso, "timeZone": timezone},
        }
        service = _build_service()
        cal_id = _get_calendar_id()
        result = (
            service.events()
            .insert(calendarId=cal_id, body=event)
            .execute()
        )
        link = result.get("htmlLink")
        logger.info("Calendar event created successfully: %s", link)
        return {"success": True, "event_link": link}
    except Exception as e:
        logger.exception("Calendar create_event failed: %s", e)
        return {"success": False, "error": str(e)}


def _create_all_day_event(summary: str, description: str, date: str) -> dict:
    """Create an all-day calendar event (e.g. birthday, anniversary)."""
    logger.info("create_event (all-day) called: summary=%r, date=%r", summary, date)
    try:
        day = date.strip()[:10]
        event = {
            "summary": summary,
            "description": description,
            "start": {"date": day},
            "end": {"date": day},
        }
        service = _build_service()
        cal_id = _get_calendar_id()
        result = (
            service.events()
            .insert(calendarId=cal_id, body=event)
            .execute()
        )
        link = result.get("htmlLink")
        logger.info("Calendar all-day event created successfully: %s", link)
        return {"success": True, "event_link": link}
    except Exception as e:
        logger.exception("Calendar create_all_day_event failed: %s", e)
        return {"success": False, "error": str(e)}


def delete_event_by_date_and_title(
    date_str: str,
    title: str,
    timezone: str = "Asia/Kolkata",
) -> dict:
    """
    Find an event on the given date with the given title and delete it.
    Uses the full day (00:00:00 to 23:59:59) in the given timezone.
    If exactly one event matches, it is deleted. Otherwise returns an error.

    Args:
        date_str: ISO date only YYYY-MM-DD.
        title: Event summary/title to match (case-insensitive).
        timezone: IANA timezone (default Asia/Kolkata).

    Returns:
        {"success": True} or {"success": False, "error": str}.
    """
    logger.info(
        "delete_event_by_date_and_title called: date_str=%r, title=%r, timezone=%s",
        date_str,
        title,
        timezone,
    )
    try:
        s = date_str.strip()[:10]
        day_dt = datetime.strptime(s, "%Y-%m-%d")
        tzinfo = gettz(timezone)
        if tzinfo is None:
            tzinfo = gettz("UTC")
        time_min = day_dt.replace(hour=0, minute=0, second=0, tzinfo=tzinfo)
        time_max = day_dt.replace(hour=23, minute=59, second=59, tzinfo=tzinfo)
        time_min_str = time_min.isoformat()
        time_max_str = time_max.isoformat()
        service = _build_service()
        cal_id = _get_calendar_id()
        result = (
            service.events()
            .list(
                calendarId=cal_id,
                timeMin=time_min_str,
                timeMax=time_max_str,
                singleEvents=True,
            )
            .execute()
        )
        items = result.get("items", [])
        title_lower = title.strip().lower()
        matches = [e for e in items if (e.get("summary") or "").strip().lower() == title_lower]
        if len(matches) == 0:
            logger.warning("No matching event found for date=%s title=%r", date_str, title)
            return {"success": False, "error": "No matching event"}
        if len(matches) > 1:
            logger.warning("Multiple matching events (%d) for date=%s title=%r", len(matches), date_str, title)
            return {"success": False, "error": "Multiple matches; specify time"}
        event_id = matches[0]["id"]
        service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        logger.info("Calendar event deleted successfully: eventId=%s", event_id)
        return {"success": True}
    except Exception as e:
        logger.exception("Calendar delete_event_by_date_and_title failed: %s", e)
        return {"success": False, "error": str(e)}
