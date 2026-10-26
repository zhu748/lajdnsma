// @vitest-environment node
import { it, expect } from 'vitest'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

it('preserves deployed assets on build failure and keeps hashed lazy imports intact', () => {
  const source = fileURLToPath(new URL('..', import.meta.url))
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gateway-build-test-'))
  const ui = path.join(temporary, 'hajimiUI')
  const templates = path.join(temporary, 'app/templates')
  try {
    fs.mkdirSync(path.join(ui, 'src'), { recursive: true })
    fs.mkdirSync(path.join(ui, 'public'))
    fs.mkdirSync(path.join(templates, 'assets'), { recursive: true })
    for (const filename of ['build.js', 'vite.config.js']) {
      fs.copyFileSync(path.join(source, filename), path.join(ui, filename))
    }
    fs.symlinkSync(path.join(source, 'node_modules'), path.join(ui, 'node_modules'), 'dir')
    fs.writeFileSync(path.join(ui, 'package.json'), '{"type":"module"}')
    fs.writeFileSync(path.join(ui, 'public/favicon.ico'), 'icon')
    fs.writeFileSync(path.join(ui, 'index.html'), '<link rel="icon" href="/favicon.ico"><script type="module" src="/src/main.js"></script>')
    fs.writeFileSync(path.join(templates, 'index.html'), 'old-html')
    fs.writeFileSync(path.join(templates, 'assets/old.js'), 'old-script')
    fs.writeFileSync(path.join(ui, 'src/main.js'), 'const = invalid;')
    const failed = spawnSync(process.execPath, ['build.js'], { cwd: ui, encoding: 'utf8' })
    expect(failed.status).not.toBe(0)
    expect(fs.readFileSync(path.join(templates, 'index.html'), 'utf8')).toBe('old-html')
    expect(fs.readFileSync(path.join(templates, 'assets/old.js'), 'utf8')).toBe('old-script')
    fs.writeFileSync(path.join(ui, 'src/main.js'), "window.load = () => import('./lazy.js')")
    fs.writeFileSync(path.join(ui, 'src/lazy.js'), 'export default 42')
    const passed = spawnSync(process.execPath, ['build.js'], { cwd: ui, encoding: 'utf8' })
    expect(passed.status, passed.stdout + passed.stderr).toBe(0)
    const html = fs.readFileSync(path.join(templates, 'index.html'), 'utf8')
    const asset = html.match(/src="\/assets\/([^"]+\.js)"/)[1]
    const script = fs.readFileSync(path.join(templates, 'assets', asset), 'utf8')
    const lazy = script.match(/\.\/([a-zA-Z0-9_-]*lazy[a-zA-Z0-9_-]*\.js)/)[1]
    expect(fs.existsSync(path.join(templates, 'assets', lazy))).toBe(true)
    expect(fs.existsSync(path.join(templates, 'assets/old.js'))).toBe(true)
    expect(fs.readdirSync(ui).filter(name => name.startsWith('.build-'))).toEqual([])
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true })
  }
}, 20000)
