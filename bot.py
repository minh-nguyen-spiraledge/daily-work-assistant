import os
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Optional

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


# --------------------------------------------------
# ENV CONFIG
# --------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

GOOGLE_CHAT_SPACE = os.getenv("GOOGLE_CHAT_SPACE")
GOOGLE_CHAT_USER_ID = os.getenv("GOOGLE_CHAT_USER_ID")

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN in .env file")

if not JIRA_BASE_URL:
    raise ValueError("Missing JIRA_BASE_URL in .env file")

if not JIRA_EMAIL:
    raise ValueError("Missing JIRA_EMAIL in .env file")

if not JIRA_API_TOKEN:
    raise ValueError("Missing JIRA_API_TOKEN in .env file")

if not GOOGLE_CHAT_SPACE:
    raise ValueError("Missing GOOGLE_CHAT_SPACE in .env file")

if not GOOGLE_CHAT_USER_ID:
    raise ValueError("Missing GOOGLE_CHAT_USER_ID in .env file")

GOOGLE_CHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.messages.create",
]


# --------------------------------------------------
# LOGGING
# --------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------
# USER STATE (in-memory)
# --------------------------------------------------

USER_STATE: Dict[int, Dict[str, object]] = {}


def get_user_id(update: Update) -> int:
    return update.effective_user.id


def ensure_user_state(user_id: int) -> None:
    if user_id not in USER_STATE:
        USER_STATE[user_id] = {
            "last_fetched_tickets": [],
            "selected_tickets": [],
            "morning_selected_keys": [],
            "last_chat_message_name": None,
            "last_chat_message_text": None,
            "last_chat_checked_at": None,
        }


# --------------------------------------------------
# JIRA API
# --------------------------------------------------

def get_assigned_tickets() -> List[Dict]:
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    payload = {
        "jql": (
            'assignee = currentUser() '
            'AND status IN ("New", "Approved for dev", "In Development", "Developed") '
            'ORDER BY updated DESC'
        ),
        "maxResults": 20,
        "fields": ["summary", "status"],
    }

    response = requests.post(
        url,
        json=payload,
        headers=headers,
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    issues = data.get("issues", [])

    tickets: List[Dict] = []
    for issue in issues:
        tickets.append(
            {
                "key": issue["key"],
                "summary": issue["fields"].get("summary", "(No summary)"),
                "status": issue["fields"].get("status", {}).get("name", "(No status)"),
            }
        )

    return tickets


# --------------------------------------------------
# GOOGLE CHAT API
# --------------------------------------------------

def get_google_chat_credentials():
    creds = None

    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file(
            "token.json",
            GOOGLE_CHAT_SCOPES,
        )

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            "credentials.json",
            GOOGLE_CHAT_SCOPES,
        )
        creds = flow.run_local_server(port=0)

        with open("token.json", "w") as token_file:
            token_file.write(creds.to_json())

    return creds


def get_google_chat_service():
    creds = get_google_chat_credentials()
    return build("chat", "v1", credentials=creds)


