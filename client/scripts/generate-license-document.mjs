import { createHash } from 'node:crypto';
import { readdir, readFile, stat, writeFile } from 'node:fs/promises';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';


const defaultRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const licensePattern = /^(?:licen[cs]e|copying|notice)(?:[._-].*)?$/i;
const readmePattern = /^readme(?:[._-].*)?$/i;
const unsafeControlCharacters = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;
const bundledWebFontLicenseMaterials = [
    ['Kosugi', ['Kosugi-LICENSE.txt']],
    ['Kosugi Maru', ['KosugiMaru-LICENSE.txt']],
    ['Open Sans', ['OpenSans-LICENSE.txt']],
    ['Noto Sans Japanese', ['NotoSansJP-LICENSE.txt']],
    ['Yaku Han JP', ['YakuHanJP-MIT.txt', 'YakuHanJP-NOTICE.md', 'YakuHanJP-OFL.txt']],
    ['Material Design Icons', ['MaterialDesignIcons-LICENSE.txt']],
    ['Twemoji Mozilla', ['Twemoji-LICENSE.md', 'CC-BY-4.0.txt']],
];

// vite-plugin-pwa の generateSW が最終成果物へ組み込む Workbox モジュールは、package.json 上では
// devDependencies の配下にある。このため通常の dependencies の再帰走査とは別に、実行時エントリとして明示する。
// workbox-expiration の依存を再帰的にたどることで、同じ Service Worker に埋め込まれる idb も収集される。
const generatedRuntimeDependencies = [
    'workbox-cacheable-response',
    'workbox-core',
    'workbox-expiration',
    'workbox-precaching',
    'workbox-routing',
    'workbox-strategies',
    'workbox-window',
];

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

const vueDevtoolsLicense = `The MIT License (MIT)

Copyright (c) 2014-present Evan You

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
`;

const copyToLicense = `The MIT License (MIT)

Copyright (c) 2014 dead_horse

Permission is hereby granted, free of charge, to any person obtaining a copy
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
SOFTWARE.`;

const nodableEntitiesLicense = `MIT License

Copyright (c) 2026 Nodable

Permission is hereby granted, free of charge, to any person obtaining a copy
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
SOFTWARE.
`;

