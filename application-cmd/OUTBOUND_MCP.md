# PathCopilot outbound MCP

PathCopilot has two distinct MCP directions:

- `pathcopilot_mcp` is the inbound FastMCP control server used by Codex,
  Claude Code, Cursor, or another external MCP client.
- `pathcopilot_outbound_mcp` is the client used by the pathology Agent when
  the Agent itself selects `D(Knowledge)` during the PathCopilot Agent Loop.

The full application enables the checked-in PubMed MCP server. The call uses
the official Python SDK and performs the standard lifecycle:

1. start the allowlisted stdio server;
2. `initialize`;
3. `tools/list`;
4. `tools/call` for `search_pubmed`;
5. close the client session and subprocess.

Only the configured server and allowlisted tools can be called. The PubMed
server cannot execute commands or read arbitrary files. It returns a bounded
set of PMID-linked summaries, which is passed back to the same Subagent for a
second evidence-aware decision. Each MCP request also has a bounded
`timeoutSeconds` value in `mcp.outbound.json`.

## Product mode

The default configuration is `mcp.outbound.json`. Optional environment
variables:

```powershell
$env:PATHCOPILOT_OUTBOUND_MCP = '1'
$env:PATHCOPILOT_OUTBOUND_MCP_CONFIG = 'C:\path\to\mcp.outbound.json'
$env:PUBMED_API_KEY = 'optional NCBI API key'
```

## Benchmark/offline mode

External evidence must be disabled for benchmark reproducibility and to avoid
information leakage:

```powershell
$env:PATHCOPILOT_OUTBOUND_MCP = '0'
```

With the switch off, D (Knowledge) returns no external evidence and no MCP
subprocess is started.
