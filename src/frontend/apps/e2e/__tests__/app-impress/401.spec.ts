import { expect, test } from '@playwright/test';

test.describe('401', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test('checks all the elements are visible', async ({ page }) => {
    await page.goto('/401');

    await expect(
      page.getByRole('heading', { level: 1, name: 'Access denied' }),
    ).toBeVisible();
    await expect(
      page.getByText('Log in to access the document.'),
    ).toBeVisible();
    await expect(page.getByTestId('header-logo-link')).toBeVisible();
    await expect(
      page.getByRole('button', { name: 'Try it now' }),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(2);
    await expect(page).toHaveTitle(/401 Unauthorized - Docs/);
  });
});
