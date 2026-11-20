#!/usr/bin/env python3
"""
GGPoker Omaha Daily Leaderboard Scraper
Basic scraper to access the page and interact with the iframe
"""

import time
import datetime
from selenium import webdriver
import argparse
from database_manager import DatabaseManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
import os

try:
    from webdriver_manager.core.utils import ChromeType
except Exception:
    try:
        from webdriver_manager.chrome import ChromeType
    except Exception:
        from webdriver_manager.utils import ChromeType
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class GGPokerScraper:
    def __init__(self, headless=False):
        """
        Initialize the scraper

        Args:
            headless (bool): If True, run browser in headless mode.
                           If False, browser will be visible so you can follow along.
        """
        self.driver = None
        self.headless = headless
        self.setup_driver()
        self.duplicate_guard = (
            None,
            None,
        )  # Keep track of last leaderboard 1st place nama and points, sometimes the leaderboard doesn't update and we don't want to insert duplicates

        # Configurable targets per replica
        self.promo_url = os.getenv(
            "PROMO_URL", "https://ggpoker.com/poker-promotions/omaha-daily-leaderboard/"
        )
        self.game_name = os.getenv("GAME_NAME", "PLO").strip()
        self.table_name = os.getenv("TABLE_NAME", "").strip()
        self.sleeptime_before_next_blindlevel = int(
            os.getenv("SLEEP_TIME_BEFORE_NEXT_BLIND_LEVEL", 12)
        )
        # self.sleeptime_before_next_run = int(
        #     os.getenv("SLEEP_TIME_BEFORE_NEXT_RUN", 20)
        # )
        # self.sleeptime_after_run = int(os.getenv("SLEEP_TIME_AFTER_RUN", 20))
        # Initialize database manager
        self.db_manager = DatabaseManager()
        print("✅ Database manager initialized")
        print(f"⚙️  Config: PROMO_URL={self.promo_url} | GAME_NAME={self.game_name}")

    def _build_xpath_literal(self, text: str) -> str:
        """Return a valid XPath string literal for the given text.
        Handles values containing single quotes by using concat().
        """
        if "'" not in text:
            return f"'{text}'"
        parts = text.split("'")
        concat_parts = []
        for i, part in enumerate(parts):
            if i > 0:
                concat_parts.append('"\'"')  # add a literal single-quote between parts
            concat_parts.append(f"'{part}'")
        return "concat(" + ", ".join(concat_parts) + ")"

    def setup_driver(self):
        """Set up the Chrome WebDriver with appropriate options"""
        chrome_options = Options()

        # Headless/non-GPU safe defaults for server/containers
        headless_env = os.getenv("HEADLESS", "1").lower() in ("1", "true", "yes")
        if self.headless or headless_env:
            chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")

        # Allow overriding Chrome binary (useful in Docker with Chromium)
        chrome_bin = os.getenv("CHROME_BIN")
        if chrome_bin:
            chrome_options.binary_location = chrome_bin

        # Install and setup ChromeDriver automatically, but allow a fixed binary path
        chromedriver_bin = os.getenv("CHROME_DRIVER_BIN")
        if chromedriver_bin and os.path.exists(chromedriver_bin):
            service = Service(executable_path=chromedriver_bin)
        else:
            chrome_type_env = os.getenv("CHROME_TYPE", "chrome").lower()
            if chrome_type_env == "chromium":
                service = Service(
                    ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install()
                )
            else:
                service = Service(ChromeDriverManager().install())

        try:
            self.driver = webdriver.Chrome(service=service, options=chrome_options)
            # Remove webdriver property to avoid detection
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            print("✅ WebDriver initialized successfully")
        except Exception as e:
            print(f"❌ Error setting up WebDriver: {e}")
            raise

    def access_ggpoker_page(self):
        """Access the GGPoker Omaha Daily Leaderboard page"""
        url = self.promo_url

        try:
            print(f"🌐 Accessing: {url}")
            self.driver.get(url)

            # Wait for the document and the main content instead of relying on a
            # fixed delay, which is especially fragile in headless Chrome.
            WebDriverWait(self.driver, 10).until(
                lambda driver: driver.execute_script("return document.readyState")
                in ("interactive", "complete")
            )
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            print("✅ Page loaded successfully")
            return True

        except Exception as e:
            print(f"❌ Error accessing page: {e}")
            return False

    def find_game_name_section(self):
        """Find the target game section with the iframe"""
        try:
            # Look for the exact game heading (env GAME_NAME)
            name_literal = self._build_xpath_literal(self.game_name)
            heading_xpath = f"//h4[normalize-space(text())={name_literal}]"
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, heading_xpath))
            )
            print(f"✅ Found {self.game_name} section heading")

            # Find the iframe that is the first responsive-iframe sibling under this heading
            iframe_xpath = (
                heading_xpath
                + "/following-sibling::div[contains(@class,'responsive-iframe')][1]//iframe"
            )
            iframe = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, iframe_xpath))
            )

            print("✅ Found matching iframe under heading")

            return iframe

        except Exception as e:
            print(f"❌ Error finding {self.game_name} section: {e}")
            return None

    def get_blind_levels_from_dropdown(self):
        """Find the dropdown-layer class and extract all blind level text from list elements"""
        try:
            print("🔍 Looking for dropdown-layer class...")
            time.sleep(2)  # Allow time for the dropdown to render
            dropdown = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "dropdown-layer"))
            )
            print("✅ Found dropdown-layer element")
            # Find all list elements (li) within the dropdown
            list_elements = WebDriverWait(self.driver, 30).until(
                lambda driver: dropdown.find_elements(By.TAG_NAME, "li") or False
            )
            print(f"📋 Found {len(list_elements)} list elements in dropdown")
            for i in range(len(list_elements)):
                text_content = (
                    list_elements[i].get_attribute("textContent") or ""
                ).strip()
                print(f"  {i}: {text_content}")

            return list_elements

        except Exception as e:
            print(f"❌ Error extracting blind levels from dropdown: {e}")
            page_source = self.driver.page_source
            if (
                "__CF$cv$params" in page_source
                or "/cdn-cgi/challenge-platform/" in page_source
            ):
                print(
                    "⚠️ Cloudflare challenge detected; pausing scraper for 65 minutes"
                )
                time.sleep(65 * 60)

            print(f"   URL: {self.driver.current_url}")
            print(f"   Title: {self.driver.title}")
            print(
                "   Body text: "
                + self.driver.find_element(By.TAG_NAME, "body")
                .get_attribute("textContent")[:500]
                .replace("\n", " ")
            )
            dropdowns = self.driver.find_elements(By.CSS_SELECTOR, "ul.dropdown-layer")
            if dropdowns:
                print(f"   Dropdown HTML: {dropdowns[0].get_attribute('outerHTML')}")
            return []

    def click_through_blind_levels(self, list_elements):
        """Click through all blind level options to make them clickable"""
        try:
            print(f"🔄 Starting to click through {len(list_elements)} blind levels...")

            # Angular can replace the dropdown nodes after each selection, so
            # retain only the count and reacquire live elements on every pass.
            total_levels = len(list_elements)

            for i in range(total_levels):
                try:
                    print(f"\n🖱️ Processing blind level {i}/{total_levels}...")

                    blind_text_element = WebDriverWait(self.driver, 15).until(
                        EC.element_to_be_clickable((By.CLASS_NAME, "blind-text"))
                    )

                    # Click on it to open the dropdown
                    print(f"  🖱️ Clicking on 'blind-text' element...")
                    blind_text_element.click()
                    time.sleep(2)  # Allow time for the dropdown to open

                    WebDriverWait(self.driver, 10).until(
                        EC.visibility_of_element_located(
                            (By.CSS_SELECTOR, "ul.dropdown-layer.layer-open")
                        )
                    )
                    current_levels = WebDriverWait(self.driver, 10).until(
                        lambda driver: (
                            elements
                            if len(
                                elements := driver.find_elements(
                                    By.CSS_SELECTOR,
                                    "ul.dropdown-layer.layer-open li",
                                )
                            )
                            >= total_levels
                            else False
                        )
                    )
                    list_element = current_levels[i]
                    text_content = (
                        list_element.get_attribute("textContent") or ""
                    ).strip()
                    print(f"  🖱️ Clicking on list element {i}...")
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", list_element
                    )
                    WebDriverWait(self.driver, 20).until(
                        lambda driver: list_element.is_displayed()
                        and list_element.is_enabled()
                    )
                    list_element.click()
                    time.sleep(5)

                    # Now extract player ranking data from the table
                    self.extract_player_ranking_data(text_content)

                    # Wait a bit before processing next element
                    time.sleep(self.sleeptime_before_next_blindlevel)

                except Exception as e:
                    print(f"  ❌ Error processing blind level {i}: {e}")
                    continue

            print(f"\n✅ Completed clicking through all {total_levels} blind levels")
            return True

        except Exception as e:
            print(f"❌ Error during blind level iteration: {e}")
            return False

    def extract_player_ranking_data(self, text_content, game=None):
        """Extract player name and points from the ranking table and store in database"""
        try:
            if game is None:
                game = self.game_name
            print("    🔍 Looking for playerRankingBody class...")
            # print("    🔄 Clicking leaderboard refresh button...")
            # refresh_button = WebDriverWait(self.driver, 10).until(
            #     EC.element_to_be_clickable((By.CLASS_NAME, "set-refresh"))
            # )
            # refresh_button.click()
            # time.sleep(5)  # Allow time for the table to render after selection
            # print("waited 5 seconds for table to render")
            ranking_bodies = self.driver.find_elements(
                By.CLASS_NAME, "playerRankingBody"
            )
            if not ranking_bodies:
                print("    ℹ️ No playerRankingBody found; skipping this blind level")
                return []
            ranking_body = ranking_bodies[0]
            print("    ✅ Found playerRankingBody element")

            # Find all tr tags within the ranking body
            tr_elements = ranking_body.find_elements(By.TAG_NAME, "tr")
            print(f"    📋 Found {len(tr_elements)} table rows")
            if not tr_elements:
                print("    ℹ️ No table rows found; skipping this blind level")
                return []

            # Get timestamp and blind level info
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {timestamp} - {text_content}")

            # Extract blind level from text_content (e.g., "$0.01/$0.02")
            blind_level = text_content.strip()

            leaderboard_game = f"{game}_{self.table_name}" if self.table_name else game
            table_name = self.db_manager.get_leaderboard_table_name(
                leaderboard_game, blind_level
            )
            print(f"   Table name: {table_name}")

            # Normalized path: create update batch and resolve leaderboard id
            lb_id = self.db_manager.get_or_create_leaderboard_id(table_name)
            update_id = self.db_manager.create_update_batch(timestamp)
            inserted_count = 0
            skipped_count = 0

            # Process each player row
            for i, tr in enumerate(tr_elements, 1):
                try:
                    td_elements = tr.find_elements(By.TAG_NAME, "td")
                    if len(td_elements) >= 4:
                        # name (td[1]), country (td[2]), points (td[3])
                        player_name = (
                            td_elements[1].get_attribute("textContent").strip()
                            if td_elements[1].get_attribute("textContent")
                            else ""
                        )
                        # Extract country from the <span> child with 'data-title' attribute
                        country = None
                        try:
                            span = td_elements[2].find_element(By.TAG_NAME, "span")
                            country = span.get_attribute("data-title")
                            if country:
                                country = country.strip()
                        except Exception:
                            country = None

                        points_text = (
                            td_elements[3].get_attribute("textContent").strip()
                            if td_elements[3].get_attribute("textContent")
                            else ""
                        )

                        if player_name and points_text:
                            try:
                                pts = float(points_text.replace(",", ""))
                            except Exception:
                                skipped_count += 1
                                continue

                            if (
                                i == 1
                                and self.duplicate_guard == (player_name, pts)
                                or self.duplicate_guard
                                == (
                                    player_name,
                                    pts + 1,
                                )  # sometimes the leaderboard doesn't update and the first place points can be off by 1ß
                            ):
                                print(
                                    f"    ⚠️ Duplicate first-place entry detected ({player_name}, {pts}); skipping insertion"
                                )
                                return  # Skip the rest of the leaderboard if the first entry is a duplicate
                            if i == 1:
                                self.duplicate_guard = (player_name, pts)

                            player_id = self.db_manager.get_or_create_player_id(
                                player_name, country
                            )
                            self.db_manager.insert_fact(
                                lb_id, update_id, player_id, pts
                            )
                            inserted_count += 1
                        else:
                            skipped_count += 1
                    else:
                        skipped_count += 1
                except Exception as e:
                    print(f"      ❌ Error processing row {i}: {e}")
                    skipped_count += 1
                    continue

            print(f"    ✅ Stored {inserted_count} player entries")
            if skipped_count:
                print(f"    ℹ️ Skipped {skipped_count} blank or invalid rows")

        except Exception as e:
            print(f"    ❌ Error extracting player ranking data: {e}")

        except Exception as e:
            print(f"    ❌ Error extracting player ranking data: {e}")
            return []

    def run_scraping_session(self):
        """Run the complete scraping session"""
        try:
            print("🚀 Starting GGPoker scraping session...")

            if not self.access_ggpoker_page():
                return False

            iframe = self.find_game_name_section()
            if iframe is None:
                return False

            WebDriverWait(self.driver, 10).until(
                EC.frame_to_be_available_and_switch_to_it(iframe)
            )
            print("✅ Switched into the leaderboard iframe")
            try:
                print("\n🎯 Now extracting blind levels from dropdown...")
                list_elements = self.get_blind_levels_from_dropdown()

                if list_elements:
                    print(
                        f"\n🎉 Successfully found {len(list_elements)} list elements!"
                    )
                    print("\n🔄 Now clicking through all blind levels...")
                    if self.click_through_blind_levels(list_elements):
                        print("✅ Successfully clicked through all blind levels")
                    else:
                        print(
                            "⚠️ Some issues occurred while clicking through blind levels"
                        )
                else:
                    print("⚠️ No list elements found in dropdown")
            finally:
                self.driver.switch_to.default_content()

            print("\n✅ Scraping session completed successfully!")
            return True

        except Exception as e:
            print(f"❌ Error during scraping session: {e}")
            return False

    def close(self):
        """Close the browser and database connection"""
        if self.driver:
            self.driver.quit()
            print("🔒 Browser closed")

        if hasattr(self, "db_manager"):
            self.db_manager.close()


