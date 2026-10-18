# Delos upgrade: Ubuntu 24.04 to 26.04 LTS

Status: PLAN ONLY, nothing executed. Written 2026-10-18 (Sunday). Delos was surveyed read-only over SSH the same day.

## Verdict

No urgency. 24.04 keeps security updates until 2029, and Delos earns money through Hestia. Delos stays on its current small plan (Pari's decision 2026-10-18: no resize or clone, no added cost; the same 1 GB / 25 GB VPS went 22.04 to 24.04 before). The survey did find **disk** (6.8 GB free of 25 GB) and **RAM** (961 MB, 512 MB swap) are tight, so the plan frees disk first and runs the upgrade from Lish with services stopped. Linode's own guidance is summarised under "Linode / Akamai guidance" below.

## What actually changes (from the official 26.04 release notes and the 24.04 to 26.04 upgrade guidance)

Sources: documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/ and a FOSS Linux upgrade guide (upgrade path from 24.04 opened 2026-09-29 after 26.04.1 shipped 2026-08-27; the date is from the guide, not the official notes).

| Area | Change | Impact on Delos |
|---|---|---|
| sudo | `sudo-rs` is the default `sudo`; the original is renamed `sudo.ws`; `sudo-ldap` removed | Delos has sudo 1.9.15p5 and only `/etc/sudoers.d/README`. Nothing algo-side calls sudo (grep of `slack_listener.py`, `leto.py`, `hestia.py`, `hestia_core/` found none). Keep a root session open during the swap. After the upgrade check `sudo -V` and `sudo systemctl status slack_listener`. |
| coreutils | `rust-coreutils` is the default; GNU versions stay available with a `gnu` prefix and can be switched back with apt; `cp`, `mv`, `rm` are still GNU because of unresolved bugs | The only coreutils use in the repo is `date`, `head`, `tail` (`hestia_core/history.py`) and `date '+%F %T'` in `run_mcx_downloader.sh` and the cron lines (`date +%Y%m%d`). Low risk, but verify the output formats after the upgrade. |
| Python (system) | 3.12 to 3.14 | `run_mcx_downloader.sh` calls bare `python3` twice to read the Slack token from a CSV. It uses only `csv`, so it should work, but test it. Everything else uses `/home/parijnan/anaconda3/bin/python`, which is unaffected. |
| Kernel | 6.8 to 7.0 | Needs the final reboot. No algo impact expected. |
| cgroup v1 | removed | Delos already runs cgroup2 (`cgroup2fs`). No impact. |
| systemd | 255 to 259; last release with SysV init-script compatibility | Delos uses native units only. No impact. |
| Netplan | 1.0 to 1.2; new `systemd-networkd-wait-online` logic | Delos uses `systemd-networkd`. Check IPv6 and IPv4 come back after the reboot (`ip a`), since both SSH aliases matter. |
| OpenSSH | 9.6p1 to 10.2p1; DSA removed; host DSA keys no longer generated; new `sshd.service` alias | `/etc/ssh/ssh_host_dsa_key` still exists on Delos. Unused and harmless. Confirm the `~/.ssh/delos_sync` key type is not DSA (not checked; DSA is removed in OpenSSH 10). Keep the existing `sshd_config` at the installer prompt. |
| Time daemon | chrony is the new default for new installs; upgraded systems keep `systemd-timesyncd` unless migrated | Delos is clock-synchronized via timesyncd. Leave it alone. Optionally `apt-mark auto systemd-timesyncd && apt install chrony` later, as a separate change. |
| initramfs | Dracut replaces `initramfs-tools` | Reboot-time risk, covered by the snapshot. Leave default. |
| APT | 2.7 to 3.1; `apt-key` removed; new `apt history-*` commands | No apt-key use in the repo. `apt history-undo`/`rollback` could help with partial failures but is not a substitute for the snapshot. |
| Postfix | no longer chrooted by default | Postfix runs on Delos (`postfix@-.service`). Likely only local mail for cron. Check `mailq` and a test mail after the upgrade. |
| Other removed or changed packages | `dmraid`, `kpartx-boot`, squid helpers, `pkcon`, `~/.pam_environment` no longer read by sshd | None used on Delos. |

Not verified: whether uutils changes `date`/`sort`/`cut`/`tr` edge-case behaviour for any script. The repo has few shell scripts (`data_pipeline/run_*.sh`), so a post-upgrade dry run of each wrapper settles it.

## Delos state at survey (2026-10-18, read-only)

- Ubuntu 24.04.3 LTS, kernel 6.8.0-147, `Prompt=lts`, no held packages, single `ubuntu.sources` (no PPAs).
- Disk `/`: 25 GB, 17 GB used, **6.8 GB free (71%)**. The upgrade guide recommends about 25 GB free. Treat under about 10 GB as a blocker.
- RAM: 961 MB total, about 584 MB available. Swap: 512 MB. Tight for `do-release-upgrade`.
- No `screen` sessions and no hestia/leto/iris/prometheus process running (Sunday, so expected).
- Running services of note: `cron`, `ssh`, `slack_listener`, `postfix`, `systemd-timesyncd`, `unattended-upgrades`, `haveged`, `ModemManager`, `multipathd`.
- Live cron lines: `55 8 * * 1-5 hestia.py`, `56 23 * * 1-5 run_mcx_downloader.sh`, and one-shot `hestia_core.cutover_reset` lines dated 2 October (past). Everything else is commented out.
- `slack_listener.service` runs as `parijnan` with `Restart=always`.

## Preconditions (all must hold)

1. Free disk before starting, without changing the plan: `apt autoremove`/`apt clean`, old kernels (6.8.0-146 is still installed), rotated and old logs under `logs/`, pip/conda caches. Then re-measure with `df -h /`. Linode's guide says 20 GB free; Ubuntu's installer checks its own minimum and will abort cleanly if short (it does not fail midway), so the real test is how far `do-release-upgrade` gets in its pre-check. Target at least about 10 GB; if the pre-check refuses, free more or move bulky data (old CSVs, backups) off the box with an approved rsync. `du` needs sudo (password required over SSH), so run it from Lish or interactively.
2. Linode manual snapshot, which needs the paid Backups service enabled on Delos (check Cloud Manager; cannot be seen from the server). Only one manual snapshot is kept and a new one overwrites it. If Backups is not enabled, either enable it for the upgrade month or take an offsite copy of `~/scripts`, `/etc`, crontab, `hestia_data/` and credentials; without a snapshot there is no whole-system rollback.
3. All four engines flat on the broker side (check Slack and the `hestia-dashboard` open positions), with no pending orders.
4. Hestia stopped via the host flag; confirm with `pgrep -af hestia` that nothing is running.
5. Window: Saturday or Sunday, finishing at least 12 hours before Monday 08:55 IST. MCX is closed at weekends.

## Procedure

1. Local: commit and push all work, so Delos can `git pull` cleanly afterwards.
2. Delos (approval needed for each write): stop Hestia via the host flag; comment out the `hestia.py` and `run_mcx_downloader.sh` cron lines (keep a copy of `crontab -l` in `~/crontab.bak`).
3. `sudo apt update && sudo apt full-upgrade && sudo apt autoremove`, reboot, confirm the box returns on 24.04.
4. Linode snapshot. Note the Lish console URL.
5. Run `sudo do-release-upgrade` from the **Lish console** (Linode's recommendation; also what a Linode community answer on a 1 GB / 25 GB 20.04 to 22.04 upgrade advises, after the output jumped from SSH to Lish mid-upgrade). If SSH is used instead, the upgrader opens a fallback sshd on **TCP 1022**: `ufw` is active on Delos, so run `sudo ufw allow 1022/tcp` first and `sudo ufw delete allow 1022/tcp` afterwards (Linode guide). Answer "keep local version" for `sshd_config`; accept new versions for files you never edited. Expect it to be slow on 1 GB RAM, and do not interrupt it.
6. Final reboot (skipping it is a named top cause of failures).
7. Verify, in order:
   - `lsb_release -a` shows 26.04; `uname -r` shows 7.x.
   - SSH over both `delos-ipv6` and `delos-ipv4`; `sudo -V` and a sudo command work.
   - `systemctl status slack_listener ssh cron postfix`; `systemctl --failed`.
   - `timedatectl` (IST, synchronized).
   - `/home/parijnan/anaconda3/bin/python --version` unchanged; `python3 --version` shows 3.14.
   - Run `data_pipeline/run_mcx_downloader.sh` by hand during a quiet period (it does a `git pull` and one download); check `cron.log`.
   - `python -m pytest tests/` on Delos.
   - `date +%Y%m%d` and `head`/`tail` on a repo CSV behave as before.
8. Re-enable the two cron lines. Restart Hestia with the `hestia-restart` skill, then run `hestia-qc` before Monday's open. Never start the standalone `prometheus.py` meanwhile (AB1007).
9. Watch the first Monday session closely: stale-tick watchdogs, Fyers candle pull, Slack session report, AB1021 clustering.

## Linode / Akamai guidance

Linode's docs have no 26.04 guide yet; the closest are the 22.04 upgrade guide and the generic "How to Upgrade a Linux System to the Latest Distribution" (both now at akamai.com/cloud/guides/) plus the techdocs.akamai.com manual-snapshot page. Points that apply:
- Take a manual snapshot first (Backups service required; one snapshot per Linode, overwritten by the next).
- Lish is the safer way to run the upgrade; over SSH the upgrader uses port 1022, so open it in `ufw`.
- Reboot before upgrading so Linode's kernel handling applies; do not stop `ssh` or other essential services.
- 20 GB free is Linode's figure (their guide has no small-plan, swap or memory notes at all).
- "This operation cannot be canceled after it is started"; declare a maintenance window.
- Afterwards: `lsb_release -a`, close port 1022, re-enable any third-party repos Ubuntu disabled (Delos has none), run `update`/`upgrade`/`autoremove`/`autoclean`.
- Linode also documents building a fresh Linode and migrating, as the alternative to in-place. Not chosen here.
- Delos boots through GRUB with `console=ttyS0,19200n8 net.ifnames=0` in `/etc/default/grub`, with `/boot` on the root disk. Keep these lines if the installer offers a new grub config, so Lish serial output and `eth0` naming survive. `net.ifnames=0` matters for netplan.
- Not found: any Linode or Akamai thread on a 24.04 to 26.04 upgrade, or on 26.04 with 1 GB RAM. The evidence for the small plan is the user's own 22.04 to 24.04 success plus an older 1 GB / 25 GB Linode community report for 20.04 to 22.04.

## Rollback

Restore the Linode snapshot (hosts, IPs, and SSH host keys return as they were). Note the Delos IPv4 and IPv6 addresses stay the same on restore, so `~/.ssh/config` needs no change. If the box is unreachable, use the Lish console.

## Open questions for Pari

- Is the Backups service enabled on Delos (needed for the manual snapshot)? If not, enable it for the upgrade, or accept an offsite copy only.
- Is it worth doing before the scaling steps (2 units the week of 19 Oct, maybe 5 after)? Recommendation: no, finish scaling first, then upgrade in a quiet weekend.
