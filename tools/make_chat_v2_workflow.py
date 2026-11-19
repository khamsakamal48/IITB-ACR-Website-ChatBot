"""Generate workflows/C-v2-chat-okf.json (n8n prod id T4BkIX8xsaZzu6hd) from
workflows/C-v2-system-prompt.md. Mirrors prod; edit here, regenerate, then push to n8n.

  python3 -I tools/make_chat_v2_workflow.py .
"""
import json, pathlib, sys
ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
# Chat model: openai/gpt-6-luna via OpenRouter on n8n's OpenAI Chat Model node (credential base URL
# https://openrouter.ai/api/v1). n8n's own OpenRouter Chat Model node 401s on this instance even with a
# valid key (same credential works in HTTP Request). Bake-off 2026-11-17: luna 1.8-2.7 s/call,
# $0.0035; gemini-3.8-flash 3.5-5.4 s, $0.023; gemini-3.5-flash-lite ~2 s but misattributed contacts.
MODEL = "openai/gpt-6-luna"
GUARD_MODEL = "google/gemini-3.5-flash-lite"   # guardrail checks (jailbreak, NSFW)
OR_CRED = {"openAiApi": {"id": "V8kzhjnzg317YSjL", "name": "ACR Website Chatbot v2 - OpenRouter (OpenAI-compatible)"}}
prompt = (ROOT / "workflows/C-v2-system-prompt.md").read_text().strip()
system = ("=" + prompt +
  "\n\n# KNOWLEDGE\n{{ $('Get KB Context').first().json.text }}\n\n"
  "Today's date: {{ $now.setZone('Asia/Kolkata').toFormat('d LLLL yyyy') }}")

KB = "http://acr-trafilatura:8000"
KB_CRED = {"httpHeaderAuth": {"id": "5DiC041ERvTk9E5S", "name": "ACR Knowledge Service"}}
PG_CRED = {"postgres": {"id": "0ZOm9jEUO1M7PyKG", "name": "ACR Chatbot (local Postgres)"}}   # acr_chatbot DB in n8n-postgres (sql/v2-local-postgres.sql)
P = "$('Prepare Input').first().json"

def reply(name, outcome, text, pos, flags="null"):
    return {"id": name.lower().replace(" ", "-").replace(":", ""), "name": name,
            "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": pos,
            "parameters": {"mode": "raw", "options": {},
              "jsonOutput": "={{ { output: " + json.dumps(text) + ", outcome: '" + outcome + "', "
                f"session_id: {P}.sessionId, question: {P}.chatInput, guard_flags: {flags}, "
                f"sanitized: null, tool_calls: 0, pages_read: [], latency_ms: Date.now() - {P}.startedAt, "
                "model: null, kb_version: null } }}"}}

def tool(name, path, desc, params, pos):
    """n8n-nodes-base.httpRequestTool (the LangChain toolHttpRequest node fails as an agent
    tool on this n8n: 'has a supplyData method but no execute method')."""
    return {"id": name, "name": name, "type": "n8n-nodes-base.httpRequestTool", "typeVersion": 4.2,
            "position": pos, "credentials": KB_CRED,
            "parameters": {"toolDescription": desc, "method": "GET", "url": KB + path,
              "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
              "sendQuery": True, "specifyQuery": "keypair",
              "queryParameters": {"parameters": [{"name": k, "value": v} for k, v in params]},
              "options": {"timeout": 8000}}}

# Answer checks live in the knowledge service now: POST /kb/check-answer (knowledge.py).

