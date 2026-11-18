/**
 * 从 server/changelog.json 生成本次 GitHub Release 的标题与说明。
 *
 * 为什么要有这个脚本：发版靠打 tag 触发 CI，但 GitHub 的 Releases 列表只认
 * Release 对象，光推 tag 不会出现在那里 —— 结果就是「npm 已经发了新版，手机上
 * 看到的 Latest 还是几天前那个」。1.1.56 就漏过一次。现在 tag 进来后由 CI 直接
 * 建 Release，标题和条目都取自 changelog，和插件里「这次更新了什么」是同一份文案。
 *
 * 用法：node scripts/release-notes.mjs [--version 1.1.56]
 * 产出：release-title.txt、release-notes.md（都在当前目录）
 */
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const pkgVersion = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version;

const argv = process.argv.slice(2);
const vIdx = argv.indexOf('--version');
const version = (vIdx >= 0 ? argv[vIdx + 1] : '') || pkgVersion;

let entry = null;
try {
  const list = JSON.parse(fs.readFileSync(path.join(root, 'server/changelog.json'), 'utf8'));
  entry = (Array.isArray(list) ? list : []).find((x) => String(x.version) === version) || null;
} catch (e) {
  console.log('读 changelog 失败：' + e.message);
}

const items = (entry && entry.items) || [];
// changelog 里的 title 是可选的短摘要；没写就只用版本号，别硬拼一条半截句子
const short = (entry && String(entry.title || '').trim()) || '';
const title = short ? `v${version} ${short}` : `v${version}`;

const lines = [];
if (items.length) {
  lines.push('### 这一版改了什么', '');
  for (const it of items) lines.push('- ' + String(it).trim());
  lines.push('');
} else {
  lines.push('### 这一版改了什么', '', '- 细节见提交记录', '');
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
console.log(`条目 ${items.length} 条，说明已写入 release-notes.md`);
