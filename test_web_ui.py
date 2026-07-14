#!/usr/bin/env python3
"""Quick Playwright smoke test for the LSI RAID Monitor Web UI."""

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

URL = "http://127.0.0.1:5200/"
SHOT = "/tmp/lsi_raid_page.png"
SHOT2 = "/tmp/lsi_raid_drawer.png"


def run_test():
    console_logs = []
    page_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        try:
            page.goto(URL, wait_until="networkidle")
            # Wait for Vue app to render
            page.locator("#app").wait_for(state="visible", timeout=10000)
            page.screenshot(path=SHOT, full_page=True)

            # Try opening the first disk slot detail drawer
            slots = page.locator(".disk-slot")
            try:
                slots.first.wait_for(state="visible", timeout=10000)
                slots.first.click()
                page.wait_for_timeout(800)
                page.screenshot(path=SHOT2, full_page=True)
                drawer_title = (
                    page.locator(".el-drawer__header span").first.inner_text()
                )
                print("Drawer title:", drawer_title)
            except PlaywrightTimeout:
                print("No visible disk slots found")
                page.screenshot(path=SHOT2, full_page=True)

            title = page.title()
            body_text = page.locator("body").inner_text().replace("\n", " ")[:500]
            visible = page.locator("#app").is_visible()

            print("URL:", URL)
            print("Title:", title)
            print("#app visible:", visible)
            print("Body preview:", body_text)
        finally:
            print("Console logs:", len(console_logs))
            for log in console_logs:
                print(" ", log)
            print("Page errors:", len(page_errors))
            for err in page_errors:
                print(" ", err)
            print("Screenshot:", SHOT)
            browser.close()


if __name__ == "__main__":
    run_test()
