import { expect, test } from '../fixtures/feature-test'

test('create and rename an organization folder in Knowledge Explorer', async ({page}) => {
  const original = 'Marketing ' + Date.now()
  const renamed = original + ' campaigns'
  await page.goto('/agents')
  const tree = page.getByRole('region', {name:'Folders'})
  await tree.getByRole('button', {name:'New folder', exact:true}).click()
  await page.getByPlaceholder('Folder name').fill(original)
  await page.locator('form').getByRole('button', {name:'New folder', exact:true}).click()
  await page.getByRole('button', {name:original, exact:true}).click()
  await page.getByRole('button', {name:'Folder settings', exact:true}).click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('form input').fill(renamed)
  await dialog.getByRole('button', {name:'Rename', exact:true}).click()
  await expect(dialog).toBeHidden()
  await page.getByRole('button', {name:'Back', exact:true}).click()
  await expect(page.getByRole('button', {name:renamed, exact:true})).toBeVisible()
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
  const tree = page.getByRole('region', {name:'Folders'})
  for (const name of [...names,'East','Campaigns','Quarterly review']) {
    await tree.getByRole('button', {name,exact:true}).click()
  }
  await tree.getByRole('button',{name:'Folder settings'}).click()
  const dialog = page.getByRole('dialog')
  for (let i=0;i<2;i++) await dialog.getByRole('button',{name:'Back',exact:true}).click()
  await dialog.getByRole('button',{name:'West',exact:true}).click()
  await dialog.getByRole('button',{name:'Campaigns',exact:true}).click()
  const pathToggle = dialog.getByRole('button', {name:'… / West / Campaigns'})
  await pathToggle.click()
  await expect(dialog.getByRole('button', {expanded:true})).toHaveText([...names, 'West', 'Campaigns'].join(' / '))
  await dialog.getByRole('button', {expanded:true}).click()
  await dialog.getByRole('button',{name:'Preview move'}).click()
  await expect(dialog.getByText('Access stays the same.')).toBeVisible()
  await page.screenshot({path:testInfo.outputPath('move-preview.png')})
  const [moveResponse] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/' + source + '/placement')
      && response.request().method() === 'PUT' && !response.request().postDataJSON().preview),
    dialog.getByRole('button',{name:'Move',exact:true}).click(),
  ])
  expect(moveResponse.ok(), await moveResponse.text()).toBeTruthy()
  await expect(dialog).toBeHidden()
  const rows = await (await page.request.get('/api/folders',{headers:scopedHeaders})).json()
  expect(rows.folders.find((f:{id:string})=>f.id===source).parent_id).toBe(destination)
  await tree.getByRole('button',{name:'Back',exact:true}).click()
  await expect(tree.getByRole('button',{name:'Quarterly review'})).toBeVisible()
  await tree.getByRole('button',{name:'Back',exact:true}).click()
  await tree.getByRole('button',{name:'Back',exact:true}).click()
  await tree.getByRole('button',{name:'Share',exact:true}).click()
  await expect(dialog.getByText('People with access')).toBeVisible()
  const inherited = dialog.getByText('Can manage · Via', {exact:false})
  await expect(inherited).toBeVisible()
  await dialog.getByRole('combobox').selectOption('Manager')
  await expect(dialog.getByRole('combobox')).toHaveValue('Manager')
  page.once('dialog', confirmation => confirmation.accept())
  await dialog.getByRole('checkbox', {name:'Inherit access from parent'}).uncheck()
  await expect(inherited).toHaveCount(0)
  await dialog.getByRole('checkbox', {name:'Inherit access from parent'}).check()
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
  await page.getByRole('option').filter({hasText:path}).click()
  const level = page.getByRole('combobox', {name:path})
  await expect(level.locator('option:checked')).toHaveText('Can query')
  await level.selectOption({label:'Can manage'})
  await page.screenshot({path:testInfo.outputPath('roles-folder.png')})
  await page.getByTestId('role-save').click()
  await page.getByTestId('role-row-Campaign editors '+stamp).click()
  await page.getByTestId('role-tab-access').click()
  await expect(level.locator('option:checked')).toHaveText('Can manage')

  await page.goto('/agents')
  const tree = page.getByRole('region',{name:'Folders'})
  const row = page.locator('[draggable=true]').filter({hasText:agent.name}).first()
  const dialog = page.getByRole('dialog')
  const start = async () => {
    await row.scrollIntoViewIfNeeded()
    const box = (await row.boundingBox())!
    await page.mouse.move(box.x + 15, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + 40, box.y + box.height / 2, {steps:6})
    await expect(tree.getByRole('button',{name:'Organization root'})).toBeVisible()
  }
  const drop = async (name:string) => {
    const target = tree.getByRole('button',{name,exact:true})
    await target.hover()
    await target.hover()
    await page.mouse.up()
    await expect(dialog.getByRole('button',{name:'Move',exact:true})).toBeEnabled()
    await page.screenshot({path:testInfo.outputPath(name === 'Organization root' ? 'drag-to-root.png' : 'drag-to-folder.png')})
    const [response] = await Promise.all([page.waitForResponse(r=>r.url().endsWith(agent.id+'/placement') &&
      !r.request().postDataJSON().preview), dialog.getByRole('button',{name:'Move',exact:true}).click()])
    expect(response.ok()).toBeTruthy()
    await expect(dialog).toBeHidden()
  }
  await start()
  const branch = tree.getByRole('button',{name:home,exact:true})
  await branch.hover()
  await branch.hover({position:{x:15,y:8}})
  await expect(tree.getByRole('button',{name:'Acquisition',exact:true})).toBeVisible({timeout:4000})
  await drop('Acquisition')
  await tree.getByRole('button',{name:home,exact:true}).click()
  await tree.getByRole('button',{name:'Acquisition',exact:true}).click()
  await start()
  await drop('Organization root')
  const persisted = await page.request.get('/api/data_sources/'+agent.id,{headers:scoped})
  expect((await persisted.json()).folder_id).toBeNull()
})

test('create and reopen a rule in the selected folder', async ({page}) => {
  await page.goto('/agents')
  const tree = page.getByRole('region', {name:'Folders'})
  const name = 'Routing ' + Date.now()
  await tree.getByRole('button', {name:'New folder', exact:true}).click()
  await tree.getByPlaceholder('Folder name').fill(name)
  await tree.locator('form').getByRole('button', {name:'New folder', exact:true}).click()
  await tree.getByRole('button', {name, exact:true}).click()
  const panel = page.getByTestId('folder-knowledge')
  const rules = panel.locator('section').filter({has:page.getByRole('heading', {name:'Instructions', exact:true})})
  await rules.getByRole('button', {name:'Create', exact:true}).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByPlaceholder('Title').fill('Paid attribution')
  await dialog.locator('textarea').fill('Use the attribution agent for customer acquisition cost.')
  await dialog.getByRole('button', {name:'Save', exact:true}).click()
  await expect(dialog).toBeHidden()
  await rules.getByRole('button', {name:'Paid attribution', exact:true}).click()
  await expect(page.getByText('Use the attribution agent for customer acquisition cost.', {exact:true}).first()).toBeVisible()
})