def build_recent_chat_filter_from_hours(hours: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return build_recent_chat_filter_from_datetime(since)


def build_recent_chat_filter_from_datetime(since: datetime) -> str:
    since_rfc3339 = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return f'createTime > "{since_rfc3339}"'


def get_chat_lookup_hours(now: Optional[datetime] = None) -> int:
    current = now or datetime.now()

    # Monday
    if current.weekday() == 0:
        return 96

    return 48


def list_recent_google_chat_messages(
    chat_filter: str,
    page_size: int = 20,
    page_token: Optional[str] = None,
) -> Tuple[List[Dict], Optional[str]]:
    service = get_google_chat_service()

    response = (
        service.spaces()
        .messages()
        .list(
            parent=GOOGLE_CHAT_SPACE,
            pageSize=page_size,
            filter=chat_filter,
            pageToken=page_token,
        )
        .execute()
    )

    messages = response.get("messages", [])
    next_page_token = response.get("nextPageToken")

    return messages, next_page_token


def find_latest_message_from_me(messages: List[Dict]) -> Optional[Dict]:
    for message in reversed(messages):
        sender = message.get("sender", {})
        sender_id = sender.get("name")

        if sender_id == GOOGLE_CHAT_USER_ID:
            return message

    return None


def get_latest_message_from_me_with_cache(user_id: int) -> Optional[Dict]:
    ensure_user_state(user_id)

    lookup_hours = get_chat_lookup_hours()
    latest_message = find_latest_message_from_me_paginated(
        hours=lookup_hours,
        page_size=20,
        max_pages=5,
    )

    if latest_message:
        USER_STATE[user_id]["last_chat_message_name"] = latest_message.get("name")
        USER_STATE[user_id]["last_chat_message_text"] = latest_message.get("text", "")
        USER_STATE[user_id]["last_chat_checked_at"] = datetime.now(timezone.utc).isoformat()
        return latest_message

    cached_name = USER_STATE[user_id]["last_chat_message_name"]
    cached_text = USER_STATE[user_id]["last_chat_message_text"]

    if cached_name and cached_text:
        return {
            "name": cached_name,
            "text": cached_text,
        }

    return None

def find_latest_message_from_me_paginated(
    hours: int,
    page_size: int = 20,
    max_pages: int = 5,
) -> Optional[Dict]:
    page_token = None
    pages_checked = 0
    chat_filter = build_recent_chat_filter_from_hours(hours)

    latest_match = None

    while pages_checked < max_pages:
        messages, next_page_token = list_recent_google_chat_messages(
            chat_filter=chat_filter,
            page_size=page_size,
            page_token=page_token,
        )

        if not messages:
            break

        match_in_page = find_latest_message_from_me(messages)

        if match_in_page:
            latest_match = match_in_page

        if not next_page_token:
            break

        page_token = next_page_token
        pages_checked += 1

    return latest_match

def send_google_chat_message(text: str) -> Dict:
    service = get_google_chat_service()

    body = {
        "text": text,
    }

    response = (
        service.spaces()
        .messages()
        .create(
            parent=GOOGLE_CHAT_SPACE,
            body=body,
        )
        .execute()
    )

    return response

# --------------------------------------------------
# TICKET RANKING
# --------------------------------------------------

STATUS_SCORES = {
    "in development": 40,
    "approved for dev": 30,
    "new": 20,
    "developed": 10,
}

PROJECT_PREFIX_PRIORITY = "CRM-"


def get_ticket_score(ticket: Dict) -> int:
    score = 0

    key = ticket.get("key", "")
    status = ticket.get("status", "").strip().lower()

    if key.startswith(PROJECT_PREFIX_PRIORITY):
        score += 100

    score += STATUS_SCORES.get(status, 0)

    return score


def rank_tickets(tickets: List[Dict]) -> List[Dict]:
    return sorted(
        tickets,
        key=lambda ticket: (-get_ticket_score(ticket), ticket["key"]),
    )


def split_suggested_tickets(
    tickets: List[Dict],
    top_n: int = 3,
) -> Tuple[List[Dict], List[Dict]]:
    ranked = rank_tickets(tickets)
    suggested = ranked[:top_n]
    remaining = ranked[top_n:]
    return suggested, remaining


# --------------------------------------------------
# FORMATTERS
# --------------------------------------------------

def format_suggested_tickets(tickets: List[Dict]) -> str:
    if not tickets:
        return "No assigned tickets found."

    suggested, remaining = split_suggested_tickets(tickets)

    lines = ["Suggested for today:", ""]

    for index, ticket in enumerate(suggested, start=1):
        score = get_ticket_score(ticket)
        lines.append(
            f"{index}. {ticket['key']} - {ticket['summary']} [{ticket['status']}] (score: {score})"
        )

    if remaining:
        lines.append("")
        lines.append("Other available tickets:")
        lines.append("")

        for ticket in remaining:
            score = get_ticket_score(ticket)
            lines.append(
                f"- {ticket['key']} - {ticket['summary']} [{ticket['status']}] (score: {score})"
            )

    return "\n".join(lines)


def format_ticket_report_line(ticket: Dict, percent: int = 0) -> str:
    return f"{ticket['key']} - {ticket['summary']} - {percent}%"


def format_morning_selector_text(tickets: List[Dict], selected_keys: List[str]) -> str:
    selected_count = len(selected_keys)
    lines = [
        "Select tickets for today:",
        f"Selected: {selected_count}/{len(tickets)}",
        "",
        "Tap ticket buttons to toggle. Press Confirm when done.",
    ]

    if selected_keys:
        lines.append("")
        lines.append("Current selection:")
        for key in selected_keys:
            lines.append(f"- {key}")

    return "\n".join(lines)


def build_morning_keyboard(tickets: List[Dict], selected_keys: List[str]) -> InlineKeyboardMarkup:
    selected_set = {key.upper() for key in selected_keys}
    rows: List[List[InlineKeyboardButton]] = []

    for ticket in rank_tickets(tickets):
        key = ticket["key"]
        checked = "✅" if key.upper() in selected_set else "☐"
        score = get_ticket_score(ticket)
        button_text = f"{checked} {key} ({score})"
        rows.append(
            [InlineKeyboardButton(button_text, callback_data=f"morning:toggle:{key}")]
        )

    rows.append(
        [
            InlineKeyboardButton("Confirm", callback_data="morning:confirm"),
            InlineKeyboardButton("Clear", callback_data="morning:clear"),
        ]
    )

    return InlineKeyboardMarkup(rows)


# --------------------------------------------------
# TICKET LOOKUP
# --------------------------------------------------

def find_tickets_by_keys(
    tickets: List[Dict],
    keys: List[str],
) -> Tuple[List[Dict], List[str]]:
    ticket_map = {ticket["key"].upper(): ticket for ticket in tickets}

    found: List[Dict] = []
    missing: List[str] = []

    for key in keys:
        normalized = key.upper()

        if normalized in ticket_map:
            found.append(ticket_map[normalized])
        else:
            missing.append(key)

    return found, missing


# --------------------------------------------------
# DAILY REPORT / PARSING
# --------------------------------------------------

def get_previous_workday_label(now: Optional[datetime] = None) -> str:
    current = now or datetime.now()

    # Monday
    if current.weekday() == 0:
        return "Last Friday's task"

    return "Yesterday's task"


def parse_todays_tasks_from_chat_text(message_text: str) -> List[str]:
    if not message_text:
        return []

    lines = [line.strip() for line in message_text.splitlines()]
    tasks: List[str] = []

    in_today_section = False

    for line in lines:
        normalized = line.lower().strip()
        normalized_plain = normalized.strip("* ").strip()

        # Case: Today: something...
        if normalized_plain.startswith("today:"):
            in_today_section = True
            after_colon = line.split(":", 1)[1].strip() if ":" in line else ""
            if after_colon:
                tasks.append(after_colon)
            continue

        # Case: Today's task / Today's tasks
        if (
            normalized_plain.startswith("today's task")
            or normalized_plain.startswith("today's tasks")
            or normalized_plain.startswith("today task")
            or normalized_plain.startswith("today tasks")
        ):
            in_today_section = True
            after_colon = line.split(":", 1)[1].strip() if ":" in line else ""
            if after_colon and after_colon.lower() not in {"task", "tasks"}:
                tasks.append(after_colon)
            continue

        # Stop if another section starts after we entered today's section
        if (
            normalized_plain.startswith("yesterday:")
            or normalized_plain.startswith("yesterday's task")
            or normalized_plain.startswith("yesterday's tasks")
            or normalized_plain.startswith("yesterday task")
            or normalized_plain.startswith("yesterday tasks")
        ):
            if in_today_section:
                break
            continue

        if in_today_section:
            if not line:
                continue

            cleaned = re.sub(r"^[\-\•\*]+\s*", "", line).strip()
            if cleaned:
                tasks.append(cleaned)

    return tasks


def get_yesterday_tasks_from_google_chat(user_id: int) -> List[str]:
    latest_message = get_latest_message_from_me_with_cache(user_id)

    if not latest_message:
        return ["(Could not find your recent message in Google Chat)"]

    message_text = latest_message.get("text", "")
    parsed_tasks = parse_todays_tasks_from_chat_text(message_text)

    if parsed_tasks:
        return parsed_tasks

    return ["(Could not parse Today's task from your latest Google Chat message)"]


def build_daily_report(user_id: int, selected_tickets: List[Dict]) -> str:
    previous_workday_label = get_previous_workday_label()
    yesterday_tasks = get_yesterday_tasks_from_google_chat(user_id)

    lines = [previous_workday_label]

    for task in yesterday_tasks:
        if task.startswith("- "):
            lines.append(task)
        else:
            lines.append(f"- {task}")

    lines.append("")
    lines.append("Today's task")

    if not selected_tickets:
        lines.append("- No tickets selected")
    else:
        for ticket in selected_tickets:
            lines.append(f"- {format_ticket_report_line(ticket, percent=100)}")

    return "\n".join(lines)


# --------------------------------------------------
# TELEGRAM COMMANDS
# --------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hello! I'm Daily Work Assistant.\n"
        "Commands:\n"
        "/morning\n"
        "/select CRM-123 CRM-456\n"
        "/selected\n"
        "/preview\n"
        "/submit\n"
        "/chat_test\n"
        "/my_last_chat\n"
        "/clearselected"
    )


