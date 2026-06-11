# Import tool modules so they self-register with the registry on startup.
import tools.web_search  # noqa: F401
import tools.terminal     # noqa: F401
import tools.file_read    # noqa: F401
import tools.file_write   # noqa: F401
import tools.apply_patch  # noqa: F401

# Legacy single-file edit tools. apply_patch is the preferred entry point for
# all mutations — it handles single replacements, multi-file patches, adds,
# deletes, and renames in one schema with richer context anchors, which keeps
# model tool-selection clean. Flip the flag to re-expose them if needed.
try:
    import json as _json
    from pathlib import Path as _Path
    _cfg_path = _Path(__file__).parent.parent / "config.json"
    _enable_legacy_edit = False
    if _cfg_path.exists():
        try:
            _enable_legacy_edit = bool(
                _json.loads(_cfg_path.read_text(encoding="utf-8"))
                     .get("enable_legacy_edit_tools", False)
            )
        except Exception:
            _enable_legacy_edit = False
    if _enable_legacy_edit:
        import tools.file_edit   # noqa: F401
        import tools.multi_edit  # noqa: F401
except Exception as _e:
    print(f"[tools] legacy edit-tools gate failed ({_e}); apply_patch only.")
import tools.grep          # noqa: F401
import tools.glob_tool     # noqa: F401
import tools.file_search   # noqa: F401
# import tools.web_fetch   # merged into http_client (extract_text=true)
import tools.vision        # noqa: F401
import tools.workspace     # noqa: F401
import tools.browser       # noqa: F401  # kept for fallback functions, registration disabled
import tools.tasks         # noqa: F401
import tools.git_tool      # noqa: F401
import tools.tts           # noqa: F401
import tools.screenshot    # noqa: F401
import tools.session_search  # noqa: F401
import tools.memory          # noqa: F401
import tools.checkpoint_tool # noqa: F401
import tools.plan            # noqa: F401
import tools.http_client     # noqa: F401
import tools.db_query        # noqa: F401
import tools.clipboard       # noqa: F401
import tools.archive         # noqa: F401
import tools.diff_tool       # noqa: F401
import tools.multi_file      # noqa: F401
import tools.file_watcher    # noqa: F401
import tools.project_loader  # noqa: F401
import tools.notify          # noqa: F401
import tools.ocr             # noqa: F401
import tools.data_extract    # noqa: F401
import tools.ssh_tool        # noqa: F401
import tools.vector_search   # noqa: F401
import tools.transcribe      # noqa: F401
import tools.browser_auto    # noqa: F401
import tools.pdf_gen         # noqa: F401
import tools.play_sound      # noqa: F401
import tools.thinking        # noqa: F401
import tools.file_viewer     # noqa: F401
import tools.workspace_terminal  # noqa: F401
import tools.workspace_browser   # noqa: F401
import tools.lsp                 # noqa: F401
import tools.subagent_tool       # noqa: F401
import tools.explore_files       # noqa: F401
import tools.audit               # noqa: F401
import tools.worktree            # noqa: F401
import tools.ask_user            # noqa: F401
import tools.reflect             # noqa: F401
import tools.notes               # noqa: F401
import tools.network_tool        # noqa: F401
try:
    import tools.mcp_tool          # noqa: F401
except Exception as _e:
    print(f"[tools] MCP tools unavailable: {_e}")
