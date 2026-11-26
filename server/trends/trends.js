// 每日热榜 + 爆文库：全部自采自存，数据落在 <DSH_HOME>/wechat-studio/trends/。
// 不读其它插件的快照，不依赖任何付费数据接口。
// 网络走 node:http(s) 直连：宿主给全局 fetch 挂了代理 dispatcher，用它会被带偏。
import { request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { brotliDecompressSync, gunzipSync, inflateSync } from 'node:zlib'
import { randomUUID } from 'node:crypto'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { readJsonSafe, writeJsonAtomic } from './fs-json.js'
import { extractLinks, fetchArticle } from './wechat-article.js'

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
// AIHOT（aihot.news）的官方 JSON 接口，个人非商业使用免费。热度/关联信源都来自它：
// 热点榜的「热度」= 同时在讨论这件事的信源数，资讯的「热度」= 站方给出的 score。
const AIHOT_UA = 'aihot-api/2.0.0'
const AIHOT_API = 'https://aihot.news/api/v1'

/** AIHOT 是境外站，直连多半超时；直连失败就用宿主环境变量里的代理再试一次。 */
export async function aihotJson(path, { timeout = 12000 } = {}) {
  const url = `${AIHOT_API}${path}`
  try {
    return await getJson(url, { timeout, ua: AIHOT_UA })
  } catch (err) {
    try {
      return await getJson(url, { timeout, ua: AIHOT_UA, proxy: true })
    } catch {
      throw err
    }
  }
}
const KEEP_DAYS = 7
const MAX_BYTES = 6 * 1024 * 1024

export const PLATFORMS = ['今日头条', '百度', '抖音', '微博', '知乎', 'B站', 'AI 热点', 'AI 资讯']

function pad(n) {
  return String(n).padStart(2, '0')
}

function localDate(at = new Date()) {
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`
}

function stamp(at = new Date()) {
  return `${localDate(at)} ${pad(at.getHours())}:${pad(at.getMinutes())}`
}

function hash(text) {
  let h = 5381
  const s = String(text ?? '')
  for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0
  return h.toString(36)
}

/** 宿主给的代理（境外站点直连一般不通，只有它跑得通）。 */
function proxyOf() {
  const raw = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy || ''
  if (!raw) return null
  try {
    const u = new URL(raw)
    return u.protocol === 'http:' || u.protocol === 'https:' ? u : null
  } catch {
    return null
  }
}

/** 用 HTTP CONNECT 打一条隧道，返回裸 socket 交给 https 自己做 TLS。 */
function tunnel(proxy, target, timeout) {
  return new Promise((resolve, reject) => {
    const port = Number(target.port || 443)
    const req = httpRequest({
      host: proxy.hostname,
      port: Number(proxy.port || 80),
      method: 'CONNECT',
      path: `${target.hostname}:${port}`,
      headers: { host: `${target.hostname}:${port}` },
    })
    req.setTimeout(Math.min(timeout, 8000), () => req.destroy(new Error('代理握手超时')))
    req.on('connect', (res, socket) => {
      if (res.statusCode !== 200) {
        socket.destroy()
        reject(new Error(`代理拒绝 CONNECT（HTTP ${res.statusCode}）`))
        return
      }
      resolve(socket)
    })
    req.on('error', reject)
    req.end()
  })
}

/** 发一个 GET，自己解压、自己跟跳转。proxy:true 时走环境变量里的 HTTP 代理。 */
async function request(url, { timeout = 9000, redirects = 3, referer, ua, proxy = false } = {}) {
  let target
  try {
    target = new URL(url)
  } catch {
    throw new Error('链接格式不对')
  }
  let socket = null
  if (proxy && target.protocol === 'https:') {
    const p = proxyOf()
    if (p) socket = await tunnel(p, target, timeout)
  }
  return new Promise((resolve, reject) => {
    const mod = target.protocol === 'http:' ? httpRequest : httpsRequest
    const req = mod(target, {
      method: 'GET',
      ...(socket ? { createConnection: () => socket } : {}),
      headers: {
        'user-agent': ua || UA,
        accept: 'application/json,text/html,application/xml;q=0.9,*/*;q=0.8',
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'accept-encoding': 'gzip, deflate, br',
        ...(referer ? { referer } : {}),
      },
    })
    const timer = setTimeout(() => req.destroy(new Error('请求超时')), timeout)
    req.on('error', err => {
      clearTimeout(timer)
      reject(err)
    })
    req.on('response', res => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location && redirects > 0) {
        clearTimeout(timer)
        res.resume()
        request(new URL(res.headers.location, target).href, { timeout, redirects: redirects - 1, referer, ua, proxy }).then(resolve, reject)
        return
      }
      const chunks = []
      let size = 0
      res.on('data', chunk => {
        size += chunk.length
        if (size > MAX_BYTES) {
          req.destroy(new Error('响应太大'))
          return
        }
        chunks.push(chunk)
      })
      res.on('end', () => {
        clearTimeout(timer)
        let buf = Buffer.concat(chunks)
        const encoding = String(res.headers['content-encoding'] ?? '').toLowerCase()
        try {
          if (encoding.includes('br')) buf = brotliDecompressSync(buf)
          else if (encoding.includes('gzip')) buf = gunzipSync(buf)
          else if (encoding.includes('deflate')) buf = inflateSync(buf)
        } catch {}
        resolve({ status: res.statusCode ?? 0, body: buf.toString('utf8') })
      })
      res.on('error', err => {
        clearTimeout(timer)
        reject(err)
      })
    })
    req.end()
  })
}

/** 取 JSON，顺带把非 2xx 变成带原因的错误。 */
async function getJson(url, opts) {
  const res = await request(url, opts)
  if (res.status < 200 || res.status >= 300) throw new Error(`HTTP ${res.status}`)
  try {
    return JSON.parse(res.body)
  } catch {
    throw new Error('返回内容不是 JSON')
  }
}

function num(value) {
  const n = Number(String(value ?? '').replace(/[^\d.]/g, ''))
  return Number.isFinite(n) && n > 0 ? Math.round(n) : 0
}

function clean(text) {
  return String(text ?? '').replace(/\s+/g, ' ').trim()
}

/** 「1366 万热度」→ 13660000 */
function heatText(text) {
  const s = clean(text)
  const m = /([\d.]+)\s*(亿|万)?/.exec(s)
  if (!m) return 0
  const base = Number(m[1])
  if (!Number.isFinite(base)) return 0
  if (m[2] === '亿') return Math.round(base * 100000000)
  if (m[2] === '万') return Math.round(base * 10000)
  return Math.round(base)
}

// ── 8 个来源 ────────────────────────────────────────────────────
/** 从 RSS 文本里抠出条目（title/link/pubDate），够用就行，不引 XML 库。 */
export function parseRss(body) {
  const out = []
  for (const raw of String(body ?? '').split(/<item[\s>]/i).slice(1)) {
    const chunk = raw.slice(0, 6000)
    const pick = tag => (new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`, 'i').exec(chunk) ?? [])[1] ?? ''
    const title = clean(cleanHtml(pick('title')))
    const link = clean(cleanHtml(pick('link')))
    if (!title || !link) continue
    const at = Date.parse(clean(pick('pubDate'))) || 0
    out.push({ title, link, at })
  }
  return out
}

