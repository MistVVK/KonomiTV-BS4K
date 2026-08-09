import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
    CollectPackage,
    GenerateLicenseDocument,
    ResolveMissingLicenseMaterials,
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

function GetPackageSection(document, name, version) {
    const startMarker = `#### ${name} ${version}\n`;
    const start = document.indexOf(startMarker);
    assert.notEqual(start, -1, `${name} ${version} must be present in the generated document.`);
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
        { level: 3, title: 'JavaScript Package Licenses' },
    ]);
    const javaScriptHeadingIndex = headings.findIndex(({ title }) => title === 'JavaScript Package Licenses');
    assert.deepEqual(headings.slice(2, javaScriptHeadingIndex), [
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

    for (const [name, version] of [
        ['@iconify/vue', '4.3.0'],
        ['idb', '7.1.1'],
        ['workbox-cacheable-response', '7.4.1'],
        ['workbox-core', '7.4.1'],
        ['workbox-expiration', '7.4.1'],
        ['workbox-precaching', '7.4.1'],
        ['workbox-routing', '7.4.1'],
        ['workbox-strategies', '7.4.1'],
        ['workbox-window', '7.4.1'],
    ]) {
        GetPackageSection(document, name, version);
    }
});

test('all packages without bundled license files use exact-version verified fallbacks', async () => {
    const temporaryDirectory = await mkdtemp(join(tmpdir(), 'konomitv-bs4k-client-licenses-'));
    const outputPath = join(temporaryDirectory, 'CLIENT_THIRD_PARTY_LICENSES.md');
    await GenerateLicenseDocument(clientRoot, outputPath);
    const document = await readFile(outputPath, 'utf8');

    for (const [name, version] of [
        ['@nodable/entities', '3.0.0'],
        ['@vue/devtools-api', '6.6.4'],
        ['cache-content-type', '1.0.1'],
        ['copy-to', '2.0.1'],
        ['humanize-number', '0.0.2'],
        ['koa-compose', '4.1.0'],
        ['koa-json', '2.0.2'],
        ['koa-logger', '3.2.1'],
        ['mitt', '2.1.0'],
        ['pwa-install-handler', '2.6.5'],
        ['vue-resize', '2.0.0-alpha.1'],
    ]) {
        const section = GetPackageSection(document, name, version);
        assert.match(section, /##### Verified (?:MIT|ISC) license from fixed upstream source/);
        assert.match(section, /SHA-256 of fixed upstream source: `[0-9a-f]{64}`/);
        assert.match(section, /Verified source: <https:\/\/github\.com\//);
        assert.ok(section.indexOf('Verified source:') < section.indexOf('````text'));
        assert.doesNotMatch(section.slice(section.indexOf('````text')), /Verified source:/);
    }
    assert.doesNotMatch(document, /terms from package metadata/);

    const vueDevtoolsSection = GetPackageSection(document, '@vue/devtools-api', '6.6.4');
    assert.match(vueDevtoolsSection, /Copyright \(c\) 2014-present Evan You/);
    assert.match(
        vueDevtoolsSection,
        /Repository: \[https:\/\/github\.com\/vuejs\/vue-devtools]\(https:\/\/github\.com\/vuejs\/vue-devtools\)/,
    );
    assert.match(GetPackageSection(document, 'copy-to', '2.0.1'), /Copyright \(c\) 2014 dead_horse/);
    assert.match(GetPackageSection(document, 'mitt', '2.1.0'), /© Jason Miller/);
    assert.match(GetPackageSection(document, '@nodable/entities', '3.0.0'), /Copyright \(c\) 2026 Nodable/);
});

test('README license text preserves only attribution and verifies humanize-number fallback', async () => {
    const temporaryDirectory = await mkdtemp(join(tmpdir(), 'konomitv-bs4k-client-licenses-'));
    const outputPath = join(temporaryDirectory, 'CLIENT_THIRD_PARTY_LICENSES.md');
    await GenerateLicenseDocument(clientRoot, outputPath);
    const document = await readFile(outputPath, 'utf8');

    const onlySection = GetPackageSection(document, 'only', '0.0.2');
    assert.match(onlySection, /Copyright \(c\) 2012 TJ Holowaychuk <tj@vision-media\.ca>/);
    assert.match(onlySection, /Permission is hereby granted, free of charge/);

    const humanizeSection = GetPackageSection(document, 'humanize-number', '0.0.2');
    assert.match(humanizeSection, /Declared license: `not declared`/);
    assert.match(humanizeSection, /bff0f636fcca0dfbcb1bf7777e46c0b8a64defbc\/Readme\.md/);
    assert.match(humanizeSection, /5c049b60e6ce9d975b5080441e4e5c530a937f27d0dcfa44f9a0d91d04663ac5/);
    assert.match(humanizeSection, /Permission is hereby granted, free of charge/);
});

test('undeclared and unverified README license is rejected instead of assuming MIT', () => {
    assert.throws(() => ResolveMissingLicenseMaterials({
        key: 'ambiguous-package@1.0.0',
        declaredLicense: 'not declared',
        packageJson: '{}',
        readmes: [['README.md', '# ambiguous-package\n\n## License\n\nMIT']],
    }), /no exact-version fallback is registered/);
});

test('declared MIT without complete text or exact-version evidence is also rejected', () => {
    assert.throws(() => ResolveMissingLicenseMaterials({
        key: 'unverified-mit-package@1.0.0',
        declaredLicense: 'MIT',
        packageJson: '{"license":"MIT"}',
        readmes: [['README.md', '# package\n\n## License\n\nMIT']],
    }), /no exact-version fallback is registered/);
});

test('corrupted license source text is rejected', () => {
    assert.throws(() => ResolveMissingLicenseMaterials({
        key: 'corrupted-package@1.0.0',
        declaredLicense: 'MIT',
        packageJson: '{"license":"MIT"}',
        readmes: [['README.md', '# package\n\n## License\n\nCopyright \uFFFD Example']],
    }), /Unicode replacement character/);
});

test('verified README fallback rejects modified package contents', () => {
    assert.throws(() => ResolveMissingLicenseMaterials({
        key: 'humanize-number@0.0.2',
        declaredLicense: 'not declared',
        packageJson: '{}',
        readmes: [['Readme.md', '# humanize-number\n\n## License\n\nMIT\nmodified']],
    }), /evidence hash mismatch/);
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
