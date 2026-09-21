#!/usr/bin/env node
/**
 * ACP runtime npm package tree のライセンス文書を生成する。
 *
 * - コミット済み package-lock.json と実際の node_modules を照合する
 * - 完成 tree に存在する全 package（推移・optional 含む）を対象にする
 * - LICENSE 等がない package は licenseFallbacks か宣言ライセンスの最小セクションへ縮退する
 * - package 数と生成文書の収録数が一致することを検証する
 *
 * Node.js 本体ライセンスは別途 Dockerfile で固定資料として結合する。
 * Grok は公式 main / platform package と同じ npm tree から収集する。
 */

import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, statSync, writeFileSync, existsSync } from 'node:fs';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const defaultRoot = scriptDir;

const licenseFilePattern = /^(?:licen[cs]e|copying|notice|third[-_]?party(?:[-_]?notices?)?)(?:[._-].*)?$/i;
const unsafeControlCharacters = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;

// package.json の license メタデータはあるが独立 LICENSE が無い配布物向けの override。
// キーは package 名のみとし、バージョンや evidence hash ではゲートしない
// (依存更新のたびにビルドが止まる保守負債になるため)。
// declaredLicense が登録値から変わった場合は定型文の埋め込みが不正確になるため、
// 本文を捏造せず宣言ライセンスだけを記す最小セクションへ縮退する。
const licenseFallbacks = {
    '@lydell/node-pty-linux-x64': {
        declaredLicense: 'MIT',
        license: 'MIT',
        source: 'https://github.com/lydell/node-pty (LICENSE from the installed main package)',
        licenseFilename: '../node-pty/LICENSE',
    },
    '@openai/codex': {
        declaredLicense: 'Apache-2.0',
        license: 'Apache-2.0',
        source: 'https://github.com/openai/codex (npm package; Apache-2.0 declared in package.json)',
        standardText: 'Apache-2.0',
    },
    '@xai-official/grok': {
        declaredLicense: 'Apache-2.0',
        license: 'Apache-2.0',
        source: 'https://www.npmjs.com/package/@xai-official/grok (official npm package)',
        standardText: 'Apache-2.0',
    },
    // platform package は binary と THIRD_PARTY_NOTICES.md を同じ tarball に収録する。
    // NOTICE に加えて宣言ライセンス本文も必ず収録するため、ファイルがあっても fallback を併用する。
    '@xai-official/grok-linux-x64': {
        declaredLicense: 'Apache-2.0',
        license: 'Apache-2.0',
        source: 'https://www.npmjs.com/package/@xai-official/grok-linux-x64 (official platform package)',
        standardText: 'Apache-2.0',
        includeWithFiles: true,
    },
};

const standardLicenseTexts = {
    'Apache-2.0': `Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.`,
};

function sha256(buffer) {
    return createHash('sha256').update(buffer).digest('hex');
}

