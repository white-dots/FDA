"""
SharePoint/OneDrive smoke test against user's M365 tenant.

Uses user-level scopes only (Files.Read) — no admin consent needed.
Tests against the user's OneDrive, which exercises the same code paths
that SharePoint document libraries use.

Tests:
  SP-1: Authenticate (Files.Read scope, user-consent)
  SP-2: Get user's OneDrive metadata
  SP-3: List recent files in OneDrive
  SP-4: Search files (Microsoft Search API for user's own content)
  SP-5: Download + extract text from a doc (.docx/.xlsx/.pdf)
  SP-6: Version history — '누가/언제/무엇 수정' promise
       (not in v1; implemented inline as proof-of-concept)

Run: python3 /tmp/sharepoint_smoke_test.py
"""
from __future__ import annotations

import json
import sys
from typing import Any, Optional

from fda.outlook import OutlookCalendar


def patch_scopes_to_user_level():
    """Use only user-consentable scopes (no admin consent needed)."""
    OutlookCalendar.SCOPES = [
        "User.Read",
        "Files.Read",            # user's own OneDrive — no admin consent
        "Calendars.Read",        # bonus, in case they want calendar test
    ]
    print(f"✓ Scopes set to user-level: {OutlookCalendar.SCOPES}")


def patch_graph_base_url():
    """
    Bug fix for v1 outlook.py: TWO inconsistent URL construction patterns
    cause SharePoint endpoints to fail.
      - line 462: urljoin(BASE, ep.lstrip('/')) — strips '/v1.0' if BASE has no trailing slash
      - line 741: f"{BASE}{ep}" — produces double slash if BASE has trailing slash and ep starts with '/'

    Fix: monkey-patch _make_request with a sane URL builder. To be cherry-picked
    into our fork as outlook.py PR.
    """
    import requests
    BASE = "https://graph.microsoft.com/v1.0"

    def _make_request(self, method, endpoint, params=None, json_data=None):
        self._ensure_authenticated()
        url = BASE + "/" + endpoint.lstrip("/")
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=json_data, timeout=30)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.HTTPError as e:
            print(f"Graph API HTTP error: {e}")
            print(f"Response: {resp.text}")
            return None
    OutlookCalendar._make_request = _make_request

    # Also patch get_file_content (line 741) for consistency
    orig_get_file_content = OutlookCalendar.get_file_content
    def get_file_content(self, item_id, drive_id=None, max_size_mb=10.0):
        self._ensure_authenticated()
        if drive_id:
            url = f"{BASE}/drives/{drive_id}/items/{item_id}/content"
        else:
            url = f"{BASE}/me/drive/items/{item_id}/content"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            resp = requests.get(url, headers=headers, timeout=60, stream=True)
            resp.raise_for_status()
            data = b""
            for chunk in resp.iter_content(chunk_size=8192):
                data += chunk
                if len(data) > max_size_mb * 1024 * 1024:
                    print(f"  File exceeds {max_size_mb} MB limit")
                    return None
            return data
        except Exception as e:
            print(f"  Download failed: {e}")
            return None
    OutlookCalendar.get_file_content = get_file_content

    print(f"✓ Patched _make_request + get_file_content (v1 URL bugs)")


def test_sp1_auth() -> OutlookCalendar:
    print("\n─── SP-1: Authenticate (user-level Files.Read) ───")
    cal = OutlookCalendar()
    if cal.is_logged_in():
        print("  Using cached token (silent login)")
    ok = cal.authenticate()  # uses cache if available, device flow if not
    if not ok:
        print("  ✗ Auth failed")
        sys.exit(1)
    print("  ✓ Authenticated")
    return cal


def test_sp2_my_drive(cal: OutlookCalendar):
    print("\n─── SP-2: Get my OneDrive ───")
    my_drive = cal._make_request("GET", "/me/drive")
    if not my_drive:
        print("  ⚠️  No drive returned (account may not have OneDrive provisioned)")
        return None
    print(f"  ✓ Drive name:  {my_drive.get('name')}")
    print(f"    Drive type:  {my_drive.get('driveType')}")
    print(f"    Drive id:    {my_drive.get('id', '')[:30]}...")
    print(f"    Owner:       {(my_drive.get('owner', {}) or {}).get('user', {}).get('displayName')}")
    print(f"    Quota used:  {my_drive.get('quota', {}).get('used', 0):,} bytes")
    return my_drive


def test_sp3_list_recent(cal: OutlookCalendar) -> Optional[dict]:
    print("\n─── SP-3: List recent files in OneDrive root ───")
    response = cal._make_request("GET", "/me/drive/root/children", params={"$top": 20})
    items = response.get("value", []) if response else []
    if not items:
        print("  ⚠️  Empty OneDrive — please upload a test file (.docx, .xlsx, or .pdf)")
        return None
    print(f"  ✓ Found {len(items)} items in OneDrive root:")
    docs = []
    for item in items[:10]:
        name = item.get("name", "")
        size = item.get("size", 0)
        modified = item.get("lastModifiedDateTime", "?")
        is_folder = "folder" in item
        kind = "📁" if is_folder else "📄"
        print(f"    {kind} {name}  ({size:,} bytes, modified {modified})")
        # Pick real documents — skip folders, Personal Vault, and zero-size special items
        if not is_folder and size > 0 and name.lower().endswith((".docx", ".xlsx", ".pdf", ".pptx", ".doc", ".xls")):
            docs.append(item)
    # Prefer .docx > .pdf > .xlsx for text extraction test (more likely to have text)
    def pick_priority(item):
        n = item["name"].lower()
        if n.endswith(".docx"): return 0
        if n.endswith(".pdf"): return 1
        return 2
    docs.sort(key=pick_priority)
    if docs:
        print(f"\n  → Will use '{docs[0]['name']}' for SP-5 / SP-6 tests")
    return docs[0] if docs else None


