// 公众号文章抓取：给「我的写作风格」直接导入线上已发布文章用。
// 只依赖 node 内置模块，绕开宿主全局代理（global fetch 会被代理 dispatcher 带偏）。
import { request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { brotliDecompressSync, gunzipSync, inflateSync } from 'node:zlib'

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
const MAX_BYTES = 8 * 1024 * 1024
const MAX_LINKS = 10
const DEFAULT_LIMIT = 8

/** 从任意粘贴文本里挑出链接，兼容带 &amp; 的分享串和中文标点结尾。 */
export function extractLinks(text) {
  const matches = String(text ?? '').match(/https?:\/\/[^\s"'`<>，。；、）】》]+/gi) ?? []
  const seen = new Set()
  const links = []
  for (const raw of matches) {
    let url = raw.replace(/&amp;/gi, '&')
    const hash = url.indexOf('#')
    if (hash > 0) url = url.slice(0, hash)
    url = url.replace(/[.,;:!?)\]}]+$/g, '')
    if (!/^https?:\/\/[^/]+/i.test(url)) continue
    if (seen.has(url)) continue
    seen.add(url)
    links.push(url)
    if (links.length >= MAX_LINKS) break
  }
  return links
}

function fetchPage(url, { timeout = 20000, signal, redirects = 4 } = {}) {
  return new Promise((resolve, reject) => {
    let done = false
    const finish = (err, value) => {
      if (done) return
      done = true
      if (err) reject(err)
      else resolve(value)
    }

    let target
    try {
      target = new URL(url)
    } catch {
      finish(new Error('链接格式不对'))
      return
    }
    const mod = target.protocol === 'http:' ? httpRequest : httpsRequest
    const req = mod(target, {
      method: 'GET',
      headers: {
        'user-agent': UA,
        accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'accept-encoding': 'gzip, deflate, br',
        'cache-control': 'no-cache',
      },
    }, res => {
      const status = res.statusCode ?? 0
      const location = res.headers.location
      if ([301, 302, 303, 307, 308].includes(status) && location) {
        res.resume()
        if (redirects <= 0) {
          finish(new Error('跳转次数过多'))
          return
        }
        done = true
        fetchPage(new URL(location, url).toString(), { timeout, signal, redirects: redirects - 1 }).then(resolve, reject)
        return
      }
      const chunks = []
      let size = 0
      res.on('data', chunk => {
        size += chunk.length
        if (size > MAX_BYTES) {
          req.destroy(new Error('页面太大，已放弃抓取'))
          return
        }
        chunks.push(chunk)
      })
      res.on('end', () => {
        let buf = Buffer.concat(chunks)
        const encoding = String(res.headers['content-encoding'] || '').toLowerCase()
        try {
          if (encoding.includes('br')) buf = brotliDecompressSync(buf)
          else if (encoding.includes('gzip')) buf = gunzipSync(buf)
          else if (encoding.includes('deflate')) buf = inflateSync(buf)
        } catch { /* 解不开就按原文试，微信多数时候是明文 */ }
        finish(null, { status, body: buf.toString('utf8'), finalUrl: url })
      })
      res.on('error', err => finish(err))
    })
    req.setTimeout(timeout, () => req.destroy(new Error('抓取超时')))
    req.on('error', err => finish(err))
    if (signal) {
      if (signal.aborted) {
        req.destroy(new Error('已取消'))
        return
      }
      signal.addEventListener('abort', () => req.destroy(new Error('已取消')), { once: true })
    }
    req.end()
  })
}

const NAMED_ENTITIES = {
  nbsp: ' ', amp: '&', lt: '<', gt: '>', quot: '"', apos: "'",
  mdash: '—', ndash: '–', hellip: '…', middot: '·', bull: '•',
  ldquo: '“', rdquo: '”', lsquo: '‘', rsquo: '’',
  ensp: ' ', emsp: ' ', thinsp: ' ', copy: '©', reg: '®', trade: '™', times: '×',
}

