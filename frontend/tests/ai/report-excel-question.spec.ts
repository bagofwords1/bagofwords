import { test, expect } from '@playwright/test';
import { createReport, attachFileInReport } from '../utils/helpers';

test('question AI', async ({ page }) => {
  const prompt = 'What is the schema of the excel file?';
  
  await createReport(page);
  await attachFileInReport(page);
  
  // Write question in the input
  await page.locator('div[contenteditable="true"]').fill(prompt);
  
  // Click submit button with arrow icon
  await page.locator('button .i-heroicons\\:arrow-right').click();
  
  // Wait for AI response
  // Wait for the response list to appear
  const responseList = page.locator('ul');
  await responseList.waitFor({ state: 'visible' });

  // Verify the prompt appears in the first list item
  const promptLi = responseList.locator('li').first();
  await promptLi.waitFor({ state: 'visible', timeout: 1000000 }); // Add explicit wait for promptLi
  await expect(promptLi).toContainText(prompt);

});
