Repo: herdrdev/herdr
Title: Client drops OSC 52 clipboard writes from a saved machine that is not the active endpoint

Symptom

With a saved SSH machine attached (`herdr machine add`), an OSC 52 write from a pane on that machine never reaches the client's clipboard when the machine is not the active endpoint at the moment the write arrives. Nothing is logged and the pane app sees no error.

Environment

- herdr 0.9.5 on both sides (client on macOS, server inside a NixOS VM under Lima, attached as a saved machine; the same `ServerMessage::Clipboard` path applies to any saved machine).
- Pane app: pi, which emits OSC 52 after its own copy (pi >= 1.2.0 with PI_CLIPBOARD_OSC52=1). A raw `printf '\033]52;c;%s\a' "$(printf hello | base64)"` shows the same thing.

Repro

1. On the Mac: `herdr machine add <alias> --label vm`. Open one workspace on Local and one on the machine `vm`, in the same client window.
2. Focus a pane on `vm`. Start in it: `sleep 5; printf '\033]52;c;%s\a' "$(printf from-vm | base64)"`.
3. Within the 5 seconds, focus a pane on Local, so the active endpoint is Local.
4. Wait for the write, then run `pbpaste` on the Mac.

Expected: `from-vm`, the same as a write from a background pane of the Local endpoint (or from `vm` while it is active).
Actual: the clipboard is unchanged.

Root cause (v0.9.5)

- The server forwards the write correctly: src/server/headless/notifications.rs:334, `AppEvent::ClipboardWrite` -> `send_to_foreground_client(ServerMessage::Clipboard { data })`.
- In the client, every message from an endpoint goes through `endpoint::accepts_endpoint_message(...)` (src/client/mod.rs:1480), and `continue`s when it returns false.
- src/client/endpoint/message_policy.rs:19: for an inactive endpoint only `EndpointControl`, `SemanticNotification`, `ServerShutdown`, `ClientShellSnapshot` and `Welcome` pass (plus two activation-scoped cases). `ServerMessage::Clipboard` is not on the list.
- The unit test `inactive_endpoints_deliver_metadata_but_not_presentation_effects` (same file, ~line 62) asserts that `Clipboard` is rejected, so this is deliberate. The doc comment above `is_presentation_effect` describes graphics and modes tied to the host presentation lease; a clipboard write is not that kind of effect.
- Introduced by 9e9bc8a14 "feat: manage multiple ssh machines from one client (#3670)", 2026-09-05, the only commit touching the file.

Proposed fix

Accept `ServerMessage::Clipboard` from inactive endpoints in `accepts_endpoint_message`, and flip the assertion in the test. A clipboard write is an explicit action of the pane's app, not presentation state, and `AppEvent::ClipboardWrite` carries no pane identity on the server, so as far as I can tell a background pane of the Local endpoint is not filtered either; remote panes would become consistent with that. If you want to limit what a background machine can do, gate it behind a config option rather than dropping it silently.

Related

- #4833 (open): herdr's own selection copy in machine panes writes the server-side clipboard instead of the client's. Different bug, different path (`AppEvent::ClipboardWrite` is raised by herdr's own copy, not by a pane's OSC 52), and a fix for it would not change this.
- #5185 (closed): confirms child OSC 52 writes are forwarded to the client from the active endpoint.
- #4612 (open): OSC 52 never reaches a `herdr terminal attach` client.

Search result: `gh issue list -R herdrdev/herdr --state all --search clipboard` and code-term searches found no existing report of the inactive-endpoint drop.

<!-- drafting note, delete before posting: the cause is from reading v0.9.5 source, not from a run. I did not reproduce it. The repro above relies on a delayed write because in normal use you copy from the pane you are looking at. If your real trigger was different (a hook, an agent finishing its copy after you switched, a pane in another tab), replace steps 2-3 with it. Confirm the repro on your Mac before posting. -->
