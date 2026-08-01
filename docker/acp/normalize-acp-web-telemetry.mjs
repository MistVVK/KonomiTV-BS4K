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


patchFile(
    'node_modules/@google/gemini-cli/bundle/gemini-6K6USV55.js',
    '07430020fdabce0f685daed01a4040e7820cf68b28259feda156abfa217c03ea',
    [
        {
            label: 'canonical invocation input',
            before: `      const confirmationDetails = await invocation.shouldConfirmExecute(abortSignal);
      if (confirmationDetails) {`,
            after: `      const confirmationDetails = await invocation.shouldConfirmExecute(abortSignal);
      const acpToolKind = toAcpToolKind(tool.kind);
      const acpIsWebTool = (
        fc.name === "google_web_search" && acpToolKind === "search"
      ) || (
        fc.name === "web_fetch" && acpToolKind === "fetch"
      );
      const acpRawInput = acpIsWebTool ? {
        type: fc.name,
        ...typeof args.url === "string" ? { url: args.url } : {},
        ...Array.isArray(confirmationDetails?.urls) ? { urls: confirmationDetails.urls } : {}
      } : void 0;
      if (confirmationDetails) {`,
        },
        {
            label: 'permission invocation input',
            before: `            content: content2,
            locations: invocation.toolLocations(),
            kind: toAcpToolKind(tool.kind)
          }
        };`,
            after: `            content: content2,
            locations: invocation.toolLocations(),
            kind: acpToolKind,
            ...acpRawInput ? { rawInput: acpRawInput } : {}
          }
        };`,
        },
        {
            label: 'tool start invocation input',
            before: `          title: displayTitle,
          content: content2,
          locations: invocation.toolLocations(),
          kind: toAcpToolKind(tool.kind)
        });
      }
      const toolResult = await invocation.execute({`,
            after: `          title: displayTitle,
          content: content2,
          locations: invocation.toolLocations(),
          kind: acpToolKind,
          ...acpRawInput ? { rawInput: acpRawInput } : {}
        });
      }
      const toolResult = await invocation.execute({`,
        },
        {
            label: 'tool completion status',
            before: `        toolCallId: callId,
        status: "completed",
        title: displayTitle,`,
            after: `        toolCallId: callId,
        status: toolResult.error ? "failed" : "completed",
        title: displayTitle,`,
        },
        {
            label: 'tool completion sources',
            before: `        title: displayTitle,
        content: updateContent,
        locations: invocation.toolLocations(),
        kind: toAcpToolKind(tool.kind)
      });`,
            after: `        title: displayTitle,
        content: updateContent,
        locations: invocation.toolLocations(),
        kind: acpToolKind,
        ...acpRawInput ? {
          rawInput: acpRawInput,
          rawOutput: {
            sources: Array.isArray(toolResult.sources) ? toolResult.sources.map((source) => ({
              title: typeof source?.web?.title === "string" ? source.web.title : "",
              url: source?.web?.uri
            })).filter((source) => typeof source.url === "string") : []
          }
        } : {}
      });`,
        },
    ],
);

console.log('Normalized Codex and Gemini ACP Web telemetry.');
