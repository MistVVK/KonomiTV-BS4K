import { createHash } from 'node:crypto';
import { readdir, readFile, stat, writeFile } from 'node:fs/promises';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';


const defaultRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const licensePattern = /^(?:licen[cs]e|copying|notice)(?:[._-].*)?$/i;
const readmePattern = /^readme(?:[._-].*)?$/i;
const unsafeControlCharacters = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;
export const LIBRESPEED_VENDOR_RELATIVE_DIRECTORY = 'public/vendor/librespeed';
export const LIBRESPEED_VENDOR_REQUIRED_FILES = Object.freeze({
    'speedtest_worker.js': '3dc577e830a7255eacd9335865af9c4a77ca1d7c115e8bfc7e1717d2c4ed08d0',
    'LICENSE-LGPL-3.0.txt': 'e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118',
    'LICENSE-GPL-3.0.txt': '3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986',
});
export const LIBRESPEED_VENDOR_MANIFEST_NAME = 'MANIFEST.md';

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

// npm 配布物に独立した LICENSE がない既知の package 向けの、人間が上流ソースと照合したフォールバック。
// キーは package 名のみとし、バージョンや evidence hash ではゲートしない
// (依存更新のたびにビルドが止まる保守負債になるため)。任意の package.json の license 値から
// 定型文を生成することはせず、ここにない package や宣言ライセンスが変わった package は
// 宣言ライセンスだけを記す最小セクションへ縮退する。
const licenseFallbacks = {
    '@nodable/entities': {
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/nodable/val-parsers/blob/d2070d76a8ba07e6c7fa142caeb51ffd756e47eb/LICENSE',
        sourceLicenseText: nodableEntitiesLicense,
    },
    '@vue/devtools-api': {
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/vuejs/devtools-v6/blob/df6ab6bb7791a7a525a97990de73b3ea5e9a1941/LICENSE',
        sourceLicenseText: vueDevtoolsLicense,
    },
    'mitt': {
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/developit/mitt/blob/22c5dcba10736aecb1f39ee88d9f85278108c988/README.md',
        copyrightNotice: '© Jason Miller',
    },
    'pwa-install-handler': {
        declaredLicense: 'ISC',
        license: 'ISC',
        source: 'https://github.com/FilipChalupa/pwa-install-handler/blob/c0069abdba10498e52ae84445b91cdc027c4b843/README.md',
    },
    'vue-resize': {
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/Akryum/vue-resize/blob/d365c5e7a69cdba8985b9d608d18f34b5f0b044d/packages/vue-resize/README.md',
    },
};

