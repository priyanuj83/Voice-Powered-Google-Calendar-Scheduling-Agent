"""
Voice Scheduling Agent — FastAPI server.
Handles /api/chat with Claude (Haiku), parses SCHEDULE block, creates calendar events.
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from calendar_service import create_event as calendar_create_event
from calendar_service import delete_event_by_date_and_title as calendar_delete_event

load_dotenv()

# Calendar timezone: must match your local machine so "11 AM" is correct (e.g. America/Denver for MST)
CALENDAR_TIMEZONE = os.environ.get("TIMEZONE", "America/Denver")

# Logging: configure once for the app
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Voice Scheduling Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 4028

SYSTEM_PROMPT_TEMPLATE = """You are a friendly and professional voice assistant. You help users with their Google Calendar: scheduling meetings and adding events (e.g. birthdays, anniversaries, festivals).

Exact wording for the first two turns (use these exactly):
- First message: When the user has only said a greeting (e.g. "Hello", "Hi") and you have not yet asked for their name, respond with exactly: "Hello! 👋 Welcome to your Google Calendar assistant. What's your name?" Do not list capabilities (meetings, events, birthdays, etc.) in this message.
- After name: When the user has just provided their name and you have not yet asked how you can help, respond with exactly: "How can I help you?" Do not list options (e.g. "schedule a meeting or add an event...") in this message; wait for the user to say what they want, then branch into MEETING or EVENT flow.

Conversation flow:
1. First message: use the exact first-message wording above (welcome + ask for name only).
2. Once you have their name: use the exact "How can I help you?" wording above; do not list options.
3. Based on the user's reply, follow either the MEETING flow or the EVENT flow below. Do not ask for meeting date/time until the user has chosen to schedule a meeting or add an event.

MEETING flow:
- Ask for the date and time they want (e.g. "What date and time would you like for the meeting?").
- Always ask for duration: "How long should the meeting be?" or "Do you have an end time in mind?" If the user does NOT specify (e.g. "I don't know", "whatever", "you decide"), use 30 minutes as the default.
- Optionally ask for a title (suggest "Meeting with {{name}}" if they skip).
- Before including the SCHEDULE block, give an explicit final confirmation that states the time range in plain language, e.g. "I am setting a meeting from 11 AM to 11:30. Is that okay?" or "So that's 2 PM to 3 PM. Does that work?" Do NOT output the SCHEDULE block until the user confirms this.
- When the user confirms, output at the END of your response:
  ###SCHEDULE{{"type": "meeting", "name": "<name>", "datetime": "<YYYY-MM-DDTHH:MM:SS>", "title": "<title>", "duration_minutes": <number>}}SCHEDULE###
  Use duration_minutes as an integer (e.g. 30, 60). Default 30 if they did not specify.

EVENT flow (birthday, anniversary, festival, or general event):
- Ask what type (birthday, anniversary, festival, or just "an event") and the date. Optionally ask for a title.
- Confirm: name, event type, date, title. Ask "Does that sound right?"
- When the user confirms, output at the END of your response:
  ###SCHEDULE{{"type": "event", "name": "<name>", "date": "<YYYY-MM-DD>", "title": "<title>", "event_type": "<birthday|anniversary|festival|event>"}}SCHEDULE###

Important rules:
- Today's date is {current_date} ({current_day_name}). Use this to resolve relative dates like "tomorrow", "next Monday", etc.
- For datetime (meetings), use 24-hour format: 1 PM = 13:00, 7 PM = 19:00, 12 PM = 12:00, 12 AM = 00:00. Example: 7:30 PM on 2026-02-19 is "datetime": "2026-02-19T19:30:00".
- If the user gives a time without AM/PM (e.g. "at 3"), ask: "Just to confirm, is that 3 AM or 3 PM?" before confirming.
- Keep responses concise — this is a voice conversation.
- NEVER include a ###SCHEDULE block until the user has explicitly confirmed the details.
- Be conversational and natural.

