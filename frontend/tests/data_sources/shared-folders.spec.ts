import { expect, test, type Page } from '../fixtures/feature-test'

// Folders live inline in the "Agents" section of Knowledge Explorer: each row
// has an expand chevron (named after the folder, aria-expanded), an open button
// (the folder name, titled with it) and, for managers, New subfolder / Folder
// settings / Share.
const agentsTree = (page: Page) => page.getByRole('region', {name:'Agents'})
const folderRow = (page: Page, name: string) => agentsTree(page).locator('div')
  .filter({has: page.getByTitle(name, {exact:true})})
  .filter({has: page.getByRole('button', {name:'Folder settings', exact:true})}).last()
const expandFolder = (page: Page, name: string) =>
  agentsTree(page).getByRole('button', {name, exact:true, expanded:false}).click()

const apiHeaders = async (page: Page) => {
  const cookie = (await page.context().cookies()).find(c => ['auth.token', 'auth_token'].includes(c.name))!
  const token = decodeURIComponent(cookie.value).replace(/^Bearer /, '')
  const headers = {Authorization: 'Bearer ' + token}
  const who = await (await page.request.get('/api/users/whoami', {headers})).json()
  return {...headers, 'X-Organization-Id': who.organizations[0].id}
}

const createFolder = async (page: Page, name: string) => {
  await agentsTree(page).getByRole('button', {name:'New folder', exact:true}).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('textbox', {name:'Folder name'}).fill(name)
  await dialog.getByRole('button', {name:'Create', exact:true}).click()
  await expect(dialog).toBeHidden()
  await expect(agentsTree(page).getByTitle(name, {exact:true})).toBeVisible()
}

test('create and rename an organization folder in Knowledge Explorer', async ({page}) => {
  const original = 'Marketing ' + Date.now()
  const renamed = original + ' campaigns'
  await page.goto('/agents')
  await createFolder(page, original)
  await folderRow(page, original).getByRole('button', {name:'Folder settings', exact:true}).click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('form input').fill(renamed)
  await dialog.getByRole('button', {name:'Rename', exact:true}).click()
  await expect(dialog).toBeHidden()
  await expect(agentsTree(page).getByTitle(renamed, {exact:true})).toBeVisible()
  await expect(agentsTree(page).getByTitle(original, {exact:true})).toHaveCount(0)
  // The rename is persisted, not only shown.
  const rows = await (await page.request.get('/api/folders', {headers: await apiHeaders(page)})).json()
  expect(rows.folders.some((f:{name:string}) => f.name === renamed)).toBeTruthy()
})

test('browse a 64-level hierarchy and move a leaf between same-named subfolders', async ({page}, testInfo) => {
  test.setTimeout(150_000)
  const scopedHeaders = await apiHeaders(page)
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
  for (const name of [...names,'East','Campaigns']) await expandFolder(page, name)
  await expect(tree.getByTitle('Quarterly review', {exact:true})).toBeVisible()
  await folderRow(page, 'Quarterly review').getByRole('button', {name:'Folder settings', exact:true}).click()
  await page.getByRole('dialog').getByRole('button', {name:'Move folder', exact:true}).click()
  // Settings closes as the move dialog opens; address the move dialog by its title.
  const dialog = page.getByRole('dialog').filter({has: page.getByRole('heading', {name:'Move Quarterly review?', exact:true})})
  // The two "Campaigns" folders are told apart by their full path.
  const westPath = [...names, 'West', 'Campaigns'].join(' / ')
  await expect(dialog.getByRole('combobox', {name:'To'}).locator('option', {hasText: westPath})).toHaveCount(1)
  await dialog.getByRole('combobox', {name:'To'}).selectOption({label: westPath})
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
  // The tree opens the destination and shows the leaf there.
  await expect(tree.getByRole('button', {name:'West', exact:true, expanded:true})).toBeVisible()
  await expect(tree.getByTitle('Quarterly review', {exact:true})).toBeVisible()

  await folderRow(page, 'Quarterly review').getByRole('button', {name:'Share', exact:true}).click()
  const share = page.getByRole('dialog').filter({has: page.getByRole('heading', {name:'Share “Quarterly review”', exact:true})})
  await expect(share.getByText('People with access')).toBeVisible()
  const inherited = share.getByText('Can manage · Via', {exact:false})
  await expect(inherited).toBeVisible()
  await share.getByRole('combobox').selectOption('Manager')
  await expect(share.getByRole('combobox')).toHaveValue('Manager')
  page.once('dialog', confirmation => confirmation.accept())
  await share.getByRole('checkbox', {name:'Inherit access from parent'}).uncheck()
  await expect(inherited).toHaveCount(0)
  await share.getByRole('checkbox', {name:'Inherit access from parent'}).check()
  await expect(inherited).toBeVisible()
  for (const width of [1360,390]) {
    await page.setViewportSize({width,height:820})
    const box = await share.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    expect(await page.locator('body').evaluate(e=>e.scrollWidth <= window.innerWidth)).toBeTruthy()
    await page.screenshot({path:testInfo.outputPath('share-'+width+'.png')})
  }
  // The inherited grant links to the folder it comes from.
  await share.getByRole('button',{name:'Can manage · Via '+names[0]}).click()
  await expect(page.getByRole('dialog').getByRole('heading', {name:'Share “' + names[0] + '”', exact:true})).toBeVisible()
})

