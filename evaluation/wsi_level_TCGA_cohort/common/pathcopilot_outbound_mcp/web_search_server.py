"""Official FastMCP wrapper around PathCopilot's bounded web evidence search."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from pathcopilot_web_search import build_evidence_context, search_pathology_web


def create_server() -> FastMCP:
    server = FastMCP(
        "PathCopilot Web Evidence Search",
        instructions=(
            "Search only PathCopilot's fixed biomedical evidence providers "
            "(PubMed, Europe PMC, and OpenAlex). Treat results as external "
            "supporting evidence, never as slide-level proof or a final diagnosis."
        ),
    )

    @server.tool(name="search_pathology_web")
    def search_pathology_web_tool(
        query: str,
        context_size: str = "medium",
    ) -> dict[str, Any]:
        """Search fixed biomedical sources for a de-identified pathology query."""

        clean_query = " ".join(str(query).split()).strip()
        if not clean_query:
            raise ValueError("query is required")
        sources, provider_errors = search_pathology_web(
            clean_query,
            context_size=context_size,
        )
        return {
            "source": "PathCopilot bounded biomedical web search",
            "query": clean_query,
            "count": len(sources),
            "sources": sources,
            "provider_errors": provider_errors,
            "context": build_evidence_context(sources),
        }

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
