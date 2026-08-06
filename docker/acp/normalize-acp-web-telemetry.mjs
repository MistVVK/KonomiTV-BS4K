import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';


const installationRoot = process.argv[2] ?? '/opt/konomitv-bs4k-acp';


function sha256(content) {
    return createHash('sha256').update(content).digest('hex');
}


function replaceExactlyOnce(content, before, after, label) {
    const firstIndex = content.indexOf(before);
    if (firstIndex < 0 || content.indexOf(before, firstIndex + before.length) >= 0) {
        throw new Error(`${label}: expected exactly one unpatched marker.`);
    }
    return content.slice(0, firstIndex) + after + content.slice(firstIndex + before.length);
}


function patchFile(relativePath, expectedSha256, replacements) {
    const filePath = join(installationRoot, relativePath);
    let content = readFileSync(filePath, 'utf8');
    const actualSha256 = sha256(content);
    if (actualSha256 !== expectedSha256) {
        throw new Error(
            `${relativePath}: unexpected pre-patch sha256 ${actualSha256}; expected ${expectedSha256}.`,
        );
    }
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
    '0deb6b820dfed8804cd76b16a50210fe12202e5e339b5edaa23f6987f1742e0a',
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