function safeCodePoint(code) {
  try {
    return String.fromCodePoint(code)
  } catch {
    return ''
  }
}

/** 解 HTML 实体，含数字实体与常见命名实体。 */
export function decodeEntities(text) {
  return String(text ?? '')
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => safeCodePoint(Number.parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, dec) => safeCodePoint(Number.parseInt(dec, 10)))
    .replace(/&([a-z][a-z0-9]*);/gi, (all, name) => NAMED_ENTITIES[name.toLowerCase()] ?? all)
}

function metaContent(html, keys) {
  const wanted = new Set(keys.map(k => k.toLowerCase()))
  const tags = String(html ?? '').match(/<meta\b[^>]*>/gi) ?? []
  for (const tag of tags) {
    const attrs = {}
    for (const m of tag.matchAll(/([a-z:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))/gi)) {
      attrs[m[1].toLowerCase()] = m[3] ?? m[4] ?? m[5] ?? ''
    }
    const key = String(attrs.property || attrs.name || '').toLowerCase()
    if (!wanted.has(key)) continue
    const value = attrs.content ?? ''
    if (value.trim()) return decodeEntities(value).trim()
  }
  return ''
}

function scriptString(html, name) {
  const patterns = [
    new RegExp(`var\\s+${name}\\s*=\\s*"([^"]*)"`),
    new RegExp(`var\\s+${name}\\s*=\\s*'([^']*)'`),
    new RegExp(`${name}\\s*=\\s*"([^"]*)"`),
  ]
  for (const re of patterns) {
    const m = re.exec(html)
    if (m) return m[1]
  }
  return ''
}

/** 从起始标签处按同名标签配对，切出完整元素（微信正文是深层嵌套 div）。 */
function sliceBalanced(html, startIdx, tag = 'div') {
  const openRe = new RegExp(`<${tag}\\b`, 'gi')
  const closeRe = new RegExp(`</${tag}\\s*>`, 'gi')
  openRe.lastIndex = startIdx
  let depth = 0
  let cursor = startIdx
  while (cursor < html.length) {
    openRe.lastIndex = cursor
    closeRe.lastIndex = cursor
    const open = openRe.exec(html)
    const close = closeRe.exec(html)
    if (!close) return html.slice(startIdx)
    if (open && open.index < close.index) {
      depth += 1
      cursor = open.index + open[0].length
      continue
    }
    depth -= 1
    cursor = close.index + close[0].length
    if (depth <= 0) return html.slice(startIdx, cursor)
  }
  return html.slice(startIdx)
}

function findByAttr(html, attr, value) {
  const re = new RegExp(`<div\\b[^>]*${attr}\\s*=\\s*["']${value}["'][^>]*>`, 'i')
  const m = re.exec(html)
  if (!m) return ''
  return sliceBalanced(html, m.index, 'div')
}

const BOILERPLATE = new Set([
  '预览时标签不可点', '微信扫一扫', '继续滑动看下一个', '向上滑动看下一个',
  '轻点两下取消赞', '轻点两下取消在看', '长按识别二维码关注', '长按识别二维码',
  '关注该公众号', '阅读原文', '分享', '点赞', '在看', '留言', '收藏', '知道了',
  '视频', '小程序', '已同步到看一看', '写下你的评论', '取消', '允许',
  '使用完整服务', '取消允许', '打开', '点击上方蓝字关注我们', '点击蓝字关注',
])

function isBoilerplate(line) {
  const text = line.trim()
  if (!text) return false
  if (BOILERPLATE.has(text)) return true
  if (/^(收录于合集|预览时标签不可点|阅读\s*\d+|赞\s*\d+|在看\s*\d+)/.test(text)) return true
  if (/^[·.\-—_=*~\s]{1,6}$/.test(text)) return true
  // 公众号常见的手势引导符号串，如 ↘↘↘、↓↓↓
  if (/^[↘↙→←↓↑⬇⬆\s]{1,10}$/.test(text)) return true
  return false
}

