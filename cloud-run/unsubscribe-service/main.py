"""Unsubscribe Service - AI-powered browser automation for email unsubscription.

Cloud Run Job that handles the heavy lifting: when emails can't be unsubscribed via
simple HTTP POST or mailto, this service uses AI browser automation to navigate
complex unsubscribe pages with dark patterns.

Three unsubscribe methods:
1. RFC 8058 One-Click POST (fastest, ~1s)
2. Mailto email (fast, ~2-3s)
3. AI Browser Automation (powerful, ~10-60s)

Environment variables (set by job execution):
- EMAIL_ID: The email ID to unsubscribe from
- TRACE_ID: Trace ID for logging
- UNSUBSCRIBE_URL: URL to unsubscribe from
- HAS_ONE_CLICK_POST: "true" if RFC 8058 is supported
- HEADERS_JSON: JSON array of email headers
- BODY_HTML: HTML body content (optional)
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from google.cloud import storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ===========================================================================
# Logging
# ===========================================================================


def log_to_storage(
    trace_id: str,
    email_id: str,
    stage: str,
    result: str = "success",
    metadata: dict | None = None,
) -> bool:
    """Log to Cloud Storage as JSONL."""
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "gmail-ai-logs")
    project_id = os.getenv("GMAIL_AI_PROJECT_ID")

    log_entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "trace_id": trace_id,
        "email_id": email_id,
        "stage": stage,
        "result": result,
        "metadata": metadata or {},
    }

    try:
        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)
        now = datetime.now(UTC)
        blob_path = f"logs/{now.strftime('%Y/%m/%d')}/log.jsonl"
        blob = bucket.blob(blob_path)

        existing = ""
        if blob.exists():
            existing = blob.download_as_text()

        blob.upload_from_string(existing + json.dumps(log_entry) + "\n")
        logger.info(f"Logged: {stage} - {result}")
        return True
    except Exception as e:
        logger.error(f"Failed to log to storage: {e}")
        return False


# ===========================================================================
# URL Validation & Extraction
# ===========================================================================


def validate_url(url: str) -> bool:
    """Validate that a URL looks complete and is likely to work."""
    if not url:
        return False

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False

    if url.rstrip().endswith("=") or url.rstrip().endswith("?"):
        return False

    if parsed.query and parsed.query.rstrip().endswith("="):
        return False

    return True


def extract_list_unsubscribe_header(headers: list[dict]) -> dict | None:
    """Extract List-Unsubscribe header from email headers."""
    list_unsubscribe = None

    for header in headers:
        name = header.get("name", "").lower()
        value = header.get("value", "")
        if name == "list-unsubscribe":
            list_unsubscribe = value

    if not list_unsubscribe:
        return None

    urls = []
    mailto_addresses = []

    url_pattern = r"<([^>]+)>"
    matches = re.findall(url_pattern, list_unsubscribe)
    for match in matches:
        match = match.replace(" ", "")
        if match.startswith("mailto:"):
            mailto_addresses.append(match[7:])
        elif match.startswith("http://") or match.startswith("https://"):
            urls.append(match)

    if not matches:
        list_unsubscribe_clean = list_unsubscribe.replace(" ", "")
        url_pattern = r"(https?://[^\s,>]+|mailto:[^\s,>]+)"
        direct_matches = re.findall(url_pattern, list_unsubscribe_clean)
        for match in direct_matches:
            if match.startswith("mailto:"):
                mailto_addresses.append(match[7:])
            else:
                urls.append(match)

    if urls:
        return {
            "link_url": urls[0],
            "mailto_address": mailto_addresses[0] if mailto_addresses else None,
            "source": "header",
        }
    elif mailto_addresses:
        return {
            "mailto_address": mailto_addresses[0],
            "source": "header",
        }

    return None


def extract_unsubscribe_urls_from_html(html_content: str) -> list[str]:
    """Extract all unsubscribe links from HTML using BeautifulSoup."""
    urls = []
    try:
        soup = BeautifulSoup(html_content, "html.parser")

        for link in soup.find_all("a", href=True):
            href_raw = link.get("href", "")
            if isinstance(href_raw, list):
                href = str(href_raw[0]) if href_raw else ""
            elif href_raw is None:
                href = ""
            else:
                href = str(href_raw)

            link_text = link.get_text(strip=True).lower()

            keywords = ["unsubscribe", "unsub", "opt-out", "optout", "remove", "preferences"]
            href_lower = href.lower()
            is_unsubscribe = any(k in href_lower for k in keywords) or any(k in link_text for k in keywords)

            if is_unsubscribe:
                url = href.strip().replace(" ", "")
                parsed = urlparse(url)
                if parsed.scheme in ("http", "https") and validate_url(url):
                    if url not in urls:
                        urls.append(url)

    except Exception:
        pass

    return urls


# ===========================================================================
# RFC 8058 One-Click HTTP POST
# ===========================================================================


def send_http_post_unsubscribe(url: str) -> bool:
    """Send HTTP POST for one-click unsubscribe (RFC 8058)."""
    try:
        response = requests.post(
            url,
            data={"List-Unsubscribe": "One-Click"},
            headers={"User-Agent": "gmail-ai-unsub/0.1.0"},
            timeout=30,
            allow_redirects=True,
        )
        return 200 <= response.status_code < 400
    except Exception as e:
        logger.error(f"RFC 8058 POST failed: {e}")
        return False


# ===========================================================================
# AI Browser Automation
# ===========================================================================


def create_browser_llm():
    """Create LLM for browser automation."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        from browser_use import ChatAnthropic
        os.environ["ANTHROPIC_API_KEY"] = api_key
        return ChatAnthropic(model="claude-sonnet-4-20250514")

    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        from browser_use import ChatOpenAI
        os.environ["OPENAI_API_KEY"] = api_key
        return ChatOpenAI(model="gpt-4o")

    api_key = os.getenv("GOOGLE_API_KEY")
    if api_key:
        from browser_use import ChatGoogle
        os.environ["GOOGLE_API_KEY"] = api_key
        return ChatGoogle(model="gemini-2.5-flash-preview-05-20")

    raise ValueError("No LLM API key found (ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY)")


