# Office 365 Calendar Setup Guide

This guide explains how to set up the Azure AD app registration so users can connect their Office 365 calendars to the FDA system.

## Overview

The FDA system uses Microsoft's device code flow for authentication. Users simply:
1. Run `fda calendar login`
2. Visit a URL and enter a code
3. Log in with their Office 365 account

**You only need to do this setup once** as the developer. After that, all users can authenticate using their own Office 365 accounts.

---

## Step 1: Create Azure AD App Registration

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **Microsoft Entra ID** (formerly Azure Active Directory)
3. Click **App registrations** in the left sidebar
4. Click **+ New registration**

Fill in the form:
- **Name**: `FDA Project Manager` (or your preferred name)
- **Supported account types**: Select **"Accounts in any organizational directory and personal Microsoft accounts"** (multi-tenant)
- **Redirect URI**: Leave blank (not needed for device code flow)

Click **Register**.

---

## Step 2: Note Your Application IDs

After registration, you'll see the **Overview** page. Copy these values:

| Field | Description |
|-------|-------------|
| **Application (client) ID** | e.g., `12345678-abcd-1234-abcd-1234567890ab` |
| **Directory (tenant) ID** | Not needed (we use "common" for multi-tenant) |

---

## Step 3: Configure API Permissions

1. In your app registration, click **API permissions** in the left sidebar
2. Click **+ Add a permission**
3. Select **Microsoft Graph**
4. Select **Delegated permissions**
5. Search and add these permissions:
   - `Calendars.Read`
   - `Calendars.ReadWrite`
   - `User.Read`
6. Click **Add permissions**

Your permissions should look like:

| Permission | Type | Status |
|------------|------|--------|
| Calendars.Read | Delegated | Granted |
| Calendars.ReadWrite | Delegated | Granted |
| User.Read | Delegated | Granted |

> **Note**: For organizational accounts, an admin may need to grant consent. For personal Microsoft accounts, users grant consent themselves during login.

---

## Step 4: Enable Public Client Flow

1. Click **Authentication** in the left sidebar
2. Scroll down to **Advanced settings**
3. Set **Allow public client flows** to **Yes**
4. Click **Save**

This enables the device code flow used by the FDA CLI.

---

## Step 5: Update the FDA Configuration

Open `fda/outlook.py` and replace the placeholder client ID:

```python
# Line 37
DEFAULT_CLIENT_ID = "YOUR_REGISTERED_APP_CLIENT_ID"  # Replace after registering
```

Replace with your actual Application (client) ID:

```python
DEFAULT_CLIENT_ID = "12345678-abcd-1234-abcd-1234567890ab"  # Your actual ID
```

Alternatively, users can set an environment variable instead of modifying the code:

```bash
export FDA_OUTLOOK_CLIENT_ID="12345678-abcd-1234-abcd-1234567890ab"
```

---

## Step 6: Test the Integration

Install the required dependencies:

```bash
pip install msal requests
```

Test the calendar login:

```bash
fda calendar login
```

You should see:

```
==================================================
OFFICE 365 LOGIN
==================================================

1. Open: https://microsoft.com/devicelogin
2. Enter code: ABCD1234
3. Log in with your Office 365 account

Waiting for you to complete login...

✓ Successfully connected to Office 365 calendar!
```

---

## User Commands

Once set up, users have these commands available:

```bash
# Log in to Office 365 calendar
fda calendar login

# Check connection status
fda calendar status

# View today's events
fda calendar today

# View upcoming events (next 60 minutes by default)
fda calendar upcoming
fda calendar upcoming --minutes 120

# Log out
fda calendar logout
```

---

## Troubleshooting

### "AADSTS700016: Application not found"
- Verify the client ID is correct
- Ensure the app registration exists in Azure

### "AADSTS65001: User needs to consent"
- The user needs to approve the app permissions during first login
- For organizational accounts, an admin may need to pre-consent

### "AADSTS7000218: Request body must contain client_assertion"
- Ensure **Allow public client flows** is set to **Yes**

### Token cache issues
- The token is cached at `~/.fda/data/.outlook_token_cache.json`
- Delete this file to force a fresh login: `fda calendar logout`

---

## Security Notes

- Tokens are cached locally with restricted file permissions (600)
- No client secret is used (public client flow)
- Users authenticate with their own credentials
- The app only requests calendar permissions, not full mailbox access
- Users can revoke access anytime at [myapps.microsoft.com](https://myapps.microsoft.com)

---

## Optional: Organization-Specific Setup

If you want to restrict the app to your organization only:

1. In app registration, change **Supported account types** to:
   - "Accounts in this organizational directory only"
2. Use your specific tenant ID instead of "common":
   ```bash
   export FDA_OUTLOOK_TENANT_ID="your-tenant-id"
   ```
