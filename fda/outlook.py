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
    # Base scopes that work with both personal and work/school accounts
    SCOPES = [
        "Calendars.Read",
        "Calendars.ReadWrite",
        "User.Read",
    ]
    # Additional scopes only available for work/school (Azure AD) accounts
    ORG_SCOPES = [
        "Sites.Read.All",
        "Files.Read.All",
    ]

    # Microsoft Graph PowerShell — well-known multi-tenant public client
    # Supports device code flow with both personal and org accounts
    # To use your own app, set FDA_OUTLOOK_CLIENT_ID environment variable
    DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
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

    # ========== SharePoint / OneDrive File Access ==========

    def search_files(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Search for files across the user's OneDrive and SharePoint using
        Microsoft Search API.

        Args:
            query: Search query string (keywords, phrases, etc.).
            max_results: Maximum number of results to return.

        Returns:
            List of file metadata dictionaries.
        """
        self._ensure_authenticated()

        # Use Microsoft Search API — searches across OneDrive + all SharePoint
        endpoint = "/search/query"
        search_body = {
            "requests": [
                {
                    "entityTypes": ["driveItem"],
                    "query": {"queryString": query},
                    "from": 0,
                    "size": max_results,
                    "fields": [
                        "id", "name", "webUrl", "lastModifiedDateTime",
                        "createdBy", "lastModifiedBy", "size",
                        "parentReference",
                    ],
                }
            ]
        }

        response = self._make_request("POST", endpoint, json_data=search_body)

        if not response or "value" not in response:
            return []

        return self._parse_search_results(response["value"])

    def _parse_search_results(
        self, search_response: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Parse Microsoft Search API response into clean file metadata."""
        files = []

        for result_set in search_response:
            hits = result_set.get("hitsContainers", [])
            for container in hits:
                for hit in container.get("hits", []):
                    resource = hit.get("resource", {})
                    summary = hit.get("summary", "")

                    # Extract drive and item IDs from the resource ID
                    # Resource ID format varies; we also get it from parentReference
                    parent_ref = resource.get("parentReference", {})

                    files.append({
                        "id": resource.get("id"),
                        "name": resource.get("name", "Unknown"),
                        "web_url": resource.get("webUrl"),
                        "last_modified": resource.get("lastModifiedDateTime"),
                        "size": resource.get("size"),
                        "created_by": (
                            resource.get("createdBy", {})
                            .get("user", {})
                            .get("displayName")
                        ),
                        "modified_by": (
                            resource.get("lastModifiedBy", {})
                            .get("user", {})
                            .get("displayName")
                        ),
                        "drive_id": parent_ref.get("driveId"),
                        "site_id": parent_ref.get("siteId"),
                        "hit_summary": summary,  # Search snippet with highlights
                    })

        return files

    def search_files_in_drive(
        self,
        query: str,
        drive_id: Optional[str] = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Search for files in a specific drive (OneDrive or SharePoint doc library).

        Args:
            query: Search query string.
            drive_id: Specific drive ID. If None, searches user's OneDrive.
            max_results: Maximum results to return.

        Returns:
            List of file metadata dictionaries.
        """
        self._ensure_authenticated()

        if drive_id:
            endpoint = f"/drives/{drive_id}/root/search(q='{query}')"
        else:
            endpoint = f"/me/drive/root/search(q='{query}')"

        params = {"$top": max_results}
        response = self._make_request("GET", endpoint, params=params)

        if not response or "value" not in response:
            return []

        results = []
        for item in response["value"]:
            parent_ref = item.get("parentReference", {})
            results.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "web_url": item.get("webUrl"),
                "last_modified": item.get("lastModifiedDateTime"),
                "size": item.get("size"),
                "mime_type": item.get("file", {}).get("mimeType"),
                "drive_id": parent_ref.get("driveId"),
                "parent_path": parent_ref.get("path"),
            })

        return results

    def get_file_content(
        self,
        item_id: str,
        drive_id: Optional[str] = None,
        max_size_mb: float = 10.0,
    ) -> Optional[bytes]:
        """
        Download the content of a file from OneDrive/SharePoint.

        Args:
            item_id: The item (file) ID.
            drive_id: The drive ID. If None, uses user's OneDrive.
            max_size_mb: Maximum file size to download in MB.

        Returns:
            File content as bytes, or None if failed.
        """
        try:
            import requests as req_lib
        except ImportError:
            raise ImportError("requests is required. Install with: pip install requests")

        self._ensure_authenticated()

        if drive_id:
            endpoint = f"/drives/{drive_id}/items/{item_id}/content"
        else:
            endpoint = f"/me/drive/items/{item_id}/content"

        url = f"{self.GRAPH_API_BASE}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }

        try:
            response = req_lib.get(url, headers=headers, timeout=60, stream=True)
            response.raise_for_status()

            # Check content length before downloading
            content_length = int(response.headers.get("Content-Length", 0))
            if content_length > max_size_mb * 1024 * 1024:
                logger.warning(
                    f"File too large ({content_length / 1024 / 1024:.1f} MB), "
                    f"skipping (max {max_size_mb} MB)"
                )
                return None

            return response.content

        except req_lib.HTTPError as e:
            logger.error(f"Failed to download file {item_id}: {e}")
            return None
        except req_lib.RequestException as e:
            logger.error(f"Request failed for file {item_id}: {e}")
            return None

    def get_file_text_content(
        self,
        item_id: str,
        drive_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Get the text content of a file. Handles common Office formats
        by extracting plain text.

        Supports: .txt, .csv, .json, .md, .html, .docx, .pptx, .xlsx, .pdf

        Args:
            item_id: The item (file) ID.
            drive_id: The drive ID. If None, uses user's OneDrive.

        Returns:
            Extracted text content, or None if failed/unsupported.
        """
        # First get file metadata to determine type
        self._ensure_authenticated()

        if drive_id:
            endpoint = f"/drives/{drive_id}/items/{item_id}"
        else:
            endpoint = f"/me/drive/items/{item_id}"

        metadata = self._make_request("GET", endpoint, params={"$select": "name,size,file"})
        if not metadata:
            return None

        file_name = metadata.get("name", "")
        mime_type = metadata.get("file", {}).get("mimeType", "")
        file_ext = Path(file_name).suffix.lower()

        # For plain text formats, download directly
        if file_ext in (".txt", ".csv", ".json", ".md", ".html", ".xml", ".log"):
            content = self.get_file_content(item_id, drive_id)
            if content:
                try:
                    return content.decode("utf-8")
                except UnicodeDecodeError:
                    return content.decode("utf-8", errors="replace")
            return None

        # For Office documents, try to extract text
        content = self.get_file_content(item_id, drive_id)
        if not content:
            return None

        return self._extract_text_from_binary(content, file_ext, file_name)

    def _extract_text_from_binary(
        self,
        content: bytes,
        file_ext: str,
        file_name: str,
    ) -> Optional[str]:
        """
        Extract text from binary file formats (docx, pptx, xlsx, pdf).

        Args:
            content: Raw file bytes.
            file_ext: File extension (e.g. '.docx').
            file_name: Original file name.

        Returns:
            Extracted text or None.
        """
        import io

        if file_ext == ".docx":
            return self._extract_docx_text(io.BytesIO(content))
        elif file_ext == ".pptx":
            return self._extract_pptx_text(io.BytesIO(content))
        elif file_ext == ".xlsx":
            return self._extract_xlsx_text(io.BytesIO(content))
        elif file_ext == ".pdf":
            return self._extract_pdf_text(io.BytesIO(content))
        else:
            logger.info(f"Unsupported file type for text extraction: {file_ext} ({file_name})")
            return None

    @staticmethod
    def _extract_docx_text(file_obj: Any) -> Optional[str]:
        """Extract text from a .docx file."""
        try:
            import zipfile
            import xml.etree.ElementTree as ET

            with zipfile.ZipFile(file_obj) as zf:
                if "word/document.xml" not in zf.namelist():
                    return None
                xml_content = zf.read("word/document.xml")
                tree = ET.fromstring(xml_content)
                # Extract all text nodes
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paragraphs = []
                for p in tree.iter(f"{{{ns['w']}}}p"):
                    texts = [t.text for t in p.iter(f"{{{ns['w']}}}t") if t.text]
                    if texts:
                        paragraphs.append("".join(texts))
                return "\n".join(paragraphs)
        except Exception as e:
            logger.warning(f"Failed to extract docx text: {e}")
            return None

    @staticmethod
    def _extract_pptx_text(file_obj: Any) -> Optional[str]:
        """Extract text from a .pptx file."""
        try:
            import zipfile
            import xml.etree.ElementTree as ET

            slides_text = []
            with zipfile.ZipFile(file_obj) as zf:
                slide_files = sorted(
                    [f for f in zf.namelist() if f.startswith("ppt/slides/slide") and f.endswith(".xml")]
                )
                for slide_file in slide_files:
                    xml_content = zf.read(slide_file)
                    tree = ET.fromstring(xml_content)
                    # Extract all text from the slide
                    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
                    texts = [t.text for t in tree.iter(f"{{{ns['a']}}}t") if t.text]
                    if texts:
                        slide_num = slide_file.split("slide")[-1].replace(".xml", "")
                        slides_text.append(f"[Slide {slide_num}]\n" + "\n".join(texts))

            return "\n\n".join(slides_text) if slides_text else None
        except Exception as e:
            logger.warning(f"Failed to extract pptx text: {e}")
            return None

    @staticmethod
    def _extract_xlsx_text(file_obj: Any) -> Optional[str]:
        """Extract text from a .xlsx file (first sheet, headers + sample rows)."""
        try:
            import zipfile
            import xml.etree.ElementTree as ET

            with zipfile.ZipFile(file_obj) as zf:
                # Read shared strings
                shared_strings = []
                if "xl/sharedStrings.xml" in zf.namelist():
                    ss_xml = zf.read("xl/sharedStrings.xml")
                    ss_tree = ET.fromstring(ss_xml)
                    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                    for si in ss_tree.iter(f"{{{ns['s']}}}si"):
                        texts = [t.text or "" for t in si.iter(f"{{{ns['s']}}}t")]
                        shared_strings.append("".join(texts))

                # Read first sheet
                if "xl/worksheets/sheet1.xml" not in zf.namelist():
                    return None
                sheet_xml = zf.read("xl/worksheets/sheet1.xml")
                sheet_tree = ET.fromstring(sheet_xml)
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

                rows_text = []
                for row in list(sheet_tree.iter(f"{{{ns['s']}}}row"))[:20]:  # First 20 rows
                    cells = []
                    for cell in row.iter(f"{{{ns['s']}}}c"):
                        cell_type = cell.get("t", "")
                        value_el = cell.find(f"{{{ns['s']}}}v")
                        if value_el is not None and value_el.text:
                            if cell_type == "s":
                                idx = int(value_el.text)
                                cells.append(shared_strings[idx] if idx < len(shared_strings) else "")
                            else:
                                cells.append(value_el.text)
                        else:
                            cells.append("")
                    rows_text.append(" | ".join(cells))

                return "\n".join(rows_text) if rows_text else None
        except Exception as e:
            logger.warning(f"Failed to extract xlsx text: {e}")
            return None

    @staticmethod
    def _extract_pdf_text(file_obj: Any) -> Optional[str]:
        """Extract text from a PDF file. Requires PyPDF2 or falls back to basic extraction."""
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(file_obj)
            pages_text = []
            for i, page in enumerate(reader.pages[:20]):  # First 20 pages
                text = page.extract_text()
                if text and text.strip():
                    pages_text.append(f"[Page {i + 1}]\n{text.strip()}")
            return "\n\n".join(pages_text) if pages_text else None
        except ImportError:
            logger.info("PyPDF2 not installed — skipping PDF text extraction")
            return None
        except Exception as e:
            logger.warning(f"Failed to extract PDF text: {e}")
            return None

    def search_files_for_meeting(
        self,
        event_details: dict[str, Any],
        max_files: int = 5,
        max_text_length: int = 3000,
    ) -> list[dict[str, Any]]:
        """
        Search SharePoint/OneDrive for files relevant to a meeting.

        Builds search queries from the meeting subject, body, and attendee names,
        then returns file metadata + extracted text summaries.

        Args:
            event_details: Event details dict from get_event_details().
            max_files: Maximum number of files to retrieve content for.
            max_text_length: Max characters of text to extract per file.

        Returns:
            List of dicts with file metadata and extracted text content.
        """
        subject = event_details.get("subject", "")
        body_preview = event_details.get("body_preview") or event_details.get("body", "")
        attendees = event_details.get("attendees", [])

        if not subject:
            return []

        # Build search queries from meeting context
        queries = self._build_meeting_search_queries(subject, body_preview, attendees)

        # Search and deduplicate results
        seen_ids: set[str] = set()
        all_results: list[dict[str, Any]] = []

        for query in queries:
            try:
                results = self.search_files(query, max_results=5)
                for r in results:
                    file_id = r.get("id")
                    if file_id and file_id not in seen_ids:
                        seen_ids.add(file_id)
                        all_results.append(r)
            except Exception as e:
                logger.warning(f"SharePoint search failed for query '{query}': {e}")

        # Sort by last_modified (most recent first) and limit
        all_results.sort(
            key=lambda x: x.get("last_modified", ""),
            reverse=True,
        )
        top_results = all_results[:max_files]

        # Extract text content for top results
        enriched = []
        for file_meta in top_results:
            file_id = file_meta.get("id")
            drive_id = file_meta.get("drive_id")

            text_content = None
            try:
                text_content = self.get_file_text_content(file_id, drive_id)
            except Exception as e:
                logger.warning(f"Could not extract text from {file_meta.get('name')}: {e}")

            enriched.append({
                **file_meta,
                "text_content": (
                    text_content[:max_text_length] + "..." if text_content and len(text_content) > max_text_length
                    else text_content
                ),
            })

        return enriched

    @staticmethod
    def _build_meeting_search_queries(
        subject: str,
        body: str,
        attendees: list[dict[str, Any]],
    ) -> list[str]:
        """
        Build search queries from meeting metadata.

        Generates multiple queries to maximize relevant file discovery:
        1. Full subject line
        2. Key terms from subject (removing common words)
        3. Attendee names (people often share files with their name)

        Args:
            subject: Meeting subject.
            body: Meeting body/description.
            attendees: List of attendee dicts.

        Returns:
            List of search query strings (deduplicated).
        """
        queries = []

        # 1. Full subject as a query
        if subject:
            queries.append(subject)

        # 2. Extract key terms from subject (drop noise words)
        noise_words = {
            "meeting", "call", "sync", "catchup", "catch-up", "check-in",
            "checkin", "discussion", "review", "update", "weekly", "daily",
            "monthly", "bi-weekly", "standup", "stand-up", "1:1", "1on1",
            "the", "a", "an", "and", "or", "for", "with", "about", "on",
            "in", "at", "to", "of", "re", "fwd", "fw",
        }
        if subject:
            key_terms = [
                w for w in subject.split()
                if w.lower().strip(":-/()[]") not in noise_words
                and len(w) > 2
            ]
            if key_terms and " ".join(key_terms) != subject:
                queries.append(" ".join(key_terms))

        # 3. Search by attendee names (top 3, skip the organizer if many)
        attendee_names = [
            a.get("name") or a.get("email", "").split("@")[0]
            for a in attendees[:3]
            if a.get("name") or a.get("email")
        ]
        for name in attendee_names:
            if name and subject:
                # Combine attendee name with key subject terms for relevance
                short_subject = " ".join(key_terms[:2]) if key_terms else subject.split()[0]
                queries.append(f"{name} {short_subject}")

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_queries = []
        for q in queries:
            q_lower = q.lower().strip()
            if q_lower and q_lower not in seen:
                seen.add(q_lower)
                unique_queries.append(q)

        return unique_queries[:5]  # Cap at 5 queries to avoid rate limits
