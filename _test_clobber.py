"""Headless repro of the conversation-clobber race + fix verification.

Simulates the exact _auto_save ownership guard without spinning up Qt:
mirror the relevant attributes and methods' guard logic.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

# --- Minimal stand-in mirroring the real guard in chat_widget._auto_save ---
class FakeWidget:
    def __init__(self):
        self._current_conv_id = ""
        self._meta_owner_cid = ""
        self._message_meta = []
        self.saved = {}  # conv_id -> messages snapshot (what hits the DB)

    def _is_remote_id(self, cid):
        return cid.startswith("remote:")

    # Exact ported guard logic from the patched _auto_save
    def _auto_save(self):
        if not self._current_conv_id or self._is_remote_id(self._current_conv_id):
            return
        if self._meta_owner_cid != self._current_conv_id:
            return  # the fix: refuse to save during the async-load gap
        self.saved[self._current_conv_id] = list(self._message_meta)

    # Simulate switching: save old, flip id, meta NOT yet swapped (async gap)
    def switch_to(self, conv_id):
        self._auto_save()                 # save the conv we're leaving
        self._current_conv_id = conv_id   # flips immediately (real code)
        # NOTE: _message_meta still holds the OLD conv here (load is async)

    # Simulate the background load landing
    def load_landed(self, conv_id, messages):
        self._message_meta = list(messages)
        self._meta_owner_cid = conv_id


def main():
    w = FakeWidget()
    results = []

    # Start in Harness Work with content
    w._current_conv_id = "harness"
    w._meta_owner_cid = "harness"
    w._message_meta = [{"role": "user", "content": "harness msg 1"},
                       {"role": "assistant", "content": "harness reply"}]
    # Seed SD on disk with its real content
    w.saved["sd"] = [{"role": "user", "content": "stable diffusion msg"}]
    w._auto_save()  # persist harness
    results.append(("harness persisted", w.saved.get("harness") is not None))

    # USER CLICKS Stable Diffusion -> switch (async load NOT landed yet)
    w.switch_to("sd")

    # The 10s autosave timer / any of ~20 callers FIRES during the gap
    w._auto_save()
    # BUG would be: sd row now overwritten with harness meta
    sd_now = w.saved.get("sd")
    clobbered = sd_now == w._message_meta and sd_now != [{"role": "user", "content": "stable diffusion msg"}]
    results.append(("SD NOT clobbered during load gap", not clobbered))
    results.append(("SD content intact", w.saved["sd"] == [{"role": "user", "content": "stable diffusion msg"}]))

    # Background load lands
    w.load_landed("sd", [{"role": "user", "content": "stable diffusion msg"}])
    # Now autosave is allowed again and writes SD's OWN content
    w._auto_save()
    results.append(("autosave re-enabled after load", w._meta_owner_cid == w._current_conv_id))
    results.append(("SD still its own content", w.saved["sd"] == [{"role": "user", "content": "stable diffusion msg"}]))

    # Harness must be untouched and correct
    results.append(("Harness content intact",
                    w.saved["harness"] == [{"role": "user", "content": "harness msg 1"},
                                           {"role": "assistant", "content": "harness reply"}]))

    ok = all(p for _, p in results)
    for name, p in results:
        print(f"  [{'PASS' if p else 'FAIL'}] {name}")
    print(f"\n{'ALL PASS' if ok else 'FAILURES'} ({sum(p for _,p in results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
