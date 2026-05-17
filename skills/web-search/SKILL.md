---
name: web-search
display_name: "Web Search"
description: "Search the web for current information, news, and answers"
category: general
icon: search
skill_type: tool
catalog_type: core
tool_schema:
  name: web_search
  description: "Search the web for current information using Tavily search API"
  parameters:
    type: object
    properties:
      query:
        type: "string"
        description: "The search query to look up on the web"
    required: [query]
---
# Web Search
Search the web for current information, news, and answers.

- **query** — Provide a search query to find relevant web results.

Returns the top 5 results with titles, URLs, and content snippets. Use this when users ask about recent events, need factual answers, or want to look something up online.