function normalizeText(content) {
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

function formatLicenseField(license) {
    if (typeof license === 'string') return license;
    if (license && typeof license === 'object' && typeof license.type === 'string') {
        return license.type;
    }
    return 'unknown';
}

function walkInstalledPackages(nodeModulesRoot) {
    /** @type {Map<string, { lockPath: string, dir: string, name: string, version: string, license: string }>} */
    const found = new Map();

    function walk(dir, rel) {
        if (!existsSync(dir)) return;
        for (const entry of readdirSync(dir, { withFileTypes: true })) {
            if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
            const full = join(dir, entry.name);
            const nextRel = rel ? `${rel}/${entry.name}` : entry.name;
            if (entry.name.startsWith('@') && !existsSync(join(full, 'package.json'))) {
                walk(full, nextRel);
                continue;
            }
            const pkgJsonPath = join(full, 'package.json');
            if (existsSync(pkgJsonPath)) {
                const pkg = JSON.parse(readFileSync(pkgJsonPath, 'utf8'));
                const lockPath = `node_modules/${nextRel}`;
                found.set(lockPath, {
                    lockPath,
                    dir: full,
                    name: pkg.name || entry.name,
                    version: pkg.version || '',
                    license: formatLicenseField(pkg.license),
                });
                const nested = join(full, 'node_modules');
                if (existsSync(nested)) {
                    walk(nested, `${nextRel}/node_modules`);
                }
            }
        }
    }

    walk(nodeModulesRoot, '');
    return found;
}

function collectLicenseFiles(packageDir) {
    const materials = [];
    for (const entry of readdirSync(packageDir, { withFileTypes: true })) {
        if (!entry.isFile()) continue;
        if (!licenseFilePattern.test(entry.name)) continue;
        const full = join(packageDir, entry.name);
        const text = normalizeText(readFileSync(full, 'utf8'));
        materials.push({ filename: entry.name, text, sha256: sha256(Buffer.from(text, 'utf8')) });
    }
    return materials;
}

function resolveLicenseMaterials(pkg) {
    const files = collectLicenseFiles(pkg.dir);
    const fallback = licenseFallbacks[pkg.name];
    if (files.length > 0 && fallback?.includeWithFiles !== true) {
        return { kind: 'files', materials: files, declaredLicense: pkg.license };
    }

    // LICENSE を同梱しない package でも生成を止めない。
    // 宣言ライセンスだけを記す最小セクションに縮退する（定型文の捏造はしない）。
    const declaredOnly = (reason) => {
        console.warn(
            `WARNING: ${reason}; recording only the declared license for ${pkg.name}@${pkg.version}.`,
        );
        return { kind: 'declared-only', materials: [], declaredLicense: pkg.license };
    };

    if (fallback === undefined) {
        return declaredOnly(`Package ${pkg.name}@${pkg.version} has no LICENSE/NOTICE files and no registered fallback`);
    }

    // 宣言ライセンスが登録値から変わった場合は定型文の埋め込みが不正確になるため縮退する。
    if (fallback.declaredLicense !== pkg.license) {
        return declaredOnly(
            `Declared license changed for ${pkg.name}: fallback=${fallback.declaredLicense}, package.json=${pkg.license}`,
        );
    }

    let fallbackFilename;
    let fallbackText;
    if (fallback.licenseFilename !== undefined) {
        const licensePath = join(pkg.dir, fallback.licenseFilename);
        if (!existsSync(licensePath)) {
            return declaredOnly(`Fallback license missing for ${pkg.name}@${pkg.version}: ${fallback.licenseFilename}`);
        }
        fallbackFilename = `VERIFIED-UPSTREAM-${fallback.license}.txt`;
        fallbackText = normalizeText(readFileSync(licensePath, 'utf8'));
    } else {
        const standard = standardLicenseTexts[fallback.standardText];
        if (standard === undefined) {
            throw new Error(`Unknown standard license text key for ${pkg.name}: ${fallback.standardText}`);
        }
        fallbackFilename = `VERIFIED-FALLBACK-${fallback.license}.txt`;
        fallbackText = normalizeText(standard);
    }
    const fallbackMaterial = {
        filename: fallbackFilename,
        text: fallbackText,
        sha256: sha256(Buffer.from(fallbackText, 'utf8')),
        source: fallback.source,
    };
    return {
        kind: files.length > 0 ? 'files+fallback' : 'fallback',
        materials: [fallbackMaterial, ...files],
        declaredLicense: pkg.license,
    };
}

function buildDocument(packages) {
    const lines = [
        '## ACP Runtime Dependencies',
        '',
        'This section covers Node.js package tree installed under `/opt/konomitv-bs4k-acp/` for',
        'KonomiTV-BS4K recorded-series ACP adapters (Codex ACP, Codex CLI, Gemini CLI, Grok CLI, and transitive dependencies).',
        'Grok main/platform packages and their binary notices are covered by the same lockfile-backed npm tree.',
        'The Node.js runtime license is appended as a separate subsection.',
        '',
        `Recorded npm packages: **${packages.length}**`,
        '',
    ];

    // 同一本文の重複排除: 本文 SHA -> 最初の package 見出し
    /** @type {Map<string, string>} */
    const bodyOwners = new Map();

    for (const pkg of packages) {
        const heading = `${pkg.name} ${pkg.version}`;
        lines.push(`### ${heading}`);
        lines.push('');
        lines.push(`- Package: \`${pkg.name}\``);
        lines.push(`- Version: \`${pkg.version}\``);
        lines.push(`- Install path: \`${pkg.lockPath}\``);
        lines.push(`- Declared license: \`${pkg.resolved.declaredLicense}\``);
        if (pkg.registryTarball) {
            lines.push(`- Registry tarball: \`${pkg.registryTarball}\``);
        }
        if (pkg.npmIntegrity) {
            lines.push(`- npm integrity: \`${pkg.npmIntegrity}\``);
        }
        lines.push('');

        // LICENSE を同梱しない package は宣言ライセンスだけを記す（定型文の捏造はしない）。
        if (pkg.resolved.materials.length === 0) {
            lines.push('The npm package does not bundle a license text. The declared license above');
            lines.push('comes from the package metadata; see the package source for the full text.');
            lines.push('');
        }

        for (const material of pkg.resolved.materials) {
            lines.push(`#### ${material.filename}`);
            lines.push('');
            const owner = bodyOwners.get(material.sha256);
            if (owner !== undefined) {
                lines.push(`Same license text as \`${owner}\` (SHA-256 \`${material.sha256}\`).`);
                if (material.source) {
                    lines.push('');
                    lines.push(`Source: ${material.source}`);
                }
                lines.push('');
                continue;
            }
            bodyOwners.set(material.sha256, `${heading} / ${material.filename}`);
            if (material.source) {
                lines.push(`Source: ${material.source}`);
                lines.push('');
            }
            lines.push('```text');
            lines.push(material.text);
            lines.push('```');
            lines.push('');
        }
    }

    return `${lines.join('\n').trimEnd()}\n`;
}

function main() {
    const outputPath = process.argv[2];
    if (!outputPath) {
        console.error('Usage: generate-license-document.mjs <output.md> [acp-root]');
        process.exit(2);
    }
    const root = process.argv[3] ? process.argv[3] : defaultRoot;
    const lockPath = join(root, 'package-lock.json');
    const nodeModules = join(root, 'node_modules');
    if (!existsSync(lockPath)) {
        throw new Error(`package-lock.json not found: ${lockPath}`);
    }
    if (!existsSync(nodeModules)) {
        throw new Error(`node_modules not found: ${nodeModules}`);
    }

    const lock = JSON.parse(readFileSync(lockPath, 'utf8'));
    const lockPackages = lock.packages || {};
    const installed = walkInstalledPackages(nodeModules);

    // lockfile との path/name/version 照合（optional 未インストールは許容）
    for (const [lockPkgPath, info] of installed.entries()) {
        const locked = lockPackages[lockPkgPath];
        if (locked === undefined) {
            throw new Error(`Installed path missing from lockfile: ${lockPkgPath}`);
        }
        if (locked.version && info.version && locked.version !== info.version) {
            throw new Error(
                `Version mismatch for ${lockPkgPath}: lock=${locked.version} installed=${info.version}`,
            );
        }
        if (locked.name && info.name && locked.name !== info.name) {
            throw new Error(
                `Name mismatch for ${lockPkgPath}: lock=${locked.name} installed=${info.name}`,
            );
        }
    }
    for (const [lockPkgPath, locked] of Object.entries(lockPackages)) {
        if (!lockPkgPath.startsWith('node_modules/')) continue;
        if (locked.optional) continue;
        if (!installed.has(lockPkgPath)) {
            throw new Error(`Required lock path not installed: ${lockPkgPath}`);
        }
    }

    const packages = [...installed.values()]
        .sort((a, b) => a.lockPath.localeCompare(b.lockPath))
        .map((pkg) => ({
            ...pkg,
            registryTarball: lockPackages[pkg.lockPath].resolved,
            npmIntegrity: lockPackages[pkg.lockPath].integrity,
            resolved: resolveLicenseMaterials(pkg),
        }));

    const document = buildDocument(packages);

    // LICENSE / NOTICE 本文中の Markdown 見出しを package 見出しとして誤算入しない。
    // generator が付与した外側 code fence の外にある H3 だけを数える。
    let inFence = false;
    let headingCount = 0;
    for (const line of document.split('\n')) {
        if (line.startsWith('```')) {
            inFence = !inFence;
            continue;
        }
        if (!inFence && line.startsWith('### ')) {
            headingCount++;
        }
    }
    if (headingCount !== packages.length) {
        throw new Error(
            `Package count mismatch: installed=${packages.length} headings=${headingCount}`,
        );
    }

    // 必須の直接依存が収録されていること
    // （version は依存更新で変わるため、package.json の dependencies を正本として名前だけを照合する）
    const rootPackage = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
    for (const name of Object.keys(rootPackage.dependencies ?? {})) {
        if (!document.includes(`### ${name} `)) {
            throw new Error(`Required direct dependency missing from license document: ${name}`);
        }
    }

    writeFileSync(outputPath, document, 'utf8');
    console.log(
        `Wrote ACP license document: ${outputPath} (${packages.length} packages)`,
    );
}

try {
    main();
} catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
}