async def unsubscribe_via_browser(
    url: str,
    headless: bool = True,
    timeout: int = 60,
) -> tuple[bool, str | None]:
    """Unsubscribe from a URL using AI browser automation."""
    from browser_use import Agent, Browser, BrowserProfile

    browser = None
    try:
        llm = create_browser_llm()

        browser_profile = BrowserProfile(
            headless=headless,
            minimum_wait_page_load_time=0.5,
            wait_between_actions=0.3,
        )

        browser = Browser(
            browser_profile=browser_profile,
            window_size={"width": 1280, "height": 720},
        )

        agent: Agent = Agent(
            task=f"""Navigate to {url} and unsubscribe from this mailing list.

CRITICAL RULES:
1. **Success Detection**: If the page shows "OK", "Success", "Unsubscribed", "You have been unsubscribed", the task is COMPLETE.

2. **Options and Checkboxes**:
   - If there are multiple options, ALWAYS select the MOST BROAD option
   - Look for: "Unsubscribe from all", "All emails", "Everything"
   - AVOID selecting specific categories

3. **DO NOT**:
   - Try to login to any website
   - Search for "how to unsubscribe"
   - Fill out forms asking for email/password

4. **Dark Patterns to Watch For**:
   - Some sites have "Stay subscribed" buttons that look like unsubscribe buttons
   - Read button text carefully

INSTRUCTIONS:
1. Navigate to the URL
2. Look for unsubscribe buttons, checkboxes, or forms
3. If there are options, select the most broad one
4. Click "Unsubscribe", "Confirm", or similar buttons
5. If you see a success message, the task is complete""",
            llm=llm,
            browser=browser,
            use_vision=True,
            max_steps=15,
            step_timeout=timeout,
            max_failures=3,
        )

        result = await agent.run()

        success = False
        final_message = None

        if result is not None:
            all_results = getattr(result, "all_results", None)
            if all_results:
                for action_result in reversed(all_results):
                    if getattr(action_result, "is_done", False):
                        action_success = getattr(action_result, "success", None)
                        if action_success is True:
                            success = True
                        elif action_success is None:
                            judgement = getattr(action_result, "judgement", None)
                            if judgement and getattr(judgement, "verdict", None) is True:
                                success = True

                        final_message = getattr(action_result, "extracted_content", None)
                        if not final_message:
                            final_message = getattr(action_result, "text", None)

                        if final_message:
                            msg_lower = str(final_message).lower()
                            success_phrases = [
                                "already unsubscribed", "successfully unsubscribed",
                                "unsubscribed", "task completed successfully",
                                "you have been unsubscribed",
                            ]
                            if any(phrase in msg_lower for phrase in success_phrases):
                                success = True
                        break

            if not success:
                result_str = str(result).lower()
                if any(p in result_str for p in ["unsubscribed", "task completed"]):
                    success = True
                    final_message = str(result)

        if success:
            return (True, final_message)
        else:
            return (False, "Browser automation did not complete successfully")

    except Exception as e:
        return (False, f"Browser automation failed: {str(e)}")

    finally:
        if browser:
            try:
                if hasattr(browser, "close"):
                    await browser.close()
                elif hasattr(browser, "__aexit__"):
                    await browser.__aexit__(None, None, None)
            except Exception:
                pass


