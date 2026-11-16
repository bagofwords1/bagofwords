import type { Locator, Page } from '@playwright/test'
import { expect, test } from '../fixtures/feature-test'

// The agents tree on /agents: one "Agents" region holding folders and agents.
// Each folder row carries an expand chevron (aria-expanded, named after the
// folder) and an open button (titled with the folder name); its actions
// (New subfolder, Folder settings, Share) appear on hover.
const agentsTree = (page: Page) => page.getByRole('region', {name:'Agents'})
const folderOpen = (tree: Locator, name: string) => tree.getByTitle(name, {exact:true})
const expandFolder = (tree: Locator, name: string) =>
  tree.getByRole('button', {name, exact:true, expanded:false}).first().click()
// After a move or a drop the tree reveals the destination itself, so expand
// a folder only if it is still collapsed.
async function ensureExpanded(tree: Locator, name: string) {
  const collapsed = tree.getByRole('button', {name, exact:true, expanded:false})
  const open = tree.getByRole('button', {name, exact:true, expanded:true})
  await expect(collapsed.or(open).first()).toBeVisible()
  if (await open.count() === 0) await collapsed.first().click()
}
async function folderAction(tree: Locator, name: string, action: 'New subfolder' | 'Folder settings' | 'Share') {
  const open = folderOpen(tree, name)
  await open.hover()
  await open.locator('..').getByRole('button', {name:action, exact:true}).click()
}
async function createFolder(page: Page, name: string) {
  await agentsTree(page).getByRole('button', {name:'New folder', exact:true}).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('textbox', {name:'Folder name'}).fill(name)
  await dialog.getByRole('button', {name:'Create', exact:true}).click()
  await expect(dialog).toBeHidden()
  await expect(folderOpen(agentsTree(page), name)).toBeVisible()
}

test('create and rename an organization folder in Knowledge Explorer', async ({page}) => {
  const original = 'Marketing ' + Date.now()
  const renamed = original + ' campaigns'
  await page.goto('/agents')
  const tree = agentsTree(page)
  await createFolder(page, original)
  await folderAction(tree, original, 'Folder settings')
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('textbox', {name:'Folder name'}).fill(renamed)
  await dialog.getByRole('button', {name:'Rename', exact:true}).click()
  await expect(dialog).toBeHidden()
  await expect(folderOpen(tree, renamed)).toBeVisible()
  await expect(folderOpen(tree, original)).toHaveCount(0)
})

