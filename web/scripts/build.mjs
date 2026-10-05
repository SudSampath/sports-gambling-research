import {cp, mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const destination = path.join(root, 'dist');
execFileSync(process.env.SGR_PYTHON || path.join(root, '../.venv/bin/python'),
  [path.join(root, '../scripts/validate_public_dashboard.py')], {cwd: root, stdio: 'inherit'});
const manifest = JSON.parse(await readFile(path.join(root, 'data/index.json'), 'utf8'));
if (manifest.schema_version !== 1 || !Array.isArray(manifest.campaigns)) throw Error('Invalid public manifest');
const files = [...manifest.campaigns, 'recovery-proof.json'];
for (const file of files) {
  if (!/^[a-z0-9-]+\.json$/.test(file)) throw Error('Unsafe data filename');
  JSON.parse(await readFile(path.join(root, 'data', file), 'utf8'));
}
await rm(destination, {recursive: true, force: true});
await mkdir(destination, {recursive: true});
for (const file of ['index.html', 'styles.css', 'app.js', 'format.js', 'icon.svg']) {
  await cp(path.join(root, file), path.join(destination, file));
}
await mkdir(path.join(destination, 'data'));
for (const file of [...files, 'index.json']) await cp(path.join(root, 'data', file), path.join(destination, 'data', file));
await writeFile(path.join(destination, '.nojekyll'), '');
const sourceCommit = execFileSync('git', ['rev-parse', 'HEAD'], {cwd: root, encoding: 'utf8'}).trim();
const digest = createHash('sha256');
for (const file of ['index.html', 'styles.css', 'app.js', 'format.js', 'icon.svg', ...files.map(f => `data/${f}`), 'data/index.json']) {
  digest.update(file); digest.update(await readFile(path.join(root, file)));
}
await writeFile(path.join(destination, 'build.json'), JSON.stringify({schema_version: 1, source_commit: sourceCommit,
  built_at: new Date().toISOString(), content_sha256: digest.digest('hex')}, null, 2) + '\n');
console.log(`Built public dashboard from ${files.length - 1} campaign reports (${sourceCommit.slice(0, 7)}).`);