# ===========================================================================
# Main Unsubscribe Logic
# ===========================================================================


async def unsubscribe_email(
    email_id: str,
    headers: list[dict],
    body_html: str | None = None,
    has_one_click_post: bool = False,
) -> dict:
    """
    Attempt to unsubscribe from an email using all available methods.

    Order:
    1. RFC 8058 One-Click POST (if supported)
    2. Browser automation for URL
    """
    # Extract unsubscribe link from headers
    unsub_link = extract_list_unsubscribe_header(headers)

    # Also try body URLs
    body_urls = []
    if body_html:
        body_urls = extract_unsubscribe_urls_from_html(body_html)

    # Method 1: RFC 8058 One-Click POST
    if unsub_link and unsub_link.get("link_url") and has_one_click_post:
        logger.info(f"Trying RFC 8058 POST for {email_id}")
        if send_http_post_unsubscribe(unsub_link["link_url"]):
            return {"success": True, "method": "rfc8058", "error": None}

    # Method 2: Browser automation for header URL
    if unsub_link and unsub_link.get("link_url"):
        logger.info(f"Trying browser automation for {email_id}: {unsub_link['link_url']}")
        success, error = await unsubscribe_via_browser(unsub_link["link_url"])
        if success:
            return {"success": True, "method": "browser", "error": None}
        logger.warning(f"Browser automation failed: {error}")

    # Method 3: Browser automation for body URLs
    for url in body_urls:
        logger.info(f"Trying browser automation for body URL: {url}")
        success, error = await unsubscribe_via_browser(url)
        if success:
            return {"success": True, "method": "browser", "error": None}
        logger.warning(f"Browser automation failed for {url}: {error}")

    # All methods failed
    return {
        "success": False,
        "method": "none",
        "error": "All unsubscribe methods failed",
    }


# ===========================================================================
# Main Entry Point
# ===========================================================================


async def main():
    """Main entry point for Cloud Run Job."""
    # Read environment variables
    email_id = os.getenv("EMAIL_ID", "")
    trace_id = os.getenv("TRACE_ID", f"trace-{email_id}")
    headers_json = os.getenv("HEADERS_JSON", "[]")
    body_html = os.getenv("BODY_HTML", "")
    has_one_click_post = os.getenv("HAS_ONE_CLICK_POST", "false").lower() == "true"

    if not email_id:
        logger.error("EMAIL_ID environment variable is required")
        sys.exit(1)

    logger.info(f"Processing unsubscribe for email: {email_id}")

    # Parse headers
    try:
        headers = json.loads(headers_json)
    except json.JSONDecodeError:
        logger.error("Invalid HEADERS_JSON")
        headers = []

    # LOG: Job started
    log_to_storage(
        trace_id=trace_id,
        email_id=email_id,
        stage="unsubscribe_job_start",
    )

    try:
        result = await unsubscribe_email(
            email_id=email_id,
            headers=headers,
            body_html=body_html if body_html else None,
            has_one_click_post=has_one_click_post,
        )
    except Exception as e:
        logger.error(f"Unsubscribe failed: {e}")
        log_to_storage(
            trace_id=trace_id,
            email_id=email_id,
            stage="unsubscribe",
            result="failure",
            metadata={"error": str(e)},
        )
        sys.exit(1)

    # LOG: Result
    log_to_storage(
        trace_id=trace_id,
        email_id=email_id,
        stage="unsubscribe",
        result="success" if result["success"] else "failure",
        metadata=result,
    )

    logger.info(f"Unsubscribe result: {result}")
    logger.info(f"Done processing {email_id}")

    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