test('browse a 64-level hierarchy and move a leaf between same-named subfolders', async ({page}, testInfo) => {
  test.setTimeout(150_000)
  const cookie = (await page.context().cookies()).find(c => ['auth.token', 'auth_token'].includes(c.name))
  const token = decodeURIComponent(cookie!.value)
  const headers = { Authorization: token.startsWith('Bearer ') ? token : 'Bearer ' + token }
  const user = await page.request.get('/api/users/whoami', {headers})
  const org = (await user.json()).organizations[0].id
  const scopedHeaders = {...headers, 'X-Organization-Id': org}
  const names = Array.from({length:64}, (_, i) => i === 0 ? 'Deep QA ' + Date.now() : 'Planning region ' + i)
  names[63] = 'Customer acquisition and retention '.repeat(4).trim()
  const seed = async (segments: string[], parent_id?: string) => {
    const response = await page.request.post('/api/folders/ensure', {headers: scopedHeaders, data:{parent_id,path_segments:segments}})
    expect(response.ok(), await response.text()).toBeTruthy()
    return (await response.json()).id
  }
  const parent = await seed(names)
  const source = await seed(['East','Campaigns','Quarterly review'], parent)
  const destination = await seed(['West','Campaigns'], parent)
  await page.goto('/agents')
  const tree = agentsTree(page)
  // Browse all the way down; West stays collapsed, so one "Campaigns" shows.
  for (const name of [...names,'East','Campaigns']) await expandFolder(tree, name)
  await expect(folderOpen(tree, 'Quarterly review')).toBeVisible()

  await folderAction(tree, 'Quarterly review', 'Folder settings')
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('button', {name:'Move folder', exact:true}).click()
  await expect(dialog.getByRole('heading', {name:'Move Quarterly review?'})).toBeVisible()
  await expect(dialog.getByRole('definition').first()).toHaveText([...names, 'East', 'Campaigns'].join(' / '))
  // Destinations are full paths, so the two "Campaigns" folders stay distinct.
  await dialog.getByRole('combobox', {name:'To'}).selectOption({label:[...names, 'West', 'Campaigns'].join(' / ')})
  await expect(dialog.getByText('Inherited access stays the same.')).toBeVisible()
  await page.screenshot({path:testInfo.outputPath('move-preview.png')})
  const [moveResponse] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/' + source + '/placement')
      && response.request().method() === 'PUT' && !response.request().postDataJSON().preview),
    dialog.getByRole('button',{name:'Move folder',exact:true}).click(),
  ])
  expect(moveResponse.ok(), await moveResponse.text()).toBeTruthy()
  await expect(dialog).toBeHidden()
  const rows = await (await page.request.get('/api/folders',{headers:scopedHeaders})).json()
  expect(rows.folders.find((f:{id:string})=>f.id===source).parent_id).toBe(destination)
  // The leaf now lives under West / Campaigns (East's Campaigns is the one
  // already open, so any collapsed "Campaigns" left is West's).
  await ensureExpanded(tree, 'West')
  const westCampaigns = tree.getByRole('button', {name:'Campaigns', exact:true, expanded:false})
  if (await westCampaigns.count()) await westCampaigns.first().click()
  await expect(folderOpen(tree, 'Quarterly review')).toHaveCount(1)

  await folderAction(tree, names[1], 'Share')
  await expect(dialog.getByText('People with access')).toBeVisible()
  const inherited = dialog.getByText('Can manage · Via', {exact:false})
  await expect(inherited).toBeVisible()
  // Each change saves, then reloads the audience; on a 64-level branch that
  // reload is slow, so wait for it rather than for a fixed time.
  const reloaded = () => page.waitForResponse(r => r.url().endsWith('/audience') && r.request().method() === 'GET')
  await Promise.all([reloaded(), dialog.getByRole('combobox').selectOption('Manager')])
  await expect(dialog.getByRole('combobox')).toHaveValue('Manager')
  page.once('dialog', confirmation => confirmation.accept())
  await Promise.all([reloaded(), dialog.getByRole('checkbox', {name:'Inherit access from parent'}).uncheck()])
  await expect(inherited).toHaveCount(0)
  await Promise.all([reloaded(), dialog.getByRole('checkbox', {name:'Inherit access from parent'}).check()])
  await expect(inherited).toBeVisible()
  for (const width of [1360,390]) {
    await page.setViewportSize({width,height:820})
    const box = await dialog.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    expect(await page.locator('body').evaluate(e=>e.scrollWidth <= window.innerWidth)).toBeTruthy()
    await page.screenshot({path:testInfo.outputPath('share-'+width+'.png')})
  }
  await dialog.getByRole('button',{name:'Can manage · Via '+names[0]}).click()
  await expect(dialog.getByRole('heading').first()).toContainText(names[0])
})