def main():
    """Main function to run the scraper"""
    parser = argparse.ArgumentParser(description="Run the GGPoker scraper")
    parser.add_argument(
        "--headless", action="store_true", help="Run Chrome in headless mode"
    )
    default_interval_before = int(os.getenv("INTERVAL_BEFORE", "1"))
    default_interval_after = int(os.getenv("INTERVAL_AFTER", "25"))
    parser.add_argument(
        "--interval_before",
        type=int,
        default=default_interval_before,
        help="Seconds before each run; 0 runs once",
    )
    parser.add_argument(
        "--interval_after",
        type=int,
        default=default_interval_after,
        help="Seconds after each run; 0 runs once",
    )
    args = parser.parse_args()

    def run_once(headless_flag: bool) -> bool:
        scraper = None
        try:
            scraper = GGPokerScraper(headless=headless_flag)
            success = scraper.run_scraping_session()
            return success
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            return False
        finally:
            if scraper:
                scraper.close()

    if args.interval_before and args.interval_after:
        print(
            f"🔁 Running with {args.interval_before} seconds before and {args.interval_after} seconds after each run. Press Ctrl+C to stop."
        )
        try:
            while True:
                print(
                    f"\n⏳ Sleeping {args.interval_before} seconds before next run..."
                )
                time.sleep(args.interval_before)
                start_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n⏱️ {start_ts} - Starting run")
                run_once(args.headless)
                print(f"⏳ Sleeping {args.interval_after} seconds...")
                time.sleep(args.interval_after)
        except KeyboardInterrupt:
            print("\n⏹️ Stopped by user")
    else:
        print("⏱️ Running once with no intervals...")
        run_once(args.headless)


if __name__ == "__main__":
    main()

# docker-compose down
# DOCKER_BUILDKIT=0 docker-compose up -d --build
