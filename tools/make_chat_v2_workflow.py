"""Generate workflows/C-v2-chat-okf.json (n8n prod id T4BkIX8xsaZzu6hd) from
workflows/C-v2-system-prompt.md. Mirrors prod; edit here, regenerate, then push to n8n.

  python3 -I tools/make_chat_v2_workflow.py .
"""
import json, pathlib, sys
ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
MODEL = "models/gemini-3.5-flash-lite"   # interim: fast; 3.8 Flash on Vertex (low thinking) planned
prompt = (ROOT / "workflows/C-v2-system-prompt.md").read_text().strip()
system = ("=" + prompt +
  "\n\n# KNOWLEDGE\n{{ $('Get KB Context').first().json.text }}\n\n"
  "Today's date: {{ $now.setZone('Asia/Kolkata').toFormat('d LLLL yyyy') }}")

KB = "http://acr-trafilatura:8000"
KB_CRED = {"httpHeaderAuth": {"id": "5DiC041ERvTk9E5S", "name": "ACR Knowledge Service"}}
PG_CRED = {"postgres": {"id": "uM0P1pO2vip8g5GB", "name": "Supabase - IT Ops - ACR Website Chatbot"}}
GEM_CRED = {"googlePalmApi": {"id": "apsURcCTtxKzPb77", "name": "ACR Website Chatbot - Gemini"}}
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

