"""POST /api/search/chat — Multi-turn questioning for search refinement."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, get_storage
from search.questioner import conversation_to_context, next_question

# In-memory conversation store (per function instance).
# Conversations are short-lived (during search creation), so this is fine.
CONVERSATIONS: dict[str, list[dict]] = {}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        body = read_json_body(self)
        query = body.get("query", "")
        session_id = body.get("session_id", "")
        user_answer = body.get("answer")
        search_id = body.get("search_id")

        storage = get_storage(account["account_id"])

        existing = None
        if search_id:
            existing = storage.load_search(search_id)

        global_rules = storage.load_rules()

        if session_id not in CONVERSATIONS:
            CONVERSATIONS[session_id] = []
        convo = CONVERSATIONS[session_id]

        if user_answer is not None:
            convo.append({"role": "user", "text": user_answer})

        try:
            result = next_question(query, convo, global_rules, existing)

            if result.get("done"):
                context = conversation_to_context(convo, result.get("summary", ""))
                # Clean up conversation
                CONVERSATIONS.pop(session_id, None)
                json_response(self, 200, {
                    "done": True,
                    "summary": result.get("summary", ""),
                    "context": context,
                })
            else:
                convo.append({"role": "assistant", "text": result["question"]})
                json_response(self, 200, {
                    "done": False,
                    "question": result["question"],
                })
        except Exception:
            json_response(self, 200, {
                "done": True,
                "summary": query,
                "context": query,
            })

    def log_message(self, format, *args):
        pass
