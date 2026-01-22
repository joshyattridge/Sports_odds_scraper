"""HTTP fetching utilities with JavaScript rendering support."""

import logging
from typing import Dict, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

DEFAULT_TIMEOUT = 30


def get_domain(url: str) -> str:
    """Extract domain from URL for rule storage keying."""
    parsed = urlparse(url)
    return parsed.netloc.lower()


def fetch_page(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    use_browser: bool = False,
    wait_for_selector: Optional[str] = None,
    wait_time: float = 3.0,
) -> str:
    """
    Fetch HTML content from a URL.

    Args:
        url: The URL to fetch.
        headers: Optional custom headers (merged with defaults).
        timeout: Request timeout in seconds.
        use_browser: If True, use Playwright to render JavaScript.
        wait_for_selector: CSS selector to wait for (browser mode only).
        wait_time: Seconds to wait for JS to load (browser mode only).

    Returns:
        Raw HTML string.
    """
    if use_browser:
        return fetch_with_browser(
            url,
            wait_for_selector=wait_for_selector,
            wait_time=wait_time,
            timeout=timeout,
        )

    merged_headers = {**DEFAULT_HEADERS, **(headers or {})}
    logger.info("Fetching URL: %s", url)

    response = requests.get(url, headers=merged_headers, timeout=timeout)
    response.raise_for_status()

    logger.debug("Fetched %d bytes from %s", len(response.text), url)
    return response.text


def fetch_with_browser(
    url: str,
    *,
    wait_for_selector: Optional[str] = None,
    wait_time: float = 3.0,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """
    Fetch page using Playwright (renders JavaScript).

    Args:
        url: The URL to fetch.
        wait_for_selector: CSS selector to wait for before capturing HTML.
        wait_time: Additional seconds to wait for dynamic content.
        timeout: Navigation timeout in seconds.

    Returns:
        Fully rendered HTML string.
    """
    from playwright.sync_api import sync_playwright

    logger.info("Fetching URL with browser: %s", url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=DEFAULT_HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        try:
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")

            # Wait for specific element if provided
            if wait_for_selector:
                try:
                    page.wait_for_selector(wait_for_selector, timeout=10000)
                except Exception as e:
                    logger.warning("Selector '%s' not found: %s", wait_for_selector, e)

            # Additional wait for dynamic content
            if wait_time > 0:
                page.wait_for_timeout(int(wait_time * 1000))

            html = page.content()
            logger.info("Fetched %d bytes (browser rendered)", len(html))
            return html

        finally:
            browser.close()


def fetch_snapshot(
    url: str,
    *,
    wait_time: float = 5.0,
    timeout: int = 30,
    headless: bool = True,
) -> tuple:
    """
    Fetch a page and return a text snapshot plus HTML.
    
    The text snapshot is MUCH smaller than HTML and better for LLM analysis.
    Returns (snapshot_text, html_content).
    """
    from playwright.sync_api import sync_playwright

    logger.info("Fetching accessibility snapshot: %s", url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=DEFAULT_HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        try:
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")

            # Wait for dynamic content
            if wait_time > 0:
                page.wait_for_timeout(int(wait_time * 1000))

            # Get HTML
            html = page.content()
            
            # Get inner text (clean text representation)
            # This is much more efficient than raw HTML
            body = page.locator("body")
            text_content = body.inner_text()
            
            logger.info("Snapshot: %d chars text (vs %d bytes HTML)", len(text_content), len(html))
            
            return text_content, html

        finally:
            browser.close()


class PersistentBrowser:
    """
    A browser session that stays open for repeated snapshots.
    Much faster than launching a new browser each time.
    """
    
    def __init__(self, url: str, wait_time: float = 2.0, headless: bool = True):
        from playwright.sync_api import sync_playwright
        
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=headless)
        self._context = self._browser.new_context(
            user_agent=DEFAULT_HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
        )
        self._page = self._context.new_page()
        self._url = url
        self._wait_time = wait_time
        
        # Initial load
        logger.info("Opening persistent browser: %s", url)
        self._page.goto(url, timeout=30000, wait_until="domcontentloaded")
        self._page.wait_for_timeout(int(wait_time * 1000))
    
    def refresh_snapshot(self) -> str:
        """Refresh the page and get new snapshot. Much faster than full fetch."""
        self._page.reload(wait_until="domcontentloaded")
        self._page.wait_for_timeout(int(self._wait_time * 1000))
        
        body = self._page.locator("body")
        text_content = body.inner_text()
        return text_content
    
    def close(self):
        """Clean up browser resources."""
        self._browser.close()
        self._playwright.stop()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def fetch_and_parse(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    use_browser: bool = False,
) -> BeautifulSoup:
    """
    Fetch and parse HTML into BeautifulSoup object.
    """
    html = fetch_page(url, headers=headers, timeout=timeout, use_browser=use_browser)
    return BeautifulSoup(html, "html.parser")


def extract_text_content(soup: BeautifulSoup) -> str:
    """Extract readable text content from parsed HTML."""
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    return soup.get_text(separator=" ", strip=True)
