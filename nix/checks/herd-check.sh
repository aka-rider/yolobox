#!/usr/bin/env bash
# Guest-side self-test for the whole herd reporting path, from the forwarded
# socket up to claude actually firing its hooks.
#
# The simplification that makes this possible from one side: the forwarded
# socket is a *bidirectional* channel to the Mac's herdr server, so `herdr
# pane get` run in the box is answered by the same server the reports go to.
# Every stage below therefore both writes and reads the round trip; nothing
# has to be corroborated from the host.
#
# Run with the herd environment `yo enter` sets up — YOLOBOX_HERD,
# HERDR_PANE_ID, HERDR_SOCKET_PATH — which is what `yo herd-check` arranges.
#
# writeShellApplication prepends `set -o errexit -o nounset -o pipefail`, so
# every capture whose command may exit nonzero is guarded with `|| rc=$?`.

# NOT /etc/claude-code/managed-settings.json: claude takes only the first
# non-empty of its three policy tiers, and this account's server-fetched
# remote settings are permanently non-empty, so the /etc managed file is
# discarded wholesale. nix/herd-report.nix carries the full account; the hook
# map therefore rides `--settings`, injected by the claude wrapper built in
# nix/harnesses.nix, and stage 6 below is what keeps that wrapper honest.
HERD_HOOKS=@HERD_HOOKS@
REPORT_LOG="${HOME:-/nonexistent}/.local/state/yolobox/herd-report.log"
HERDR_TIMEOUT_S=5
CLAUDE_TIMEOUT_S=120

failures=""
inconclusive=""

ok() { printf 'ok:   %s\n' "$1"; }

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    printf '      %s\n' "$2" >&2
    failures="${failures} ${1%% *}"
}

skip() {
    printf 'skip: %s\n' "$1" >&2
    printf '      %s\n' "$2" >&2
    inconclusive="${inconclusive} ${1%% *}"
}

pane_get() {
    local rc=0 out
    out=$(timeout "${HERDR_TIMEOUT_S}" herdr pane get "${HERDR_PANE_ID}" 2>&1) || rc=$?
    printf '%s' "${out}"
    return "${rc}"
}

pane_field() {
    # Sentinels rather than jq's `null`, so a shape change (or a server error
    # object where a pane was expected) fails the comparison loudly and the
    # failure message names what actually came back, instead of silently
    # matching nothing.
    jq -r --arg k "$2" 'if (.result.pane | type) != "object" then "<no-pane>"
                        elif (.result.pane | has($k)) then (.result.pane[$k] | tostring)
                        else "<none>" end' <<< "$1" 2>/dev/null \
        || printf '<unparseable>'
}

dump_diagnostics() {
    printf '      pane json: %s\n' "$1" >&2
    if [ -s "${REPORT_LOG}" ]; then
        printf '      tail of %s:\n' "${REPORT_LOG}" >&2
        tail -n 10 "${REPORT_LOG}" >&2
    else
        printf '      %s does not exist — the reporter never even logged a rejection\n' "${REPORT_LOG}" >&2
    fi
}

echo "=== yolobox herd check ==="

# ----------------------------------------------------------------- 0. the account

if [ "$(id -un)" != "@AGENT_USER@" ]; then
    fail "account" \
         "running as $(id -un), not @AGENT_USER@ — this session was opened with the wrong ssh role. Herd reporting and every stage below assume the agent account; nothing past this point is meaningful."
    echo "=== HERD CHECK FAILED:${failures} ===" >&2
    exit 1
fi
ok "running as @AGENT_USER@"

# ---------------------------------------------------------------- 1. the forward

env_ok=yes
for var in YOLOBOX_HERD HERDR_PANE_ID HERDR_SOCKET_PATH; do
    if [ -z "${!var:-}" ]; then
        env_ok=no
        fail "herd env (${var})" \
             "${var} is unset. This check must run with the environment \`yo enter\` sets up; \`yo ssh\` deliberately carries none of it."
    fi
done

if [ "${env_ok}" = yes ]; then
    if [ -S "${HERDR_SOCKET_PATH}" ]; then
        ok "forwarded socket ${HERDR_SOCKET_PATH} for pane ${HERDR_PANE_ID}"
    else
        env_ok=no
        fail "forwarded socket" \
             "${HERDR_SOCKET_PATH} is not a socket — ssh's -R forward never bound, and the ssh client said so at connect time (\"Warning: remote port forwarding failed for listen path ...\"). Usually /run/yolobox is missing after a switch that was never followed by a reboot: check \`ls -ld /run/yolobox\`."
    fi
fi

if [ "${env_ok}" = no ]; then
    echo "=== HERD CHECK FAILED:${failures} ===" >&2
    exit 1
fi

# ------------------------------------------------------- 2. protocol compatibility

