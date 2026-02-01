# Unsubscribe Techniques in gmail-ai-unsub

This document explains all the unsubscribe methods and technologies used in this repository.

## Overview

The unsubscribe system uses a **multi-layered approach** - it tries multiple methods in sequence until one succeeds. This maximizes success rate because different email senders use different unsubscribe mechanisms.

## Unsubscribe Methods (in order of attempt)

### 1. **RFC 8058 One-Click HTTP POST** (Fastest, ~1 second)

**What it is:**
- Standard protocol for one-click unsubscribe
- Email includes `List-Unsubscribe-Post` header indicating support
- Sends HTTP POST request with `List-Unsubscribe=One-Click` data

**How it works:**
```python
# From email_unsub.py
response = requests.post(
    url,
    data={"List-Unsubscribe": "One-Click"},
    headers={"User-Agent": "gmail-ai-unsub/0.1.0"},
    timeout=30,
)
```

**When it works:**
- Email has `List-Unsubscribe-Post` header
- URL supports one-click POST
- No user interaction needed

**Success rate:** ~30-40% of emails (many senders don't support it)

---

### 2. **Mailto Unsubscribe** (Fast, ~2-3 seconds)

**What it is:**
- Sends an email to unsubscribe address
- Uses Gmail API to send the unsubscribe email
- Email includes standard unsubscribe message

**How it works:**
```python
# From email_unsub.py
msg = EmailMessage()
msg["To"] = mailto_address
msg["Subject"] = "Unsubscribe"
msg.set_content("Please unsubscribe me from this mailing list.")
client.send_message(msg.as_string())
```

**When it works:**
- Email has `mailto:` link in `List-Unsubscribe` header
- Unsubscribe address accepts emails
- No browser needed

**Success rate:** ~20-30% of emails

---

### 3. **AI Browser Automation** (Most powerful, ~10-60 seconds)

**What it is:**
- Uses AI vision models to navigate unsubscribe pages
- Handles complex forms, checkboxes, multi-step flows
- Can detect success/failure states

**Technologies used:**
- **browser-use**: Browser automation framework
- **Playwright**: Browser engine (headless Chrome/Firefox)
- **AI Vision Models**: 
  - Browser-Use's optimized model (fastest)
  - Gemini 2.5 Computer Use (specialized for UI)
  - Claude 4.5 (excellent vision)
  - GPT-5 (general purpose)

**How it works:**
```python
# From browser_agent.py
agent = Agent(
    task=f"Navigate to {url} and unsubscribe from this mailing list.
    
    CRITICAL RULES:
    1. Look for unsubscribe buttons, checkboxes, or forms
    2. If there are options, select the MOST BROAD option (unsubscribe from all)
    3. Click 'Unsubscribe', 'Confirm', or similar buttons
    4. If you see success message, task is complete
    ...",
    llm=llm,  # AI vision model
    browser=browser,
    use_vision=True,  # Screenshot-based understanding
    max_steps=15,
)
result = await agent.run()
```

**What the AI does:**
1. Takes screenshot of the page
2. AI analyzes the screenshot to find unsubscribe elements
3. AI decides what to click/type
4. Repeats until success or timeout
5. Detects success messages ("Unsubscribed", "OK", etc.)

**When it works:**
- Any unsubscribe page (even complex ones)
- Multi-step unsubscribe flows
- Pages with dark patterns (tricky UI)
- Forms requiring checkboxes/radio buttons

**Success rate:** ~70-80% of remaining emails (after methods 1-2 fail)

**Key features:**
- **Vision-based**: Sees the page like a human
- **Dark pattern detection**: Avoids "Stay subscribed" buttons
- **Multi-option handling**: Chooses "unsubscribe from all" when multiple options exist
- **Success detection**: Recognizes confirmation messages

---

## Unsubscribe Link Extraction

Before unsubscribing, the system needs to find the unsubscribe link. It uses multiple extraction methods:

### 1. **List-Unsubscribe Header** (RFC 2369)
- Extracts from email headers
- Can contain both `mailto:` and `https://` links
- Most reliable source

### 2. **Email Body Parsing**
- Uses BeautifulSoup to parse HTML
- Searches for links with "unsubscribe" keywords
- Handles quoted-printable encoding
- Validates URLs (removes spaces, checks for truncation)

### 3. **Multiple URL Collection**
- Collects ALL unsubscribe URLs found
- Tries each one until one works
- Handles cases where header URL is broken but body URL works

---

## Complete Unsubscribe Flow

```
1. Extract unsubscribe link(s)
   ├─ From List-Unsubscribe header
   ├─ From email body (HTML parsing)
   └─ Validate URLs (remove spaces, check for truncation)

2. Try Method 1: RFC 8058 One-Click POST
   └─ If email supports it → Success! (fastest)

3. Try Method 2: Mailto Email
   └─ If mailto address exists → Send email

4. Try Method 3: Browser Automation (for URLs)
   ├─ Collect all URLs (header + body)
   ├─ For each URL:
   │  ├─ Test accessibility (skip 404s)
   │  ├─ Launch browser (headless or visible)
   │  ├─ AI navigates and unsubscribes
   │  └─ Detect success
   └─ Stop when one succeeds

5. Update Gmail labels
   ├─ Success → Apply "Unsubscribed" label
   └─ Failure → Apply "Unsubscribe-Failed" label
```

---

## Key Technologies

### browser-use
- Modern browser automation framework
- Integrates with LangChain
- Supports vision models
- Handles complex UI interactions

### AI Vision Models
- **Browser-Use Model**: Fastest, optimized for browser tasks
- **Gemini 2.5 Computer Use**: Specialized for UI automation
- **Claude 4.5**: Excellent at understanding complex pages
- **GPT-5**: Good general-purpose vision

### Playwright
- Browser engine (headless Chrome/Firefox)
- Provides browser automation capabilities
- Used by browser-use under the hood

---

## Why This Approach Works

1. **Layered Strategy**: Tries fast methods first, falls back to slower but more powerful methods
2. **Multiple URL Sources**: Doesn't rely on just header - also searches body
3. **AI Vision**: Can handle pages that simple automation can't
4. **Dark Pattern Detection**: AI can read button text and avoid tricks
5. **Success Detection**: AI recognizes confirmation messages

---

## Success Rates (Estimated)

- **RFC 8058 POST**: ~30-40% of emails
- **Mailto**: ~20-30% of emails  
- **Browser Automation**: ~70-80% of remaining emails
- **Overall**: ~85-90% success rate

---

## Files Involved

- `src/gmail_ai_unsub/unsubscribe/extractor.py` - Link extraction
- `src/gmail_ai_unsub/unsubscribe/email_unsub.py` - RFC 8058 & mailto
- `src/gmail_ai_unsub/unsubscribe/browser_agent.py` - AI browser automation
- `src/gmail_ai_unsub/cli.py` - Orchestrates all methods (lines 770-960)

---

## Usage (Standalone)

If you just want the unsubscribe functionality:

```python
from gmail_ai_unsub.unsubscribe.extractor import extract_list_unsubscribe_header
from gmail_ai_unsub.unsubscribe.email_unsub import (
    send_http_post_unsubscribe,
    send_mailto_unsubscribe,
)
from gmail_ai_unsub.unsubscribe.browser_agent import unsubscribe_via_browser_sync
from gmail_ai_unsub.gmail.client import GmailClient
from gmail_ai_unsub.config import Config

# 1. Get email
client = GmailClient(...)
message = client.get_message(message_id)

# 2. Extract unsubscribe link
unsub_link = extract_list_unsubscribe_header(message)

# 3. Try methods in order
if unsub_link.link_url:
    # Try RFC 8058 POST first
    if has_one_click_support(message):
        success = send_http_post_unsubscribe(unsub_link.link_url, message)
    
    # Then browser automation
    if not success:
        config = Config()
        success, error = unsubscribe_via_browser_sync(
            unsub_link.link_url,
            config,
            headless=True,
        )

if unsub_link.mailto_address:
    success = send_mailto_unsubscribe(client, unsub_link.mailto_address, message)
```
