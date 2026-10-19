"""Operator commands: python -m app.core.meeting_google {connect,sync,status,keygen}."""

import argparse
import asyncio
import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.clients import create_google_client
from app.db.models import MeetingGoogleConnection
from app.db.session import SessionFactory, engine
from app.env import get_settings
from app.integrations.google_meet_calendar import CalendarError, connect, oauth_request, sync


def receive_code(settings, open_browser=True):
    """Desktop OAuth loopback callback, one process, ten-minute state lifetime."""
    result = {}
    expected_state = ""

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log URLs carrying an authorization code.

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            valid = (
                parsed.path == "/callback"
                and len(query.get("state", [])) == 1
                and secrets.compare_digest(query["state"][0], expected_state)
            )
            if not valid:
                self.send_error(400, "Invalid OAuth callback")
                return
            if "error" in query:
                result["error"] = "Google authorization was declined or failed."
            elif len(query.get("code", [])) == 1:
                result["code"] = query["code"][0]
            else:
                self.send_error(400, "Missing authorization code")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Authorization received. Return to the terminal for the result.")

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        url, expected_state, verifier = oauth_request(settings, redirect_uri)
        print("Open this URL on this computer to connect the configured bot account:\n" + url)
        if open_browser:
            webbrowser.open(url)
        deadline = time.monotonic() + 600
        server.timeout = 1
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise CalendarError("Authorization timed out; run connect again.")
    if "error" in result:
        raise CalendarError(result["error"])
    return result["code"], verifier, redirect_uri


async def run(command, no_browser=False):
    settings = get_settings()
    try:
        credentials = None
        if command == "connect":
            credentials = await asyncio.to_thread(receive_code, settings, not no_browser)
        async with SessionFactory() as db, create_google_client() as client:
            if command == "connect":
                await connect(db, client, settings, *credentials)
                print("Bot account connected. Calendar access is read-only.")
            elif command == "sync":
                print(json.dumps(await sync(db, client, settings)))
            else:
                connection = await db.scalar(select(MeetingGoogleConnection))
                print(
                    json.dumps(
                        {"status": connection.status, "email": connection.email}
                        if connection
                        else {"status": "not_connected"}
                    )
                )
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["connect", "sync", "status", "keygen"])
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.command == "keygen":
        print(Fernet.generate_key().decode())
        return
    try:
        asyncio.run(run(args.command, args.no_browser))
    except CalendarError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