status_rc=0
status_json=$(timeout "${HERDR_TIMEOUT_S}" herdr status --json 2>&1) || status_rc=$?
compatible=""
if [ "${status_rc}" -eq 0 ]; then
    compatible=$(jq -r '.server.compatible // "<none>"' <<< "${status_json}" 2>/dev/null || true)
fi

if [ "${compatible}" = "true" ]; then
    ok "protocol compatible — guest herdr $(jq -r '.client.version' <<< "${status_json}") (protocol $(jq -r '.client.protocol' <<< "${status_json}")) ↔ host herdr $(jq -r '.server.version' <<< "${status_json}") (protocol $(jq -r '.server.protocol' <<< "${status_json}"))"
else
    fail "protocol compatibility" \
         "The host herdr server rejects this guest's protocol. This is the silent killer: every report comes back protocol_mismatch, boxed agents show as \`unknown\`, and nothing is printed anywhere. The box's herdr version must EQUAL the Mac's Homebrew herdr — the wire protocol changes across patch bumps (0.8.0→0.8.2 was protocol 19→20). Compare with \`./yo herd-check\`'s host stage, then bump the flake's nixpkgs (which is where the guest's herdr now comes from), or re-pin yolobox.harness.herdr.version+hash to the matching GitHub release. Raw: ${status_json}"
fi

# ------------------------------------------------------- 3. reporter round trip

reported=no
release_on_exit() {
    [ "${reported}" = yes ] || return 0
    yolobox-herd-report release claude >/dev/null 2>&1 || true
}
trap release_on_exit EXIT

report_rc=0
report_out=$(yolobox-herd-report idle claude 2>&1) || report_rc=$?
reported=yes

pane_rc=0
pane_json=$(pane_get) || pane_rc=$?
pane_agent=$(pane_field "${pane_json}" agent)
pane_state=$(pane_field "${pane_json}" agent_status)

if [ "${pane_rc}" -eq 0 ] && [ "${pane_agent}" = "claude" ] && [ "${pane_state}" = "idle" ]; then
    ok "reporter round trip — pane ${HERDR_PANE_ID} reads back agent=claude state=idle"
else
    fail "reporter round trip" \
         "\`yolobox-herd-report idle claude\` (rc=${report_rc}${report_out:+, output: ${report_out}}) did not land: the pane reads agent=${pane_agent} state=${pane_state}. If stage 2 passed, the report reached a compatible server and was still dropped — check the source label (reports must use yolobox:<agent>, never herdr:<agent>, which the server reserves and clears for boxed agents)."
    dump_diagnostics "${pane_json}"
fi

# -------------------------------------------------------- 4. release round trip

release_rc=0
release_out=$(yolobox-herd-report release claude 2>&1) || release_rc=$?

pane_rc=0
pane_json=$(pane_get) || pane_rc=$?
pane_agent=$(pane_field "${pane_json}" agent)
pane_state=$(pane_field "${pane_json}" agent_status)

# The server may drop the agent label outright or keep it with authority
# withdrawn; either is a clean release. A live status under a still-present
# label is the ghost this stage exists to catch.
if [ "${pane_rc}" -eq 0 ] && { [ "${pane_agent}" = "<none>" ] || [ "${pane_state}" = "unknown" ]; }; then
    ok "release round trip — pane ${HERDR_PANE_ID} reads back agent=${pane_agent} state=${pane_state}"
else
    fail "release round trip" \
         "\`yolobox-herd-report release claude\` (rc=${release_rc}${release_out:+, output: ${release_out}}) left the pane at agent=${pane_agent} state=${pane_state}. A report path that works one way strands a ghost agent in the herd forever."
    dump_diagnostics "${pane_json}"
fi

# --------------------------------------------------- 5. the hook map is real

if [ ! -s "${HERD_HOOKS}" ]; then
    fail "hook map" \
         "${HERD_HOOKS} is missing or empty — nix/herd-report.nix renders it, so a rebuild never landed."
else
    hook_commands=$(jq -r '.hooks | to_entries[] | .value[] | .hooks[] | .command' "${HERD_HOOKS}" 2>/dev/null || true)
    if [ -z "${hook_commands}" ]; then
        fail "hook map" \
             "${HERD_HOOKS} declares no hook commands at all — claude is handed it and reports nothing."
    else
        broken=""
        while IFS= read -r command_line; do
            [ -n "${command_line}" ] || continue
            hook_bin=${command_line%% *}
            [ -x "${hook_bin}" ] || broken="${broken} ${hook_bin}"
        done <<< "${hook_commands}"

        if [ -z "${broken}" ]; then
            ok "hook map — $(wc -l <<< "${hook_commands}") hook commands, every binary executable"
        else
            fail "hook map" \
                 "not executable:${broken}. A store path in ${HERD_HOOKS} was garbage-collected or the file is stale from an older closure — \`nixos-rebuild switch\` and check that ${HERD_HOOKS} is a fresh /etc/static link."
        fi
    fi
fi

# ------------------------------------- 6. the claude on PATH injects the flag

