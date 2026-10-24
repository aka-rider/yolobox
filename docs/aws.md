# AWS credentials

Every allow-listed SSO profile on the Mac is available to every process in the VM, and each process picks its own with `AWS_PROFILE=<name>`. A broker on the Mac mints the credentials and the guest reaches it over a reverse unix socket. This page explains why the box works that way and how to recognise each piece failing. The rules are in `CONSTITUTION.md`.

## Design

The design changed twice. Version 1 minted STS credentials once, at `yo enter`, and froze them in the guest session's environment (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`). A session that outlived the STS duration needed a fresh `yo enter`. Version 2 added a broker but bound it to one profile per pane. It was botocore's container credential provider: a bearer token plus a URL, reached over a per-session TCP `-R` forward to the guest's gateway address. The current design drops the per-pane binding, and a live test found the gateway-address path fragile under a VPN (see Transport below), so the broker now travels the way 1Password does.

Why not one profile that the operator can switch? botocore fixes a process's credential source at first use and refreshes it only 10 to 15 minutes before the cached credentials expire. It never re-reads which profile it should use. So a global profile flipped mid-session fails in four ways:

- A running terraform or boto3 process silently lands in the other account at its next refresh, tens of minutes after the switch.
- Two panes started at different times disagree about the current profile for as long as that window lasts.
- The broker has to keep serving the old profile's cache until only minutes remain on it, instead of cutting over when the operator asks.
- The region drifts out from under the account, which reopens the DNS sinkhole.

Fixing the profile at process start avoids all four, because nothing is switched under a running process.

## Allowlist

`aws_allowlist()` in `yo` reads the Mac's `~/.aws/config` with `configparser`. It allow-lists every `[profile X]` and `[default]` section that has `sso_session` or `sso_start_url`. `[sso-session ...]` sections describe a session, not an account, so they are excluded.

A profile name must `fullmatch` `AWS_PROFILE_NAME_RE` (`^[A-Za-z0-9][A-Za-z0-9._-]*$`). A name with a space, a shell metacharacter, or a leading `-`, `.` or `_` is excluded with a stderr line naming it. `yo` would otherwise pass it down to `credential_process P` unescaped, and a leading dash would read as an option. The pattern has three spellings that are the same text on purpose:

- `AWS_PROFILE_NAME_RE` in `yo`.
- `PROFILE_NAME_PATTERN` in `aws-broker`, which re-checks every `--allow` argument in `parse_args`, so a broker started by anything but `yo` still refuses such a name.
- The regex in `yolobox-guest aws-creds`, which exits 2 with `invalid profile name` before building the URL path.

`tests/test_aws_broker.py` reads the two Python literals out of the source and fails when they differ. `tests/test_consistency.py` checks the guest's spelling against them. `fullmatch` is load-bearing in Python, because `$` before a trailing newline still matches under `match`.

