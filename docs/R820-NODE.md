# The R820 as always-on node and nightly trainer

Decided 2026-10-05. The R820 (4× E5-4650v2, 40 cores, 192 GB, 8× 1 TB, H710)
is **not** used for JEPA inference — it has no GPU and those are Ivy Bridge
cores with AVX but no AVX-512 or AMX, so the encoder would run in seconds
where the MSI's GPU does it in **168 ms, measured**. It earns its place doing
two things the MSI genuinely cannot.

## Job 1: the always-on Tailscale node

This closes a gap that has been open since M0 and is listed in `CLAUDE.md` as
owed by Shaurya: **there is no always-on device at home**, so waking the MSI
needs something already awake on the tailnet to send the magic packet. Today
that means the MSI has to be reachable in order to be woken, which is
circular, and the fallback has been "hope it is already on".

With the R820 on the tailnet permanently:

- `wake-msi.sh` sends its Wake-on-LAN packet **from the R820**, on the same
  L2 segment as the MSI, which is where a magic packet actually works. Over
  Tailscale a WoL broadcast does not route; this is the real fix, not a
  convenience.
- Wake timers stop mattering as much. `powercfg /waketimers` only fires on AC
  and only on trigger-start boundaries, both of which have bitten this project
  already; on-demand wake sidesteps the whole mechanism.
- Nothing about the camera or the arm moves to it. Those stay on the MSI.

**Needs:** Tailscale installed and authenticated, `--ssh` optional, the MSI's
MAC address, and the two machines on the same wired segment. The wired part is
not optional for WoL.

## Job 2: the nightly trainer

Everything in `docs/LEARNING-DESIGN.md` above the encoder trains on **latent
vectors**, not pixels, and that is what makes this fit a GPU-less box:

| job | why it suits the R820 |
|---|---|
| Layer 2 predictor, nightly | minutes of CPU on a few MB of vectors; 40 cores and latency that does not matter |
| `latent_probe` sweeps | embarrassingly parallel over k and seeds |
| bulk VLM relabelling | **no** — needs the GPU, stays on the MSI |
| latent and log archive | 8 TB raw, and it is already spinning |

The division is clean because of the storage format: the MSI writes
`latents.db`, the R820 pulls it, trains, and writes back a small model file.
No frame is ever involved, so nothing about the privacy rule changes.

## What to weigh before it goes in a bedroom

Flagged plainly because they are the reason this might not be worth it:

- **Power.** A four-socket R820 idles around 200–350 W and loads 600–900 W+.
  At roughly $0.15/kWh that is **$25–90/month** to keep a wake-on-LAN sender
  and a nightly cron job alive. A Raspberry Pi does job 1 for about 3 W.
- **Noise and heat.** High-RPM 1U/2U fans. This belongs in a garage, a
  basement or a closet with airflow, not next to where he sleeps.
- **Adding a GPU is awkward.** Limited slots and typically no supplementary
  GPU power connectors. A 75 W Tesla P4 could run off the slot; a P40 (250 W)
  needs adapters and cooling work. Not worth it while the MSI's GPU is idle
  most of the day.

**Honest recommendation:** if the noise and power are acceptable somewhere out
of the way, do it — job 1 alone fixes a real architectural gap. If it has to
live in his room, a Pi for job 1 and the MSI for job 2 is the better trade,
and the R820 gets powered on for training when he wants it.

## Setup, in order

1. Rack or shelf it somewhere with airflow, **wired to the same switch as the
   MSI** (required for WoL).
2. Install a server OS. Ubuntu Server LTS is the path of least resistance for
   Tailscale, Python and cron.
3. `tailscale up`, confirm it appears in the tailnet, note its 100.x address.
4. Tell me the address and the MSI's MAC, and I will:
   - move `wake-msi.sh`'s packet send to originate from the R820
   - add a `scripts/trainer.sh` that pulls `latents.db` and runs the nightly
     training as a systemd timer
   - add it to `msi-status.sh` so there is one place to see both machines
5. Confirm the H710 is in a sane RAID (or HBA/passthrough) before anything is
   stored on it. Eight 1 TB drives of unknown age is not a backup.

## What I have NOT done

Nothing on this machine. It is not on the tailnet, I have never reached it,
and every number above about its performance is an **estimate from the CPU
generation, not a measurement**. When it is online I will benchmark the
encoder on it so the inference decision is made from numbers too — I expect it
to lose to the MSI by a wide margin, and that expectation should be checked
rather than trusted.