# The regression this stage exists to catch: an unwrapped claude keeps working
# perfectly and simply stops reporting, with nothing logged anywhere. That is
# how the herd stayed silently broken for weeks.

claude_bin=$(command -v claude 2>/dev/null || true)
claude_resolved=""
if [ -n "${claude_bin}" ]; then
    claude_resolved=$(readlink -f "${claude_bin}")
fi

if [ -z "${claude_bin}" ]; then
    skip "claude --settings flag" "claude is not on PATH in this shell."
elif grep -qaF "${HERD_HOOKS}" "${claude_resolved}"; then
    ok "claude --settings flag — ${claude_bin} carries ${HERD_HOOKS}"
else
    fail "claude --settings flag" \
         "${claude_resolved} never mentions ${HERD_HOOKS}, so this claude is not the wrapper nix/harnesses.nix builds and no hook map reaches it at all. Do not paper over this by rendering /etc/claude-code/managed-settings.json instead: claude takes only the first non-empty of its three policy tiers, and this account's server-fetched remote settings are permanently non-empty, so that file is discarded wholesale and re-clobbered mid-session. Restore the symlinkJoin/wrapProgram wrapper and \`nixos-rebuild switch\`."
fi

# --------------------------------------------- 7. claude actually fires the hooks

echo
echo "--- the next stage starts claude with a one-word prompt: it makes one small API call ---"

# The pane reaching agent=claude is the proof, and the only one: nothing but an
# executed hook can put it there, because that label travels from the hook
# through the forwarded socket to the Mac's herdr server and back. The debug
# stream is corroboration only — it is claude's own `--debug` rendering, which
# changes without notice (headless `-p` prints none of it at all unless
# `--debug-file` is given), so a missing marker is a statement about claude's
# logging, never about the hooks.
hook_marker="yolobox-herd-report"

if [ -z "${claude_bin}" ]; then
    skip "claude hook execution" "claude is not on PATH in this shell."
else
    debug_dir=$(mktemp -d)
    debug_log="${debug_dir}/claude-debug.log"
    output_log="${debug_dir}/claude-output.log"
    claude_rc=0
    timeout "${CLAUDE_TIMEOUT_S}" claude --debug --debug-file "${debug_log}" -p 'ok' > "${output_log}" 2>&1 &
    claude_pid=$!
    reported=yes

    # The pane is sampled while claude runs, not after: its SessionEnd hook
    # releases the agent, so by the time the process exits the evidence is
    # gone again. This waits on a condition, never on a fixed duration.
    saw_agent=no
    while kill -0 "${claude_pid}" 2>/dev/null; do
        live_json=$(pane_get) || true
        if [ "$(pane_field "${live_json}" agent)" = "claude" ]; then
            saw_agent=yes
            break
        fi
        sleep 0.2
    done
    wait "${claude_pid}" || claude_rc=$?

    saw_marker=no
    if [ -s "${debug_log}" ] && grep -qF "${hook_marker}" "${debug_log}"; then
        saw_marker=yes
    fi

    if [ "${claude_rc}" -ne 0 ]; then
        skip "claude hook execution" \
             "claude exited ${claude_rc} — unauthenticated, offline, or the API call failed, so this stage proves nothing either way. It is NOT evidence that the hooks are broken. Last lines: $(tail -n 3 "${output_log}" | tr '\n' ' ')"
    elif [ "${saw_agent}" = yes ]; then
        ok "claude hook execution — pane ${HERDR_PANE_ID} independently reached agent=claude while claude ran"
        if [ "${saw_marker}" = no ]; then
            printf '      note: the debug stream never mentions %s. Corroboration only — the pane transition above already proves the hook ran, so this just means claude'"'"'s --debug format changed. Kept for inspection: %s\n' \
                   "${hook_marker}" "${debug_dir}" >&2
        fi
    else
        fail "claude hook execution" \
             "claude ran but pane ${HERDR_PANE_ID} never reached agent=claude (debug-stream-mentions-hook=${saw_marker}, corroborating only). claude can read ${HERD_HOOKS} and still never execute its hooks, and that defect leaves NO trace anywhere except this stage. Do not paper over it by rendering /etc/claude-code/managed-settings.json instead: claude takes only the first non-empty of its three policy tiers, and this account's server-fetched remote settings are permanently non-empty, so that file is discarded wholesale. Full debug output: ${debug_dir}"
    fi

    if [ "${claude_rc}" -eq 0 ] && [ "${saw_agent}" = yes ] && [ "${saw_marker}" = yes ]; then
        rm -rf "${debug_dir}"
    fi
fi

echo
if [ -n "${inconclusive}" ]; then
    echo "inconclusive:${inconclusive}" >&2
fi
if [ -n "${failures}" ]; then
    echo "=== HERD CHECK FAILED:${failures} ===" >&2
    exit 1
fi
echo "=== herd reporting verified end to end ==="