/** 正文 HTML 转纯文本：保留段落换行，去掉脚本样式图片与页尾固定话术。 */
export function htmlToText(html) {
  let text = String(html ?? '')
  text = text.replace(/<!--[\s\S]*?-->/g, '')
  text = text.replace(/<(script|style|svg|noscript|iframe|video|audio|canvas)\b[\s\S]*?<\/\1>/gi, '')
  text = text.replace(/<(script|style|svg|link|meta|source)\b[^>]*\/?>/gi, '')
  text = text.replace(/<img\b[^>]*>/gi, '\n')
  text = text.replace(/<br\s*\/?>/gi, '\n')
  text = text.replace(/<\/(p|div|section|h[1-6]|li|blockquote|tr|td|th|figure|figcaption|header|footer|article|aside)\s*>/gi, '\n')
  text = text.replace(/<[^>]+>/g, '')
  text = decodeEntities(text)
  text = text.replace(/\u00a0|\u3000/g, ' ')
  text = text.replace(/[ \t\f\v]+/g, ' ')

  const kept = []
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!trimmed) {
      if (kept.length && kept[kept.length - 1] !== '') kept.push('')
      continue
    }
    if (isBoilerplate(trimmed)) continue
    kept.push(trimmed)
  }
  return kept.join('\n').replace(/\n{3,}/g, '\n\n').trim()
}

function formatPublishTime(ct) {
  const seconds = Number(ct)
  if (!Number.isFinite(seconds) || seconds <= 0) return ''
  const date = new Date(seconds * 1000)
  const pad = n => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

const BLOCKED = [
  ['环境异常', '微信要求先完成验证，请在微信里打开这篇文章再复制链接'],
  ['该内容已被发布者删除', '这篇文章已被发布者删除'],
  ['此内容因违规无法查看', '这篇文章因违规无法查看'],
  ['链接已过期', '链接已过期'],
  ['该公众号已迁移', '该公众号已迁移，请用新链接'],
  ['参数错误', '链接参数不完整，请重新复制整条链接'],
]

/** 抓一篇：返回标题、正文、作者、发布日期。 */
export async function fetchArticle(url, { timeout = 20000, signal } = {}) {
  const page = await fetchPage(url, { timeout, signal })
  if (page.status === 404) throw new Error('页面不存在（404）')
  if (page.status === 403) throw new Error('被微信拦截（403），链接可能已失效或需要验证')
  if (page.status >= 400) throw new Error(`抓取失败（HTTP ${page.status}）`)

  const html = page.body
  for (const [needle, reason] of BLOCKED) {
    if (html.includes(needle) && !html.includes('id="js_content"')) throw new Error(reason)
  }

  const contentHtml = findByAttr(html, 'id', 'js_content') || findByAttr(html, 'class', 'rich_media_content')
  const text = htmlToText(contentHtml || '')
  if (text.length < 30) throw new Error('没抓到正文，可能需要验证或这篇文章是纯图片')

  const rawTitle = [
    /<h1[^>]*class="[^"]*rich_media_title[^"]*"[^>]*>([\s\S]*?)<\/h1>/i.exec(html)?.[1],
    /<h1[^>]*id="activity-name"[^>]*>([\s\S]*?)<\/h1>/i.exec(html)?.[1],
    metaContent(html, ['og:title', 'twitter:title']),
    /<span[^>]*class="[^"]*js_title_inner[^"]*"[^>]*>([\s\S]*?)<\/span>/i.exec(html)?.[1],
    scriptString(html, 'msg_title'),
    /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html)?.[1],
  ].find(value => typeof value === 'string' && htmlToText(value).trim())

  const title = htmlToText(rawTitle ?? '').replace(/\s+/g, ' ').trim().slice(0, 120) || '未命名文章'
  const byline = (() => {
    const candidates = [
      metaContent(html, ['og:article:author', 'article:author', 'author']),
      /<a[^>]*id="js_name"[^>]*>([\s\S]*?)<\/a>/i.exec(html)?.[1],
      /<strong[^>]*class="[^"]*profile_nickname[^"]*"[^>]*>([\s\S]*?)<\/strong>/i.exec(html)?.[1],
      scriptString(html, 'nickname'),
      scriptString(html, 'user_name'),
    ]
    for (const value of candidates) {
      const cleaned = htmlToText(value ?? '').trim().slice(0, 60)
      if (cleaned && !/^gh_[0-9a-z]+$/i.test(cleaned)) return cleaned
    }
    return ''
  })()
  const account = htmlToText(/<a[^>]*id="js_name"[^>]*>([\s\S]*?)<\/a>/i.exec(html)?.[1] ?? '').trim().slice(0, 60)

  return {
    url: page.finalUrl,
    title,
    text,
    author: byline,
    account,
    publishedAt: formatPublishTime(scriptString(html, 'ct')),
    chars: text.length,
    source: /mp\.weixin\.qq\.com|weixin\.qq\.com/.test(page.finalUrl) ? 'wechat' : 'web',
  }
}

