import { expect, test } from "@playwright/test";

test("creates, persists, and deletes an item", async ({ page }) => {
  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));

  const value = `E2E item ${Date.now()}`;
  await page.goto("/");
  await expect(page.getByTestId("app-root")).toBeVisible();

  await page.getByTestId("primary-input").fill(value);
  await page.getByTestId("create-submit").click();

  let item = page.getByTestId("resource-item").filter({ hasText: value });
  await expect(item).toBeVisible();

  await page.reload();
  item = page.getByTestId("resource-item").filter({ hasText: value });
  await expect(item).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await item.getByTestId("delete-button").click();
  await expect(item).toHaveCount(0);
  expect(browserErrors).toEqual([]);
});