SANITIZE = r"""// Why Code: allow-list every link and email in the answer (markdown links, bare URLs,
// mailto, plain emails) and extract which pages the agent read — no native node does either.
const p = $('Prepare Input').first().json;
const kb = $('Get KB Context').first().json;
let text = String($json.output ?? '').trim();
const HOSTS = ['iitbombay.org', 'iitb.ac.in', 'iitbacr.com', 'sarc-iitb.org'];
const PREFIXES = ['https://docs.google.com/forms/'];
const okEmail = e => HOSTS.some(h => e.toLowerCase().endsWith('@' + h) || e.toLowerCase().endsWith('.' + h));
// No URL() here: the Code-node sandbox lacks it, so parse the host with a regex.
const okUrl = u => {
  const mail = /^mailto:([^?]+)/i.exec(u);
  if (mail) return okEmail(decodeURIComponent(mail[1]));
  const m = /^https?:\/\/([^\/?#:@\s]+)(?::\d+)?(?:[\/?#]|$)/i.exec(u);
  if (!m) return false;
  const host = m[1].toLowerCase();
  return HOSTS.some(h => host === h || host.endsWith('.' + h)) || PREFIXES.some(pr => u.startsWith(pr));
};
const removed = [];
text = text.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (m, label, url) => okUrl(url) ? m : (removed.push(url), label));
text = text.replace(/https?:\/\/[^\s)\]>"'<]+/g, u => {
  const clean = u.replace(/[.,;:!?]+$/, '');
  return okUrl(clean) ? u : (removed.push(clean), '[link removed]');
});
text = text.replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/g, e => okEmail(e) ? e : (removed.push(e), '[email removed]'));
// Phone numbers must be grounded: present in the prompt knowledge or a tool result this turn.
const stepsAll = $json.intermediateSteps ?? [];
const evidence = String(kb.text ?? '') + ' ' + stepsAll.map(s => typeof s.observation === 'string' ? s.observation : JSON.stringify(s.observation ?? '')).join(' ');
const PHONE = /\+?\d[\d ()\-]{6,}\d/g;
const digits = s => s.replace(/\D/g, '');
const known = new Set((evidence.match(PHONE) || []).map(digits).filter(d => d.length >= 8).flatMap(d => [d, d.slice(-8)]));
const yearRange = /^(19|20)\d{2}\s*[-\u2013]\s*(19|20)?\d{2}$/;
text = text.replace(PHONE, ph => {
  const d = digits(ph);
  if (d.length < 8 || yearRange.test(ph.trim())) return ph;
  return (known.has(d) || known.has(d.slice(-8))) ? ph : (removed.push(ph), '[number removed]');
});
if (!text) text = "Sorry, I couldn't put an answer together. Please try rephrasing, or write to us at dean.acr.office@iitbombay.org.";
const steps = $json.intermediateSteps ?? [];
const pages = [];
for (const s of steps) {
  let inp = s.action?.toolInput ?? {};
  if (typeof inp === 'string') { try { inp = JSON.parse(inp); } catch { inp = {}; } }
  if (s.action?.tool === 'read_page' && inp.path) pages.push(String(inp.path));
}
return { json: {
  output: text, outcome: 'answered', session_id: p.sessionId, question: p.chatInput,
  guard_flags: null, sanitized: removed.length ? { removed } : null,
  tool_calls: steps.length, pages_read: pages, latency_ms: Date.now() - p.startedAt,
  model: 'gemini-3.8-flash', kb_version: kb.version ?? null,
} };"""

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
    "query": "SELECT count(*)::int AS recent FROM acr_website_chatbot.chat_logs WHERE session_id = $1 AND ts > now() - interval '10 minutes';",
    "options": {"queryReplacement": "={{ [ $json.sessionId ] }}"}}},
 {"id": "withinlimit", "name": "Within Rate Limit?", "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [-320, 200],
  "parameters": {"options": {}, "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
    "combinator": "and", "conditions": [
      {"id": "r1", "leftValue": "={{ Number($json.recent ?? 0) }}", "rightValue": 20, "operator": {"type": "number", "operation": "lt"}}]}}},
 {"id": "guard", "name": "Input Guardrails", "type": "@n8n/n8n-nodes-langchain.guardrails", "typeVersion": 2, "position": [-100, 100],
  "onError": "continueRegularOutput",
  "parameters": {"operation": "classify", "text": f"={{{{ {P}.chatInput }}}}",
    "guardrails": {"jailbreak": {"value": {"threshold": 0.8}}, "nsfw": {"value": {"threshold": 0.8}}}}},
 {"id": "gemlite", "name": "Gemini Flash-Lite (guardrails)", "type": "@n8n/n8n-nodes-langchain.lmChatGoogleGemini", "typeVersion": 1.2,
  "position": [-100, 320], "credentials": GEM_CRED,
  "parameters": {"modelName": "models/gemini-3.5-flash-lite", "options": {"temperature": 0, "maxOutputTokens": 200}}},
 {"id": "kbctx", "name": "Get KB Context", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [140, 0],
  "credentials": KB_CRED, "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 500, "onError": "continueErrorOutput",
  "parameters": {"method": "GET", "url": KB + "/kb/context", "authentication": "genericCredentialType",
    "genericAuthType": "httpHeaderAuth", "options": {"timeout": 8000}}},
 {"id": "agent", "name": "AI Agent", "type": "@n8n/n8n-nodes-langchain.agent", "typeVersion": 3.1, "position": [400, 0],
  "onError": "continueErrorOutput",
  "parameters": {"promptType": "define", "text": f"={{{{ {P}.chatInput }}}}",
    "options": {"systemMessage": system, "maxIterations": 6, "returnIntermediateSteps": True}}},
 {"id": "gemflash", "name": "Gemini 3.8 Flash", "type": "@n8n/n8n-nodes-langchain.lmChatGoogleGemini", "typeVersion": 1.2,
  "position": [260, 260], "credentials": GEM_CRED,
  "parameters": {"modelName": MODEL, "options": {"temperature": 0.2, "maxOutputTokens": 1500}}},
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
 {"id": "sanitize", "name": "Check Links & Emails", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [760, -60],
  "notes": "Why Code: allow-list every link and email in the answer and extract pages read from intermediate steps; no native node does either.",
  "parameters": {"mode": "runOnceForEachItem", "jsCode": SANITIZE}},
 reply("Reply: Invalid Input", "invalid", "Please type your question (up to 1,000 characters) and we'll do our best to help.", [-540, 460]),
 reply("Reply: Rate Limited", "rate_limited", "You've sent quite a few messages in a short time. Please wait a few minutes and try again, or write to us at dean.acr.office@iitbombay.org.", [-100, 460]),
 reply("Reply: Blocked", "blocked", "I can only help with questions about IIT Bombay alumni, giving and ACR programmes. Could you rephrase your question?", [140, 200], flags="$json"),
 reply("Reply: Error", "error", "Sorry, I'm having trouble answering right now. Please try again in a moment, or write to us at dean.acr.office@iitbombay.org.", [760, 140]),
 {"id": "respond", "name": "Respond to Widget", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.4, "position": [1040, 200],
  "parameters": {"respondWith": "json", "responseBody": "={{ { output: $json.output } }}", "options": {}}},
 {"id": "log", "name": "Log Chat", "type": "n8n-nodes-base.postgres", "typeVersion": 2.6, "position": [1260, 200],
  "credentials": PG_CRED, "onError": "continueRegularOutput",
  "parameters": {"operation": "executeQuery",
    "query": "INSERT INTO acr_website_chatbot.chat_logs (session_id, question, answer, outcome, guard_flags, sanitized, tool_calls, pages_read, latency_ms, model, kb_version) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, ARRAY(SELECT jsonb_array_elements_text($8::jsonb)), $9, $10, $11);",
    "options": {"queryReplacement": "={{ [ $json.session_id || 'unknown', $json.question || '', $json.output, $json.outcome, JSON.stringify($json.guard_flags ?? null), JSON.stringify($json.sanitized ?? null), $json.tool_calls ?? 0, JSON.stringify($json.pages_read ?? []), $json.latency_ms ?? null, $json.model ?? null, $json.kb_version ?? null ] }}"}}},
]
conn = {
 "Webhook": {"main": [[{"node": "Prepare Input", "type": "main", "index": 0}]]},
 "Prepare Input": {"main": [[{"node": "Valid Input?", "type": "main", "index": 0}]]},
 "Valid Input?": {"main": [[{"node": "Recent Messages", "type": "main", "index": 0}], [{"node": "Reply: Invalid Input", "type": "main", "index": 0}]]},
 "Recent Messages": {"main": [[{"node": "Within Rate Limit?", "type": "main", "index": 0}]]},
 "Within Rate Limit?": {"main": [[{"node": "Input Guardrails", "type": "main", "index": 0}], [{"node": "Reply: Rate Limited", "type": "main", "index": 0}]]},
 "Input Guardrails": {"main": [[{"node": "Get KB Context", "type": "main", "index": 0}], [{"node": "Reply: Blocked", "type": "main", "index": 0}]]},
 "Gemini Flash-Lite (guardrails)": {"ai_languageModel": [[{"node": "Input Guardrails", "type": "ai_languageModel", "index": 0}]]},
 "Get KB Context": {"main": [[{"node": "AI Agent", "type": "main", "index": 0}], [{"node": "Reply: Error", "type": "main", "index": 0}]]},
 "Gemini 3.8 Flash": {"ai_languageModel": [[{"node": "AI Agent", "type": "ai_languageModel", "index": 0}]]},
 "Postgres Chat Memory": {"ai_memory": [[{"node": "AI Agent", "type": "ai_memory", "index": 0}]]},
 "search_knowledge": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]},
 "read_page": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]},
 "browse_folder": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]},
 "Escalate to ACR Team": {"ai_tool": [[{"node": "AI Agent", "type": "ai_tool", "index": 0}]]},
 "AI Agent": {"main": [[{"node": "Check Links & Emails", "type": "main", "index": 0}], [{"node": "Reply: Error", "type": "main", "index": 0}]]},
 "Check Links & Emails": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}]]},
 "Reply: Invalid Input": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}]]},
 "Reply: Rate Limited": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}]]},
 "Reply: Blocked": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}]]},
 "Reply: Error": {"main": [[{"node": "Respond to Widget", "type": "main", "index": 0}]]},
 "Respond to Widget": {"main": [[{"node": "Log Chat", "type": "main", "index": 0}]]},
}
wf = {"name": "ACR Chat v2 — Gemini + OKF knowledge", "nodes": nodes, "connections": conn,
      "settings": {"executionOrder": "v1", "timezone": "Asia/Kolkata", "saveDataErrorExecution": "all",
                   "saveDataSuccessExecution": "all", "executionTimeout": 55}}
(ROOT / "workflows/C-v2-chat-okf.json").write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
print(len(nodes), "nodes;", len(system), "chars system message")