/**
 * 批量抓取。输入可以是多条链接，也可以是带标题的整段分享文本。
 * 单条失败不影响其他条，失败原因逐条返回给页面。
 */export async function fetchArticles(input, { limit = DEFAULT_LIMIT, timeout = 20000, signal } = {}) {
  const links = extractLinks(input)
  if (!links.length) {
    throw Object.assign(new Error('没找到链接。把公众号文章链接整段粘进来就行，例如 https://mp.weixin.qq.com/s/xxxx'), { status: 400 })
  }
  const capped = links.slice(0, Math.max(1, Math.min(limit, MAX_LINKS)))
  const items = []
  const failed = []
  const seenText = new Set()
  for (const url of capped) {
    if (signal?.aborted) break
    try {
      const article = await fetchArticle(url, { timeout, signal })
      const key = article.text.slice(0, 200)
      if (seenText.has(key)) {
        failed.push({ url, reason: '和前面抓到的是同一篇，已跳过' })
        continue
      }
      seenText.add(key)
      items.push(article)
    } catch (err) {
      failed.push({ url, reason: err?.message || '抓取失败' })
    }
  }
  return { items, failed, requested: links.length, truncated: links.length > capped.length }
}

const BARE_LINK_MAX = 600

/** 这段粘贴内容是不是「只贴了链接」：几百字以内、开头就是链接、去掉链接后几乎没正文。 */
export function isBareLinkSample(sample) {
  const text = String(sample?.text ?? '').trim()
  if (!text || text.length > BARE_LINK_MAX) return false
  if (!/^https?:\/\//i.test(text)) return false
  return text.replace(/https?:\/\/\S+/gi, ' ').trim().length <= 40
}

/**
 * 把「只贴了链接」的样本自动抓成正文，用户直接往粘贴框里丢链接也能用。
 * 其余样本原样保留；抓不到的链接记在 notes 里，不打断整次分析。
 */
export async function expandLinkOnlySamples(samples, { limit = 8, signal } = {}) {
  const list = Array.isArray(samples) ? samples : []
  const out = []
  const notes = []
  for (const sample of list) {
    if (!isBareLinkSample(sample)) {
      out.push(sample)
      continue
    }
    try {
      const result = await fetchArticles(String(sample.text), { limit, signal })
      for (const article of result.items) out.push({ title: article.title, text: article.text })
      for (const failure of result.failed) notes.push(`${failure.url}：${failure.reason}`)
    } catch (err) {
      notes.push(`${String(sample.text).slice(0, 80)}：${err?.message || '抓取失败'}`)
    }
  }
  return { samples: out, notes }
}