test('manage folder access in Roles', async ({page}, testInfo) => {
  const scoped = await apiHeaders(page)
  const stamp = Date.now()
  const home = 'Channels ' + stamp
  const path = home + ' / Acquisition'
  const ensured = await page.request.post('/api/folders/ensure', {headers:scoped, data:{path_segments:[home, 'Acquisition']}})
  expect(ensured.ok(), await ensured.text()).toBeTruthy()
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
})

test('drag an agent between a nested folder and root', async ({page}, testInfo) => {
  test.setTimeout(150_000)
  const scoped = await apiHeaders(page)
  const post = async (path:string, data:object) => {
    const response = await page.request.post('/api/' + path, {headers:scoped,data})
    expect(response.ok(), await response.text()).toBeTruthy()
    return response.json()
  }
  const stamp = Date.now()
  const home = 'Channels ' + stamp
  const acquisition = await post('folders/ensure', {path_segments:[home, 'Acquisition']})
  const agent = await post('data_sources', {name:'Attribution '+stamp, type:'sqlite',
    config:{database:':memory:'}, credentials:{}, auth_policy:'system_only'})

  await page.goto('/agents')
  const tree = agentsTree(page)
  const row = tree.locator('[draggable=true]').filter({hasText:agent.name}).first()
  const dialog = page.getByRole('dialog')
  const drag = async (target: ReturnType<Page['locator']>, shot: string, expected: string | null) => {
    await row.scrollIntoViewIfNeeded()
    await row.dragTo(target)
    await expect(dialog.getByRole('heading')).toHaveText(`Move ${agent.name}?`)
    await expect(dialog.getByRole('button',{name:'Move agent',exact:true})).toBeEnabled()
    await page.screenshot({path:testInfo.outputPath(shot)})
    const [response] = await Promise.all([page.waitForResponse(r=>r.url().endsWith(agent.id+'/placement') &&
      r.request().method() === 'PUT' && !r.request().postDataJSON().preview), dialog.getByRole('button',{name:'Move agent',exact:true}).click()])
    expect(response.ok()).toBeTruthy()
    await expect(dialog).toBeHidden()
    const persisted = await page.request.get('/api/data_sources/'+agent.id,{headers:scoped})
    expect((await persisted.json()).folder_id).toBe(expected)
  }
  await expandFolder(page, home)
  await drag(folderRow(page, 'Acquisition'), 'drag-to-folder.png', acquisition.id)
  // The agent is now listed inside the (expanded) nested folder; drop it on the
  // section header, which is the organization root.
  await expect(tree.getByRole('button', {name:'Acquisition', exact:true, expanded:true})).toBeVisible()
  await drag(tree.getByText('Agents', {exact:true}), 'drag-to-root.png', null)
})

test('create and reopen a rule in the selected folder', async ({page}) => {
  await page.goto('/agents')
  const name = 'Routing ' + Date.now()
  await createFolder(page, name)
  await agentsTree(page).getByTitle(name, {exact:true}).click()
  const panel = page.getByTestId('folder-knowledge')
  await panel.getByRole('button', {name:/^Instructions/}).click()
  await panel.getByRole('button', {name:'+ Add', exact:true}).click()
  await page.getByRole('textbox', {name:'Untitled instruction'}).fill('Paid attribution')
  await page.locator('.ProseMirror').first().fill('Use the attribution agent for customer acquisition cost.')
  await page.getByRole('button', {name:'Create', exact:true}).click()
  await expect(page.getByRole('textbox', {name:'Untitled instruction'})).toBeHidden()
  await agentsTree(page).getByTitle(name, {exact:true}).click()
  await panel.getByRole('button', {name:/^Instructions/}).click()
  await panel.getByText('Paid attribution', {exact:true}).click()
  await expect(page.getByText('Use the attribution agent for customer acquisition cost.', {exact:true}).first()).toBeVisible()
})