async def morning_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        tickets = get_assigned_tickets()

        user_id = get_user_id(update)
        ensure_user_state(user_id)
        USER_STATE[user_id]["last_fetched_tickets"] = tickets

        logger.info("Fetched %s tickets for user %s", len(tickets), user_id)

        if not tickets:
            await update.message.reply_text("No assigned tickets found.")
            return

        previous_selected = USER_STATE[user_id]["selected_tickets"]
        available_keys = {ticket["key"].upper() for ticket in tickets}
        preselected = [
            ticket["key"]
            for ticket in previous_selected
            if ticket["key"].upper() in available_keys
        ]
        USER_STATE[user_id]["morning_selected_keys"] = preselected

        await update.message.reply_text(
            format_morning_selector_text(tickets, preselected),
            reply_markup=build_morning_keyboard(tickets, preselected),
        )

    except Exception as e:
        logger.exception("Error fetching Jira tickets")
        await update.message.reply_text(
            f"Failed to fetch Jira tickets.\nError: {e}"
        )


async def morning_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("morning:"):
        return

    user_id = query.from_user.id
    ensure_user_state(user_id)

    data = query.data
    action = data.split(":", 2)[1] if ":" in data else ""
    payload = data.split(":", 2)[2] if data.count(":") >= 2 else ""

    tickets = USER_STATE[user_id]["last_fetched_tickets"]
    if not tickets:
        await query.answer("No ticket list found. Run /morning first.", show_alert=True)
        return

    selected_keys = list(USER_STATE[user_id].get("morning_selected_keys", []))
    selected_set = {key.upper() for key in selected_keys}
    ticket_keys = {ticket["key"].upper(): ticket["key"] for ticket in tickets}

    if action == "toggle":
        key = payload.upper()
        if key not in ticket_keys:
            await query.answer("Ticket not found in current queue.", show_alert=True)
            return

        normalized_key = ticket_keys[key]
        if key in selected_set:
            selected_keys = [item for item in selected_keys if item.upper() != key]
            await query.answer(f"Unchecked {normalized_key}")
        else:
            selected_keys.append(normalized_key)
            await query.answer(f"Checked {normalized_key}")

        USER_STATE[user_id]["morning_selected_keys"] = selected_keys

        try:
            await query.edit_message_text(
                format_morning_selector_text(tickets, selected_keys),
                reply_markup=build_morning_keyboard(tickets, selected_keys),
            )
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
        return

    if action == "clear":
        USER_STATE[user_id]["morning_selected_keys"] = []

        await query.answer("Selection cleared")
        try:
            await query.edit_message_text(
                format_morning_selector_text(tickets, []),
                reply_markup=build_morning_keyboard(tickets, []),
            )
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
        return

    if action == "confirm":
        if not selected_keys:
            await query.answer("Select at least one ticket.", show_alert=True)
            return

        selected_set = {key.upper() for key in selected_keys}
        selected_tickets = [
            ticket for ticket in tickets if ticket["key"].upper() in selected_set
        ]

        USER_STATE[user_id]["selected_tickets"] = selected_tickets

        lines = ["Selected tickets for today:", ""]
        for ticket in selected_tickets:
            lines.append(f"- {format_ticket_report_line(ticket)}")

        await query.answer("Selection confirmed")
        await query.edit_message_text("\n".join(lines))
        return

    await query.answer()