function NormalizeText(content) {
    const normalized = content.replaceAll('\r\n', '\n').trim();
    // 上流由来の文字化けは警告だけを出して素通しする
    // (ライセンス文書の体裁の問題でビルドを止めない)。
    if (normalized.includes('\uFFFD')) {
        console.warn('WARNING: License source contains a Unicode replacement character (left unrepaired).');
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
        .replaceAll('&#39;', '\'')
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

    // LICENSE 欠落 package の縮退セクション。定型文の捏造はせず宣言ライセンスだけを記し、
    // 生成は止めない（ライセンス文書の体裁の問題でビルドを止めない）。
    const declaredOnly = (reason) => {
        console.warn(`WARNING: ${pkg.key}: ${reason}; recording only the declared license.`);
        return [{
            name: 'Declared license (npm package does not bundle a license text)',
            notes: [`Declared license: ${pkg.declaredLicense}`],
            content: 'The npm package does not bundle a license text. The declared license above '
                + 'comes from the package metadata; see the package source for the full text.',
        }];
    };

    // 人間が上流ソースと照合したフォールバックを package 名だけで引く。
    const fallback = licenseFallbacks[pkg.name];
    if (fallback === undefined) {
        return declaredOnly('no complete license file or README license was found, and no fallback is registered');
    }

    // 宣言ライセンスが登録値から変わった場合は定型文の埋め込みが不正確になるため縮退する。
    if (pkg.declaredLicense !== fallback.declaredLicense) {
        return declaredOnly(`declared license changed from the registered fallback (${fallback.declaredLicense})`);
    }

    // 上流 LICENSE が npm 配布物から除外されている package は、照合済みの原文を埋め込む。
    // それ以外は、上流ソースと同一と確認した宣言・帰属表示を利用する。
    let licenseText;
    const notes = [`Verified source: <${fallback.source}>`];
    if (fallback.sourceLicenseText !== undefined) {
        licenseText = NormalizeText(fallback.sourceLicenseText);
    } else if (fallback.copyrightNotice !== undefined) {
        licenseText = [
            fallback.copyrightNotice,
            '',
            standardLicenseTexts[fallback.license],
        ].join('\n');
    } else {
        notes.push('Copyright notice: the upstream source does not publish a separate notice.');
        licenseText = standardLicenseTexts[fallback.license];
    }

    return [{
        name: `Verified ${fallback.license} license from fixed upstream source`,
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

function HashFileBuffer(buffer) {
    return createHash('sha256').update(buffer).digest('hex');
}

/**
 * LibreSpeed 固定 vendor ディレクトリが MANIFEST と完全一致することを検証する。
 * 欠落・改変・未知ファイルはすべて失敗にする。
 */
export async function VerifyLibreSpeedVendorDirectory(vendorDirectory) {
    let entries;
    try {
        entries = await readdir(vendorDirectory, { withFileTypes: true });
    } catch {
        throw new Error(`LibreSpeed vendor directory is missing: ${vendorDirectory}`);
    }

    const fileNames = entries.filter((entry) => entry.isFile()).map((entry) => entry.name).sort();
    const expectedNames = [...Object.keys(LIBRESPEED_VENDOR_REQUIRED_FILES), LIBRESPEED_VENDOR_MANIFEST_NAME].sort();
    if (fileNames.join('\n') !== expectedNames.join('\n')) {
        throw new Error(
            `LibreSpeed vendor directory must contain exactly ${expectedNames.join(', ')}. found: ${fileNames.join(', ')}`,
        );
    }
    if (entries.some((entry) => !entry.isFile())) {
        throw new Error('LibreSpeed vendor directory must not contain subdirectories.');
    }

    const manifest = await readFile(join(vendorDirectory, LIBRESPEED_VENDOR_MANIFEST_NAME), 'utf8');
    if (!manifest.includes('892674a084a3dd354d823545cd0191023325b89c')) {
        throw new Error('LibreSpeed vendor manifest must record the pinned upstream commit.');
    }
    if (!manifest.includes('https://github.com/librespeed/speedtest')) {
        throw new Error('LibreSpeed vendor manifest must record the upstream source URL.');
    }

    for (const [fileName, expectedSha256] of Object.entries(LIBRESPEED_VENDOR_REQUIRED_FILES)) {
        const fileBuffer = await readFile(join(vendorDirectory, fileName));
        const actualSha256 = HashFileBuffer(fileBuffer);
        if (actualSha256 !== expectedSha256) {
            throw new Error(`LibreSpeed vendor file hash mismatch: ${fileName}`);
        }
        if (!manifest.includes(expectedSha256)) {
            throw new Error(`LibreSpeed vendor manifest must record the hash of ${fileName}.`);
        }
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

    const libreSpeedVendorDirectory = join(rootDirectory, LIBRESPEED_VENDOR_RELATIVE_DIRECTORY);
    await VerifyLibreSpeedVendorDirectory(libreSpeedVendorDirectory);
    const libreSpeedManifest = await readFile(
        join(libreSpeedVendorDirectory, LIBRESPEED_VENDOR_MANIFEST_NAME),
        'utf8',
    );
    const libreSpeedManifestBody = libreSpeedManifest.replace(/^# Bundled LibreSpeed Worker manifest\r?\n+/, '');
    if (libreSpeedManifestBody === libreSpeedManifest) {
        throw new Error('LibreSpeed vendor manifest must start with the expected document title.');
    }
    lines.push('### Bundled LibreSpeed Worker', '');
    lines.push(libreSpeedManifestBody.trim(), '');
    lines.push('', '以下に固定配布する LibreSpeed Worker へ適用されるライセンス全文と帰属表示を掲載します。', '');
    for (const fileName of ['LICENSE-LGPL-3.0.txt', 'LICENSE-GPL-3.0.txt']) {
        lines.push(
            `#### ${fileName}`,
            '',
            '````text',
            (await readFile(join(libreSpeedVendorDirectory, fileName), 'utf8')).trim(),
            '````',
            '',
        );
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
    if (document.includes('\uFFFD')) {
        console.warn('WARNING: Generated license document contains Unicode replacement characters (left unrepaired).');
    }
    if (unsafeControlCharacters.test(document)) {
        throw new Error('Generated license document contains unsupported control characters.');
    }
    await writeFile(outputPath, document, 'utf8');
}

const invokedPath = process.argv[1] === undefined ? null : resolve(process.argv[1]);
if (invokedPath === fileURLToPath(import.meta.url)) {
    const outputPath = process.argv[2] ?? '/tmp/CLIENT_THIRD_PARTY_LICENSES.md';
    await GenerateLicenseDocument(defaultRoot, outputPath);
}
