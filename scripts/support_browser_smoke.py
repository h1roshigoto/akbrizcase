"""Fixed synthetic DB operations while the real HTTPS support form stays open."""

import json
import subprocess
import threading
from uuid import UUID

from https_smoke import main
from request_browser_smoke import assert_smtp_unavailable


def run_browser(environment, config, compose, stack):
    stack.verify_database()
    assert_smtp_unavailable(compose)
    process = subprocess.Popen(
        ["node", "frontend/tests/live-support.mjs"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timer = threading.Timer(240, process.kill)
    timer.start()
    assert process.stdin and process.stdout and process.stderr
    try:
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.flush()
        proof = None
        for line in process.stdout:
            event = json.loads(line)
            action = event["action"]
            if action == "progress":
                print("Support browser: " + str(event["stage"]), flush=True)
                continue
            if action == "done":
                proof = event["proof"]
                break
            stack.verify_database()
            identifier = str(UUID(event["session_id"]))
            if action == "delete_chat":
                number = int(event["number"])
                assert (
                    stack.sql(
                        f"SELECT count(*) FROM app.orders WHERE number={number} AND chat_session_id='{identifier}'"
                    )
                    == "1"
                )
                stack.sql(f"DELETE FROM app.chat_sessions WHERE id='{identifier}'")
                assert (
                    stack.sql(
                        f"SELECT count(*) FROM app.orders WHERE number={number} AND chat_session_id IS NULL AND comment='  SYNTHETIC-036 confirmed <script>literal</script>  '"
                    )
                    == "1"
                )
            elif action == "expire":
                assert (
                    stack.sql(
                        f"SELECT count(*) FROM app.chat_sessions WHERE id='{identifier}'"
                    )
                    == "1"
                )
                stack.sql(
                    f"WITH moment AS (SELECT clock_timestamp() AS at) UPDATE app.chat_sessions SET created_at=at-interval '25 hours', last_activity_at=at-interval '25 hours', expires_at=at-interval '1 hour' FROM moment WHERE id='{identifier}'"
                )
            else:
                raise AssertionError("Unsupported support acceptance operation")
            process.stdin.write("true\n")
            process.stdin.flush()
        process.stdin.close()
        code = process.wait(timeout=20)
        diagnostic = process.stderr.read()
        if code or proof is None:
            raise RuntimeError("Support browser failed: " + diagnostic)
        assert stack.sql("SELECT count(*) FROM app.orders") == "3"
        return subprocess.CompletedProcess(
            process.args, code, json.dumps(proof), diagnostic
        )
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


if __name__ == "__main__":
    main(support_handoff=True)