Deleting events (only when the user asks):
- Do NOT suggest or offer to delete events. Only react when the user says they want to remove, delete, or cancel a calendar event.
- When the user asks to delete, ask for: 1) Full date (day, month, year), 2) The title of the event.
- Use today's date {current_date} to resolve relative phrases. After they confirm, output:
  ###DELETE{{"date": "<YYYY-MM-DD>", "title": "<title>"}}DELETE###
"""


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


def _get_system_prompt() -> str:
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    current_day_name = now.strftime("%A")
    return SYSTEM_PROMPT_TEMPLATE.format(
        current_date=current_date,
        current_day_name=current_day_name,
    )


SCHEDULE_PATTERN = re.compile(
    r"###SCHEDULE\s*(\{.*?\})\s*SCHEDULE###",
    re.DOTALL,
)


def _extract_schedule_block(response_text: str) -> dict[str, Any] | None:
    """Extract JSON from ###SCHEDULE{...}SCHEDULE###. Returns parsed dict or None.
    Supports type "meeting" (datetime, duration_minutes) and type "event" (date).
    Backward compatible: if type missing but datetime present, treat as meeting with 30 min.
    """
    m = SCHEDULE_PATTERN.search(response_text)
    if not m:
        return None
    raw_json = m.group(1).strip()
    try:
        data = json.loads(raw_json)
        if not isinstance(data, dict) or "name" not in data or "title" not in data:
            logger.warning("SCHEDULE block missing required keys name/title: %s", data)
            return None
        event_type = (data.get("type") or "").strip().lower()
        if event_type == "event":
            if "date" not in data:
                logger.warning("SCHEDULE block type=event missing date: %s", data)
                return None
            return data
        # meeting or legacy (datetime + title + name without type)
        if "datetime" in data:
            if "duration_minutes" not in data:
                data = {**data, "duration_minutes": 30}
            if "type" not in data:
                data = {**data, "type": "meeting"}
            return data
        logger.warning("SCHEDULE block missing datetime (meeting) or date (event): %s", data)
        return None
    except json.JSONDecodeError as e:
        logger.exception("Failed to parse SCHEDULE JSON: raw=%r, error=%s", raw_json, e)
        return None


def _remove_schedule_block(response_text: str) -> str:
    """Remove the ###SCHEDULE...SCHEDULE### block from the response."""
    return SCHEDULE_PATTERN.sub("", response_text).strip()


DELETE_PATTERN = re.compile(
    r"###DELETE\s*(\{.*?\})\s*DELETE###",
    re.DOTALL,
)


def _extract_delete_block(response_text: str) -> dict[str, Any] | None:
    """Extract JSON from ###DELETE{...}DELETE###. Returns parsed dict or None."""
    m = DELETE_PATTERN.search(response_text)
    if not m:
        return None
    raw_json = m.group(1).strip()
    try:
        data = json.loads(raw_json)
        if isinstance(data, dict) and "date" in data and "title" in data:
            return data
        logger.warning("DELETE block missing required keys: %s", data)
        return None
    except json.JSONDecodeError as e:
        logger.exception("Failed to parse DELETE JSON: raw=%r, error=%s", raw_json, e)
        return None


def _remove_delete_block(response_text: str) -> str:
    """Remove the ###DELETE...DELETE### block from the response."""
    return DELETE_PATTERN.sub("", response_text).strip()


@app.get("/")
def health():
    logger.info("GET / health check")
    return {"status": "ok"}


@app.get("/api/init")
def init_greeting():
    logger.info("GET /api/init")
    greeting = (
        "Hello! 👋 Welcome to your Google Calendar assistant. What's your name?"
    )
    return {"greeting": greeting}


