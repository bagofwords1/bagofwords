import { expect, Page } from '@playwright/test';

export async function login(page: Page) {
  await page.goto('/users/login');
  await page.fill('#email', 'yochay@gmail.com');
  await page.fill('#password', 'Password123!');
  await page.click('button[type="submit"]');
}

export async function createReport(page: Page) {
    // Navigate to excel home page
    await page.waitForLoadState('domcontentloaded');
  
    const newReportDiv = page.locator('div.flex.cursor-pointer', {
      hasText: 'Create a new report to analyze your data'
    });
    
    await newReportDiv.waitFor({ state: 'visible' });
    await expect(newReportDiv).toBeEnabled();
    await page.waitForTimeout(1000);
    await newReportDiv.click({ force: true });
    await page.waitForURL(/\/excel\/reports\/.*/);
  }

  export async function attachFileInReport(page: Page) {
    
    // Click the Add Files button
    await page.getByRole('button', { name: '+ Add Files' }).click();
    
    // dialog
    // Verify dialog is open
    const dialog = page.locator('[data-headlessui-state="open"]');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('heading')).toHaveText('Upload files');
    await expect(dialog.getByText('Upload excel files to analyze')).toBeVisible();
    

    // Set up file input handling
    const fileInput = dialog.locator('input[type="file"]');
    
    // Upload the test file
    await fileInput.setInputFiles('tests/fixtures/aggregated_customer_transactions.xlsx');
    
    // verify upload started 
    await expect(dialog.locator('.i-heroicons\\:arrow-path-rounded-square.animate-spin')).toBeVisible();
    
    
    // successfully uploaded 
    await expect(dialog.locator('li')).toContainText('aggregated_customer_transactions.xlsx');
    await expect(dialog.locator('.i-heroicons\\:check')).toBeVisible();
  }