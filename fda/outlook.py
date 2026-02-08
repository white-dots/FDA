"""
Outlook Calendar integration via Microsoft Graph API.

Uses MSAL for OAuth authentication and Microsoft Graph API for calendar access.
Users simply log in with their Office 365 account - no configuration required.
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timedelta
from urllib.parse import urljoin

from fda.config import OUTLOOK_API_ENDPOINT, DATA_DIR

logger = logging.getLogger(__name__)

# Token cache file for persistent login
TOKEN_CACHE_FILE = DATA_DIR / ".outlook_token_cache.json"


class OutlookCalendar:
    """
    Interface to Microsoft Outlook calendar via Microsoft Graph API.

    Handles OAuth authentication and calendar event retrieval.
    Users simply run authenticate() and log in with their Office 365 account.
    """

    GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
    SCOPES = ["Calendars.Read", "Calendars.ReadWrite", "User.Read"]

    # Pre-registered multi-tenant app for FDA system
    # This allows any Office 365 user to log in without app registration
    # To use your own app, set FDA_OUTLOOK_CLIENT_ID environment variable
    DEFAULT_CLIENT_ID = "YOUR_REGISTERED_APP_CLIENT_ID"  # Replace after registering
    DEFAULT_TENANT = "common"  # "common" allows any Microsoft account

    def __init__(
        self,
        client_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        """
        Initialize the Outlook Calendar client.

        For most users, no arguments needed - just call authenticate().

        Args:
            client_id: Optional custom Azure app client ID.
            tenant_id: Optional tenant ID (default: "common" for any account).
            client_secret: Client secret (only for service principal auth).
        """
        import os

        self.client_id = client_id or os.environ.get("FDA_OUTLOOK_CLIENT_ID") or self.DEFAULT_CLIENT_ID
        self.tenant_id = tenant_id or os.environ.get("FDA_OUTLOOK_TENANT_ID") or self.DEFAULT_TENANT
        self.client_secret = client_secret
        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None

        self._msal_app = None
        self._account = None
        self._token_cache = None

    def _get_token_cache(self) -> Any:
        """Get or create a persistent token cache."""
        if self._token_cache is not None:
            return self._token_cache

        try:
            import msal
        except ImportError:
            raise ImportError(
                "MSAL is required for OutlookCalendar. "
                "Install it with: pip install msal"
            )

        self._token_cache = msal.SerializableTokenCache()

        # Load existing cache if available
        if TOKEN_CACHE_FILE.exists():
            try:
                self._token_cache.deserialize(TOKEN_CACHE_FILE.read_text())
                logger.debug("Loaded token cache from disk")
            except Exception as e:
                logger.warning(f"Could not load token cache: {e}")

        return self._token_cache

    def _save_token_cache(self) -> None:
        """Save token cache to disk for persistent login."""
        if self._token_cache and self._token_cache.has_state_changed:
            try:
                TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                TOKEN_CACHE_FILE.write_text(self._token_cache.serialize())
                # Secure the file (read/write for owner only)
                TOKEN_CACHE_FILE.chmod(0o600)
                logger.debug("Saved token cache to disk")
            except Exception as e:
                logger.warning(f"Could not save token cache: {e}")

    def _get_msal_app(self) -> Any:
        """Get or create the MSAL application instance."""
        if self._msal_app is not None:
            return self._msal_app

        try:
            import msal
        except ImportError:
            raise ImportError(
                "MSAL is required for OutlookCalendar. "
                "Install it with: pip install msal"
            )

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        cache = self._get_token_cache()

        if self.client_secret:
            # Confidential client (service principal)
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=authority,
                token_cache=cache,
            )
        else:
            # Public client (interactive auth)
            self._msal_app = msal.PublicClientApplication(
                client_id=self.client_id,
                authority=authority,
                token_cache=cache,
            )

        return self._msal_app

    def authenticate(self, force_new: bool = False) -> bool:
        """
        Authenticate with Microsoft Graph API.

        For regular users: Opens a browser-based login flow. You'll see a code
        to enter at microsoft.com/devicelogin, then log in with your Office 365
        credentials. After first login, you stay logged in.

        Args:
            force_new: If True, ignore cached credentials and force new login.

        Returns:
            True if authentication successful, False otherwise.
        """
        try:
            app = self._get_msal_app()

            if self.client_secret:
                # Service principal authentication
                result = app.acquire_token_for_client(
                    scopes=["https://graph.microsoft.com/.default"]
                )
            else:
                # Try to get token from cache first (silent login)
                if not force_new:
                    accounts = app.get_accounts()
                    if accounts:
                        result = app.acquire_token_silent(
                            scopes=self.SCOPES,
                            account=accounts[0],
                        )
                        if result and "access_token" in result:
                            self._set_token(result)
                            self._save_token_cache()
                            user = accounts[0].get("username", "user")
                            print(f"✓ Logged in as {user}")
                            return True

                # Interactive device flow - user logs in via browser
                flow = app.initiate_device_flow(scopes=self.SCOPES)
                if "user_code" not in flow:
                    logger.error(f"Failed to create device flow: {flow}")
                    return False

                print("\n" + "=" * 50)
                print("OFFICE 365 LOGIN")
                print("=" * 50)
                print(f"\n1. Open: {flow['verification_uri']}")
                print(f"2. Enter code: {flow['user_code']}")
                print("3. Log in with your Office 365 account")
                print("\nWaiting for you to complete login...\n")

                result = app.acquire_token_by_device_flow(flow)

            if "access_token" in result:
                self._set_token(result)
                self._save_token_cache()
                logger.info("Successfully authenticated with Microsoft Graph")
                print("✓ Successfully connected to Office 365 calendar!")
                return True
            else:
                error_msg = result.get("error_description", str(result))
                logger.error(f"Authentication failed: {error_msg}")
                print(f"✗ Login failed: {error_msg}")
                return False

        except Exception as e:
            logger.error(f"Authentication error: {e}")
            print(f"✗ Login error: {e}")
            return False

    def logout(self) -> None:
        """Log out and clear saved credentials."""
        self.access_token = None
        self.token_expires_at = None
        self._account = None
        self._msal_app = None

        if TOKEN_CACHE_FILE.exists():
            try:
                TOKEN_CACHE_FILE.unlink()
                print("✓ Logged out of Office 365 calendar")
            except Exception as e:
                logger.warning(f"Could not remove token cache: {e}")

    def is_logged_in(self) -> bool:
        """Check if user is currently logged in (has valid cached token)."""
        try:
            app = self._get_msal_app()
            accounts = app.get_accounts()
            return len(accounts) > 0
        except Exception:
            return False

    def _set_token(self, result: dict[str, Any]) -> None:
        """Store the access token from authentication result."""
        self.access_token = result.get("access_token")
        expires_in = result.get("expires_in", 3600)
        self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
        self._account = result.get("account")

    def _ensure_authenticated(self) -> None:
        """Ensure we have a valid access token."""
        if not self.access_token:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        if self.token_expires_at and datetime.now() >= self.token_expires_at:
            # Token expired, try to refresh
            if not self.authenticate():
                raise RuntimeError("Failed to refresh authentication token.")

    def get_events_today(self) -> list[dict[str, Any]]:
        """
        Get all events scheduled for today.

        Returns:
            List of event dictionaries.
        """
        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        return self.get_events_range(start_of_day, end_of_day)

    def get_upcoming_events(self, within_minutes: int = 45) -> list[dict[str, Any]]:
        """
        Get upcoming events within a specified time window.

        Args:
            within_minutes: Number of minutes to look ahead (default 45).

        Returns:
            List of event dictionaries.
        """
        now = datetime.now()
        end_time = now + timedelta(minutes=within_minutes)

        return self.get_events_range(now, end_time)

    def get_events_range(
        self,
        start: datetime,
        end: datetime,
        calendar_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Get events within a date/time range.

        Args:
            start: Start datetime.
            end: End datetime.
            calendar_id: Optional specific calendar ID (default is primary).

        Returns:
            List of event dictionaries.
        """
        self._ensure_authenticated()

        # Format dates for Graph API
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%S")

        if calendar_id:
            endpoint = f"/me/calendars/{calendar_id}/calendarView"
        else:
            endpoint = "/me/calendarView"

        params = {
            "startDateTime": start_str,
            "endDateTime": end_str,
            "$orderby": "start/dateTime",
            "$select": "id,subject,start,end,location,organizer,attendees,bodyPreview,isOnlineMeeting,onlineMeetingUrl",
        }

        response = self._make_request("GET", endpoint, params=params)

        if response and "value" in response:
            return self._parse_events(response["value"])

        return []

    def _parse_events(self, raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Parse raw Graph API events into a cleaner format.

        Args:
            raw_events: Raw event data from Graph API.

        Returns:
            List of parsed event dictionaries.
        """
        events = []

        for event in raw_events:
            parsed = {
                "id": event.get("id"),
                "subject": event.get("subject", "No Subject"),
                "start": event.get("start", {}).get("dateTime"),
                "end": event.get("end", {}).get("dateTime"),
                "timezone": event.get("start", {}).get("timeZone"),
                "location": event.get("location", {}).get("displayName"),
                "organizer": event.get("organizer", {}).get("emailAddress", {}).get("name"),
                "organizer_email": event.get("organizer", {}).get("emailAddress", {}).get("address"),
                "attendees": [
                    {
                        "name": att.get("emailAddress", {}).get("name"),
                        "email": att.get("emailAddress", {}).get("address"),
                        "response": att.get("status", {}).get("response"),
                    }
                    for att in event.get("attendees", [])
                ],
                "body_preview": event.get("bodyPreview"),
                "is_online": event.get("isOnlineMeeting", False),
                "online_meeting_url": event.get("onlineMeetingUrl"),
            }
            events.append(parsed)

        return events

    def get_event_details(self, event_id: str) -> dict[str, Any]:
        """
        Get detailed information about a specific event.

        Args:
            event_id: The event ID from the calendar.

        Returns:
            Event details dictionary.
        """
        self._ensure_authenticated()

        endpoint = f"/me/events/{event_id}"
        params = {
            "$select": "id,subject,body,start,end,location,organizer,attendees,importance,sensitivity,isOnlineMeeting,onlineMeeting,recurrence",
        }

        response = self._make_request("GET", endpoint, params=params)

        if response:
            return {
                "id": response.get("id"),
                "subject": response.get("subject"),
                "body": response.get("body", {}).get("content"),
                "body_type": response.get("body", {}).get("contentType"),
                "start": response.get("start", {}).get("dateTime"),
                "end": response.get("end", {}).get("dateTime"),
                "timezone": response.get("start", {}).get("timeZone"),
                "location": response.get("location", {}).get("displayName"),
                "organizer": response.get("organizer", {}).get("emailAddress", {}),
                "attendees": response.get("attendees", []),
                "importance": response.get("importance"),
                "sensitivity": response.get("sensitivity"),
                "is_online": response.get("isOnlineMeeting", False),
                "online_meeting": response.get("onlineMeeting"),
                "recurrence": response.get("recurrence"),
            }

        return {}

    def get_calendars(self) -> list[dict[str, Any]]:
        """
        Get list of user's calendars.

        Returns:
            List of calendar dictionaries.
        """
        self._ensure_authenticated()

        response = self._make_request("GET", "/me/calendars")

        if response and "value" in response:
            return [
                {
                    "id": cal.get("id"),
                    "name": cal.get("name"),
                    "color": cal.get("color"),
                    "can_edit": cal.get("canEdit", False),
                    "is_default": cal.get("isDefaultCalendar", False),
                }
                for cal in response["value"]
            ]

        return []

    def _make_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
    ) -> Any:
        """
        Make an authenticated request to Microsoft Graph API.

        Args:
            method: HTTP method (GET, POST, etc.).
            endpoint: API endpoint path.
            params: Query parameters.
            json_data: JSON body data.

        Returns:
            Response JSON or None if error.
        """
        try:
            import requests
        except ImportError:
            raise ImportError(
                "requests is required for OutlookCalendar. "
                "Install it with: pip install requests"
            )

        self._ensure_authenticated()

        url = urljoin(self.GRAPH_API_BASE, endpoint.lstrip("/"))

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data,
                timeout=30,
            )

            response.raise_for_status()
            return response.json()

        except requests.HTTPError as e:
            logger.error(f"Graph API HTTP error: {e}")
            if e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return None
        except requests.RequestException as e:
            logger.error(f"Graph API request failed: {e}")
            return None

    def create_event(
        self,
        subject: str,
        start: datetime,
        end: datetime,
        body: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[list[str]] = None,
        is_online: bool = False,
    ) -> Optional[dict[str, Any]]:
        """
        Create a new calendar event.

        Args:
            subject: Event subject/title.
            start: Start datetime.
            end: End datetime.
            body: Event body/description.
            location: Event location.
            attendees: List of attendee email addresses.
            is_online: Whether to create as online meeting.

        Returns:
            Created event details or None if failed.
        """
        self._ensure_authenticated()

        event_data: dict[str, Any] = {
            "subject": subject,
            "start": {
                "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "UTC",
            },
        }

        if body:
            event_data["body"] = {
                "contentType": "HTML",
                "content": body,
            }

        if location:
            event_data["location"] = {"displayName": location}

        if attendees:
            event_data["attendees"] = [
                {
                    "emailAddress": {"address": email},
                    "type": "required",
                }
                for email in attendees
            ]

        if is_online:
            event_data["isOnlineMeeting"] = True
            event_data["onlineMeetingProvider"] = "teamsForBusiness"

        return self._make_request("POST", "/me/events", json_data=event_data)

    def respond_to_event(
        self,
        event_id: str,
        response: str,
        comment: Optional[str] = None,
    ) -> bool:
        """
        Respond to a calendar event invitation.

        Args:
            event_id: The event ID.
            response: Response type ("accept", "tentativelyAccept", "decline").
            comment: Optional response comment.

        Returns:
            True if successful, False otherwise.
        """
        self._ensure_authenticated()

        endpoint = f"/me/events/{event_id}/{response}"
        data = {}
        if comment:
            data["comment"] = comment

        result = self._make_request("POST", endpoint, json_data=data or None)
        return result is not None
