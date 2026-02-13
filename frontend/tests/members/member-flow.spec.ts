import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

// ES Module equivalent of __dirname
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Load test user data created by global setup
function getTestUsers() {
  const testUsersPath = path.join(__dirname, '../config/test-users.json');
  return JSON.parse(fs.readFileSync(testUsersPath, 'utf-8'));
}

// Use serial to ensure invite runs before signup
test.describe.serial('Member Flow', () => {

  test('step 1: admin invites a new member', async ({ browser }) => {
    const { member } = getTestUsers();
    
    // Create context with admin auth
    const context = await browser.newContext({
      storageState: 'tests/config/admin.json'
    });
    const page = await context.newPage();

    // Navigate to settings/members
    await page.goto('/settings/members');
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(3000);

    // If redirected to onboarding, dismiss it first
    if (page.url().includes('/onboarding')) {
      const skipButton = page.getByRole('button', { name: 'Skip onboarding' });
      if (await skipButton.isVisible({ timeout: 10000 }).catch(() => false)) {
        await skipButton.click();
        await page.waitForURL((url) => !url.pathname.includes('/onboarding'), { timeout: 15000 });
      }
      // Now navigate to the actual target page
      await page.goto('/settings/members');
      await page.waitForLoadState('networkidle');
    }

    // Verify we're on the members page (longer timeout for CI)
    await expect(page.getByRole('heading', { name: 'Settings' }))
      .toBeVisible({ timeout: 30000 });

    // Check if member is already in the table (from previous run/retry)
    const existingMemberRow = page.locator('tr').filter({ hasText: member.email });
    const alreadyExists = await existingMemberRow.isVisible({ timeout: 2000 }).catch(() => false);
    
    if (alreadyExists) {
      // Member already exists from previous run/retry
      await context.close();
      return;
    }

    // Click Add Member button
    const addMemberButton = page.getByRole('button', { name: 'Add Member' });
    await expect(addMemberButton).toBeVisible({ timeout: 5000 });
    await addMemberButton.click();

    // Wait for modal to appear
    await expect(page.getByRole('heading', { name: 'Invite Member' }))
      .toBeVisible({ timeout: 5000 });

    // Fill in email
    await page.getByPlaceholder('member@example.com').fill(member.email);

    // Role dropdown should already be set to "Member" (default)

    // Click Send Invitation
    await page.getByRole('button', { name: 'Send Invitation' }).click();

    // Wait for success toast (contains "Invitation sent")
    await expect(page.getByText(/invitation sent/i)).toBeVisible({ timeout: 15000 });

    // Modal should close
    await expect(page.getByRole('heading', { name: 'Invite Member' }))
      .not.toBeVisible({ timeout: 5000 });

    // Verify member appears in the table with Pending status
    const memberRow = page.locator('tr').filter({ hasText: member.email });
    await expect(memberRow).toBeVisible({ timeout: 5000 });
    await expect(memberRow.getByText('Pending')).toBeVisible();

    await context.close();
  });

  test('step 2: invited user signs up and logs in', async ({ browser }) => {
    const { member } = getTestUsers();

    // Create fresh context (no auth)
    const context = await browser.newContext();
    const page = await context.newPage();

    // First, try to sign in - if user already exists from previous run
    await page.goto('/users/sign-in');
    await page.waitForLoadState('networkidle');

    // Try signing in first
    await page.waitForSelector('#email', { state: 'visible', timeout: 30000 });
    await page.fill('#email', member.email);
    await page.fill('#password', member.password);
    await page.click('button[type="submit"]');

    // Wait for result - longer timeout
    await page.waitForTimeout(3000);
    
    // Check if we navigated away from sign-in (success)
    if (!page.url().includes('/users/sign-in')) {
      // Member already exists from previous run, logged in instead
      await page.waitForLoadState('networkidle');
      await context.storageState({ path: 'tests/config/member.json' });
      await context.close();
      return;
    }

    // Sign-in failed, need to sign up
    
    await page.goto('/users/sign-up');
    await page.waitForLoadState('networkidle');

    // Wait for form to be visible
    await page.waitForSelector('#name', { state: 'visible', timeout: 30000 });

    // Fill the form with invited member's email - MUST match the invite
    await page.fill('#name', member.name);
    await page.fill('#email', member.email);
    await page.fill('#password', member.password);

    // Submit the form
    const submitButton = page.locator('button[type="submit"]');
    await submitButton.click();
    
    // Wait a moment for response
    await page.waitForTimeout(3000);

    // Wait for navigation (success) or error - longer timeout
    const result = await Promise.race([
      page.waitForURL((url) => !url.pathname.includes('/users/sign-up'), { timeout: 30000 })
        .then(() => 'navigated'),
      page.waitForSelector('.text-red-500', { timeout: 30000 })
        .then(() => 'error')
    ]);

    if (result === 'error') {
      const errorText = await page.locator('.text-red-500').textContent();
      
      // If email already registered, try signing in again
      if (errorText?.includes('already registered') || errorText?.includes('already exists')) {
        await page.goto('/users/sign-in');
        await page.waitForLoadState('networkidle');
        await page.waitForSelector('#email', { state: 'visible', timeout: 30000 });
        await page.fill('#email', member.email);
        await page.fill('#password', member.password);
        await page.click('button[type="submit"]');
        await page.waitForURL((url) => !url.pathname.includes('/users/sign-in'), { timeout: 30000 });
        await context.storageState({ path: 'tests/config/member.json' });
        await context.close();
        return;
      }
      
      if (errorText?.includes('must be invited') || errorText?.includes('registration is disabled')) {
        throw new Error(`Member was not invited properly. Run invite test first. Error: ${errorText}`);
      }
      throw new Error(`Member sign-up failed: ${errorText}`);
    }

    await page.waitForLoadState('networkidle');

    // Wait a moment for any redirects
    await page.waitForTimeout(3000);

    // Verify we're not on sign-up/sign-in page
    expect(page.url()).not.toContain('/users/sign-up');
    expect(page.url()).not.toContain('/users/sign-in');

    // If on onboarding, skip it (member joining existing org may see onboarding)
    if (page.url().includes('/onboarding')) {
      const skipButton = page.getByRole('button', { name: 'Skip onboarding' });
      if (await skipButton.isVisible({ timeout: 3000 }).catch(() => false)) {
        await skipButton.click();
        await page.waitForURL('/', { timeout: 10000 });
      }
    }

    // Save member's auth state
    await context.storageState({ path: 'tests/config/member.json' });

    await context.close();
  });
});

