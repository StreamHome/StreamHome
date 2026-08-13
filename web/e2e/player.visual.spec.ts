import { expect, test } from "@playwright/test";

const fixtureUrl = "/__player-visual-fixture";

async function startFixturePlayback(page: import("@playwright/test").Page) {
  const masterRequest = page.waitForResponse((response) => response.url().includes("/master.m3u8"));
  await page.goto(fixtureUrl);
  await masterRequest;
  const video = page.locator("video");
  await expect(video).toBeAttached();
  await video.evaluate(async (element: HTMLVideoElement) => {
    element.muted = true;
    await element.play();
  });
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime)).toBeGreaterThan(0);
  return video;
}

test("plays the prepared HLS fixture and keeps Pause authoritative", async ({ page }) => {
  const video = await startFixturePlayback(page);
  await page.mouse.move(40, 40);
  await page.mouse.move(55, 40);
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
  await page.getByRole("button", { name: "Pause" }).click();
  await expect(page.locator("[data-player-root='true']")).toHaveAttribute("data-player-phase", "paused");
  const pausedAt = await video.evaluate((element: HTMLVideoElement) => element.currentTime);
  await page.waitForTimeout(1_200);
  const afterWait = await video.evaluate((element: HTMLVideoElement) => element.currentTime);
  expect(afterWait).toBeLessThanOrEqual(pausedAt + 0.1);
  expect(await video.evaluate((element: HTMLVideoElement) => element.paused)).toBe(true);
});

test("keeps focused controls mounted and visible past the idle timeout", async ({ page }) => {
  await startFixturePlayback(page);
  const exitButton = page.getByRole("button", { name: "Exit player" });
  await exitButton.focus();
  await page.waitForTimeout(3_250);
  await expect(exitButton).toBeFocused();
  await expect(exitButton).toBeAttached();
  await expect(page.locator("[data-player-root='true']")).toHaveAttribute("data-controls-visible", "true");
});

test("renders only prepared HLS qualities with neutral Ember menu effects", async ({ page }) => {
  await startFixturePlayback(page);
  await page.getByRole("button", { name: "Quality: Auto" }).click();
  await expect(page.getByRole("option", { name: /540p/ })).toBeVisible();
  await expect(page.getByRole("option", { name: /480p/ })).toHaveCount(0);
  const menu = page.getByRole("listbox", { name: "Quality" });
  const effects = await menu.evaluate((element) => {
    const style = getComputedStyle(element);
    return { backdropFilter: style.backdropFilter, boxShadow: style.boxShadow };
  });
  expect(effects.backdropFilter).toBe("none");
  expect(effects.boxShadow).not.toContain("255, 95, 31");
});

test("serves progressive fixture ranges for browser fallback validation", async ({ request }) => {
  const response = await request.get("/__player-visual-fixture.mp4", {
    headers: { Range: "bytes=0-99" },
  });
  expect(response.status()).toBe(206);
  expect(response.headers()["content-range"]).toMatch(/^bytes 0-99\/\d+$/);
  expect((await response.body()).byteLength).toBe(100);
});
