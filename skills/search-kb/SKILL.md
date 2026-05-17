---
name: search-kb
display_name: "Knowledge Base"
description: "Search the agent's uploaded documents and knowledge base"
category: general
icon: book-open
skill_type: tool
catalog_type: core
tool_schema:
  name: search_knowledge_base
  description: "Search the agent's knowledge base for relevant information from uploaded documents"
  parameters:
    type: object
    properties:
      query:
        type: "string"
        description: "Search query to find relevant knowledge base content"
      top_k:
        type: "integer"
        description: "Number of results to return (default 5)"
        default: 5
    required: [query]
---
# Knowledge Base
Search the agent's uploaded documents and knowledge base.

- **search** — Provide a `query` to find relevant content from uploaded documents, PDFs, URLs, and other knowledge sources.

Results are ranked by semantic similarity. Use this when users ask about stored content, documentation, or any information that might be in the knowledge base.
