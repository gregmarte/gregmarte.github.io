import pytest
import pytest_asyncio
from playwright.async_api import async_playwright, BrowserContext
import httpx
from urllib.parse import urljoin, urlparse
from collections import deque
from typing import AsyncGenerator, Tuple, List, Union

# --- CONFIGURATION ---

# Set the base URL for the test. 
TEST_BASE_URL = "http://localhost:8000/"
# Maximum depth for recursive crawling
MAX_DEPTH = 5 

# --- TYPE ALIASES ---

# Define the structure for the broken links list: 
# (URL string, status code (int) or error message (str))
BrokenLinkItem = Tuple[str, Union[int, str]]
# Define the structure for the page queue: (URL string, current depth)
PageQueueItem = Tuple[str, int]


# --- PYTEST FIXTURES ---

@pytest_asyncio.fixture(scope="function")
async def page_clients() -> AsyncGenerator[Tuple[BrowserContext, httpx.AsyncClient], None]:
    """
    Sets up and tears down the asynchronous clients (Playwright Context and httpx client).
    Using scope="function" ensures the environment is fresh for each test.
    """
    print("\n--- Fixture Setup: Initializing Clients ---")
    
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context()

    http_client = httpx.AsyncClient(http2=True, follow_redirects=True, timeout=10)
    yield context, http_client

    print("\n--- Fixture Teardown: Closing Clients ---")
    await http_client.aclose()
    await browser.close()
    await p.stop()


# --- CRAWLING LOGIC ---

async def recursive_link_check(
    start_url: str,
    context: BrowserContext, 
    http_client: httpx.AsyncClient
) -> List[BrokenLinkItem]:
    """
    Recursively crawls internal links starting from start_url up to MAX_DEPTH.
    External links are checked for status but are NOT crawled further.
    """
    # Use a set to store cleaned URLs already processed or queued.
    visited_urls = {start_url}
    
    # Use a deque to store tuples of (url, depth)
    url_queue: deque[PageQueueItem] = deque([(start_url, 0)])
    
    broken_links: List[BrokenLinkItem] = []

    while url_queue:
        current_url, current_depth = url_queue.popleft()
        
        print(f"\n[{current_depth}/{MAX_DEPTH}] ➡️ Crawling: {current_url}")

        if current_depth >= MAX_DEPTH:
            print(f"   [SKIPPED] Max depth reached.")
            broken_links.append((current_url, "Max Crawl Depth Reached"))
            continue
            
        try:
            # 1. Navigate to the page using Playwright to scrape links.
            page = await context.new_page()
            await page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
            
            # 2. Extract all href attributes from anchor tags
            links = await page.evaluate('''() => {
                const anchors = Array.from(document.querySelectorAll('a'));
                return anchors.map(a => a.href);
            }''')
            
            await page.close()
            
            # 3. Process and validate each discovered link
            for link in links:
                # Resolve relative paths and remove fragments/queries
                full_link = urljoin(current_url, link)
                parsed_link = urlparse(full_link)
                clean_link = parsed_link._replace(fragment="", query="").geturl()
                
                # Skip external links
                if not clean_link.startswith(TEST_BASE_URL):
                    continue

                # Skip already visited links
                if clean_link in visited_urls:
                    continue

                visited_urls.add(clean_link)
                
                # Check status and add to queue if OK
                try:
                    print(f" 🔍 Checking status of: {clean_link}")
                    # Use HEAD request for speed, as we only need the status code
                    response = await http_client.head(clean_link)
                    
                    if response.status_code == 200:
                        print(f"  ✅ OK [{response.status_code}]: {clean_link}")
                        # Add link to queue for deeper crawl
                        url_queue.append((clean_link, current_depth + 1))
                    else:
                        broken_links.append((clean_link, response.status_code))
                        print(f"  ❌ BROKEN [{response.status_code}]: {clean_link}")

                except httpx.RequestError as e:
                    # Catch connection errors, DNS lookup failures, etc.
                    broken_links.append((clean_link, str(e)))
                    print(f"  ❌ ERROR [REQUEST FAILED]: {clean_link} -> {e}")

        except Exception as e:
            # If the initial navigation or scraping fails for the current URL
            broken_links.append((current_url, f"Crawl Failed: {e.__class__.__name__}"))
            print(f"  ❌ ERROR [CRAWL FAILED]: {current_url} -> {e}")

    return broken_links


# --- PYTEST TEST FUNCTION ---

@pytest.mark.asyncio
async def test_crawls_start_page_links_successfully(page_clients: Tuple[BrowserContext, httpx.AsyncClient]):
    """
    Initiates the recursive link crawling process.
    """
    
    # Unpack the clients provided by the fixture
    context, http_client = page_clients
    
    print(f"Starting RECURSIVE crawl on base URL: {TEST_BASE_URL} (Max Depth: {MAX_DEPTH})")

    # Run the recursive check
    broken_links = await recursive_link_check(TEST_BASE_URL, context, http_client)

    # Final Assertion: The list of broken links must be empty for the test to pass.
    assert not broken_links, (
        f"\nFound {len(broken_links)} broken link(s) during recursive crawl:"
        f"\n{chr(10).join([f'- {url} ({status})' for url, status in broken_links])}"
    )

    print(f"\n✅ Recursive crawl passed: All discovered links up to depth {MAX_DEPTH} are valid.")