// npm 配布物に独立した LICENSE がない既知の版だけを、公式リポジトリの固定コミットと
// npm 配布物に含まれる証拠ファイルの SHA-256 で結び付ける。任意の package.json の license 値から
// 定型文を生成することはせず、ここにない版や内容が変化した配布物では必ず生成を停止する。
const verifiedLicenseFallbacks = {
    '@nodable/entities@3.0.0': {
        evidenceFilename: 'package.json',
        evidenceSha256: '548526ce0e1cad9ffe904fba19067013c9fea336eafd3cd52a337ec44406c9ed',
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/nodable/val-parsers/blob/d2070d76a8ba07e6c7fa142caeb51ffd756e47eb/LICENSE',
        sourceSha256: '750cb3fb6362804957ef52caaf9b5c824015be44d494637330d7cd8834d31d40',
        sourceLicenseText: nodableEntitiesLicense,
    },
    '@vue/devtools-api@6.6.4': {
        evidenceFilename: 'package.json',
        evidenceSha256: '16103b215db2ade43369020415869db5f7e89158c2c07524ef8fb5c8d71864cf',
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/vuejs/devtools-v6/blob/df6ab6bb7791a7a525a97990de73b3ea5e9a1941/LICENSE',
        sourceSha256: '050bbca6960784db52ff387271bf2ecc5cbed7cf8581b415d528a6ecb6585015',
        sourceLicenseText: vueDevtoolsLicense,
    },
    'cache-content-type@1.0.1': {
        evidenceFilename: 'package.json',
        evidenceSha256: 'ae20f7bf56c6e991dbe8d03940aedcbf4bfa0db23555ef9a3f55b018dd2bdecd',
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/node-modules/cache-content-type/blob/8a43ff8b3f800aef8fa366d99e0a2828705a9ffe/package.json',
        sourceSha256: 'ae20f7bf56c6e991dbe8d03940aedcbf4bfa0db23555ef9a3f55b018dd2bdecd',
    },
    'copy-to@2.0.1': {
        evidenceFilename: 'README.md',
        evidenceSha256: 'e19e6263bcd65e0211b796e68e197f1b822b4f9501b1a2a3b780ad91d1c5b9e2',
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/node-modules/copy-to/blob/16cc01116fbb05e48ebf96e8e8f9b14cf2a4fba1/LICENSE',
        sourceSha256: '92176cf79405c47e534084ca5dcbfdc1db638831ea3717609cdc7f0c2ef27d8e',
        sourceLicenseText: copyToLicense,
    },
    'koa-compose@4.1.0': {
        evidenceFilename: 'Readme.md',
        evidenceSha256: 'c4276dbbcb0ce9a41d3f1dc312c38b17b30d903009b6fb73b12d0f8f6188fb7c',
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/koajs/compose/blob/06e82e65a368ac12cd6405beaf19fd5d208a1477/Readme.md',
        sourceSha256: 'c4276dbbcb0ce9a41d3f1dc312c38b17b30d903009b6fb73b12d0f8f6188fb7c',
    },
    'koa-json@2.0.2': {
        evidenceFilename: 'Readme.md',
        evidenceSha256: '1bc45119ab9a572492b0f5b8b8d6f76fa15e877e1c335ddf986cc55f580c2186',
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/koajs/json/blob/741f78c09f4b1db55f857d0489b14498e7a42e30/Readme.md',
        sourceSha256: '1bc45119ab9a572492b0f5b8b8d6f76fa15e877e1c335ddf986cc55f580c2186',
    },
    'koa-logger@3.2.1': {
        evidenceFilename: 'Readme.md',
        evidenceSha256: 'f27ac07518fcea4c8c89ca3c8022393e38b0012073b2dce3c7583c4824062cfe',
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/koajs/logger/blob/e7b24bd5a112e5928ebbc19e810bcf9fc4bba189/Readme.md',
        sourceSha256: 'f27ac07518fcea4c8c89ca3c8022393e38b0012073b2dce3c7583c4824062cfe',
    },
    'mitt@2.1.0': {
        evidenceFilename: 'README.md',
        evidenceSha256: 'f57a62589e29dd909c7a6b4130b5c35bd123d557fe8a0e57844a506f7c9d8b13',
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/developit/mitt/blob/22c5dcba10736aecb1f39ee88d9f85278108c988/README.md',
        sourceSha256: 'f57a62589e29dd909c7a6b4130b5c35bd123d557fe8a0e57844a506f7c9d8b13',
        copyrightNotice: '© Jason Miller',
    },
    'pwa-install-handler@2.6.5': {
        evidenceFilename: 'README.md',
        evidenceSha256: 'cb96414e120d073ff1847c275e557f0a71ce2ca4b6919172757c914fef638027',
        declaredLicense: 'ISC',
        license: 'ISC',
        source: 'https://github.com/FilipChalupa/pwa-install-handler/blob/c0069abdba10498e52ae84445b91cdc027c4b843/README.md',
        sourceSha256: 'cb96414e120d073ff1847c275e557f0a71ce2ca4b6919172757c914fef638027',
    },
    'vue-resize@2.0.0-alpha.1': {
        evidenceFilename: 'README.md',
        evidenceSha256: 'e595ba74b7e59b16b702f19a957cf39f15c06bf394d24e377add73e425b6c748',
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/Akryum/vue-resize/blob/d365c5e7a69cdba8985b9d608d18f34b5f0b044d/packages/vue-resize/README.md',
        sourceSha256: 'e595ba74b7e59b16b702f19a957cf39f15c06bf394d24e377add73e425b6c748',
    },
    'humanize-number@0.0.2': {
        evidenceFilename: 'Readme.md',
        evidenceSha256: '5c049b60e6ce9d975b5080441e4e5c530a937f27d0dcfa44f9a0d91d04663ac5',
        declaredLicense: 'not declared',
        license: 'MIT',
        source: 'https://github.com/component/humanize-number/blob/bff0f636fcca0dfbcb1bf7777e46c0b8a64defbc/Readme.md',
        sourceSha256: '5c049b60e6ce9d975b5080441e4e5c530a937f27d0dcfa44f9a0d91d04663ac5',
    },
};

function NormalizeText(content) {
    const normalized = content.replaceAll('\r\n', '\n').trim();
    if (normalized.includes('\uFFFD')) {
        throw new Error('License source contains a Unicode replacement character.');
    }
    if (unsafeControlCharacters.test(normalized)) {
        throw new Error('License source contains an unsupported control character.');
    }
    return normalized;
}

