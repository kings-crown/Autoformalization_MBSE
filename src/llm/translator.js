import { buildPrompt } from './prompt.js';
import { createChatCompletion } from './openaiClient.js';
import { getMockLogicalForm } from './mockResponses.js';
import { parseJsonFromText } from '../utils/json.js';

export async function translateRequirementsToLogicalForm(requirementsDoc, options = {}) {
  const useMock = process.env.MOCK_LLM === '1' || options.mock === true;
  if (useMock) {
    const mock = await getMockLogicalForm(requirementsDoc.id);
    if (!mock) {
      throw new Error(
        `MOCK_LLM is enabled but no canned logical form exists for requirements id=${requirementsDoc.id}`
      );
    }
    return mock;
  }

  const messages = buildPrompt(requirementsDoc);
  const response = await createChatCompletion({
    model: options.model,
    messages
  });
  return parseJsonFromText(response);
}
