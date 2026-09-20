// Syntax-checks the inline <script type="module"> in every page, with the
// local imports stubbed out. Catches exactly the class of damage that a
// line-based edit can do to a multi-line call.
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
const pages = ['index.html','probe.html','stage1.html','stage2.html','stage3.html','dashboard.html'];
let bad = 0;
for (const page of pages) {
  const src = readFileSync(new URL('./www/' + page, import.meta.url), 'utf8');
  const re = /<script(?:\s+type="module")?>([\s\S]*?)<\/script>/g;
  let m, n = 0;
  while ((m = re.exec(src))) {
    n++;
    const body = m[1].replace(/from\s+'\.\/[^']+'/g, "from 'data:text/javascript,export default 0'");
    const tmp = `/tmp/_chk_${page}_${n}.mjs`;
    writeFileSync(tmp, body);
    try {
      execFileSync(process.execPath, ['--check', tmp], { stdio: 'pipe' });
      console.log(`  PASS  ${page} script#${n}`);
    } catch (e) {
      bad++;
      const msg = (e.stderr ? e.stderr.toString() : String(e)).split('\n').slice(0, 4).join(' ').trim();
      console.log(`  FAIL  ${page} script#${n}: ${msg}`);
    }
  }
  if (!n) console.log(`  --    ${page}: no inline script`);
}
console.log(bad ? `\n  ${bad} page(s) with broken script` : '\n  all page scripts parse');
process.exit(bad ? 1 : 0);
