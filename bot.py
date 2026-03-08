import os
import logging
from typing import List, Dict

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN in .env file")

if not JIRA_BASE_URL:
    raise ValueError("Missing JIRA_BASE_URL in .env file")

if not JIRA_EMAIL:
    raise ValueError("Missing JIRA_EMAIL in .env file")

if not JIRA_API_TOKEN:
    raise ValueError("Missing JIRA_API_TOKEN in .env file")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


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

    tickets = []
    for issue in issues:
        tickets.append(
            {
                "key": issue["key"],
                "summary": issue["fields"].get("summary", "(No summary)"),
                "status": issue["fields"].get("status", {}).get("name", "(No status)"),
            }
        )

    return tickets

def split_priority_tickets(tickets: List[Dict]) -> tuple[List[Dict], List[Dict]]:
    crm_tickets = []
    other_tickets = []

    for ticket in tickets:
        if ticket["key"].startswith("CRM-"):
            crm_tickets.append(ticket)
        else:
            other_tickets.append(ticket)

    return crm_tickets, other_tickets


def format_ticket_groups(tickets: List[Dict]) -> str:
    if not tickets:
        return "No assigned tickets found."

    crm_tickets, other_tickets = split_priority_tickets(tickets)

    lines = []

    if crm_tickets:
        lines.append("Priority CRM tickets:")
        lines.append("")
        for ticket in crm_tickets:
            lines.append(
                f"- {ticket['key']} - {ticket['summary']} [{ticket['status']}]"
            )

    if other_tickets:
        if lines:
            lines.append("")
            lines.append("")
        lines.append("Other tickets:")
        lines.append("")
        for ticket in other_tickets:
            lines.append(
                f"- {ticket['key']} - {ticket['summary']} [{ticket['status']}]"
            )

    return "\n".join(lines)


def format_ticket_list(tickets: List[Dict]) -> str:
    if not tickets:
        return "No assigned tickets found."

    lines = ["Assigned tickets:", ""]
    for ticket in tickets:
        lines.append(f"- {ticket['key']} - {ticket['summary']}")

    return "\n".join(lines)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hello! I'm Daily Work Assistant.\nUse /morning to get your assigned Jira tickets."
    )


async def morning_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        tickets = get_assigned_tickets()
        message = format_ticket_groups(tickets)
        await update.message.reply_text(message)
    except requests.HTTPError as e:
        logger.exception("Jira HTTP error")
        await update.message.reply_text(
            f"Failed to fetch Jira tickets.\nHTTP error: {e}"
        )
    except Exception as e:
        logger.exception("Unexpected error while fetching Jira tickets")
        await update.message.reply_text(
            f"Something went wrong while fetching Jira tickets.\nError: {e}"
        )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("morning", morning_command))

    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()