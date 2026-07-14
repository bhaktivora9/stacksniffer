import asyncio

import google.generativeai as genai
from dotenv import load_dotenv
from os import getenv

load_dotenv()

genai.configure(api_key=getenv("GEMINI_API_KEY", ""))

CHAT_MODEL = getenv("GEMINI_CHAT_MODEL", "gemini-2.0-flash")
MAX_CHAT_TOKENS = int(getenv("GEMINI_MAX_TOKENS_CHAT", 10000))


def build_system_prompt(analysis: dict) -> str:
    stack = analysis.get("stack", {})
    repo = analysis.get("repo", {})

    def tech_list(category):
        techs = stack.get(category, [])
        return ", ".join(
            f"{t['name']} ({int(t['confidence']*100)}%, {t['detection_source']})"
            for t in techs
        ) or "none detected"

    ai_inferences = stack.get("ai_inferences", [])
    inference_text = "\n".join(
        f"  - {i['tech']} ({i['category']}): {i['reasoning']}"
        for i in ai_inferences
    ) or "  none"

    pattern_matches = stack.get("pattern_matches", [])
    pattern_text = "\n".join(
        f"  - {p['tech']}: matched '{p['matched_keyword']}' in {p['matched_file']} (confidence {int(p['confidence']*100)}%)"
        for p in pattern_matches[:10]
    ) or "  none"

    return f"""You are StackSniffer's repository analysis assistant.
You have deep knowledge of ONE specific repository that has been analyzed.
You answer questions about this repository's tech stack, architecture, and detection results.

REPOSITORY: {repo.get('full_name', 'unknown')}
DESCRIPTION: {repo.get('description', 'no description')}
STARS: {repo.get('stars', 0)} | TOPICS: {', '.join(repo.get('topics', []))}

DETECTED TECH STACK:
  Languages:  {tech_list('languages')}
  Frameworks: {tech_list('frameworks')}
  Databases:  {tech_list('databases')}
  Messaging:  {tech_list('messaging')}
  AI/ML:      {tech_list('ai_ml')}
  Infra:      {tech_list('infra')}
  Testing:    {tech_list('testing')}

AI ANALYSIS RESULTS:
  Domain:             {stack.get('domain', 'unknown')} ({int(stack.get('domain_confidence', 0)*100)}% confidence)
  Domain reasoning:   {stack.get('domain_reasoning', '')}
  Architecture style: {stack.get('architecture_style', 'unknown')}
  Stack pattern:      {stack.get('stack_pattern', 'unknown')}
  Why this stack:     {stack.get('why_this_stack', '')}
  Ecosystem context:  {stack.get('ecosystem_context', '')}
  Notable combos:     {', '.join(stack.get('notable_combinations', []))}
  Missing patterns:   {', '.join(stack.get('missing_patterns', []))}

AI INFERENCES (techs Gemini identified beyond pattern rules):
{inference_text}

PATTERN MATCHES (rules that fired):
{pattern_text}

COMPLEXITY SCORE: {stack.get('complexity_score', 0)}/10
AI CALLS MADE: {stack.get('ai_calls_made', 0)}
FILES ANALYZED: {stack.get('files_analyzed', 0)}
PATTERNS CHECKED: {stack.get('patterns_checked', 0)}

STRICT RULES:
1. Only discuss what is in the analysis above. Do not invent features.
2. If asked about something not detected, say "StackSniffer did not detect X in this repo."
3. When explaining AI inferences, cite the reasoning string above.
4. When explaining pattern matches, cite the matched file and keyword.
5. Do NOT generate documentation, READMEs, or code. That is a different tool.
6. Keep answers concise — 2-4 sentences unless the user asks for detail.
7. You may suggest follow-up questions relevant to the detected stack.
"""


async def stream_chat(
    analysis: dict,
    conversation_history: list[dict],
    user_message: str,
):
    system_instruction = build_system_prompt(analysis)

    chat_model = genai.GenerativeModel(
        CHAT_MODEL,
        system_instruction=system_instruction,
        generation_config=genai.types.GenerationConfig(
            max_output_tokens=MAX_CHAT_TOKENS,
            temperature=0.3,
        ),
    )

    gemini_history = [
        {
            "role": "user" if m["role"] == "user" else "model",
            "parts": [m["content"]],
        }
        for m in conversation_history
    ]

    chat = chat_model.start_chat(history=gemini_history)

    try:
        # stream=False returns a GenerateContentResponse — access .text directly
        response = await asyncio.to_thread(
            chat.send_message, user_message, stream=False
        )
        yield response.text

    except Exception as e:
        err = str(e)
        if "429" in err or "ResourceExhausted" in err or "quota" in err.lower():
            yield "Gemini rate limit reached. Please wait a minute and try again."
        elif "403" in err:
            yield "Gemini API key invalid or permissions error. Check your GEMINI_API_KEY."
        else:
            yield f"Chat error: {err[:200]}"
