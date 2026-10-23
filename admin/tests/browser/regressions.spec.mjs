import { test, expect } from '@playwright/test'

for (const theme of ['light', 'dark']) {
  for (const width of [1440, 390]) {
    test(`Supply Depot menu escapes clipping at ${width}px in ${theme} mode`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await page.goto('/')
      if (theme === 'dark') await page.getByRole('button', { name: 'Toggle theme' }).click()
      const trigger = page.getByRole('button', { name: 'More actions' })
      await trigger.click()
      const menu = page.getByRole('menu')
      await expect(menu).toBeVisible()
      expect(await menu.evaluate(element => {
        const box = element.getBoundingClientRect()
        return box.left >= 0 && box.top >= 0 && box.right <= innerWidth && box.bottom <= innerHeight &&
          [...element.querySelectorAll('[role="menuitem"]')].every(item => {
            const b = item.getBoundingClientRect()
            return item.contains(document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2))
          })
      })).toBe(true)
      await page.keyboard.press('ArrowDown')
      await page.keyboard.press('Escape')
      await expect(menu).toHaveCount(0)
      await expect(trigger).toBeFocused()
      await trigger.click()
      await page.getByRole('menuitem', { name: 'Logs', exact: true }).click()
      await expect(page.getByTestId('action')).toHaveText('logs')
      await expect(menu).toHaveCount(0)
    })
  }
}

test('the production map worker renders a vector feature and camera controls work', async ({ page }) => {
  const errors = []
  const failures = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  page.on('response', response => { if (response.status() >= 400) failures.push(response.url()) })
  await page.goto('/')
  const worker = await page.request.get('/maplibre-gl-worker.mjs')
  expect(worker.ok()).toBe(true)
  expect(worker.headers()['content-type']).toContain('javascript')
  await expect(page.locator('.maplibregl-canvas')).toBeVisible()
  await expect.poll(async () => Number(await page.getByTestId('features').innerText())).toBeGreaterThan(0)
  await page.locator('.maplibregl-ctrl-zoom-in').click()
  await expect.poll(async () => Number(await page.getByTestId('zoom').innerText())).toBeGreaterThan(6)
  await page.getByRole('button', { name: 'Fly to location' }).click()
  await expect(page.getByTestId('zoom')).toHaveText('8')
  expect(errors).toEqual([])
  expect(failures).toEqual([])
})
