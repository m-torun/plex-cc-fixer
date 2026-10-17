"""Pause, play and jump on an Apple TV, through Apple's own remote-control protocol (pyatv).

Plex for Apple TV does not take Plex's remote-control requests, so the daemon runs this as a separate
process for a player on an Apple TV. pyatv is not part of the standard library; install it on its own with
    python3 -m pip install --target DIR pyatv
and give DIR to the daemon as --pyatv (or install it where python3 finds it).

  python3 -m plex_cc_fixer.appletv pair --host IP [--protocol airplay|companion] [--pin-file F]
  python3 -m plex_cc_fixer.appletv state|pause|play --host IP --credentials FILE
  python3 -m plex_cc_fixer.appletv seek SECONDS --host IP --credentials FILE

Pairing makes the Apple TV show a PIN for a short while, which is typed in here (or written to --pin-file).
Pair both protocols: AirPlay carries the playback commands, Companion the remote's buttons. The credentials
it saves let this control that Apple TV from then on: keep the file private.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

COMMAND_TIMEOUT = 25  # seconds a command may take, finding the Apple TV and connecting included
CREDENTIALS = "~/.config/plex-cc-fixer/appletv.json"


class AppleTV:
    """An Apple TV playing from Plex, with the same commands as plex.Player. Each command is one run of this module."""

    def __init__(self, host, credentials, pyatv_path=None, name=""):
        self.host, self.credentials, self.pyatv_path, self.name = host, credentials, pyatv_path, name or "the Apple TV"

    def run(self, *command):
        from .plex import PlayerError
        env = dict(os.environ, PYTHONWARNINGS="ignore")
        if self.pyatv_path:
            env["PYTHONPATH"] = self.pyatv_path
        try:
            done = subprocess.run([sys.executable, "-m", "plex_cc_fixer.appletv", *command, "--host", self.host,
                                   "--credentials", self.credentials], capture_output=True, text=True, env=env,
                                  timeout=COMMAND_TIMEOUT, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        except subprocess.TimeoutExpired:
            raise PlayerError(f"{self.name} did not answer within {COMMAND_TIMEOUT} s")
        if done.returncode:
            raise PlayerError(f"{self.name}: " + ((done.stderr or done.stdout).strip().splitlines() or ["failed"])[-1])
        return done.stdout

    def seek(self, seconds):
        self.run("seek", "%.1f" % seconds)

    def pause(self, resume=False):
        self.run("play" if resume else "pause")


def saved(path):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def paired(path, host):
    """The saved pairing of the Apple TV at this address, or None."""
    return next((entry for entry in saved(path).values() if entry.get("host") == host), None)


async def find(host):
    import pyatv
    found = await pyatv.scan(asyncio.get_event_loop(), hosts=[host], timeout=5)
    if not found:
        raise SystemExit(f"no Apple TV answers at {host}")
    return found[0]


def read_pin(pin_file):
    if not pin_file:
        return int(input("PIN shown on the TV: "))
    print(f"Waiting for the PIN in {pin_file}", flush=True)
    since = time.time()
    while time.time() - since < 1800:
        try:
            with open(pin_file, encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                return int(text)
        except OSError:
            pass
        time.sleep(1)
    raise SystemExit("no PIN within 30 minutes")


async def pair(args):
    import pyatv
    from pyatv.const import Protocol
    config = await find(args.host)
    protocol = {"airplay": Protocol.AirPlay, "companion": Protocol.Companion}[args.protocol]
    pairing = await pyatv.pair(config, protocol, asyncio.get_event_loop())
    try:
        await pairing.begin()
        if pairing.device_provides_pin:
            pairing.pin(await asyncio.get_event_loop().run_in_executor(None, read_pin, args.pin_file))
        await pairing.finish()
        if not pairing.has_paired:
            raise SystemExit("the Apple TV did not accept the pairing")
        path = os.path.expanduser(args.credentials)
        known = saved(path)
        entry = known.setdefault(config.identifier, {})
        entry.update(host=args.host, name=config.name, **{args.protocol: pairing.service.credentials})
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        descriptor = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            json.dump(known, f, indent=1)
        os.replace(path + ".tmp", path)
        print(f"Paired with {config.name} ({args.protocol}); credentials saved in {path}")
    finally:
        await pairing.close()


async def control(args):
    import pyatv
    from pyatv.const import DeviceState, Protocol
    config = await find(args.host)
    entry = saved(args.credentials).get(config.identifier)
    if not entry:
        raise SystemExit(f"not paired with the Apple TV at {args.host}; run the pair command")
    for name, protocol in (("airplay", Protocol.AirPlay), ("companion", Protocol.Companion)):
        if entry.get(name):
            config.set_credentials(protocol, entry[name])
    atv = await pyatv.connect(config, asyncio.get_event_loop())
    try:
        playing = await atv.metadata.playing()
        if args.command == "state":
            print(json.dumps({"app": atv.metadata.app.identifier if atv.metadata.app else None,
                              "state": playing.device_state.name.lower(), "position": playing.position,
                              "total": playing.total_time, "title": playing.title}))
            return
        if playing.device_state not in (DeviceState.Playing, DeviceState.Paused):
            raise SystemExit(f"the Apple TV is not playing anything ({playing.device_state.name.lower()})")
        if args.command == "pause":
            await atv.remote_control.pause()
        elif args.command == "play":
            await atv.remote_control.play()
        else:
            await atv.remote_control.set_position(int(round(args.seconds)))
            if playing.device_state == DeviceState.Paused:
                await atv.remote_control.play()  # a jump means play from there
    finally:
        await asyncio.gather(*atv.close(), return_exceptions=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["pair", "state", "pause", "play", "seek"])
    p.add_argument("seconds", nargs="?", type=float, help="for seek: where to jump to")
    p.add_argument("--host", required=True, help="the Apple TV's address")
    p.add_argument("--credentials", default=CREDENTIALS, help="file the pairing credentials are kept in (default %(default)s)")
    p.add_argument("--protocol", choices=["airplay", "companion"], default="airplay", help="for pair: which one")
    p.add_argument("--pin-file", help="for pair: read the PIN from this file once it appears, instead of asking")
    args = p.parse_args(argv)
    if args.command == "seek" and args.seconds is None:
        p.error("seek needs SECONDS")
    asyncio.get_event_loop().run_until_complete(pair(args) if args.command == "pair" else control(args))


if __name__ == "__main__":
    main()