/** 「AI 热点」只留跟大模型沾边的标题，省得整块变成泛科技列表。 */
export const AI_RE = /(?:\bAI\b|AIGC|人工智能|大模型|模型|智能体|\bAgent\b|多模态|算力|推理|训练|英伟达|NVIDIA|OpenAI|Anthropic|ChatGPT|GPT-?\d|Claude|Gemini|DeepSeek|Qwen|通义|文心|豆包|Kimi|智谱|机器人|具身|Sora|Midjourney|Llama)/i

// ── AIHOT（aihot.news）───────────────────────────────────────────
// 个人非商业使用免费；商业用途需自行取得授权。热度口径：
//   热点榜 hot = 此刻同时报道这件事的信源数；资讯 hot = 站方给的 score。
/** 热点榜 + 每条热点的「来龙去脉」（多段事实综述，拿来当写稿素材）。 */
export async function loadAihotHotTopics() {
  const j = await aihotJson('/hot-topics')
  const items = Array.isArray(j?.items) ? j.items : []
  if (!items.length) throw new Error('热点榜是空的')
  const list = items.slice(0, 30)
  // 单条综述拿不到不影响整块，没拿到就只留标题
  const briefs = await Promise.allSettled(
    list.slice(0, 12).map(it => (it.storyPublicId
      ? aihotJson('/stories/' + it.storyPublicId, { timeout: 8000 })
      : Promise.reject(new Error('没有 story id')))),
  )
  return list.map((it, i) => {
    const story = briefs[i]?.status === 'fulfilled' ? briefs[i].value?.story : null
    return {
      rank: Number(it.rank) || i + 1,
      title: clean(it.title),
      hot: Number(it.sourceCount) || 0,
      url: clean(it.links?.original) || clean(it.links?.aihot) || '',
      sources: Array.isArray(it.sourceNames) ? it.sourceNames.slice(0, 12) : [],
      summary: clean(story?.digest || story?.latest || '').slice(0, 1500),
      at: Date.parse(it.latestAt) || 0,
    }
  })
}

