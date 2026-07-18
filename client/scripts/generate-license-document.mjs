import { readdir, readFile, stat, writeFile } from 'node:fs/promises';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';


const root = dirname(dirname(fileURLToPath(import.meta.url)));
const output = process.argv[2] ?? '/tmp/CLIENT_THIRD_PARTY_LICENSES.md';
const packages = new Map();
const licensePattern = /^(?:licen[cs]e|copying|notice)(?:[._-].*)?$/i;
const standardLicenseTexts = {
    MIT: `Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.`,
    ISC: `Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.`,
};

async function resolvePackage(name, fromDirectory) {
    let directory = fromDirectory;
    while (directory.startsWith(root)) {
        const candidate = join(directory, 'node_modules', name);
        try {
            if ((await stat(join(candidate, 'package.json'))).isFile()) return candidate;
        } catch { /* Try the parent node_modules directory. */ }
        const parent = dirname(directory);
        if (parent === directory) break;
        directory = parent;
    }
    return null;
}

async function collectPackage(directory) {
    let metadata;
    try {
        metadata = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8'));
    } catch {
        return;
    }
    const key = `${metadata.name ?? basename(directory)}@${metadata.version ?? 'unknown'}`;
    if (!packages.has(key)) {
        const licenses = [];
        for (const entry of await readdir(directory, { withFileTypes: true })) {
            if (entry.isFile() && licensePattern.test(entry.name)) {
                const path = join(directory, entry.name);
                if ((await stat(path)).size <= 2 * 1024 * 1024) {
                    licenses.push([entry.name, (await readFile(path, 'utf8')).replaceAll('\r\n', '\n').trim()]);
                }
            }
        }
        packages.set(key, {
            name: metadata.name ?? basename(directory),
            version: metadata.version ?? 'unknown',
            declaredLicense: typeof metadata.license === 'string' ? metadata.license : 'not declared',
            author: typeof metadata.author === 'string' ? metadata.author : metadata.author?.name,
            repository: typeof metadata.repository === 'string' ? metadata.repository : metadata.repository?.url,
            licenses,
        });
    }
    for (const dependency of Object.keys(metadata.dependencies ?? {})) {
        const resolved = await resolvePackage(dependency, directory);
        if (resolved !== null) await collectPackage(resolved);
    }
}

const application = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
for (const dependency of Object.keys(application.dependencies ?? {})) {
    const resolved = await resolvePackage(dependency, root);
    if (resolved === null) throw new Error(`Unable to resolve production dependency: ${dependency}`);
    await collectPackage(resolved);
}

const lines = ['# Client Third-Party Software Licenses', ''];
const fontManifest = await readFile(join(root, 'licenses/fonts/MANIFEST.md'), 'utf8');
const fontManifestBody = fontManifest.replace(/^# Bundled web font manifest\r?\n+/, '');
if (fontManifestBody === fontManifest) {
    throw new Error('Bundled web font manifest must start with the expected document title.');
}
lines.push('## Bundled web fonts', '');
// MANIFEST.md を単体で読める H1 は保ちつつ、結合文書では親の H2 より強い見出しが混入しないよう本文だけを追加する
lines.push(fontManifestBody.trim(), '');
lines.push('', '以下に各固定配布物へ適用されるライセンス全文と帰属表示を掲載します。', '');
lines.push('### Bundled font license texts', '');
for (const filename of (await readdir(join(root, 'licenses/fonts'))).filter((name) => name !== 'MANIFEST.md').sort()) {
    lines.push(`#### ${filename}`, '', '````text', (await readFile(join(root, 'licenses/fonts', filename), 'utf8')).trim(), '````', '');
}

lines.push('# JavaScript Package Licenses', '');
lines.push(`Collected packages: ${packages.size}`, '');
for (const [, pkg] of [...packages].sort(([a], [b]) => a.localeCompare(b))) {
    lines.push(`## ${pkg.name} ${pkg.version}`, '', `- Declared license: \`${pkg.declaredLicense}\``);
    if (pkg.author) lines.push(`- Declared author: ${pkg.author}`);
    if (pkg.repository) lines.push(`- Repository: ${pkg.repository}`);
    lines.push('');
    if (pkg.licenses.length === 0) {
        const normalizedLicense = pkg.declaredLicense === 'not declared' ? 'MIT' : pkg.declaredLicense.replace(/[()]/g, '').split(/\s+(?:OR|AND)\s+/)[0];
        lines.push('No standalone license file was present in the published package. The following standard text is included according to its package metadata or published README.', '');
        if (standardLicenseTexts[normalizedLicense]) {
            lines.push(`#### Standard ${normalizedLicense} license text`, '', '````text', standardLicenseTexts[normalizedLicense], '````', '');
        }
    } else {
        for (const [name, content] of pkg.licenses) {
            lines.push(`#### ${name}`, '', '````text', content, '````', '');
        }
    }
}

await writeFile(output, `${lines.join('\n')}\n`, 'utf8');