nodes = [
 {"id": "webhook", "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [-1200, 300],
  "webhookId": "c2f6a3d1-8e4b-4c7a-9f2e-6b1d0a5e3c27", "credentials": {"httpHeaderAuth": {"id": "18HABdiT6BevZo2H", "name": "ACR Chat v2 Webhook (BravesChat)"}},
  "parameters": {"httpMethod": "POST", "path": "acr-chat-v2", "authentication": "headerAuth", "responseMode": "responseNode", "options": {}}},
 {"id": "prepare", "name": "Prepare Input", "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [-980, 300],
  "parameters": {"mode": "manual", "options": {}, "assignments": {"assignments": [
    {"id": "a1", "name": "chatInput", "type": "string", "value": "={{ String($json.body?.chatInput ?? '').trim() }}"},
    {"id": "a2", "name": "sessionId", "type": "string", "value": "={{ String($json.body?.sessionId ?? '').trim().slice(0, 100) }}"},
    {"id": "a3", "name": "startedAt", "type": "number", "value": "={{ Date.now() }}"}]}}},
 {"id": "valid", "name": "Valid Input?", "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [-760, 300],
  "parameters": {"options": {}, "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
    "combinator": "and", "conditions": [
      {"id": "c1", "leftValue": "={{ $json.chatInput.length }}", "rightValue": 1, "operator": {"type": "number", "operation": "gte"}},
      {"id": "c2", "leftValue": "={{ $json.chatInput.length }}", "rightValue": 1000, "operator": {"type": "number", "operation": "lte"}},
      {"id": "c3", "leftValue": "={{ $json.sessionId }}", "rightValue": "", "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}]}}},
 {"id": "ratecheck", "name": "Recent Messages", "type": "n8n-nodes-base.postgres", "typeVersion": 2.6, "position": [-540, 200],
  "credentials": PG_CRED, "alwaysOutputData": True, "onError": "continueRegularOutput",
  "parameters": {"operation": "executeQuery",
    "query": "SELECT count(*)::int AS recent FROM chat_logs WHERE session_id = $1 AND ts > now() - interval '10 minutes';",
    "options": {"queryReplacement": "={{ [ $json.sessionId ] }}"}}},
 {"id": "withinlimit", "name": "Within Rate Limit?", "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [-320, 200],
  "parameters": {"options": {}, "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
    "combinator": "and", "conditions": [
      {"id": "r1", "leftValue": "={{ Number($json.recent ?? 0) }}", "rightValue": 20, "operator": {"type": "number", "operation": "lt"}}]}}},
 {"id": "guard", "name": "Input Guardrails", "type": "@n8n/n8n-nodes-langchain.guardrails", "typeVersion": 2, "position": [-100, 100],
  "onError": "continueRegularOutput",
  "parameters": {"operation": "classify", "text": f"={{{{ {P}.chatInput }}}}",
    "guardrails": {"jailbreak": {"value": {"threshold": 0.8}}, "nsfw": {"value": {"threshold": 0.8}}}}},
 {"id": "guardmodel", "name": "Guardrail Model (gemini-3.5-flash-lite via OpenRouter)", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
  "typeVersion": 1.3, "position": [-100, 320], "credentials": OR_CRED,
  "notes": "Must return raw JSON the Guardrails node can parse. gpt-6-luna wraps verdicts in ```json fences -> node fails closed and blocks everything. Direct Gemini API was slow (~6 s) on 2026-11-19; via OpenRouter ~1.1 s.",
  "parameters": {"model": {"__rl": True, "value": GUARD_MODEL, "mode": "id"}, "builtInTools": {},
    "options": {"maxTokens": 200, "temperature": 0, "timeout": 15000, "maxRetries": 1}}},
 {"id": "kbctx", "name": "Get KB Context", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [140, 0],
  "credentials": KB_CRED, "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 500, "onError": "continueErrorOutput",
  "parameters": {"method": "GET", "url": KB + "/kb/context", "authentication": "genericCredentialType",
    "genericAuthType": "httpHeaderAuth", "options": {"timeout": 8000}}},
 {"id": "agent", "name": "AI Agent", "type": "@n8n/n8n-nodes-langchain.agent", "typeVersion": 3.1, "position": [400, 0],
  "onError": "continueErrorOutput",
  "parameters": {"promptType": "define", "text": f"={{{{ {P}.chatInput }}}}",
    "options": {"systemMessage": system, "maxIterations": 6, "returnIntermediateSteps": True}}},
 {"id": "chatmodel", "name": "Chat Model (gpt-6-luna via OpenRouter)", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi",
  "typeVersion": 1.3, "position": [260, 260], "credentials": OR_CRED,
  "parameters": {"model": {"__rl": True, "value": MODEL, "mode": "id"}, "builtInTools": {},
    "options": {"maxTokens": 1500, "timeout": 30000, "maxRetries": 1}}},
 {"id": "memory", "name": "Postgres Chat Memory", "type": "@n8n/n8n-nodes-langchain.memoryPostgresChat", "typeVersion": 1.4,
  "position": [400, 260], "credentials": PG_CRED,
  "parameters": {"sessionIdType": "customKey", "sessionKey": f"={{{{ {P}.sessionId }}}}",
    "tableName": "chat_histories", "contextWindowLength": 6}},
 tool("search_knowledge", "/kb/search",
   "Find which knowledge pages cover a topic. Returns up to 8 pages with path, title, description, url, modified date and a snippet. Then open the best page with read_page.",
   [("q", "={{ $fromAI('q', 'Short keyword search query, e.g. \"Distinguished Alumnus Award 1976 chemical\" or \"hostel room naming\"', 'string') }}"),
    ("limit", "8")], [520, 260]),
 tool("read_page", "/kb/page",
   "Read one whole knowledge page by its path (from search results or the index), e.g. 'faq/donations.md' or 'sources/page/young-faculty-fellowship.md'. If the result has next_offset, call again with offset = next_offset to read the rest of a long document.",
   [("path", "={{ $fromAI('path', 'Page path such as concepts/ways-of-giving.md or sources/awards/mr-nandan-m-nilekani-3.md', 'string') }}"),
    ("offset", "={{ $fromAI('offset', 'Character offset to continue a long page: next_offset from the previous call, or 0 for the start', 'number', 0) }}")], [660, 260]),
 tool("browse_folder", "/kb/list",
   "List all pages in a folder with one-line descriptions. Folders: 'concepts', 'faq', or 'sources/<type>' such as 'sources/awards' (all award recipients, grouped by award, with degree and year), 'sources/events_list', 'sources/batch_legacy', 'sources/legacy_project', 'sources/pdf' (annual reports).",
   [("folder", "={{ $fromAI('folder', 'Folder to list, e.g. sources/awards', 'string') }}")], [800, 260]),
 {"id": "escalate", "name": "Escalate to ACR Team", "type": "n8n-nodes-base.microsoftOutlookTool", "typeVersion": 2, "position": [940, 260],
  "webhookId": "7d3e9a1b-5c2f-4e6d-8a9b-0f4c2e8d1a63",
  "credentials": {"microsoftOutlookOAuth2Api": {"id": "sOlo4aRn9GbBUHhw", "name": "Kamal Outlook Account"}},
  "parameters": {"toRecipients": "kamal.hamza@iitbacr.com", "subject": "Enquiry", "additionalFields": {"bodyContentType": "html"}}},
 {"id": "checkanswer", "name": "Check Answer", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [760, -60],
  "credentials": KB_CRED, "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 300, "onError": "continueErrorOutput",
  "notes": "Link/email allow-list + grounded phone numbers + pages read, in the knowledge service (POST /kb/check-answer). Returns the full chat_logs record.",
  "parameters": {"method": "POST", "url": KB + "/kb/check-answer", "authentication": "genericCredentialType",
    "genericAuthType": "httpHeaderAuth", "sendBody": True, "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ answer: $json.output ?? '', steps: $json.intermediateSteps ?? [], "
                f"session_id: {P}.sessionId, question: {P}.chatInput, started_at: {P}.startedAt, model: '" + MODEL + "' }) }}",
    "options": {"timeout": 5000}}},
 reply("Reply: Invalid Input", "invalid", "Please type your question (up to 1,000 characters) and we'll do our best to help.", [-540, 460]),
 reply("Reply: Rate Limited", "rate_limited", "You've sent quite a few messages in a short time. Please wait a few minutes and try again, or write to us at dean.acr.office@iitbombay.org.", [-100, 460]),
 reply("Reply: Blocked", "blocked", "I can only help with questions about IIT Bombay alumni, giving and ACR programmes. Could you rephrase your question?", [140, 200], flags="$json"),
 reply("Reply: Error", "error", "Sorry, I'm having trouble answering right now. Please try again in a moment, or write to us at dean.acr.office@iitbombay.org.", [760, 140]),
 {"id": "respond", "name": "Respond to Widget", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.4, "position": [1040, 200],
  "parameters": {"respondWith": "json", "responseBody": "={{ { output: $json.output } }}", "options": {}}},
 {"id": "log", "name": "Log Chat", "type": "n8n-nodes-base.postgres", "typeVersion": 2.6, "position": [1260, 200],
  "credentials": PG_CRED, "onError": "continueRegularOutput",
  "parameters": {"operation": "executeQuery",
    "query": "INSERT INTO chat_logs (session_id, question, answer, outcome, guard_flags, sanitized, tool_calls, pages_read, latency_ms, model, kb_version) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, ARRAY(SELECT jsonb_array_elements_text($8::jsonb)), $9, $10, $11);",
    "options": {"queryReplacement": "={{ [ $json.session_id || 'unknown', $json.question || '', $json.output, $json.outcome, JSON.stringify($json.guard_flags ?? null), JSON.stringify($json.sanitized ?? null), $json.tool_calls ?? 0, JSON.stringify($json.pages_read ?? []), $json.latency_ms ?? null, $json.model ?? null, $json.kb_version ?? null ] }}"}}},
]
conn = {
 "Webhook": {"main": [[{"node": "Prepare Input", "type": "main", "index": 0}]]},
 "Prepare Input": {"main": [[{"node": "Valid Input?", "type": "main", "index": 0}]]},
 "Valid Input?": {"main": [[{"node": "Recent Messages", "type": "main", "index": 0}], [{"node": "Reply: Invalid Input", "type": "main", "index": 0}]]},
 "Recent Messages": {"main": [[{"node": "Within Rate Limit?", "type": "main", "index": 0}]]},
 "Within Rate Limit?": {"main": [[{"node": "Input Guardrails", "type": "main", "index": 0}], [{"node": "Reply: Rate Limited", "type": "main", "index": 0}]]},
 "Input Guardrails": {"main": [[{"node": "Get KB Context", "type": "main", "index": 0}], [{"node": "Reply: Blocked", "type": "main", "index": 0}]]},
 "Guardrail Model (gemini-3.5-flash-lite via OpenRouter)": {"ai_languageModel": [[{"node": "Input Guardrails", "type": "ai_languageModel", "index": 0}]]},
 "Get KB Context": {"main": [[{"node": "AI Agent", "type": "main", "index": 0}], [{"node": "Reply: Error", "type": "main", "index": 0}]]},
 "Chat Model (gpt-6-luna via OpenRouter)": {"ai_languageModel": [[{"node": "AI Agent", "type": "ai_languageModel", "index": 0}]]},
 "Postgres Chat Memory": {"ai_memory": [[{"node": "AI Agent", "type": "ai_memory", "index": 0}]]},
 "search_knowledge": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]},
 "read_page": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]},
 "browse_folder": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]},
 "Escalate to ACR Team": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]},
 "AI Agent": {"main": [[{"node": "Check Answer", "type": "main", "index": 0}], [{"node": "Reply: Error", "type": "main", "index": 0}]]},
 "Check Answer": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}], [{"node": "Reply: Error", "type": "main", "index": 0}]]},
 "Reply: Invalid Input": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}]]},
 "Reply: Rate Limited": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}]]},
 "Reply: Blocked": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}]]},
 "Reply: Error": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}]]},
 "Respond to Widget": {"main": [[{"node": "Log Chat", "type": "main", "index": 0}]]},
}
wf = {"name": "ACR Chat v2 — OKF knowledge (gpt-6-luna)", "nodes": nodes, "connections": conn,
      "settings": {"executionOrder": "v1", "timezone": "Asia/Kolkata", "saveDataErrorExecution": "all",
                   "saveDataSuccessExecution": "all", "executionTimeout": 55}}
(ROOT / "workflows/C-v2-chat-okf.json").write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
print(len(nodes), "nodes;", len(system), "chars system message")
