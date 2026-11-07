# Working on yolobox as an agent

`CLAUDE.md` is the tour of the repo and `CONSTITUTION.md` its rules; read both
first. This file holds what an agent must know before it touches a box.

## End-to-end tests run on a throwaway box

Never use the operator's working box for a live or end-to-end check. The
acceptance scripts stop services, delete the egress table for a while and
reboot the box; a box mid-work is not a test bed. Make one of your own:

```sh
yo -m e2e<N> up --memory 4 --cpus 2 --disk 40
yo -m e2e<N> bootstrap --version github:OWNER/REPO/<branch>
yo -m e2e<N> cp tests/box-hardening-check.sh :/tmp/
yo -m e2e<N> ssh bash /tmp/box-hardening-check.sh   # inside the box, as the operator
tests/box-libvirt-check.sh e2e<N>                    # from a Linux host, in the checkout
yo -m e2e<N> destroy --yes
```

- 4 GiB is the measured minimum (`MIN_MEMORY_GIB`): the first bootstrap runs
  on the seed image, which has no swap yet, and a 2 GiB box is OOM-killed
  there. A box this small relies on its swapfile for every later
  `nixos-rebuild`, so expect a slow rebuild, not a failed one. 40 is the disk
  floor.
- A bootstrap builds only from a published ref, so push the branch first and
  name it without a `/` (the ref is `github:OWNER/REPO/<branch>`).
- The e2e box's memory counts against the host-room check, so the operator's
  box keeps running beside it; ask for no more than the host has left.
- The operator approves the 1Password prompt at each unlock (`yo up`,
  `yo bootstrap`, `yo disk-grow`). Say which command needs it and wait.
- The scripts are not on the box: copy them in, or push a checkout into it.
- Destroy the box when done. `TODO.md` lists the live checks still open.
