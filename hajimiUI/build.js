import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'

const root = path.dirname(fileURLToPath(import.meta.url))
const templatesDir = path.resolve(root, '../app/templates')
// Existing resources survive a failed build.
const staging = await fs.mkdtemp(path.join(root, '.build-'))
try {
  await build({ root, build: { outDir: staging, emptyOutDir: true } })
  await fs.mkdir(path.join(templatesDir, 'assets'), { recursive: true })
  await fs.cp(path.join(staging, 'assets'), path.join(templatesDir, 'assets'), { recursive: true })
  await fs.copyFile(path.join(staging, 'favicon.ico'), path.join(templatesDir, 'assets/favicon.ico'))
  const html = (await fs.readFile(path.join(staging, 'index.html'), 'utf8'))
    .replace('href="/favicon.ico"', 'href="/assets/favicon.ico"')
  const temporaryIndex = path.join(templatesDir, '.index.html.tmp')
  await fs.writeFile(temporaryIndex, html)
  await fs.rename(temporaryIndex, path.join(templatesDir, 'index.html'))
  // Keep earlier hashed assets for clients that loaded the old HTML.
  console.log('构建完成，模板已更新；已有资源保留以兼容打开中的页面。')
} finally {
  await fs.rm(staging, { recursive: true, force: true })
}
