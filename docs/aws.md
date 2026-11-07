# AWS credentials

Every allow-listed SSO profile on the Mac is available to every process in the VM, and each process picks its own with `AWS_PROFILE=<name>`. A broker on the Mac mints the credentials and the guest reaches it over a reverse unix socket. This page explains why the box works that way and how to recognise each piece failing. The rules are in `CONSTITUTION.md`.

## Design

Two simpler designs fail. Minting STS credentials once, at `yo enter`, and freezing them in the guest session's environment (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`) means a session that outlives the STS duration needs a fresh `yo enter`. A broker bound to one profile per pane, through botocore's container credential provider (a bearer token plus a URL, reached over a per-session TCP `-R` forward to the guest's gateway address), pins the pane to one account, and the gateway-address path is fragile under a VPN (see Transport below). So the broker serves every allow-listed profile and travels the way 1Password does.

Why not one profile that the operator can switch? botocore fixes a process's credential source at first use and refreshes it only 10 to 15 minutes before the cached credentials expire. It never re-reads which profile it should use. So a global profile flipped mid-session fails in four ways:

- A long-running SDK process silently lands in the other account at its next refresh, tens of minutes after the switch.
- Two panes started at different times disagree about the current profile for as long as that window lasts.
- The broker has to keep serving the old profile's cache until only minutes remain on it, instead of cutting over when the operator asks.
- The region drifts out from under the account, and a region-less profile targets the global STS endpoint, which a host DNS filter may sinkhole (see [the DNS sinkhole](host-network.md#the-dns-sinkhole)).

Fixing the profile at process start avoids all four, because nothing is switched under a running process.

## Allowlist

`aws_allowlist()` in `yolobox_cli/aws.py` reads the Mac's `~/.aws/config` with `configparser`. It allow-lists every `[profile X]` and `[default]` section that has `sso_session` or `sso_start_url`. `[sso-session ...]` sections describe a session, not an account, so they are excluded.

A missing `~/.aws/config` means no profiles. One that exists but cannot be opened (mode `000`, say) is a `YoError` naming the path, never an empty allowlist: `ConfigParser.read()` skips a file it cannot open, and an empty allowlist makes `ensure_aws_broker()` kill the running broker and delete the guest's aws-config.

### The profile name pattern

A profile name must `fullmatch` `PROFILE_NAME_RE` (`^[A-Za-z0-9][A-Za-z0-9._-]*$`). A name with a space, a shell metacharacter, or a leading `-`, `.` or `_` is excluded with a stderr line naming it. `yo` would otherwise pass it down to `credential_process P` unescaped, and a leading dash would read as an option. The pattern has two spellings that are the same text on purpose:

- `PROFILE_NAME_PATTERN` and `PROFILE_NAME_RE` in `yolobox_cli/common.py`, one definition that `yo` and `aws-broker` both import. `aws-broker` re-checks every `--allow` argument in `parse_args`, so a broker started by anything but `yo` still refuses such a name.
- The regex in `yolobox-guest aws-creds`, which exits 2 with `invalid profile name` before building the URL path.

`tests/test_consistency.py` checks the guest's spelling against the Python one. `fullmatch` is load-bearing in Python, because `$` before a trailing newline still matches under `match`.

A profile's region must match `^[a-z]{2,4}(-[a-z]+)+-\d+$`, ASCII only. The four-letter prefix is the AWS European Sovereign Cloud's `eusc-de-east-1`. The region lands in the rendered guest config, so a value with a newline in it would add lines to that file. A profile with no region is excluded too, with a stderr line naming it, because a region-less profile targets the global STS endpoint, which a host DNS filter may sinkhole (see [the DNS sinkhole](host-network.md#the-dns-sinkhole)). `[default]` and `[profile default]` both name the profile `default`. `aws_allowlist()` keeps the last one in file order and prints a warning naming both sections. The last one wins because botocore's `build_profile_map` does the same, and the broker's `aws configure export-credentials` follows botocore, so yo and the broker agree on which section's `region` and SSO fields the profile has. Keeping `default` once also matters on its own: a guest config with two `[profile default]` sections makes botocore raise `DuplicateSectionError`, and an allowlist that held `default` twice never equalled the broker's own list, so every `yo` run restarted the broker. A malformed config file (bad ini syntax, undecodable bytes) raises instead of allow-listing whatever partial read it got. With no `aws` CLI, or no SSO profiles, `yo` prints one stderr line and starts no broker.

## Guest config

`yo` renders `/home/agent/.config/yolobox/aws-config`, the file that `AWS_CONFIG_FILE` in `nix/lib/agent-env.nix` points every agent session at. It has one section per allow-listed profile:

```
[profile P]
region = R
credential_process = yolobox-guest aws-creds P
```

Because `AWS_CONFIG_FILE` replaces the guest's own AWS config path, an agent-written `~/.aws/config` inside the VM is never read. `yo up` warns on stderr when it finds one, so its presence is at least visible. `AWS_PROFILE=P aws ...` is then the whole of how a process picks an account.

`push_aws_guest_files()` writes the file atomically: `mkdir -p` the directory, `cat` the text into a `mktemp` sibling, then `mv` it over the real path. A `credential_process` call can therefore never see a half-written config. When the allowlist empties out entirely, `disable_aws_broker()` stops any running broker and removes the file from the guest instead of leaving a stale one, and `yo status` reports `aws: disabled (...)`.

## Transport

Why `credential_process` and not the container credential provider? botocore dials the container provider only as an HTTP(S) URI and has no unix-socket form. Once the broker moved to a unix socket, that provider stopped being a candidate. `credential_process` runs any command and reads process-credential JSON from its stdout, so `yolobox-guest aws-creds P` can `curl --unix-socket` the Mac-side broker.

Why a unix socket and not a forwarded TCP port? A VPN on the host that blackholes lima's gateway blackholes every guest connection to `192.168.5.2:<port>`, the path a TCP `-R` forward relies on (see [A VPN that blackholes lima's gateway](host-network.md#a-vpn-that-blackholes-limas-gateway)). Lima's reverse unix-socket forwards ride lima's own ssh connection and are unaffected. 1Password's forward already had that shape (see [1Password reaches the agent](ssh.md#1password-reaches-the-agent)), so the broker uses it too.

The chain has four links:

1. `aws-broker` binds `~/.local/state/yolobox/aws-broker.sock` on the Mac, born `0600`.
2. Lima reverses it to `/run/yolobox-op/aws-broker.sock` in the guest, bound as the operator exactly like 1Password's.
3. `opProxy` in `nix/base.nix`, the same chokepoint that proxies 1Password, bridges it through `systemd-socket-proxyd` to an agent-owned `/run/yolobox/aws-broker.sock`.
4. `yolobox-guest aws-creds` calls that socket.

The broker binds a second socket, `~/.local/state/yolobox/aws-broker.ctl.sock`, also `0600`, that nothing forwards. It is yo's own line to the broker, for `/health`; see [The control socket](#the-control-socket). It serves nothing else, `/creds/` included.

No bearer token guards any of the forwarded chain. The filesystem permissions on the last socket are the whole authorization boundary, as for 1Password. [Healing reverse forwards](ssh.md#healing-reverse-forwards) covers how `yo` revives a dead forward.

There is exactly one broker socket, not one URL per profile, so the Mac and the guest do not share a port or address to keep in sync. `nix/lib/agent-env.nix` has `awsBrokerSock`, the one place that names the agent-facing path. It sits outside the exported `env` attrset because it names a proxy target, not a variable an agent process should export. `nix/guest.nix` folds it into `YOLOBOX_AWS_BROKER_SOCK` through `runtimeEnv`, so `yolobox-guest.sh` reads it from the environment.

The Mac-side half still has to be kept in sync by hand:

- `AWS_BROKER_GUEST_SOCK` in `yolobox_cli/config.py` (folded into `LIMA_PORT_FORWARDS` in `yolobox_cli/lima.py`).
- `AGENT_AWS_BROKER_SOCK` in `yolobox_cli/config.py`, the target of its health probe. `TestAgentEnvMatchesYoSockets` checks it against `agent-env.nix`.
- `lima/yolobox.yaml`, the one-time seed for a new instance. `TestLimaYamlMatchesPortForwardsConstant` checks it against `LIMA_PORT_FORWARDS`.

On an existing instance, `LimaDriver.config_gaps()` catches the lima side drifting at every `vm_up()`. For a missing socket forward it prints a stderr note and does not refuse, since the healing step adds the forward live regardless (see [Checking a running instance](../lima/yolobox.md#checking-a-running-instance)).

## Broker lifetime

The broker outlives any one `yo enter` pane, because profiles are chosen per process and no session is left to tie its life to. It follows lima's hostagent instead. `aws-broker --watch-pid <pid>` uses `select.kqueue` with `EVFILT_PROC` and `NOTE_EXIT` (`pid_exit_watch` in `yolobox_cli/common.py`) to notice the instant the hostagent exits, without polling. Where there is no kqueue it uses a pidfd (`os.pidfd_open`, Linux 5.3 and Python 3.9 or newer); a Python with neither makes `pid_exit_watch` raise an `OSError` that names the missing mechanism, and `yo` reports it as an error instead of starting a broker that cannot end. `LimaDriver.keepalive_pid()` supplies the pid from `hostAgentPID` in `limactl list yolobox --json`. It never opens `~/.lima/yolobox/ha.pid`, whose mtime lima rewrites on every start.

`ensure_aws_broker()` asks `GET /health` first, through `broker_needs_restart()`. `aws_broker_health()` answers one of three states (`BrokerState`): unreachable, healthy with the broker's JSON, or `NO_CONTROL`. It asks the control socket first (see [The control socket](#the-control-socket)); anything but a `200` with a JSON object there counts as no answer. If the control socket says nothing but the forwarded one answers, the broker was started by an older `yo` and has no control socket: that is `NO_CONTROL`, which always needs a restart and is stopped by the pid it reports, the same as any stale broker. The operator never has to kill it by hand. `yo status` calls it out as a warning until the next `yo up` replaces it. An older broker that is itself being flooded answers `/health` with `503`, which reads as unreachable: the restart then fails once with "another aws-broker is already serving", and `kill`ing that broker or running the command again once the flood ends clears it. A healthy running broker stays only when both its reported `watch_pid` matches today's hostagent pid and its reported `allow` list equals today's sorted allowlist. A new `yo up`, or an edited `~/.aws/config` between two `yo` runs, therefore forces a fresh broker rather than serving a stale profile set. The stale one is sent SIGTERM and waited out with the same pid-exit mechanism. A broker that answers `/health` without a usable `pid` cannot be stopped, and that is an error naming the socket, never a quiet skip; `disable_aws_broker()` (no `aws` CLI, or no SSO profiles) raises in the same cases, and when it cannot remove the guest config.

Two yos can reach `ensure_aws_broker()` at once (`yo up` in one terminal, `yo enter` in another). Check, kill and start are one step under `state.host_state_lock()` on `aws-broker.yo.lock` in the state directory, and `disable_aws_broker()` takes the same lock. Without it both yos see no broker, both start one, and the second fails `yo up` with "another aws-broker is already serving" although a healthy broker exists. The lock is yo's own: the broker flocks `aws-broker.sock.lock` while it starts, and a yo holding that file would make the broker it starts fail on its own lock.

Start-up and shutdown details:

- The broker's stderr is truncated and reopened at `state_dir()/aws-broker/broker.log` on every start. The directory is `0700` and the log `0600`, because the log holds every detail the guest is not told.
- `start_aws_broker()` waits up to `BROKER_HANDSHAKE_SECONDS` for that line. A broker that exits first closes its stdout, which `read_handshake()` reports as `closed`, and the error says `exited with code N before its handshake — see broker.log` (or `was killed by SIGKILL ...` for a signal). EOF means every writer closed the pipe, not that the process exited, so yo waits for the exit only for the time the handshake deadline has left; a broker that closed its stdout and still runs is terminated and reported as having "closed its output without exiting", and yo does not hold `aws-broker.yo.lock` while it waits for ever. Only a broker that is still silent at the deadline is "no handshake within 30s". These have different causes (a crash, a closed pipe, a hang), so they must not share a message.
- Its stdout carries exactly one line, `READY`, before it `os.dup2`s `/dev/null` over itself. Never write a secret to stdout after that line, or to any log.
- `main()` holds an exclusive `flock` on `aws-broker.sock.lock`, beside the forwarded socket, from `reclaim_socket_path()` of both sockets until both binds and the SIGTERM handler are done, then releases it before `READY`. Reclaim (probe, then unlink) and bind are two steps; without the lock two brokers could pass the same probe, and the second would unlink the first one's fresh socket, leaving two brokers and one of them nameless. With it the second waits, then finds the first one live and fails with the collision. The lock file is never removed, because a removed one would let two brokers lock two different files.
- `reclaim_socket_path()` probes a connect before unlinking anything and fails, naming the collision, if something is still served there. A stale socket from a dead broker is unlinked. A non-socket file at that path is refused, never removed. Both sockets go through it, so a live broker's control socket stops a second broker as its forwarded one does. If the second bind fails, `bind_servers()` unbinds the first, so a failed start leaves no socket behind.
- SIGTERM, which is how a replaced broker is stopped, runs the same cleanup as the watched pid exiting, so a replaced broker unlinks its sockets (when they are still its own) instead of dying with the default action and leaving the file behind. SIGTERM is blocked with `pthread_sigmask` across the bind and unblocked once the handler is installed, so a signal that arrives in between reaches the handler, not the default action.
- At shutdown, `unlink_if_same_identity()` removes the socket only when its `(st_dev, st_ino)` still match the pair recorded at bind. A superseded broker exiting late therefore cannot delete a newer broker's socket. Both sides use `lstat`, because `stat` on a path since replaced by a symlink would report the target's identity and delete the wrong file.

On a Linux host there is no hostagent, so the broker follows the operator's ssh ControlMaster instead. `LibvirtDriver.keepalive_pid()` reads its pid from `ssh -O check` (`Master running (pid=N)`), and `--watch-pid` waits on it through a pidfd (`os.pidfd_open`) where macOS uses a kqueue. The master persists after its last client (`ControlPersist yes`) but dies with the connection, so the broker can end while the box keeps running. That is healed, not prevented: `vm_services()` runs before `ssh`, `enter`, `cp`, `code` and `zed` and inside `up`, `bootstrap` and `disk-grow` (see [yo.md](../yo.md#which-commands-heal)), re-creates the master, and `ensure_aws_broker()` restarts the broker when its reported `watch_pid` no longer matches, in the same pass that heals the reverse sockets (see [docs/ssh.md](ssh.md#healing-reverse-forwards)). Until the next such command, a box whose master died has no broker.

## Minting

Minting is lazy and per profile. Each profile has its own lock, cache, throttle and last error, so one profile's expired SSO session never blocks another's `/creds/<P>` request. An IAM user's long-lived keys never expire and must never enter the guest this way, so a request for such a profile is a request-time 500. `aws sso login --profile P` on the Mac heals a running guest session with no re-enter, because the next `/creds/<P>` hit simply mints again.

Every failure of a request the broker has read answers with the same clean 500. The body is ``mint failed: run `aws sso login --profile P` on the Mac; the detail is in broker.log`` and nothing else. The detail (`aws`'s stderr, Mac paths, account ids, a traceback) goes to `broker.log` and never to the guest, because the guest is the untrusted side and the response is the one channel it reads. That includes an exception raised inside the handler, which must not drop the connection and reach `credential_process` as an unexplained empty reply. A connection over the connection bound, or a request head over the cap, is the one exception: it is closed before there is a request to answer, so it gets no answer at all (see [Bounds on one request](#bounds-on-one-request)). The mint also validates what `aws configure export-credentials` returned: a JSON object, four non-empty string fields (a message names the field, never its value), and a parseable `Expiration`.

A failing `aws configure export-credentials` is logged with its stderr only, never its stdout: a CLI that fails after printing the credentials would otherwise put them in `broker.log`. The line says how many bytes of stdout there were instead.

The comments in `aws-broker` carry the rest: the timestamp shapes the parser accepts, the cache re-mint window (`REMINT_WINDOW_SECONDS`, 600 s) and throttle, the limit of 32 concurrent `/creds/` requests with an immediate `503` beyond it (`/health` takes no slot), `sanitize()` on every log line, and the `0o177` umask that makes the socket `0600`.

### What a request may cost in broker.log

A guest that loops on the broker must not fill the host's disk with `broker.log`, and the stdlib server would let it in three ways: an access line per response, an HTML error page's log line per bad request, and a full traceback per client that hangs up before the answer is written (`BrokenPipeError` from the write, re-raised into `handle_error`). So every log line a guest connection can cause goes through one chokepoint, `EventLog`. Each event has a kind, a fixed string in the broker's own code, never guest text, and may carry a detail (the request line clipped to `LOGGED_PATH_MAX`, a traceback clipped to `LOGGED_DETAIL_MAX`). The first event of each kind in a window of `LOG_WINDOW_SECONDS` (60 s) is written with its detail; the rest of the window only counts repeats of that kind, and the count comes out as one line (`in the last 60s, also: 199 x answered 404`) when the window closes. The window closes lazily, at the first event after it has passed: there is no timer thread, so a log line only ever costs the thread that caused it. A window therefore costs at most one line per kind plus the count line, whatever the guest sends, and the kinds are a dozen fixed strings. The mint lines go through it too, so a flood of requests for a profile whose SSO login has expired logs the `aws` error once a minute, not once a request.

The guest also sees as little as possible: the `Server` header is `aws-broker` with no Python version, a bad request line or an unsupported method gets the same `text/plain` generic body as any other failure instead of the stdlib's HTML page, and the forwarded socket's `/health` answers `{"ok": true}` and nothing else (see [The control socket](#the-control-socket)).

### The control socket

The guest is the untrusted side and reaches the forwarded socket as fast as it likes. A thread per connection and a deadline timer per thread (both from the stdlib server) made that a way to starve the host: under a flood of connections `yo`'s own `/health` timed out and read as "unreachable", `yo` then tried to start a second broker, and that failed with "another aws-broker is already serving" although the first one was fine.

So yo asks on a second socket, `aws-broker.ctl.sock`, which only the host can reach: Lima forwards the first socket into the guest, and nothing forwards this one. Its handler (`HealthHandler`) answers `/health` and 404s everything else. It has its own small connection bound (`MAX_CONTROL_CONNECTIONS`), so nothing on the guest socket can crowd it out. The forwarded socket's `GuestHandler` extends `HealthHandler` with `/creds/`, and the `/creds/` slots exist only there; both sockets share the `0600` bind, the request-head cap and the deadline. The forwarded socket keeps a `/health` too, because the guest-side forward probe (`aws_broker_forward()` in `forwards.py`) curls it from inside the VM. It answers `{"ok": true}` and nothing else: the broker's pid and the watched pid are the host's business, and the guest has its profile list in its own config. Only the control socket reports `pid`, `watch_pid` and `allow`. A `yo` from before the control socket read the forwarded `/health` for the pid; against this broker it finds none and says it cannot stop the broker, which a current `yo up` does.

### Bounds on one request

Four bounds stop a client from costing the broker more than a fixed amount, whatever it sends: the three below, and the log (see [What a request may cost in broker.log](#what-a-request-may-cost-in-brokerlog)).

- **Connections.** At most `MAX_CONNECTIONS` (64, twice the `/creds/` slots) are open on the forwarded socket at once. The bound is taken in the accept loop (`UnixHTTPServer.process_request`) with a non-blocking semaphore, before a thread or a timer is started. A connection over it is closed at once without an answer, and released on every path: the thread finishing, the handler raising, or the thread failing to start. The refusal is an event like any other (see [What a request may cost in broker.log](#what-a-request-may-cost-in-brokerlog)), so a flood cannot grow `broker.log` either. A client sees an empty reply (curl exit 52) and `yolobox-guest aws-creds` already treats that as "broker unreachable". While the flood lasts the guest-side forward probe fails the same way, so `yo` may re-issue the forward; that is harmless, and the flood is the agent's own.
- **Request head.** The request line and the headers together may not pass `MAX_REQUEST_HEAD_BYTES` (8 KiB). The stdlib allows 64 KiB for the line and 100 headers of 64 KiB, which is 6.4 MB of buffering per connection. `CappedReader` wraps `rfile`, and a head over the cap closes the connection unanswered (logged as a line, not a traceback).
- **Time.** The wall-clock deadline below.

The host-only control socket (see [The control socket](#the-control-socket)) has the same head cap and deadline but a connection bound of its own, so a guest flood fills the forwarded socket's bound and never reaches yo's `/health`.

A connection lives under one wall-clock deadline, `REQUEST_DEADLINE_SECONDS` (60 s), from accept to close. A timer shuts the socket down when it passes, which fails whatever read or write the handler is blocked in. A per-`recv` socket timeout would reset on every byte, so a client that trickles one byte at a time would never be cut off. Slots are taken in `do_GET` after the request has been read, and only `/creds/` takes one, so such a client holds no slot while it trickles and the 60 s deadline is what bounds it. A request also waits at most `PROFILE_LOCK_TIMEOUT_SECONDS` (15 s) for a profile's lock, then gets `503`: one profile whose mint hangs for its `MINT_TIMEOUT_SECONDS` (30 s) would otherwise hold the lock while every queued request for it holds a slot, and take all 32. The deadline is longer than the lock wait plus a mint, so a waiter that gets the lock can still finish. The `aws` CLI the broker runs to mint gets `/dev/null` as stdin, and `yo` starts the broker with it too, so neither can read a terminal the operator is typing into. `Expiration` is served in UTC with a `Z` suffix whatever zone `aws` printed (a zone-less value from `aws` is read as UTC), because botocore in the guest raises on a timestamp without a zone.

## aws-creds

`cmd_aws_creds` in `nix/guest/yolobox-guest.sh` runs `curl -sS -g --fail-with-body --unix-socket "${YOLOBOX_AWS_BROKER_SOCK}" "http://broker/creds/${profile}"` after the name check. `-g` stops curl globbing `[]` and `{}` in the URL, so a name cannot expand into several requests. Curl's stdout and stderr are captured apart. The success body is JSON that the AWS SDK reads from this command's stdout, so a curl warning must never join it, and on failure both curl's error and the body go to stderr.

Three curl exit codes mean "the broker is unreachable" and get the `yo up`-on-the-Mac remedy instead of curl's generic message: 7 (connection refused, so the proxy socket unit itself is absent, an old box), 52 and 56 (empty reply, connection reset). The last two are the common case. The agent-side `yolobox-aws-broker.socket` accepts a connection whether or not anything real listens behind lima's forward, so a dead forward or a dead Mac-side broker reads as a broken connection mid-request. `op_probe_alive` has the same two-faced exit code for 1Password.

`yo aws-check` is the doctor. It runs `ensure_aws_broker()` for real, then for each profile section in the guest's `AWS_CONFIG_FILE` runs `AWS_PROFILE=P aws sts get-caller-identity` in the VM and asserts a role ARN comes back.
