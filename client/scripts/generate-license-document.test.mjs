import assert from 'node:assert/strict';
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
    CollectPackage,
    GenerateLicenseDocument,
    LIBRESPEED_VENDOR_RELATIVE_DIRECTORY,
    ResolveMissingLicenseMaterials,
    VerifyLibreSpeedVendorDirectory,
} from './generate-license-document.mjs';


const clientRoot = dirname(dirname(fileURLToPath(import.meta.url)));

function GetDocumentHeadings(document) {
    const headings = [];
    let fenceLength = 0;
    for (const line of document.split('\n')) {
        const fence = line.match(/^(`{3,})(.*)$/);
        if (fence !== null) {
            if (fenceLength === 0) {
                fenceLength = fence[1].length;
            } else if (fence[1].length >= fenceLength && fence[2].trim() === '') {
                fenceLength = 0;
            }
            continue;
        }
        if (fenceLength !== 0) continue;

        const heading = line.match(/^(#{1,6})\s+(.+)$/);
        if (heading !== null) headings.push({ level: heading[1].length, title: heading[2] });
    }
    return headings;
}

function GetPackageSection(document, name) {
    // version は依存更新で変わるため、見出しは package 名の prefix だけで照合する
    const startMarker = `#### ${name} `;
    const start = document.indexOf(startMarker);
    assert.notEqual(start, -1, `${name} must be present in the generated document.`);
    const next = document.indexOf('\n#### ', start + startMarker.length);
    return document.slice(start, next === -1 ? undefined : next);
}

test('generated document includes browser and service worker runtime packages', async () => {
    const temporaryDirectory = await mkdtemp(join(tmpdir(), 'konomitv-bs4k-client-licenses-'));
    const outputPath = join(temporaryDirectory, 'CLIENT_THIRD_PARTY_LICENSES.md');
    await GenerateLicenseDocument(clientRoot, outputPath);
    const document = await readFile(outputPath, 'utf8');
    const headings = GetDocumentHeadings(document);

    assert.ok(document.startsWith('## Client Third-Party Software Licenses\n'));
    assert.deepEqual(headings.filter(({ level }) => level <= 3), [
        { level: 2, title: 'Client Third-Party Software Licenses' },
        { level: 3, title: 'Bundled web fonts' },
        { level: 3, title: 'Bundled LibreSpeed Worker' },
        { level: 3, title: 'JavaScript Package Licenses' },
    ]);
    assert.match(document, /LibreSpeed/);
    assert.match(document, /892674a084a3dd354d823545cd0191023325b89c/);
    assert.match(document, /GNU LESSER GENERAL PUBLIC LICENSE/);
    assert.match(document, /GNU GENERAL PUBLIC LICENSE/);
    const javaScriptHeadingIndex = headings.findIndex(({ title }) => title === 'JavaScript Package Licenses');
    const libreSpeedHeadingIndex = headings.findIndex(({ title }) => title === 'Bundled LibreSpeed Worker');
    assert.deepEqual(headings.slice(2, libreSpeedHeadingIndex), [
        { level: 4, title: 'Kosugi' },
        { level: 5, title: 'Kosugi-LICENSE.txt' },
        { level: 4, title: 'Kosugi Maru' },
        { level: 5, title: 'KosugiMaru-LICENSE.txt' },
        { level: 4, title: 'Open Sans' },
        { level: 5, title: 'OpenSans-LICENSE.txt' },
        { level: 4, title: 'Noto Sans Japanese' },
        { level: 5, title: 'NotoSansJP-LICENSE.txt' },
        { level: 4, title: 'Yaku Han JP' },
        { level: 5, title: 'YakuHanJP-MIT.txt' },
        { level: 5, title: 'YakuHanJP-NOTICE.md' },
        { level: 5, title: 'YakuHanJP-OFL.txt' },
        { level: 4, title: 'Material Design Icons' },
        { level: 5, title: 'MaterialDesignIcons-LICENSE.txt' },
        { level: 4, title: 'Twemoji Mozilla' },
        { level: 5, title: 'Twemoji-LICENSE.md' },
        { level: 5, title: 'CC-BY-4.0.txt' },
    ]);
    assert.deepEqual(headings.slice(libreSpeedHeadingIndex, javaScriptHeadingIndex), [
        { level: 3, title: 'Bundled LibreSpeed Worker' },
        { level: 4, title: 'LICENSE-LGPL-3.0.txt' },
        { level: 4, title: 'LICENSE-GPL-3.0.txt' },
    ]);
    const javaScriptHeadings = headings.slice(javaScriptHeadingIndex + 1);
    assert.ok(javaScriptHeadings.length > 0);
    for (let index = 0; index < javaScriptHeadings.length; index++) {
        const heading = javaScriptHeadings[index];
        assert.ok(heading.level === 4 || heading.level === 5);
        if (heading.level === 4) assert.equal(javaScriptHeadings[index + 1]?.level, 5);
    }
    assert.match(document, /^#### Kosugi$/m);
    assert.match(document, /^##### Kosugi-LICENSE\.txt$/m);
    assert.doesNotMatch(document, /^#### Bundled font license texts$/m);

    for (const name of [
        '@iconify/vue',
        'idb',
        'workbox-cacheable-response',
        'workbox-core',
        'workbox-expiration',
        'workbox-precaching',
        'workbox-routing',
        'workbox-strategies',
        'workbox-window',
    ]) {
        GetPackageSection(document, name);
    }
});

test('all packages without bundled license files use registered upstream-verified fallbacks', async () => {
    const temporaryDirectory = await mkdtemp(join(tmpdir(), 'konomitv-bs4k-client-licenses-'));
    const outputPath = join(temporaryDirectory, 'CLIENT_THIRD_PARTY_LICENSES.md');
    await GenerateLicenseDocument(clientRoot, outputPath);
    const document = await readFile(outputPath, 'utf8');

    // バージョンやハッシュではなく、登録済みフォールバックの package 名と照合済みソースだけを検査する
    for (const name of [
        '@nodable/entities',
        '@vue/devtools-api',
        'cache-content-type',
        'copy-to',
        'humanize-number',
        'koa-compose',
        'koa-json',
        'koa-logger',
        'mitt',
        'pwa-install-handler',
        'vue-resize',
    ]) {
        const section = GetPackageSection(document, name);
        assert.match(section, /##### Verified (?:MIT|ISC) license from fixed upstream source/);
        assert.match(section, /Verified source: <https:\/\/github\.com\//);
        assert.ok(section.indexOf('Verified source:') < section.indexOf('````text'));
        assert.doesNotMatch(section.slice(section.indexOf('````text')), /Verified source:/);
    }
    assert.doesNotMatch(document, /terms from package metadata/);

    const vueDevtoolsSection = GetPackageSection(document, '@vue/devtools-api');
    assert.match(vueDevtoolsSection, /Copyright \(c\) 2014-present Evan You/);
    assert.match(
        vueDevtoolsSection,
        /Repository: \[https:\/\/github\.com\/vuejs\/vue-devtools]\(https:\/\/github\.com\/vuejs\/vue-devtools\)/,
    );
    assert.match(GetPackageSection(document, 'copy-to'), /Copyright \(c\) 2014 dead_horse/);
    assert.match(GetPackageSection(document, 'mitt'), /© Jason Miller/);
    assert.match(GetPackageSection(document, '@nodable/entities'), /Copyright \(c\) 2026 Nodable/);
});

test('README license text preserves only attribution and verifies humanize-number fallback', async () => {
    const temporaryDirectory = await mkdtemp(join(tmpdir(), 'konomitv-bs4k-client-licenses-'));
    const outputPath = join(temporaryDirectory, 'CLIENT_THIRD_PARTY_LICENSES.md');
    await GenerateLicenseDocument(clientRoot, outputPath);
    const document = await readFile(outputPath, 'utf8');

    const onlySection = GetPackageSection(document, 'only', '0.0.2');
    assert.match(onlySection, /Copyright \(c\) 2012 TJ Holowaychuk <tj@vision-media\.ca>/);
    assert.match(onlySection, /Permission is hereby granted, free of charge/);

    const humanizeSection = GetPackageSection(document, 'humanize-number');
    assert.match(humanizeSection, /Declared license: `not declared`/);
    assert.match(humanizeSection, /bff0f636fcca0dfbcb1bf7777e46c0b8a64defbc\/Readme\.md/);
    assert.match(humanizeSection, /Permission is hereby granted, free of charge/);
});

test('undeclared and unverified README license degrades to a declared-only entry without assuming MIT', () => {
    // 未登録 package の license 値だけから定型文を捏造しない。生成は止めず宣言ライセンスだけを記す。
    const materials = ResolveMissingLicenseMaterials({
        key: 'ambiguous-package@1.0.0',
        name: 'ambiguous-package',
        declaredLicense: 'not declared',
        packageJson: '{}',
        readmes: [['README.md', '# ambiguous-package\n\n## License\n\nMIT']],
    });
    assert.equal(materials.length, 1);
    assert.match(materials[0].name, /Declared license/);
    assert.doesNotMatch(materials[0].content, /Permission is hereby granted/);
});

test('declared MIT without complete text or registered fallback also degrades to a declared-only entry', () => {
    const materials = ResolveMissingLicenseMaterials({
        key: 'unverified-mit-package@1.0.0',
        name: 'unverified-mit-package',
        declaredLicense: 'MIT',
        packageJson: '{"license":"MIT"}',
        readmes: [['README.md', '# package\n\n## License\n\nMIT']],
    });
    assert.equal(materials.length, 1);
    assert.match(materials[0].name, /Declared license/);
    assert.doesNotMatch(materials[0].content, /Permission is hereby granted/);
});

test('corrupted README license text is left unrepaired with a warning instead of failing the build', () => {
    const materials = ResolveMissingLicenseMaterials({
        key: 'corrupted-package@1.0.0',
        name: 'corrupted-package',
        declaredLicense: 'MIT',
        packageJson: '{"license":"MIT"}',
        readmes: [['README.md', '# package\n\n## License\n\nCopyright \uFFFD Example']],
    });
    assert.equal(materials.length, 1);
    assert.match(materials[0].name, /Declared license/);
});

test('registered fallback applies by package name even when the published README content changes', () => {
    // フォールバックは配布物の hash ではなく package 名と宣言ライセンスの一致だけを条件にする
    const materials = ResolveMissingLicenseMaterials({
        key: 'humanize-number@9.9.9',
        name: 'humanize-number',
        declaredLicense: 'not declared',
        packageJson: '{}',
        readmes: [['Readme.md', '# humanize-number\n\n## License\n\nMIT\nmodified']],
    });
    assert.equal(materials.length, 1);
    assert.match(materials[0].name, /Verified MIT license from fixed upstream source/);
    assert.match(materials[0].content, /Permission is hereby granted/);
});

test('declared dependency resolution fails closed with package and dependency names', async () => {
    const rootDirectory = await mkdtemp(join(tmpdir(), 'konomitv-bs4k-client-license-dependency-'));
    const packageDirectory = join(rootDirectory, 'node_modules', 'parent-package');
    await mkdir(packageDirectory, { recursive: true });
    await writeFile(join(packageDirectory, 'package.json'), JSON.stringify({
        name: 'parent-package',
        version: '1.0.0',
        license: 'MIT',
        dependencies: {
            'missing-package': '1.0.0',
        },
    }), 'utf8');
    await writeFile(join(packageDirectory, 'LICENSE'), 'License text', 'utf8');

    await assert.rejects(
        () => CollectPackage(packageDirectory, rootDirectory, new Map()),
        /parent-package@1\.0\.0: unable to resolve declared dependency: missing-package/,
    );
});

async function CopyLibreSpeedVendorFixture() {
    const fixtureDirectory = await mkdtemp(join(tmpdir(), 'konomitv-bs4k-librespeed-vendor-'));
    await cp(join(clientRoot, LIBRESPEED_VENDOR_RELATIVE_DIRECTORY), fixtureDirectory, { recursive: true });
    return fixtureDirectory;
}

test('LibreSpeed vendor directory matches the pinned manifest and hashes', async () => {
    await VerifyLibreSpeedVendorDirectory(join(clientRoot, LIBRESPEED_VENDOR_RELATIVE_DIRECTORY));
});

test('LibreSpeed vendor verification fails closed on missing, modified, or unknown files', async () => {
    const missingDirectory = await CopyLibreSpeedVendorFixture();
    await rm(join(missingDirectory, 'speedtest_worker.js'));
    await assert.rejects(
        () => VerifyLibreSpeedVendorDirectory(missingDirectory),
        /must contain exactly/,
    );

    const modifiedDirectory = await CopyLibreSpeedVendorFixture();
    await writeFile(join(modifiedDirectory, 'speedtest_worker.js'), 'modified worker', 'utf8');
    await assert.rejects(
        () => VerifyLibreSpeedVendorDirectory(modifiedDirectory),
        /hash mismatch/,
    );

    const unknownDirectory = await CopyLibreSpeedVendorFixture();
    await writeFile(join(unknownDirectory, 'extra.txt'), 'unexpected', 'utf8');
    await assert.rejects(
        () => VerifyLibreSpeedVendorDirectory(unknownDirectory),
        /must contain exactly/,
    );
});