/** 精选资讯：热度=score，带中文摘要。24 小时不够 5 条就放宽到 7 天。 */
export async function loadAihotItems() {
  let j = await aihotJson('/items?window=24h&mode=selected&limit=20')
  let items = Array.isArray(j?.items) ? j.items : []
  if (items.length < 5) {
    j = await aihotJson('/items?window=7d&mode=selected&limit=20')
    items = Array.isArray(j?.items) ? j.items : []
  }
  if (!items.length) throw new Error('精选条目是空的')
  return items.slice(0, 20).map((it, i) => ({
    rank: i + 1,
    title: clean(it.title),
    hot: Number(it.score) || 0,
    url: clean(it.links?.original) || clean(it.links?.aihot) || '',
    summary: clean(it.summary).slice(0, 600),
    note: clean(it.category),
    at: Date.parse(it.publishedAt) || 0,
  }))
}

const SOURCES = [
  {
    name: '今日头条',
    async load() {
      const data = await getJson('https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc', { referer: 'https://www.toutiao.com/' })
      return (data?.data ?? []).slice(0, 30).map((item, i) => ({
        rank: i + 1,
        title: clean(item.Title),
        hot: num(item.HotValue),
        url: item.Url ?? '',
      }))
    },
  },
  {
    name: '百度',
    async load() {
      // 网页版带 hotScore，接口版没有；先试网页，退回去用接口。
      try {
        const page = await request('https://top.baidu.com/board?tab=realtime', { referer: 'https://top.baidu.com/' })
        const words = [...page.body.matchAll(/"word":"((?:[^"\\]|\\.){2,120})"/g)].map(m => m[1].replace(/\\(["\\/])/g, '$1'))
        const scores = [...page.body.matchAll(/"hotScore":"?(\d+)"?/g)].map(m => Number(m[1]))
        if (words.length >= 10) {
          return words.slice(0, 30).map((word, i) => ({
            rank: i + 1,
            title: clean(word),
            hot: scores[i] || 0,
            url: 'https://www.baidu.com/s?wd=' + encodeURIComponent(word),
          }))
        }
      } catch {}
      const data = await getJson('https://top.baidu.com/api/board?platform=wise&tab=realtime', { referer: 'https://top.baidu.com/' })
      const rows = data?.data?.cards?.[0]?.content?.[0]?.content ?? []
      return rows.slice(0, 30).map((item, i) => ({
        rank: item.index ?? i + 1,
        title: clean(item.word),
        hot: num(item.hotScore),
        url: item.url ?? '',
      }))
    },
  },
  {
    name: '抖音',
    async load() {
      const data = await getJson('https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/')
      return (data?.word_list ?? []).slice(0, 30).map((item, i) => ({
        rank: i + 1,
        title: clean(item.word),
        hot: num(item.hot_value),
        url: 'https://www.douyin.com/search/' + encodeURIComponent(clean(item.word)),
      }))
    },
  },
  {
    name: '微博',
    async load() {
      const data = await getJson('https://weibo.com/ajax/side/hotSearch', { referer: 'https://weibo.com/' })
      const rows = data?.data?.realtime ?? []
      return rows
        .filter(item => !item.is_ad)
        .slice(0, 30)
        .map((item, i) => ({
          rank: item.realpos ?? i + 1,
          title: clean(item.word_scheme || item.word),
          hot: num(item.num),
          url: 'https://s.weibo.com/weibo?q=' + encodeURIComponent(item.word_scheme || `#${item.word}#`),
        }))
    },
  },
  {
    name: '知乎',
    async load() {
      const data = await getJson('https://api.zhihu.com/topstory/hot-list?limit=30', { referer: 'https://www.zhihu.com/hot' })
      const rows = data?.data ?? []
      return rows
        .filter(item => item?.target?.title)
        .map((item, i) => {
          const id = String(item.target.id ?? '').replace(/^\D+/, '')
          return {
            rank: i + 1,
            title: clean(item.target.title),
            hot: heatText(item.detail_text),
            url: id ? `https://www.zhihu.com/question/${id}` : '',
          }
        })
    },
  },
  {
    name: 'B站',
    async load() {
      const data = await getJson('https://api.bilibili.com/x/web-interface/popular?ps=30&pn=1', { referer: 'https://www.bilibili.com/' })
      if (data?.code !== 0) throw new Error(`B 站返回 ${data?.code ?? '未知错误'}`)
      return (data?.data?.list ?? []).map((item, i) => ({
        rank: i + 1,
        title: clean(item.title),
        hot: num(item?.stat?.view),
        url: item.bvid ? `https://www.bilibili.com/video/${item.bvid}` : '',
      }))
    },
  },
  {
    name: 'AI 热点',
    async load() {
      try {
        return await loadAihotHotTopics()
      } catch (err) {
        // 兜底：两家科技媒体的 RSS，没有热度但至少不空着
        const feeds = ['https://www.infoq.cn/feed', 'https://www.ithome.com/rss/']
        const settled = await Promise.allSettled(feeds.map(url => request(url, { timeout: 12000 })))
        const all = []
        for (const item of settled) {
          if (item.status !== 'fulfilled') continue
          all.push(...parseRss(item.value.body))
        }
        if (!all.length) throw err
        const seen = new Set()
        const rows = []
        for (const item of all) {
          const title = clean(item.title)
          if (!title || seen.has(title)) continue
          seen.add(title)
          if (!AI_RE.test(title)) continue
          rows.push({ title, link: item.link, at: item.at })
        }
        const list = rows.length >= 5 ? rows : all
        list.sort((a, b) => (b.at || 0) - (a.at || 0))
        return list.slice(0, 20).map((item, i) => ({
          rank: i + 1,
          title: clean(item.title),
          hot: 0,
          url: item.link ?? '',
        }))
      }
    },
  },
  {
    name: 'AI 资讯',
    async load() {
      try {
        return await loadAihotItems()
      } catch (err) {
        // 兜底：量子位 RSS
        const page = await request('https://www.qbitai.com/feed', { timeout: 12000 })
        const items = page.body.split(/<item>/i).slice(1)
        const rows = []
        for (const raw of items) {
          const title = clean(cleanHtml((/<title>([\s\S]*?)<\/title>/i.exec(raw) ?? [])[1] ?? ''))
          const link = clean((/<link>([\s\S]*?)<\/link>/i.exec(raw) ?? [])[1] ?? '')
          if (!title || !link) continue
          rows.push({ rank: rows.length + 1, title, hot: 0, url: link })
          if (rows.length >= 30) break
        }
        if (!rows.length) throw err
        return rows
      }
    },
  },
]