A profile's region must match `^[a-z]{2,4}(-[a-z]+)+-\d+$`, ASCII only. The four-letter prefix is the AWS European Sovereign Cloud's `eusc-de-east-1`. The region lands in the rendered guest config, so a value with a newline in it would add lines to that file. A profile with no region is excluded too, with a stderr line naming it, because a region-less profile reopens the DNS sinkhole (see [the DNS sinkhole](mac-network.md#the-dns-sinkhole)). A malformed config file (bad ini syntax, undecodable bytes) raises instead of allow-listing whatever partial read it got. With no `aws` CLI, or no SSO profiles, `yo` prints one stderr line and starts no broker.

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

Why a unix socket and not a forwarded TCP port? A connected ProtonVPN blackholes every guest connection to `192.168.5.2:<port>`, the path version 2's TCP `-R` relied on (see [ProtonVPN connected](mac-network.md#protonvpn-connected)). Lima's reverse unix-socket forwards ride lima's own ssh connection and are unaffected. 1Password's forward already had that shape (see [1Password reaches the agent](ssh.md#1password-reaches-the-agent)), so the broker uses it too.

The chain has four links:

1. `aws-broker` binds `~/.local/state/yolobox/aws-broker.sock` on the Mac, born `0600`.
2. Lima reverses it to `/run/yolobox-op/aws-broker.sock` in the guest, bound as the operator exactly like 1Password's.
3. `opProxy` in `nix/base.nix`, the same chokepoint that proxies 1Password, bridges it through `systemd-socket-proxyd` to an agent-owned `/run/yolobox/aws-broker.sock`.
4. `yolobox-guest aws-creds` calls that socket.

No bearer token guards any of it. The filesystem permissions on the last socket are the whole authorization boundary, as for 1Password. [Healing reverse forwards](ssh.md#healing-reverse-forwards) covers how `yo` revives a dead forward.

There is exactly one broker socket, not one URL per profile, so the Mac and the guest no longer share a port or address to keep in sync. `nix/lib/agent-env.nix` has `awsBrokerSock`, the one place that names the agent-facing path. It sits outside the exported `env` attrset because it names a proxy target, not a variable an agent process should export. `nix/guest.nix` folds it into `YOLOBOX_AWS_BROKER_SOCK` through `runtimeEnv`, so `yolobox-guest.sh` reads it from the environment.

The Mac-side half still has to be kept in sync by hand:

- `AWS_BROKER_GUEST_SOCK` in `yo` (folded into `LIMA_PORT_FORWARDS`).
- `AGENT_AWS_BROKER_SOCK` in `yo`, the target of its health probe. `TestAgentEnvMatchesYoSockets` checks it against `agent-env.nix`.
- `lima/yolobox.yaml`, the one-time seed for a new instance. `TestLimaYamlMatchesPortForwardsConstant` checks it against `LIMA_PORT_FORWARDS`.

On an existing instance, `lima_config_gaps()` catches the lima side drifting at every `vm_up()`. It prints a stderr note and never refuses, since the healing step adds the forward live regardless (see [Checking a running instance](../lima/yolobox.md#checking-a-running-instance)).

## Broker lifetime

The broker outlives any one `yo enter` pane, because profiles are chosen per process and no session is left to tie its life to. It follows lima's hostagent instead. `aws-broker --watch-pid <pid>` uses `select.kqueue` with `EVFILT_PROC` and `NOTE_EXIT` to notice the instant the hostagent exits, without polling. `ha_pid()` supplies the pid from `hostAgentPID` in `limactl list yolobox --json`. It never opens `~/.lima/yolobox/ha.pid`, whose mtime lima rewrites on every start.

`ensure_aws_broker()` asks `GET /health` first, through `broker_needs_restart()`. A running broker stays only when both its reported `watch_pid` matches today's hostagent pid and its reported `allow` list equals today's sorted allowlist. A new `yo up`, or an edited `~/.aws/config` between two `yo` runs, therefore forces a fresh broker rather than serving a stale profile set. The stale one is sent SIGTERM and waited out with the same kqueue mechanism.

Start-up and shutdown details:

- The broker's stderr is truncated and reopened at `state_dir()/aws-broker/broker.log` on every start. The directory is `0700` and the log `0600`, because the log holds every detail the guest is not told.
- Its stdout carries exactly one line, `READY`, before it `os.dup2`s `/dev/null` over itself. Never write a secret to stdout after that line, or to any log.
- `reclaim_socket_path()` probes a connect before unlinking anything and fails, naming the collision, if something is still served there. A stale socket from a dead broker is unlinked. A non-socket file at that path is refused, never removed.
- At shutdown, `unlink_if_same_identity()` removes the socket only when its `(st_dev, st_ino)` still match the pair recorded at bind. A superseded broker exiting late therefore cannot delete a newer broker's socket. Both sides use `lstat`, because `stat` on a path since replaced by a symlink would report the target's identity and delete the wrong file.

## Minting

Minting is lazy and per profile. Each profile has its own lock, cache, throttle and last error, so one profile's expired SSO session never blocks another's `/creds/<P>` request. An IAM user's long-lived keys never expire and must never enter the guest this way, so a request for such a profile is a request-time 500. `aws sso login --profile P` on the Mac heals a running guest session with no re-enter, because the next `/creds/<P>` hit simply mints again.

Every failure on the request path answers with the same clean 500. The body is ``mint failed: run `aws sso login --profile P` on the Mac; the detail is in broker.log`` and nothing else. The detail (`aws`'s stderr, Mac paths, account ids, a traceback) goes to `broker.log` and never to the guest, because the guest is the untrusted side and the response is the one channel it reads. That includes an exception raised inside the handler, which used to drop the connection and reach `credential_process` as an unexplained empty reply. The mint also validates what `aws configure export-credentials` returned: a JSON object, four non-empty string fields (a message names the field, never its value), and a parseable `Expiration`.

The comments in `aws-broker` carry the rest: the timestamp shapes the parser accepts, the cache re-mint window and throttle, the limit of 32 concurrent requests with an immediate `503` beyond it, the 10 s socket timeout, `sanitize()` on every log line, and the `0o177` umask that makes the socket `0600`.

## aws-creds

`cmd_aws_creds` in `nix/guest/yolobox-guest.sh` runs `curl -sS -g --fail-with-body --unix-socket "${YOLOBOX_AWS_BROKER_SOCK}" "http://broker/creds/${profile}"` after the name check. `-g` stops curl globbing `[]` and `{}` in the URL, so a name cannot expand into several requests. Curl's stdout and stderr are captured apart. The success body is JSON that the AWS SDK reads from this command's stdout, so a curl warning must never join it, and on failure both curl's error and the body go to stderr.

Three curl exit codes mean "the broker is unreachable" and get the `yo up`-on-the-Mac remedy instead of curl's generic message: 7 (connection refused, so the proxy socket unit itself is absent, an old box), 52 and 56 (empty reply, connection reset). The last two are the common case. The agent-side `yolobox-aws-broker.socket` accepts a connection whether or not anything real listens behind lima's forward, so a dead forward or a dead Mac-side broker reads as a broken connection mid-request. `op_probe_alive` has the same two-faced exit code for 1Password.

`yo aws-check` is the doctor. It runs `ensure_aws_broker()` for real, then for each profile section in the guest's `AWS_CONFIG_FILE` runs `AWS_PROFILE=P aws sts get-caller-identity` in the VM and asserts a role ARN comes back.
