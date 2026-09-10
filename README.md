# RankYoinker

Ranks, stats, instalock, and Discord Rich Presence for **VALORANT** and
**League of Legends**, live in your browser, no manual setup once installed.

Built on top of [VALORANT Rank Yoinker](https://github.com/zayKenyon/VALORANT-rank-yoinker)
by Zay Kenyon and Contributors (ISC license, see `VRY-LICENSE.txt` /
`setup/VRY-LICENSE.txt`).

- Website & download: https://rankyoinker.de
- Discord: https://discord.gg/rankyoinker

## What's in this repo

This is the actual source that ships to users, minus build output and
runtime state:

- `index.html`: the overlay itself (all UI, all client-side logic).
- `vry_log_server.py`: the local backend the overlay talks to. Reads the
  local Riot client (League/VALORANT) session, proxies Riot's own APIs, and
  serves `index.html` locally.
- `setup/build_source/`: the Python source that gets frozen (via
  `cx_Freeze`, see `setup/build_source/setup.py` /
  `setup/build_source/build_and_deploy.bat`) into `vry.exe`, the small process
  that hooks into VALORANT itself. This is the part most worth reading if
  you want to verify there's nothing sketchy going on before running the
  installer.
- `setup/install_all.py` / `setup/uninstall_all.py`: what the installer
  actually runs. Autostart, a firewall rule, and an `http://vry` shortcut
  (both needed for the optional phone-pairing feature), nothing else.
- `setup/RankYoinker.iss`: the Inno Setup installer definition.

Not included: the embedded Python runtime (`.pyd`/`.dll`/`python.exe`,
just the stock python.org "embeddable" distribution, not our code),
build output, and any runtime state (logs, presets, your own `config.json`).

## Why you might see a SmartScreen warning

RankYoinker doesn't have a paid code-signing certificate. That's the only
reason Windows shows "Windows protected your PC" on first launch. It's not
a virus, just an unsigned installer from a small/new publisher. Click "More
info", then "Run anyway".

## License

See `frozen_application_license.txt` and `setup/VRY-LICENSE.txt`.
