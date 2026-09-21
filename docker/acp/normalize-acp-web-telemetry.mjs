import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';


const installationRoot = process.argv[2] ?? '/opt/konomitv-bs4k-acp';


function replaceExactlyOnce(content, before, after, label) {
    const firstIndex = content.indexOf(before);
    if (firstIndex < 0 || content.indexOf(before, firstIndex + before.length) >= 0) {
        throw new Error(`${label}: expected exactly one unpatched marker.`);
    }
    return content.slice(0, firstIndex) + after + content.slice(firstIndex + before.length);
}


// patch 対象ファイルの事前 hash 照合は行わない。適用点は marker のちょうど1件一致で確定するため、
// hash 照合は依存更新のたびにビルドを止めるだけの冗長なゲートになる。
function patchFile(relativePath, replacements) {
    const filePath = join(installationRoot, relativePath);
    let content = readFileSync(filePath, 'utf8');
    for (const replacement of replacements) {
        content = replaceExactlyOnce(
            content,
            replacement.before,
            replacement.after,
            `${relativePath} (${replacement.label})`,
        );
    }
    writeFileSync(filePath, content, 'utf8');
}


patchFile(
    'node_modules/@agentclientprotocol/codex-acp/dist/index.js',
    [{
        label: 'completed WebSearch sources',
        before: `function createWebSearchCompleteUpdate(item) {
  return {
    sessionUpdate: "tool_call_update",
    toolCallId: item.id,
    title: formatWebSearchTitle(item),
    status: "completed",
    rawInput: createWebSearchRawInput(item)
  };
}`,
        after: `function createWebSearchCompleteUpdate(item) {
  return {
    sessionUpdate: "tool_call_update",
    toolCallId: item.id,
    title: formatWebSearchTitle(item),
    status: "completed",
    rawInput: createWebSearchRawInput(item),
    rawOutput: {
      sources: Array.isArray(item.results) ? item.results.filter((result) => result && typeof result.url === "string").map((result) => ({
        title: typeof result.title === "string" ? result.title : "",
        url: result.url
      })) : []
    }
  };
}`,
    }],
);

console.log('Normalized Codex ACP Web telemetry.');
