/**
 * 从 server/changelog.json 生成本次 GitHub Release 的标题与说明。
 *
 * 为什么要有这个脚本：发版靠打 tag 触发 CI，但 GitHub 的 Releases 列表只认
 * Release 对象，光推 tag 不会出现在那里 —— 结果就是「npm 已经发了新版，手机上
 * 看到的 Latest 还是几天前那个」。1.1.56 就漏过一次。现在 tag 进来后由 CI 直接
 * 建 Release，标题和条目都取自 changelog，和插件里「这次更新了什么」是同一份文案。
 *
 * 用法：node scripts/release-notes.mjs [--version 1.1.56] [--since v1.1.56]
 * 产出：release-title.txt、release-notes.md（都在当前目录）
 *
 * --since 是给「跨多版一次性发布」用的：平时每版都打 tag，Release 只需要写这一版；
 * 但连续几版没发版时（如 1.1.110~1.1.120），只写最新一版的几条会把中间改动全丢掉，
 * 用户看到的 Release 说明和实际升级后的变化对不上。带上 --since <上一个已发布 tag>
 * 就会把这之间的所有版本按顺序合并成一份说明。
 */
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const pkgVersion = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version;

const argv = process.argv.slice(2);
const argOf = (name) => {
  const i = argv.indexOf(name);
  return i >= 0 ? String(argv[i + 1] || '').trim() : '';
};
const version = argOf('--version') || pkgVersion;
const since = argOf('--since').replace(/^v/i, '');

let list = [];
try {
  list = JSON.parse(fs.readFileSync(path.join(root, 'server/changelog.json'), 'utf8'));
  if (!Array.isArray(list)) list = [];
} catch (e) {
  console.log('读 changelog 失败：' + e.message);
}

const at = (v) => list.findIndex((x) => String(x.version) === String(v));
let picked = [];
const curIdx = at(version);
if (curIdx >= 0) {
  let stopIdx = -1; // 不合并时只取当前这一版
  if (since) {
    const sinceIdx = at(since);
    // changelog 是新版在前；区间 = (since, 当前]，再翻转成由旧到新
    if (sinceIdx > curIdx) stopIdx = sinceIdx;
  }
  picked = list.slice(curIdx, stopIdx < 0 ? curIdx + 1 : stopIdx).reverse();
}
if (!picked.length) picked = [{ version, items: [] }];

const top = picked[picked.length - 1] || {};
const short = String(top.title || '').trim();
const title = short ? `v${version} ${short}` : `v${version}`;
const multi = picked.length > 1;

const lines = [];
lines.push(multi
  ? `### 这一版改了什么（含 ${picked[0].version} → ${version} 共 ${picked.length} 个版本）`
  : '### 这一版改了什么', '');
for (const entry of picked) {
  const items = (entry.items || []).map((it) => String(it).trim()).filter(Boolean);
  if (multi) {
    const day = String(entry.date || '').trim();
    lines.push(`#### v${entry.version}${day ? `（${day}）` : ''}`, '');
  }
  if (items.length) {
    for (const it of items) lines.push('- ' + it);
  } else {
    lines.push('- 细节见提交记录');
  }
  lines.push('');
}
lines.push('安装或升级：');
lines.push('');
lines.push('```');
lines.push(`dsh-plugin-image-workbench@${version}`);
lines.push('```');
lines.push('');
lines.push('把这一行填进 DSH 的「添加插件」；已经装过的，在插件页点升级即可。');
lines.push('');

fs.writeFileSync(path.join(root, 'release-title.txt'), title + '\n');
fs.writeFileSync(path.join(root, 'release-notes.md'), lines.join('\n'));
console.log(`TITLE=${title}`);
console.log(`版本 ${picked.map((x) => x.version).join(', ')}，条目 ${picked.reduce((n, x) => n + (x.items || []).length, 0)} 条，说明已写入 release-notes.md`);