function cleanHtml(text) {
  return String(text ?? '')
    .replace(/<!\[CDATA\[|\]\]>/g, '')
    .replace(/<[^>]+>/g, '')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
}

// ── 热榜 ────────────────────────────────────────────────────────
const hotFile = root => join(root, 'trends', 'hot.json')

function hotView(snap) {
  const byDate = snap?.byDate ?? {}
  const dates = Object.keys(byDate).sort()
  const today = localDate()
  const latest = byDate[today] ? today : dates[dates.length - 1] ?? ''
  return {
    ok: true,
    today: latest,
    dates,
    platforms: PLATFORMS,
    fetchedAt: snap?.updatedAt ?? '',
    byDate,
    sources: snap?.sources ?? [],
  }
}

async function refreshHot(root, previous) {
  const settled = await Promise.all(
    SOURCES.map(async source => {
      try {
        const rows = await source.load()
        return { name: source.name, rows, ok: true }
      } catch (err) {
        return { name: source.name, rows: [], ok: false, error: String(err?.message ?? err) }
      }
    }),
  )
  const day = localDate()
  const rows = []
  const sources = []
  for (const result of settled) {
    sources.push({ platform: result.name, ok: result.ok, count: result.rows.length, error: result.error ?? null })
    result.rows.forEach((row, i) => {
      const title = clean(row.title)
      if (!title) return
      rows.push({
        key: `${result.name}|${row.rank ?? i + 1}|${hash(title)}`,
        platform: result.name,
        rank: row.rank ?? i + 1,
        title,
        hot: row.hot ?? 0,
        url: row.url ?? '',
        ...(row.sources?.length ? { sources: row.sources } : {}),
        ...(row.summary ? { summary: row.summary } : {}),
        ...(row.note ? { note: row.note } : {}),
        date: day,
      })
    })
  }
  if (!rows.length) throw new Error('八个来源一条都没抓到，大概率是断网或被拦了')
  const byDate = { ...(previous?.byDate ?? {}), [day]: rows }
  const keep = Object.keys(byDate).sort().slice(-KEEP_DAYS)
  const pruned = {}
  for (const date of keep) pruned[date] = byDate[date]
  const snap = { v: 2, updatedAt: stamp(), byDate: pruned, sources }
  await mkdir(join(root, 'trends'), { recursive: true })
  await writeJsonAtomic(hotFile(root), snap)
  return snap
}