def test_sp4_drive_search(cal: OutlookCalendar):
    """
    Use /me/drive/root/search instead of Microsoft Search API.
    Microsoft Search (/search/query) doesn't support MSA accounts.
    Drive search works for both MSA + work/school accounts.
    """
    print("\n─── SP-4: OneDrive search (using /me/drive/root/search) ───")
    for q in ["docx", "xlsx", "문서", "saales", "Book"]:
        response = cal._make_request("GET", f"/me/drive/root/search(q='{q}')", params={"$top": 5})
        items = response.get("value", []) if response else []
        if items:
            print(f"  ✓ Query '{q}' → {len(items)} hits:")
            for item in items[:3]:
                modified_by = (item.get("lastModifiedBy", {}) or {}).get("user", {}).get("displayName", "?")
                print(f"    📄 {item['name']} (modified by {modified_by})")
            return items[0]
    print("  ⚠️  No search results")
    return None


def test_sp5_download_and_extract(cal: OutlookCalendar, file_meta: dict):
    print(f"\n─── SP-5: Download + extract text from '{file_meta['name']}' ───")
    item_id = file_meta.get("id")
    name = file_meta.get("name", "")
    if not item_id:
        print("  ⚠️  Missing item_id, skipping")
        return

    # Step 1: Raw download — bypass get_file_text_content to isolate failures
    print("  [1/3] Downloading raw bytes...")
    content = cal.get_file_content(item_id)
    if content is None:
        print("  ✗ Download returned None — see error above")
        return
    print(f"  ✓ Downloaded {len(content):,} bytes")

    # Step 2: Verify file signature
    print(f"  [2/3] First 8 bytes (hex): {content[:8].hex()}")
    # XLSX/DOCX/PPTX = ZIP starts with 50 4B 03 04
    # Old XLS = D0 CF 11 E0 A1 B1 1A E1
    # PDF = 25 50 44 46
    sig_map = {
        b"PK\x03\x04": "ZIP/Office Open XML (xlsx/docx/pptx)",
        b"\xd0\xcf\x11\xe0": "Old MS Office (xls/doc/ppt)",
        b"%PDF": "PDF",
    }
    sig = next((label for s, label in sig_map.items() if content.startswith(s)), "unknown")
    print(f"        Format detected: {sig}")

    # Step 3: Extract via the v1 helper
    print("  [3/3] Extracting text...")
    import io, os
    ext = os.path.splitext(name)[1].lower()
    try:
        text = cal._extract_text_from_binary(content, ext, name)
        if text:
            print(f"  ✓ Extracted {len(text)} chars")
            preview = text[:300].replace("\n", " ").strip()
            print(f"  Preview: {preview!r}")
        else:
            print(f"  ⚠️  Extractor returned None for '{ext}'")
            print(f"      Likely cause: empty workbook, or {ext} extractor missing dependency")
            print(f"      Try a docx file instead — let's check the next document")
    except Exception as e:
        print(f"  ✗ Extraction error: {type(e).__name__}: {e}")


def test_sp6_version_history(cal: OutlookCalendar, file_meta: dict):
    """
    NOT in v1 — implementing inline. Proof for our '누가/언제/무엇 수정' promise.
    Uses /me/drive/items/{id}/versions which works with Files.Read.
    """
    print(f"\n─── SP-6: Version history for '{file_meta['name']}' ───")
    item_id = file_meta.get("id")
    if not item_id:
        print("  ⚠️  Missing item_id, skipping")
        return
    endpoint = f"/me/drive/items/{item_id}/versions"
    response = cal._make_request("GET", endpoint)
    versions = response.get("value", []) if response else []
    if not versions:
        print("  ⚠️  No version history. Try editing the file once and re-run.")
        return
    print(f"  ✓ Found {len(versions)} version(s):")
    for v in versions:
        last_mod = v.get("lastModifiedDateTime", "?")
        modified_by = (v.get("lastModifiedBy", {}) or {}).get("user", {}).get("displayName", "?")
        ver_id = v.get("id", "?")
        size = v.get("size", "?")
        print(f"    v{ver_id}  {last_mod}  by {modified_by}  ({size} bytes)")
    if len(versions) >= 2:
        print("\n  ✓ '누가/언제/무엇 수정' promise CONFIRMED")
        print("    → /drives/{id}/items/{id}/versions endpoint works")
        print("    → Will be a small (~30 LOC) addition to outlook.py for our fork")
    else:
        print("\n  ℹ️  Only 1 version — edit the file once and re-run for full proof.")


def main():
    print("=" * 64)
    print("SharePoint/OneDrive smoke test — fda-system v1 against M365 tenant")
    print("=" * 64)
    patch_scopes_to_user_level()
    patch_graph_base_url()
    cal = test_sp1_auth()
    drive = test_sp2_my_drive(cal)
    file_from_list = test_sp3_list_recent(cal)
    file_from_search = test_sp4_drive_search(cal)
    target = file_from_list or file_from_search  # prefer list (better filtered)
    if target:
        test_sp5_download_and_extract(cal, target)
        test_sp6_version_history(cal, target)
    else:
        print("\n⚠️  No file to test against.")
        print("   Upload any .docx / .xlsx / .pdf to your OneDrive root, edit it once,")
        print("   then re-run this script.")
    print("\n─── DONE ───")


if __name__ == "__main__":
    main()
