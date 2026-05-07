"""POST /api/search/chat — Multi-turn questioning for search refinement."""

import threading
from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, get_storage
from search.questioner import conversation_to_context, next_question

# In-memory conversation store (per process). Conversations are short-lived
# (during search creation). Under the EC2 ThreadingHTTPServer, multiple
# requests can land at once — `_lock` keeps reads/writes to the dict and
# the per-session list atomic. Trade-off: a different session_id used
# concurrently won't collide; a same session_id used concurrently will
# serialize on this lock (which is fine — same conversation).
CONVERSATIONS: dict[str, list[dict]] = {}
_lock = threading.Lock()


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

        with _lock:
            convo = CONVERSATIONS.setdefault(session_id, [])
            if user_answer is not None:
                convo.append({"role": "user", "text": user_answer})

        try:
            result = next_question(query, convo, global_rules, existing)

            if result.get("done"):
                context = conversation_to_context(convo, result.get("summary", ""))
                with _lock:
                    CONVERSATIONS.pop(session_id, None)
                json_response(self, 200, {
                    "done": True,
                    "summary": result.get("summary", ""),
                    "context": context,
                })
            else:
                with _lock:
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
