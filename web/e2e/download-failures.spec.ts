import { expect, test } from "@playwright/test";

const task = {
  id: "00000000-0000-4000-8000-000000000001", title: "A synthetic title with enough words to verify narrow layout wrapping", status: "Failed", progress: 0, speed: "Failed", eta: "00:00:00",
  failure: { code: "SOURCE_FORBIDDEN", message: "The source provider refused access from this server.", guidance: "Check source permissions or contact the source provider.", stage: "source_preparation", input_role: "video", http_status: 403 },
};

for (const theme of ["ember", "aurora", "cinema"]) {
  for (const width of [320, 1440]) {
    test(`${theme} failure explanation at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.route("**/api/downloads/stream", route => route.fulfill({
        status: 200, contentType: "text/event-stream", body: `data: ${JSON.stringify([task])}\n\n`,
      }));
      for (const admin of [false, true]) {
        await page.goto(`/e2e/fixtures/download-failures.html?theme=${theme}&admin=${admin ? "1" : "0"}`);
        await expect(page.getByText("Source access denied (HTTP 403)")).toBeVisible();
        await expect(page.getByText(task.failure.message)).toBeVisible();
        const disclosure = page.locator("summary");
        await disclosure.focus();
        await expect(disclosure).toBeFocused();
        await page.keyboard.press("Enter");
        await expect(page.getByText(task.id)).toBeVisible();
        expect(await disclosure.evaluate(el => el.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44);
        const notice = page.locator(".download-failure");
        expect(await notice.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        expect(await notice.locator("p").first().evaluate(el => Number.parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(14);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath(`${theme}-${width}-${admin ? "admin" : "catalog"}.png`), fullPage: true });
        await page.reload();
        await expect(page.getByText(task.failure.message)).toBeVisible();
      }
    });
  }
}
