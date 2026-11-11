"""Bounded offline bridge to the existing MVP-027 parser; no DB or publication."""

import base64
import json
import sys
from pathlib import Path


def main():
    try:
        data = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError
        request = json.loads(data)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        try:
            from app.modules.kb.tree_schema import parse_tree
        except ImportError:
            print("NOT RUN")
            return 2
        tree = parse_tree(base64.b64decode(request["yaml"], validate=True))
        items = request["items"]
        observed = {
            "key": tree.key,
            "version": tree.version,
            "model": tree.model_code,
            "start": tree.start,
            "nodes": [
                {
                    "id": key,
                    "type": node.type,
                    "next": [option.next for option in node.options],
                    "source_refs": node.source_refs,
                    "recommendations": [
                        {"item": items[item.item_id], "qty": item.qty}
                        for item in node.recommendations
                    ],
                }
                for key, node in tree.nodes.items()
            ],
        }
        if observed != request["expected"]:
            raise ValueError
        print("PASS")
        return 0
    except Exception:
        # No original text, parser/DTO exception, private filename or traceback.
        print("FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