@app.post("/api/chat")
def chat(request: ChatRequest):
    messages = request.messages
    n = len(messages)
    logger.info("POST /api/chat received, message_count=%d", n)
    if n > 0:
        logger.debug("Last user message: %s", messages[-1].content[:200] if messages[-1].role == "user" else "(assistant)")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        raise HTTPException(status_code=500, detail="Server configuration error")

    system_prompt = _get_system_prompt()
    anthropic_messages = [{"role": m.role, "content": m.content} for m in messages]

    logger.info("Calling Anthropic API: model=%s, message_count=%d", ANTHROPIC_MODEL, len(anthropic_messages))
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=anthropic_messages,
        )
    except Exception as e:
        logger.exception("Anthropic API call failed: %s", e)
        raise HTTPException(status_code=502, detail="Assistant temporarily unavailable")

    response_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            response_text += block.text

    usage = getattr(response, "usage", None)
    if usage:
        logger.info(
            "Anthropic response: input_tokens=%s, output_tokens=%s, response_length=%d",
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
            len(response_text),
        )
    else:
        logger.info("Anthropic response received, response_length=%d", len(response_text))

    schedule_data = _extract_schedule_block(response_text)
    event_created = False
    event_link = None
    event_deleted = False

    if schedule_data:
        name = schedule_data.get("name", "")
        title = schedule_data.get("title", "")
        schedule_type = (schedule_data.get("type") or "meeting").strip().lower()

        if schedule_type == "event":
            date_str = schedule_data.get("date", "")
            event_kind = schedule_data.get("event_type") or "event"
            logger.info("SCHEDULE block extracted (event): name=%r, date=%r, title=%r", name, date_str, title)
            description = f"{event_kind.capitalize()} added by Voice Assistant for {name}"
            result = calendar_create_event(
                summary=title,
                description=description,
                timezone=CALENDAR_TIMEZONE,
                date=date_str,
            )
        else:
            dt_str = schedule_data.get("datetime", "")
            duration_minutes = schedule_data.get("duration_minutes", 30)
            if isinstance(duration_minutes, (int, float)):
                duration_minutes = max(1, min(480, int(duration_minutes)))
            else:
                duration_minutes = 30
            logger.info(
                "SCHEDULE block extracted (meeting): name=%r, datetime=%r, title=%r, duration_minutes=%s",
                name,
                dt_str,
                title,
                duration_minutes,
            )
            description = f"Meeting scheduled by Voice Assistant for {name}"
            result = calendar_create_event(
                summary=title,
                description=description,
                timezone=CALENDAR_TIMEZONE,
                start_datetime_str=dt_str,
                duration_minutes=duration_minutes,
            )

        if result.get("success"):
            event_created = True
            event_link = result.get("event_link")
            logger.info("POST /api/chat returning with event_created=true, event_link=%s", event_link)
        else:
            logger.error("Calendar create_event failed: %s", result.get("error"))
            response_text = _remove_schedule_block(response_text)
            response_text += " I had trouble creating the calendar event. Please try again or add it manually."
        if event_created:
            response_text = _remove_schedule_block(response_text)
    else:
        logger.info("POST /api/chat returning with event_created=false")

    delete_data = _extract_delete_block(response_text)
    if delete_data:
        date_str = delete_data.get("date", "")
        title = delete_data.get("title", "")
        logger.info("DELETE block extracted: date=%r, title=%r", date_str, title)
        result = calendar_delete_event(date_str=date_str, title=title, timezone=CALENDAR_TIMEZONE)
        if result.get("success"):
            event_deleted = True
            response_text = _remove_delete_block(response_text)
            logger.info("POST /api/chat returning with event_deleted=true")
        else:
            logger.error("Calendar delete failed: %s", result.get("error"))
            response_text = _remove_delete_block(response_text)
            response_text += " I couldn't find that event to remove."
    else:
        logger.info("POST /api/chat returning with event_deleted=false")

    return {
        "reply": response_text,
        "event_created": event_created,
        "event_link": event_link,
        "event_deleted": event_deleted,
    }
