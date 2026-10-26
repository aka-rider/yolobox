# podman

The code is `nix/podman.nix`. It turns on rootless podman with the docker-compatible socket, prunes weekly as the agent, and sets the box's logging defaults in `containers.conf`.

## Container output and the journal

On 2026-10-25 a box's journal held 2.8 GiB, written in about two days (2.5 million lines, 11 boots). A compose stack with 14 healthchecks, each every 2 seconds, caused it. Three things fed the journal:

- Container stdout, 48 percent of the lines. Podman's default `log_driver` is `journald` when systemd is present, so every line a container prints lands in the agent's user journal. A healthcheck makes the service log too: `mongosh` pinging mongod made mongod log about twelve connection lines per probe, and that one container wrote 780,000 lines.
- Podman's own `container health_status` event, 25 percent: one line per check. The default `events_logger` is also `journald`.
- A conmon warning, 22 percent: one `Failed to open cgroups file: /sys/fs/cgroup/memory.events` per check. Its cause is open, see `TODO.md`.

journald did not stop it. Its default cap is 10 percent of the disk, at most 4 GiB, and the rate limit (10,000 lines per 30 seconds per service) is never reached at two lines per second per container. The repo never configured any of this, so the defaults were the whole story. No commit introduced it.

### What the repo sets

- `log_driver = "k8s-file"` and `log_size_max` of 10 MiB in `nix/podman.nix`. `podman logs` keeps working. The file lives in the container's own directory and goes with the container (`podman rm`, or the weekly `podman-prune`). conmon truncates it at the cap, so a chatty container is bounded at 10 MiB.
- `events_logger = "file"`. This is what removes the health events, which no log driver reaches. `podman events` keeps working from podman's own file, which podman rotates.
- `services.journald.settings.Journal.SystemMaxUse = "512M"` in `nix/base.nix`. This is a backstop for every other emitter, not the fix: at the incident's rate it would have been vacuumed away within about ten hours, taking the tailscaled and boot history with it. `yolobox-guest gc` already vacuums to 50M by hand.

`services.journald.extraConfig` no longer exists in this nixpkgs; the freeform `settings.Journal` replaced it. `containersConf.settings` is freeform toml too, so a misspelled key there evaluates without complaint. That is why the `eval` job in `.github/workflows/verify.yml` reads `log_driver` and `events_logger` back out of the evaluated configuration.

### Containers made before the switch

A container keeps the log driver it was created with. After a switch, an existing container still writes to the journal until it is recreated (`docker compose up --force-recreate`, or `down` and `up`). `podman inspect <name> --format '{{.HostConfig.LogConfig.Type}}'` says which driver it has.

A compose file that names `logging: driver: journald` opts back in, and then `SystemMaxUse` is the only bound. The healthcheck interval is the project's own choice. Podman's default is 30 seconds, and a 2 second interval makes any service chatty.

### Recognising a flood

```sh
sudo journalctl --disk-usage
sudo journalctl -o json --output-fields=CONTAINER_NAME | jq -r .CONTAINER_NAME | sort | uniq -c | sort -rn | head
sudo journalctl -o json | jq -r '[.SYSLOG_IDENTIFIER, (.MESSAGE|tostring|.[0:40])]|@tsv' | sort | uniq -c | sort -rn | head
```

The first says how big it is. The second names containers still on the journald driver. The third counts lines by emitter, which is how podman's events and conmon's warning show up: they carry `SYSLOG_IDENTIFIER=podman` or `conmon`, no `CONTAINER_NAME`, and sit in transient `<container id>-<random>.service` units.