function FormatPerson(person) {
    if (typeof person === 'string') return person;
    if (person === null || typeof person !== 'object' || typeof person.name !== 'string') return null;
    const email = typeof person.email === 'string' ? ` <${person.email}>` : '';
    const url = typeof person.url === 'string' ? ` (${person.url})` : '';
    return `${person.name}${email}${url}`;
}

function FormatAuthors(metadata) {
    const authors = [];
    const author = FormatPerson(metadata.author);
    if (author !== null) authors.push(author);
    if (Array.isArray(metadata.authors)) {
        for (const person of metadata.authors) {
            const formatted = FormatPerson(person);
            if (formatted !== null) authors.push(formatted);
        }
    }
    return [...new Set(authors)].join(', ') || undefined;
}

function NormalizeRepositoryURL(repository) {
    if (typeof repository !== 'string' || repository.length === 0) return undefined;
    let url = repository.trim();
    if (/^[\w.-]+\/[\w.-]+$/.test(url)) url = `https://github.com/${url}`;
    url = url
        .replace(/^github:/, 'https://github.com/')
        .replace(/^git\+https:\/\//, 'https://')
        .replace(/^git:\/\/github\.com\//, 'https://github.com/')
        .replace(/^git:\/\//, 'https://')
        .replace(/^git\+ssh:\/\/git@github\.com\//, 'https://github.com/')
        .replace(/^git@github\.com:/, 'https://github.com/')
        .replace(/\.git$/, '');
    return url;
}

function DecodeMarkdownEntities(content) {
    return content
        .replaceAll('&lt;', '<')
        .replaceAll('&gt;', '>')
        .replaceAll('&quot;', '"')
        .replaceAll('&#39;', "'")
        .replaceAll('&amp;', '&');
}

// README の License 見出し以下だけを取得する。次の同レベル以上の見出しまでを範囲とするため、
// README 後半に別の章が増えてもライセンス本文へ混入しない。
export function ExtractCompleteReadmeLicense(content) {
    const lines = NormalizeText(content).split('\n');
    let start = -1;
    let headingLevel = 0;

    for (let index = 0; index < lines.length; index++) {
        const atxHeading = lines[index].match(/^(#{1,6})\s+licen[cs]e\s*#*\s*$/i);
        if (atxHeading !== null) {
            start = index + 1;
            headingLevel = atxHeading[1].length;
            break;
        }
        if (/^licen[cs]e\s*$/i.test(lines[index].trim()) && /^[-=]{3,}\s*$/.test(lines[index + 1] ?? '')) {
            start = index + 2;
            headingLevel = lines[index + 1].trim().startsWith('=') ? 1 : 2;
            break;
        }
    }
    if (start === -1) return null;

    let end = lines.length;
    for (let index = start; index < lines.length; index++) {
        const heading = lines[index].match(/^(#{1,6})\s+/);
        if (heading !== null && heading[1].length <= headingLevel) {
            end = index;
            break;
        }
    }
    const section = DecodeMarkdownEntities(lines.slice(start, end).join('\n').trim());

    // ライセンス名やリンクだけの節は全文ではない。著作権表示と主要条項の双方が揃ったものだけを採用する。
    const hasCopyright = /(?:copyright(?:\s*(?:\(c\)|©))?|©)\s*\d{4}/i.test(section);
    const isCompleteMIT = /permission is hereby granted, free of charge/i.test(section) &&
        /the above copyright notice and this permission notice/i.test(section) &&
        /the software is provided ['"]as is['"]/i.test(section);
    const isCompleteISC = /permission to use, copy, modify, and\/or distribute/i.test(section) &&
        /the author disclaims all warranties/i.test(section);
    return hasCopyright && (isCompleteMIT || isCompleteISC) ? section : null;
}

// 独立したライセンスファイルがないパッケージについて、公開物から検証できるライセンスだけを返す。
// package.json の license 値だけから定型文を補うことは禁止し、既知の固定フォールバック以外は必ず失敗させる。
export function ResolveMissingLicenseMaterials(pkg) {
    for (const [filename, readme] of pkg.readmes) {
        const completeLicense = ExtractCompleteReadmeLicense(readme);
        if (completeLicense !== null) {
            return [{ name: `${filename} license section`, content: completeLicense }];
        }
    }

    const verifiedFallback = verifiedLicenseFallbacks[pkg.key];
    if (verifiedFallback === undefined) {
        throw new Error(
            `${pkg.key}: no complete license file or README license was found, and no exact-version fallback is registered.`,
        );
    }

    if (pkg.declaredLicense !== verifiedFallback.declaredLicense) {
        throw new Error(`${pkg.key}: declared license changed from the verified fallback.`);
    }
    const evidence = verifiedFallback.evidenceFilename === 'package.json' ?
        pkg.packageJson :
        pkg.readmes.find(([filename]) => filename === verifiedFallback.evidenceFilename)?.[1];
    if (evidence === undefined) {
        throw new Error(`${pkg.key}: verified evidence ${verifiedFallback.evidenceFilename} is missing.`);
    }
    const evidenceHash = createHash('sha256').update(evidence).digest('hex');
    if (evidenceHash !== verifiedFallback.evidenceSha256) {
        throw new Error(`${pkg.key}: verified evidence hash mismatch.`);
    }

    // 上流 LICENSE が npm 配布物から除外されていた2件は原文も埋め込み、固定コミットのハッシュと照合する。
    // それ以外は、公式固定コミットと同一と確認した README/package.json の宣言・帰属表示を利用する。
    let licenseText;
    const notes = [
        `Verified source: <${verifiedFallback.source}>`,
        `SHA-256 of fixed upstream source: \`${verifiedFallback.sourceSha256}\``,
        `SHA-256 of published ${verifiedFallback.evidenceFilename}: \`${verifiedFallback.evidenceSha256}\``,
    ];
    if (verifiedFallback.sourceLicenseText !== undefined) {
        const sourceHash = createHash('sha256').update(verifiedFallback.sourceLicenseText).digest('hex');
        if (sourceHash !== verifiedFallback.sourceSha256) {
            throw new Error(`${pkg.key}: embedded upstream license hash mismatch.`);
        }
        licenseText = NormalizeText(verifiedFallback.sourceLicenseText);
    } else {
        if (verifiedFallback.sourceSha256 !== verifiedFallback.evidenceSha256) {
            throw new Error(`${pkg.key}: fallback source hash is not tied to the published evidence.`);
        }
        if (verifiedFallback.copyrightNotice !== undefined) {
            licenseText = [
                verifiedFallback.copyrightNotice,
                '',
                standardLicenseTexts[verifiedFallback.license],
            ].join('\n');
        } else {
            notes.push('Copyright notice: the fixed upstream source does not publish a separate notice.');
            licenseText = standardLicenseTexts[verifiedFallback.license];
        }
    }

    return [{
        name: `Verified ${verifiedFallback.license} license from fixed upstream source`,
        notes,
        content: licenseText,
    }];
}

async function ResolvePackage(name, fromDirectory, rootDirectory) {
    let directory = fromDirectory;
    while (directory.startsWith(rootDirectory)) {
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

export async function CollectPackage(directory, rootDirectory, packages) {
    let metadata;
    let packageJson;
    try {
        packageJson = await readFile(join(directory, 'package.json'), 'utf8');
        metadata = JSON.parse(packageJson);
    } catch {
        return;
    }
    const key = `${metadata.name ?? basename(directory)}@${metadata.version ?? 'unknown'}`;
    if (!packages.has(key)) {
        const licenses = [];
        const readmes = [];
        for (const entry of await readdir(directory, { withFileTypes: true })) {
            if (!entry.isFile()) continue;
            const path = join(directory, entry.name);
            if ((await stat(path)).size > 2 * 1024 * 1024) continue;
            if (licensePattern.test(entry.name)) {
                licenses.push({ name: entry.name, content: NormalizeText(await readFile(path, 'utf8')) });
            } else if (readmePattern.test(entry.name)) {
                // 固定フォールバックでは npm 配布物そのもののハッシュを検証するため、改行末尾も含む原文を保持する
                readmes.push([entry.name, await readFile(path, 'utf8')]);
            }
        }
        packages.set(key, {
            key,
            name: metadata.name ?? basename(directory),
            version: metadata.version ?? 'unknown',
            declaredLicense: typeof metadata.license === 'string' ? metadata.license : 'not declared',
            author: FormatAuthors(metadata),
            repository: NormalizeRepositoryURL(
                typeof metadata.repository === 'string' ? metadata.repository : metadata.repository?.url,
            ),
            licenses,
            packageJson,
            readmes,
        });
    }
    for (const dependency of Object.keys(metadata.dependencies ?? {})) {
        const resolved = await ResolvePackage(dependency, directory, rootDirectory);
        if (resolved === null) {
            throw new Error(`${key}: unable to resolve declared dependency: ${dependency}`);
        }
        await CollectPackage(resolved, rootDirectory, packages);
    }
}

export async function GenerateLicenseDocument(rootDirectory, outputPath) {
    const packages = new Map();
    const application = JSON.parse(await readFile(join(rootDirectory, 'package.json'), 'utf8'));
    const runtimeDependencies = new Set([
        ...Object.keys(application.dependencies ?? {}),
        ...generatedRuntimeDependencies,
    ]);
    for (const dependency of runtimeDependencies) {
        const resolved = await ResolvePackage(dependency, rootDirectory, rootDirectory);
        if (resolved === null) throw new Error(`Unable to resolve runtime dependency: ${dependency}`);
        await CollectPackage(resolved, rootDirectory, packages);
    }

    const lines = ['## Client Third-Party Software Licenses', ''];
    const fontManifest = await readFile(join(rootDirectory, 'licenses/fonts/MANIFEST.md'), 'utf8');
    const fontManifestBody = fontManifest.replace(/^# Bundled web font manifest\r?\n+/, '');
    if (fontManifestBody === fontManifest) {
        throw new Error('Bundled web font manifest must start with the expected document title.');
    }
    lines.push('### Bundled web fonts', '');
    // MANIFEST.md を単体で読める H1 は保ちつつ、結合文書では親の H2 より強い見出しが混入しないよう本文だけを追加する
    lines.push(fontManifestBody.trim(), '');
    lines.push('', '以下に各固定配布物へ適用されるライセンス全文と帰属表示を掲載します。', '');
    const fontLicenseDirectory = join(rootDirectory, 'licenses/fonts');
    const actualFontLicenseFiles = (await readdir(fontLicenseDirectory))
        .filter((name) => name !== 'MANIFEST.md').sort();
    const configuredFontLicenseFiles = bundledWebFontLicenseMaterials
        .flatMap(([, filenames]) => filenames).sort();
    if (new Set(configuredFontLicenseFiles).size !== configuredFontLicenseFiles.length ||
        actualFontLicenseFiles.join('\n') !== configuredFontLicenseFiles.join('\n')) {
        throw new Error('Bundled web font license grouping does not match licenses/fonts.');
    }
    for (const [work, filenames] of bundledWebFontLicenseMaterials) {
        lines.push(`#### ${work}`, '');
        for (const filename of filenames) {
            lines.push(
                `##### ${filename}`,
                '',
                '````text',
                (await readFile(join(fontLicenseDirectory, filename), 'utf8')).trim(),
                '````',
                '',
            );
        }
    }

    lines.push('### JavaScript Package Licenses', '');
    lines.push(`Collected packages: ${packages.size}`, '');
    for (const [, pkg] of [...packages].sort(([a], [b]) => a.localeCompare(b))) {
        lines.push(`#### ${pkg.name} ${pkg.version}`, '', `- Declared license: \`${pkg.declaredLicense}\``);
        if (pkg.author) lines.push(`- Declared author: ${pkg.author}`);
        if (pkg.repository) lines.push(`- Repository: [${pkg.repository}](${pkg.repository})`);
        lines.push('');

        const materials = pkg.licenses.length > 0 ? pkg.licenses : ResolveMissingLicenseMaterials(pkg);
        for (const material of materials) {
            lines.push(`##### ${material.name}`, '');
            if (material.notes !== undefined) {
                for (const note of material.notes) lines.push(`- ${note}`);
                lines.push('');
            }
            lines.push('````text', material.content, '````', '');
        }
    }

    const document = `${lines.join('\n')}\n`;
    if (document.includes('\uFFFD') || unsafeControlCharacters.test(document)) {
        throw new Error('Generated license document contains corrupted or unsupported text.');
    }
    await writeFile(outputPath, document, 'utf8');
}

const invokedPath = process.argv[1] === undefined ? null : resolve(process.argv[1]);
if (invokedPath === fileURLToPath(import.meta.url)) {
    const outputPath = process.argv[2] ?? '/tmp/CLIENT_THIRD_PARTY_LICENSES.md';
    await GenerateLicenseDocument(defaultRoot, outputPath);
}
