"""
Chat with FB Onborading via Python (OpenAI SDK).
pip install openai
"""
from openai import OpenAI

client = OpenAI(
    api_key="a2a_your_key_here",  # Replace with your key from https://oya.ai/api-keys
    base_url="https://oya.ai/api/v1",
)

# First message — starts a new thread
response = client.chat.completions.create(
    model="gemini/gemini-2.0-flash",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)

# Continue the conversation using thread_id
thread_id = response.thread_id
response = client.chat.completions.create(
    model="gemini/gemini-2.0-flash",
    messages=[{"role": "user", "content": "Follow up question"}],
    extra_body={"thread_id": thread_id},
)
print(response.choices[0].message.content)

# Streaming example
stream = client.chat.completions.create(
    model="gemini/gemini-2.0-flash",
    messages=[{"role": "user", "content": "Tell me about AI agents"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
