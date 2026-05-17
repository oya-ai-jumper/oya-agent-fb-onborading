---
name: fetch-url
display_name: "Fetch URL"
description: "Fetch and extract content from a web URL — articles, documentation, links, or API responses"
category: general
icon: globe
skill_type: tool
catalog_type: core
tool_schema:
  name: fetch_url
  description: "Fetch content from a web page or API endpoint. Modes: 'read' (default) extracts main article content as markdown, 'read_full' converts the entire page to markdown including navigation and sidebars, 'links' extracts all hyperlinks from the page, 'raw' returns the response body exactly as received from the server with no formatting or JSON pretty-printing. Use 'read' for articles and docs, 'read_full' when you need the complete page, 'links' to discover URLs on a page, 'raw' when you need byte-exact output."
  parameters:
    type: object
    properties:
      url:
        type: string
        description: "URL to fetch (e.g. https://example.com/article)"
      mode:
        type: string
        enum: [read, read_full, links, raw]
        description: "How to process the response. Default: 'read'"
    required: [url]
---
# Fetch URL

Retrieve content from any web page or API endpoint with mode-specific post-processing.

## Modes

- **`read`** (default) — Extract main article content as markdown using trafilatura. Best for articles, blog posts, and documentation. Strips navigation, ads, and boilerplate.
- **`read_full`** — Convert the entire page to markdown including navigation and sidebars. Use when you need the full page context (e.g., docs with sidebar TOC).
- **`links`** — Extract all hyperlinks from the page as a markdown list. Use to discover URLs for further fetching.
- **`raw`** — Return the response body exactly as received from the server, with no formatting or JSON pretty-printing. Use when you need byte-exact output (e.g., verifying signatures, parsing minified JSON yourself, or preserving whitespace).

## Security

- Only `http://` and `https://` URLs are allowed
- Private/internal IP ranges are blocked (SSRF protection)
- Response size capped at 5MB
- Redirects followed manually, up to 5 hops, each re-validated
- Output truncated at 50,000 characters

## Content type handling

- `text/html` → processed per selected mode
- `application/json` and `application/*+json` → pretty-printed (except in `raw` mode, where the exact server response is returned)
- `application/pdf` → text extracted page-by-page via pypdf in `read` / `read_full` modes; `links` mode is not supported for PDFs
- Other `text/*` → passed through as-is
- Other binary content → rejected with a descriptive message
