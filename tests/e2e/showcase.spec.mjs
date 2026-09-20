import { expect, test } from "@playwright/test";

const liveCdc = process.env.PLAYWRIGHT_LIVE_CDC === "1";

test("a user can connect to the redacted showcase and start its fixed source demonstration", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/showcase/");
  await expect(page.getByRole("heading", { name: "Watch a decision earn its evidence." })).toBeVisible();
  await expect(page.locator('[data-lane="source"]')).toBeVisible();
  await expect(page.locator('[data-lane="cdc"]')).toBeVisible();
  await expect(page.locator('[data-lane="agent"]')).toBeVisible();
  await expect(page.locator('[data-lane="transaction"]')).toBeVisible();

  if (liveCdc) {
    await page.locator("#run-id").fill(`playwright-live-${Date.now()}`);
  }
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByRole("button", { name: "Start run" })).toBeEnabled();
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page.locator("[data-message-title]")).toHaveText("Command accepted");
  if (liveCdc) {
    await expect(page.locator('[data-events="source"]')).toContainText("source_changed");
    await expect(page.locator('[data-events="cdc"]')).toContainText("projection_applied");
    await expect(page.locator('[data-events="agent"]')).toContainText("agent_decision");
    await expect(page.locator('[data-events="transaction"]')).toContainText("reserve_carrier");
  }
  await expect(page.locator("body")).not.toContainText("agentic-saga");
  expect(pageErrors).toEqual([]);
});
