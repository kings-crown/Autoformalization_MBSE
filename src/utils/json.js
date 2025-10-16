export function parseJsonFromText(text) {
  if (!text || typeof text !== 'string') {
    throw new Error('Expected a non-empty string to parse JSON content.');
  }

  const trimmed = text.trim();
  if (!trimmed) {
    throw new Error('Received empty response while parsing JSON.');
  }

  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    return JSON.parse(trimmed);
  }

  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced) {
    return JSON.parse(fenced[1]);
  }

  throw new Error('Could not locate a JSON object in the provided text.');
}