test('manage folder access in Roles and drag an agent between a nested folder and root', async ({page}, testInfo) => {
  test.setTimeout(150_000)
  const raw = (await page.context().cookies()).find(c => ['auth.token', 'auth_token'].includes(c.name))!
  const token = decodeURIComponent(raw.value).replace(/^Bearer /, '')
  const headers = {Authorization: 'Bearer ' + token}
  const who = await (await page.request.get('/api/users/whoami', {headers})).json()
  const scoped = {...headers, 'X-Organization-Id':who.organizations[0].id}
  const post = async (path:string, data:object) => {
    const response = await page.request.post('/api/' + path, {headers:scoped,data})
    expect(response.ok(), await response.text()).toBeTruthy()
    return response.json()
  }
  const stamp = Date.now()
  const home = 'Channels ' + stamp
  const path = home + ' / Acquisition'
  await post('folders/ensure', {path_segments:[home, 'Acquisition']})
  const agent = await post('data_sources', {name:'Attribution '+stamp, type:'sqlite',
    config:{database:':memory:'}, credentials:{}, auth_policy:'system_only'})
  await page.goto('/settings/members?tab=roles')
  await page.getByRole('button', {name:'New Role'}).click()
  await page.locator('#role-name').fill('Campaign editors '+stamp)
  await page.getByTestId('role-tab-access').click()
  await page.getByTestId('role-add-access').click()
  await page.getByPlaceholder('Search…', {exact:true}).fill(path)
  await page.getByRole('button', {name: 'Acquisition ' + home, exact:true}).click()
  const level = page.getByRole('combobox', {name:path})
  await expect(level.locator('option:checked')).toHaveText('Can query')
  await level.selectOption({label:'Can manage'})
  await page.screenshot({path:testInfo.outputPath('roles-folder.png')})
  await page.getByTestId('role-save').click()
  await page.getByTestId('role-row-Campaign editors '+stamp).click()
  await page.getByTestId('role-tab-access').click()
  await expect(level.locator('option:checked')).toHaveText('Can manage')

  await page.goto('/agents')
  const tree = agentsTree(page)
  const row = tree.locator('[draggable=true]').filter({hasText:agent.name}).first()
  const dialog = page.getByRole('dialog')
  // Native drag and drop onto a folder row, or onto the "Agents" header for
  // the organization root; the drop opens the move confirmation.
  const dragTo = async (target: Locator, label: string) => {
    await row.scrollIntoViewIfNeeded()
    await row.dragTo(target)
    await expect(dialog.getByRole('combobox', {name:'To'}).locator('option:checked')).toHaveText(label)
    await expect(dialog.getByRole('button',{name:'Move agent',exact:true})).toBeEnabled()
    await page.screenshot({path:testInfo.outputPath(label === 'Organization root' ? 'drag-to-root.png' : 'drag-to-folder.png')})
    const [response] = await Promise.all([page.waitForResponse(r=>r.url().endsWith(agent.id+'/placement') &&
      !r.request().postDataJSON().preview), dialog.getByRole('button',{name:'Move agent',exact:true}).click()])
    expect(response.ok()).toBeTruthy()
    await expect(dialog).toBeHidden()
  }
  await expandFolder(tree, home)
  await dragTo(folderOpen(tree, 'Acquisition'), path)
  const moved = await page.request.get('/api/data_sources/'+agent.id,{headers:scoped})
  expect((await moved.json()).folder_id).not.toBeNull()
  await ensureExpanded(tree, 'Acquisition')
  await expect(row).toBeVisible()
  await dragTo(tree.getByText('Agents', {exact:true}), 'Organization root')
  const persisted = await page.request.get('/api/data_sources/'+agent.id,{headers:scoped})
  expect((await persisted.json()).folder_id).toBeNull()
})

test('create and reopen a rule in the selected folder', async ({page}) => {
  await page.goto('/agents')
  const tree = agentsTree(page)
  const name = 'Routing ' + Date.now()
  await createFolder(page, name)
  await folderOpen(tree, name).click()
  const panel = page.getByTestId('folder-knowledge')
  await panel.getByRole('button', {name:/^Instructions/}).click()
  await page.getByRole('button', {name:'+ Add', exact:true}).first().click()
  // The new instruction is scoped to the folder it was created from.
  await expect(page.getByRole('button', {name, exact:true}).last()).toBeVisible()
  await page.getByRole('textbox', {name:'Untitled instruction'}).fill('Paid attribution')
  const body = page.locator('[contenteditable=true]').last()
  await body.click()
  await body.pressSequentially('Use the attribution agent for customer acquisition cost.')
  await page.getByRole('button', {name:'Create', exact:true}).click()
  await expect(page.getByText('Paid attribution', {exact:true}).first()).toBeVisible()
  await page.getByText('Paid attribution', {exact:true}).first().click()
  await expect(page.getByText('Use the attribution agent for customer acquisition cost.', {exact:true}).first()).toBeVisible()
})