/** 读热榜；当天没数据就顺手抓一次。 */
export async function hotViewFor(root, { refresh = false } = {}) {
  const { value } = await readJsonSafe(hotFile(root))
  const today = localDate()
  const todayRows = value?.byDate?.[today] ?? []
  // 加了新来源之后，当天快照里会缺这一列，顺手补抓一次，免得新卡片一直空着
  const missing = PLATFORMS.some(p => !todayRows.some(row => row.platform === p))
  // 旧快照里的 AI 热点/AI 资讯 没有热度和原文（v1 时代抓的 RSS），看到就重抓一次
  const staleAi = todayRows.some(row => (row.platform === 'AI 热点' || row.platform === 'AI 资讯') && !row.hot)
  if (refresh || value?.v !== 2 || !value?.byDate?.[today] || missing || staleAi) {
    return hotView(await refreshHot(root, value))
  }
  return hotView(value)
}

/** 强制重抓，返回视图 + 每个来源的成败。 */
export async function refreshHotFor(root) {
  const { value } = await readJsonSafe(hotFile(root))
  return hotView(await refreshHot(root, value))
}

// ── 爆文库 ──────────────────────────────────────────────────────
const viralFile = root => join(root, 'trends', 'viral.json')

function viralCats(rows) {
  const cats = []
  for (const row of rows) {
    for (const cat of row.cats ?? []) if (!cats.includes(cat)) cats.push(cat)
  }
  return cats
}