async def select_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id(update)
    ensure_user_state(user_id)

    last_tickets = USER_STATE[user_id]["last_fetched_tickets"]

    if not last_tickets:
        await update.message.reply_text(
            "No ticket list found. Please run /morning first."
        )
        return

    selected_keys = context.args

    if not selected_keys:
        await update.message.reply_text("Usage: /select CRM-123 CRM-456")
        return

    found_tickets, missing_keys = find_tickets_by_keys(last_tickets, selected_keys)

    if not found_tickets:
        await update.message.reply_text(
            "None of the provided ticket keys were found."
        )
        return

    USER_STATE[user_id]["selected_tickets"] = found_tickets

    logger.info(
        "User %s selected tickets: %s",
        user_id,
        [ticket["key"] for ticket in found_tickets],
    )

    lines = ["Selected tickets for today:", ""]
    for ticket in found_tickets:
        lines.append(f"- {format_ticket_report_line(ticket)}")

    if missing_keys:
        lines.append("")
        lines.append("Not found:")
        for key in missing_keys:
            lines.append(f"- {key}")

    await update.message.reply_text("\n".join(lines))


async def selected_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id(update)
    ensure_user_state(user_id)

    selected = USER_STATE[user_id]["selected_tickets"]

    if not selected:
        await update.message.reply_text("No tickets selected yet.")
        return

    lines = ["Currently selected tickets:", ""]
    for ticket in selected:
        lines.append(f"- {format_ticket_report_line(ticket)}")

    await update.message.reply_text("\n".join(lines))


