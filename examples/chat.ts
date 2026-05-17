/**
 * Chat with FB Onborading via TypeScript (OpenAI SDK).
 * npm install openai
 * Run with: npx tsx chat.ts
 */
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "a2a_your_key_here", // Replace with your key from https://oya.ai/api-keys
  baseURL: "https://oya.ai/api/v1",
});

async function main() {
  // First message — starts a new thread
  const response = await client.chat.completions.create({
    model: "gemini/gemini-2.0-flash",
    messages: [{ role: "user", content: "Hello" }],
  });
  console.log(response.choices[0].message.content);

  // Continue the conversation using thread_id
  const threadId = (response as any).thread_id;
  const followUp = await client.chat.completions.create({
    model: "gemini/gemini-2.0-flash",
    messages: [{ role: "user", content: "Follow up question" }],
    // @ts-ignore — custom field
    thread_id: threadId,
  });
  console.log(followUp.choices[0].message.content);
}

main();
