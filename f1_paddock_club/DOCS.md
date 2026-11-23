# Paddock Club F1

The F1 prediction game for Paddock Club Reeuwijk. Players predict qualifying, sprint and race; standings update live from Formula 1's official timing.

## How it is reached from the internet

The add-on runs its **own** Tailscale node (default name `f1`) in userspace mode and publishes **only the app's port 8080** with Tailscale Funnel at `https://f1.<your-tailnet>.ts.net`.

- Internet visitors reach that single web app over HTTPS. Funnel cannot forward them anywhere else.
- The node is tagged `tag:f1pool`. With the policy below, a tagged node is not allowed to open connections to any other device in your tailnet, so Home Assistant, your NAS and your subnet route stay unreachable from it.
- The node does not accept subnet routes or DNS from the tailnet.
- The add-on has no access to the Home Assistant API or the Supervisor, and no host networking.

### 1. Tailnet policy (Tailscale admin console → Access controls)

Merge these parts into your policy file. The key change is that only **members** (your own devices) may connect to other devices; the tagged game node gets no rule, so it can connect to nothing.

```json
{
  "tagOwners": {
    "tag:f1pool": ["autogroup:admin"]
  },
  "acls": [
    { "action": "accept", "src": ["autogroup:member"], "dst": ["*:*"] }
  ],
  "nodeAttrs": [
    { "target": ["autogroup:member", "tag:f1pool"], "attr": ["funnel"] }
  ]
}
```

If your policy still has the default rule `"src": ["*"]`, replace `"*"` with `"autogroup:member"`. Keep any other sections (for example `ssh`) as they are.

### 2. Auth key (Settings → Keys → Generate auth key)

- Reusable: off. Ephemeral: off. Pre-approved: on.
- Tags: `tag:f1pool`.

Paste the key in the add-on option `tailscale_authkey`. After the first start the node stays signed in (state is kept in the add-on's data folder); you can clear the key afterwards.

## Options

| Option | What it is |
|---|---|
| `admin_name` | Your player name. This account becomes admin. |
| `admin_email` | Your e-mail (for admin notifications and password reset). |
| `f1_username` / `f1_password` | Your Formula 1 account with an active F1 TV subscription. Used only to read live timing. |
| `smtp_user` / `smtp_password` | Gmail address and a Gmail **app password**, for invites and password resets. |
| `tailscale_authkey` | See above. |
| `tailscale_hostname` | Name of the game node; gives `https://<name>.<tailnet>.ts.net`. |
| `base_url` | Leave empty; it is filled in from Tailscale automatically. |

## First start

1. Start the add-on and open its **Log**. It prints an *ADMIN SETUP* link. Open it and choose your password.
2. In the app go to **Admin → Import a season from Excel** and upload the season workbook.
3. Add e-mail addresses to the players and press **Invite** for each.

## Where data comes from

- Calendar: FastF1's calendar file (built from F1's calendar), overridden by F1's official live-timing index. Date, time, venue changes and cancellations are picked up every 6 hours (or with *Sync with F1 now*).
- Live and final results: livetiming.formula1.com, the official F1 timing feed.
- Driver of the Day (with the fan-vote split) is read automatically from F1's Driver of the Day article on formula1.com. If it isn't found within 6 hours you get an e-mail, and you can paste the article link on the weekend's admin page. A weekend freezes 48 hours after the race once Driver of the Day is set (at the latest after 7 days).

- Circle of Doom (own tab, live during races and sprints, logged-in players only): every car placed by its time gap to the leader, one lap per turn, built from the same live timing feed. Tap a driver to see where a pit stop now would put him. Pit loss is measured from F1's timing archive: the median of all green-flag stops at the circuit over the last three seasons (in-lap plus out-lap minus two normal laps; wet races left out). It is fetched in the background when the first session of a weekend goes live and updated after each race. With fewer than 6 usable stops a typical figure per circuit is used instead. VSC costs 65 %, Safety Car 50 % of it, and it can be nudged with − and + in the browser. Below it, Replay plays back any race or sprint of the current season on the same circle (start, stop, start lap, 1×/2×/5×). Each replay is built once from F1's timing archive and kept in `/data/replay`.
- Race recap (race page, races and sprints): tyre strategy per driver (compound per stint, used sets hatched, Safety Car / VSC / red flag laps shaded) and a lap-time chart where you pick the drivers to compare, with pit stops marked. Built in the background after each race from F1's timing archive (about 6 MB) and kept in `/data/recap`. PCR standings links to it from the race chart header.
- Lap comparison (race page): the pole lap against P2 (also sprint pole) and the fastest lap against the second fastest, from F1's car telemetry. Speed, throttle, brake, gear and the time gap along the lap, with corner numbers, plus a track map coloured by who was quicker where. Built in the background after each session (one at a time, as the telemetry files are 10–20 MB) and kept in `/data/telemetry`; the first visitor to an older session triggers the build.

Data is stored in `/data/f1pool.sqlite3`, which is included in Home Assistant backups.