function viralView(state) {
  const rows = state?.rows ?? []
  const byDate = {}
  for (const row of rows) {
    const date = row.date || localDate()
    ;(byDate[date] ??= []).push(row)
  }
  const dates = Object.keys(byDate).sort()
  return {
    ok: true,
    today: dates[dates.length - 1] ?? '',
    dates,
    categories: viralCats(rows).length ? viralCats(rows) : ['公众号'],
    fetchedAt: state?.updatedAt ?? '',
    byDate,
    total: rows.length,
  }
}

export async function viralViewFor(root) {
  const { value } = await readJsonSafe(viralFile(root))
  return viralView(value)
}

/** 收录一篇爆文：能抓正文就抓，抓不到就用手填的标题存下来。 */
export async function addViral(root, body = {}) {
  // 三种录入方式：粘链接 / 粘整段分享文本 / 只填标题
  const input = clean(body.url || body.text || body.title || '')
  if (!input) throw Object.assign(new Error('请粘贴公众号文章链接'), { status: 400 })

  const { value } = await readJsonSafe(viralFile(root))
  const rows = value?.rows ?? []

  let title = clean(body.title)
  let account = clean(body.account)
  let url = ''
  let chars = 0
  let snippet = ''
  let cat = clean(body.cat)

  // 粘整段分享文本也行：从里面挑出第一条链接
  const link = extractLinks(input)[0] ?? ''
  if (link) {
    url = link
    if (rows.some(row => row.url === url)) throw Object.assign(new Error('这篇已经在库里了'), { status: 409 })
    try {
      const article = await fetchArticle(url, { timeout: 20000 })
      title = title || clean(article.title)
      account = account || clean(article.account || article.author)
      chars = article.chars ?? 0
      snippet = clean(article.text).slice(0, 160)
      if (!cat) cat = article.source === 'wechat' ? '公众号' : '网页'
    } catch (err) {
      if (!title) {
        throw Object.assign(new Error(`抓不到正文：${String(err?.message ?? err)}。可以把标题填上再存一次。`), { status: 502 })
      }
      if (!cat) cat = '链接'
    }
  } else {
    // 只有标题：手动录入
    title = title || input
    if (!cat) cat = '手动'
  }

  if (!title) throw Object.assign(new Error('没拿到标题'), { status: 400 })

  const row = {
    pid: randomUUID(),
    title,
    account,
    hot: num(body.hot),
    url,
    date: clean(body.date) || localDate(),
    cats: [cat],
    chars,
    snippet,
    addedAt: stamp(),
  }
  const next = { v: 1, updatedAt: stamp(), rows: [row, ...rows].slice(0, 500) }
  await mkdir(join(root, 'trends'), { recursive: true })
  await writeJsonAtomic(viralFile(root), next)
  return viralView(next)
}

export async function removeViral(root, body = {}) {
  const pid = clean(body.pid)
  const { value } = await readJsonSafe(viralFile(root))
  const rows = value?.rows ?? []
  const rest = rows.filter(row => row.pid !== pid)
  if (rest.length === rows.length) throw Object.assign(new Error('找不到这条记录'), { status: 404 })
  const next = { v: 1, updatedAt: stamp(), rows: rest }
  await mkdir(join(root, 'trends'), { recursive: true })
  await writeJsonAtomic(viralFile(root), next)
  return viralView(next)
}
