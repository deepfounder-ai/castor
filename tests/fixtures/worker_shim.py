# Auto-loaded by Python via sitecustomize when this file's parent dir is on
# PYTHONPATH. Replaces agent.run with a deterministic fake so worker-lifecycle
# tests don't need a live LLM provider.
#
# The scripted reply is read from CASTOR_TEST_FAKE_REPLY env var (default "done").

import os
import sys
import traceback


def _install_shim():
    try:
        import agent

        scripted_reply = os.environ.get("CASTOR_TEST_FAKE_REPLY", "done")

        class _FakeTurnResult:
            def __init__(self):
                self.reply = scripted_reply
                self.thinking = ""
                self.prompt_tokens = 0
                self.completion_tokens = 0
                self.total_tokens = 0
                self.tok_per_sec = 0.0
                self.tool_calls_made = []
                self.model = "fake"
                self.auto_context_hits = 0
                self.json_repairs = 0
                self.retry_successes = 0
                self.self_check_fixes = 0
                self.self_check_rejections = 0

        def _fake_run(user_input=None, thread_id=None, source="cli",
                      image_b64=None, abort_event=None, ctx=None,
                      save_user_msg=True, system_note=None):
            sys.stderr.write("[shim] fake_run fired\n")
            # Fire the checkpoint callback over a few rounds so checkpoints land.
            if ctx is not None and ctx.on_round_complete is not None:
                for r in range(1, 7):
                    ctx.on_round_complete(r, [{"role": "user", "content": str(r)}])
            return _FakeTurnResult()

        agent.run = _fake_run
        sys.stderr.write("[shim] agent.run replaced\n")
    except Exception as e:
        sys.stderr.write("[shim] install failed: " + repr(e) + "\n")
        traceback.print_exc()


_install_shim()
