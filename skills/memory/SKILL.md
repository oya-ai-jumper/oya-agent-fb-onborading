---
name: memory
display_name: "Agent Memory"
description: "Remember and recall facts about users and context across conversations, and manage your persistent scratchpad/brain"
category: general
icon: brain
skill_type: tool
catalog_type: core
tool_schema:
  name: memory
  description: "Read or write agent memory and scratchpad — persist facts and manage your brain/scratchpad"
  parameters:
    type: object
    properties:
      action:
        type: "string"
        description: "read/write for individual memories, read_scratchpad/update_scratchpad for your persistent brain document"
        enum: ["read", "write", "read_scratchpad", "update_scratchpad"]
      content:
        type: "string"
        description: "The fact to remember (for write) or full scratchpad markdown content (for update_scratchpad)"
        default: ""
    required: [action]
---
# Agent Memory

Remember and recall facts about users and context across conversations, and manage your persistent scratchpad (brain).

## Individual Memories
- **read** — Retrieve all stored memories/facts for this agent.
- **write** — Save a new fact to memory. Provide `content` with the fact to remember.

## Scratchpad (Brain)
- **read_scratchpad** — Read your full scratchpad/brain document.
- **update_scratchpad** — Replace the entire scratchpad with new content. Always read first, modify, then write back the full content.

Use individual memories for quick facts. Use the scratchpad for organized, structured knowledge — rules, learnings, preferences, and patterns you want to maintain over time.
