"""GET/POST /api/search/global_rules — List or add global rules."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, get_storage
from search.models import GlobalRule


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if not account:
            return
        storage = get_storage(account["account_id"])
        rules = storage.load_rules()
        json_response(self, 200, {
            "rules": [r.model_dump(mode="json") for r in rules]
        })

    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        body = read_json_body(self)
        rule = GlobalRule(
            text=body.get("text", ""),
            scope=body.get("scope", "all searches"),
            source="manual",
        )

        storage = get_storage(account["account_id"])
        rules = storage.load_rules()
        rules.append(rule)
        storage.save_rules(rules)

        json_response(self, 200, {"status": "ok", "rule_id": rule.id})

    def log_message(self, format, *args):
        pass
