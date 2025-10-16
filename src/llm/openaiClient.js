import assert from 'node:assert';

function resolveModel(model) {
  if (model) return model;
  return process.env.OPENAI_MODEL || 'gpt-4.1-mini';
}

export async function createChatCompletion({ model, messages }) {
  const apiKey = process.env.OPENAI_API_KEY;
  const baseUrl = process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1';

  assert(messages?.length, 'createChatCompletion requires messages');

  if (!apiKey) {
    throw new Error('OPENAI_API_KEY environment variable is not set.');
  }

  const response = await fetch(`${baseUrl}/chat/completions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`
    },
    body: JSON.stringify({
      model: resolveModel(model),
      messages,
      temperature: 0,
      top_p: 0.1,
      response_format: { type: 'json_object' }
    })
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`LLM call failed with status ${response.status}: ${body}`);
  }

  const payload = await response.json();
  const content =
    payload.choices?.[0]?.message?.content ??
    payload.choices?.[0]?.message?.tool_calls?.[0]?.function?.arguments;

  if (!content) {
    throw new Error('LLM call succeeded but no content was returned.');
  }

  return content;
}