async def preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id(update)
    ensure_user_state(user_id)

    selected = USER_STATE[user_id]["selected_tickets"]

    if not selected:
        await update.message.reply_text("No tickets selected.\nUse /morning (checkbox) or /select first.")
        return

    try:
        report = build_daily_report(user_id, selected)
        await update.message.reply_text(report)
    except Exception as e:
        logger.exception("Error building preview")
        await update.message.reply_text(
            f"Failed to build preview.\nError: {e}"
        )


async def chat_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        lookup_hours = get_chat_lookup_hours()

        latest_mine = find_latest_message_from_me_paginated(
            hours=lookup_hours,
            page_size=20,
            max_pages=5,
        )

        lines = [f"Lookup window: last {lookup_hours} hours", ""]

        if latest_mine:
            lines.append("Latest message from you:")
            lines.append(latest_mine.get("text", "(no text)")[:1000])
            lines.append("")
            lines.append(f"createTime: {latest_mine.get('createTime', 'unknown')}")
            lines.append(f"sender: {latest_mine.get('sender', {}).get('name', 'unknown')}")
        else:
            lines.append("No recent message from you found in scanned pages.")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        logger.exception("chat_test failed")
        await update.message.reply_text(f"Error: {e}")


async def my_last_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = get_user_id(update)
        ensure_user_state(user_id)

        latest_message = get_latest_message_from_me_with_cache(user_id)

        if not latest_message:
            await update.message.reply_text("No recent message from you found.")
            return

        text = latest_message.get("text", "(no text)")
        await update.message.reply_text(text[:3500])

    except Exception as e:
        logger.exception("my_last_chat failed")
        await update.message.reply_text(f"Error: {e}")


async def clearselected_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id(update)
    ensure_user_state(user_id)

    USER_STATE[user_id]["selected_tickets"] = []
    await update.message.reply_text("Cleared selected tickets.")

async def chat_latest_5_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        lookup_hours = get_chat_lookup_hours()
        chat_filter = build_recent_chat_filter_from_hours(lookup_hours)

        page_token = None
        last_page_messages = []
        pages_checked = 0
        max_pages = 10

        while pages_checked < max_pages:
            messages, next_page_token = list_recent_google_chat_messages(
                chat_filter=chat_filter,
                page_size=20,
                page_token=page_token,
            )

            if messages:
                last_page_messages = messages

            if not next_page_token:
                break

            page_token = next_page_token
            pages_checked += 1

        if not last_page_messages:
            await update.message.reply_text("No messages found.")
            return

        # trong page cuối, API vẫn đang trả cũ -> mới
        latest_5 = list(reversed(last_page_messages[-5:]))

        lines = ["5 latest messages in the scanned window:", ""]

        for i, msg in enumerate(latest_5, start=1):
            sender = msg.get("sender", {}).get("name", "Unknown")
            create_time = msg.get("createTime", "Unknown time")
            text = msg.get("text", "(no text)").replace("\n", " ")

            lines.append(f"{i}. {sender}")
            lines.append(f"   time: {create_time}")
            lines.append(f"   text: {text[:120]}")
            lines.append("")

        await update.message.reply_text("\n".join(lines[:3500]))

    except Exception as e:
        logger.exception("chat_latest_5 failed")
        await update.message.reply_text(f"Error: {e}")


async def submit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id(update)
    ensure_user_state(user_id)

    selected = USER_STATE[user_id]["selected_tickets"]

    if not selected:
        await update.message.reply_text("No tickets selected.\nUse /morning (checkbox) or /select first.")
        return

    try:
        report = build_daily_report(user_id, selected)
        response = send_google_chat_message(report)

        message_name = response.get("name", "(unknown)")
        await update.message.reply_text(
            f"Submitted to Google Chat successfully.\nMessage: {message_name}"
        )

    except Exception as e:
        logger.exception("Error submitting report to Google Chat")
        await update.message.reply_text(
            f"Failed to submit report.\nError: {e}"
        )


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("morning", morning_command))
    app.add_handler(CallbackQueryHandler(morning_callback, pattern=r"^morning:"))
    app.add_handler(CommandHandler("select", select_command))
    app.add_handler(CommandHandler("selected", selected_command))
    app.add_handler(CommandHandler("preview", preview_command))
    app.add_handler(CommandHandler("chat_test", chat_test_command))
    app.add_handler(CommandHandler("my_last_chat", my_last_chat_command))
    app.add_handler(CommandHandler("clearselected", clearselected_command))
    app.add_handler(CommandHandler("chat_latest_5", chat_latest_5_command))
    app.add_handler(CommandHandler("submit", submit_command))

    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
