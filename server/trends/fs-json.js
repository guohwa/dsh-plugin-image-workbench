// JSON 文件的可靠读写：先 fsync 临时文件再替换，并保留上一版；读坏文件时回退备份或隔离，绝不当成空文件。
import { open, readFile, rename } from 'node:fs/promises'

export async function writeJsonAtomic(file, value, { indent = 1 } = {}) {
  const tmp = `${file}.${process.pid}.tmp`
  const handle = await open(tmp, 'w', 0o600)
  try {
    await handle.writeFile(JSON.stringify(value, null, indent))
    await handle.sync()
  } finally {
    await handle.close()
  }
  // 两次 rename 之间主文件短暂不存在，读取方会落到 .bak，因此任何时刻至少有一份完整数据。
  await rename(file, `${file}.bak`).catch(err => { if (err.code !== 'ENOENT') throw err })
  await rename(tmp, file)
}

async function parseFile(file) {
  try {
    return { ok: true, value: JSON.parse(await readFile(file, 'utf8')) }
  } catch (err) {
    if (err.code === 'ENOENT') return { ok: false, missing: true }
    if (err instanceof SyntaxError) return { ok: false, missing: false }
    throw err
  }
}

/** @returns {Promise<{status: 'ok'|'backup'|'missing'|'corrupt', value: any}>} */
export async function readJsonSafe(file) {
  const main = await parseFile(file)
  if (main.ok) return { status: 'ok', value: main.value }
  const backup = await parseFile(`${file}.bak`)
  if (backup.ok) return { status: 'backup', value: backup.value }
  if (main.missing) return { status: backup.missing ? 'missing' : 'corrupt', value: undefined }
  await rename(file, `${file}.corrupt-${new Date().toISOString().replace(/[:.]/g, '-')}`)
  return { status: 'corrupt', value: undefined }
}
