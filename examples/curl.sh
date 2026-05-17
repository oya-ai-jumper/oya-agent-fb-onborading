#!/bin/bash
# Chat with FB Onborading via cURL
# Replace a2a_your_key_here with your actual API key from https://oya.ai/api-keys

# First message — starts a new thread
curl -X POST https://oya.ai/api/v1/chat/completions \
  -H "Authorization: Bearer a2a_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini/gemini-2.0-flash","messages":[{"role":"user","content":"Hello"}]}'

# Continue a conversation using thread_id from the first response:
curl -X POST https://oya.ai/api/v1/chat/completions \
  -H "Authorization: Bearer a2a_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini/gemini-2.0-flash","messages":[{"role":"user","content":"Follow up question"}],"thread_id":"thread_id_from_previous_response"}'
