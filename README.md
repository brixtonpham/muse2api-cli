# muse2api

Generate videos and images with [Muse](https://muse.ai) from a prompt plus reference images, from the command line.

> **Unofficial.** Not affiliated with or endorsed by Meta or Muse. There is no public Muse API: this tool drives the
> muse.ai web app in a browser you are logged in to. Automating a web app may break its terms of service and can get
> an account limited. Use it only with your own account, at your own risk. It can stop working whenever muse.ai
> changes.

## Requirements

- Google Chrome or Chromium (set `MUSE2API_CHROME` if it is not in the usual place)
- Python 3.11+

```bash
pip install -r requirements.txt          # or: pipx install .
```

## Usage

```bash
python3 muse2api.py login  [--account main] [--from-file cookies.json]   # save the muse.ai cookies (~30 days)
python3 muse2api.py login  --account NAME --browser [--proxy URL]         # you log in, in a window it opens
python3 muse2api.py gen    [--account main] --prompt "..." --ref a.jpg [--ref ...] --out DIR [--timeout 900]
python3 muse2api.py get    [--account main] --events turn.json --out DIR  # re-download a saved turn's media
python3 muse2api.py usage  [--account main | --all]                       # weekly % used, reset date
```

`gen` also takes `--preamble TEXT` (repeatable, answered in the same chat before the prompt), `--preamble-ref IMG`,
`--events FILE` (save the turn's raw events, for `get`), `--thread URL` (continue a chat) and `--main-chat`
(default: a new side chat per generation).

`gen` prints one JSON line `{"ok", "media": [{"file", "bytes", "mime", "path"}], "reply", "thread", "usage"}`.

| exit | meaning |
|---|---|
| 0 | media saved |
| 2 | Muse answered without media (its text in `reply`, usually a question or a refusal) |
| 3 | a step failed (`error`) |
| 4 | the account is out of quota |

## Accounts

Everything lives in `~/.config/muse2api/` (or `$MUSE2API_HOME`).

- **Own Chrome (default).** Cookies are kept in `<account>.json` (mode 600) and written back after every run, so the
  rolling session stays alive. Each account has its own Chrome profile in `profiles/<account>/`, so Muse sees the
  same device every run. One run per account at a time, because Chrome locks the profile.
  - `login` reads the cookies from your running Chrome over CDP: enable remote debugging at
    `chrome://inspect/#remote-debugging` and click Allow. `--from-file` takes a Cookie-Editor or CDP JSON export.
  - `login --browser` opens a visible Chrome on the account's profile. You type the email and code yourself; the
    script never types or reads them. Once the composer loads, it saves the cookies and closes.
  - `--proxy scheme://[user:pass@]host:port` is saved in `accounts.json` and used by every later run of that account.
- **A browser you run.** Point an account at any Chrome-based browser with a CDP endpoint (for example Chrome started
  with `--remote-debugging-port=9222`, or a profile manager). Its profile, proxy and cookies stay its own; the script
  opens one tab and closes it at the end. In `accounts.json`:

  ```json
  {
    "work":  {"cdp": "http://127.0.0.1:9222"},
    "other": {"open_cmd": ["mytool", "start", "p1"], "cdp_cmd": ["mytool", "endpoint", "p1"],
              "close_cmd": ["mytool", "stop", "p1"]}
  }
  ```

  `cdp` is an `http://host:port` or `ws://` URL; `cdp_cmd` prints one of those, or JSON `{"ws": "ws://..."}`. The
  commands run one at a time across parallel runs.

Every `gen` logs the account's usage before and after to `usage.jsonl` (or `$MUSE2API_USAGE_LOG`), so each file's cost
in % of the weekly limit is known. A run will not start while system RAM use is at or above 80 %
(`MUSE2API_MAX_RAM`).

## How it works

Muse gives each account an agent VM reached over an encrypted WebSocket, so there is no plain HTTP API. The page
holds a gateway client that sends RPC calls to that VM over the socket. The script opens muse.ai in a browser, finds
that client in the page, and uses the same calls the web app does:

| call | used for |
|---|---|
| `chat.stream` | send a message (a new side chat is a fresh session id) |
| `chat.history`, `sessions.get` | read the turn's events and its status |
| `fs.raw` | download a generated file by its `sandbox://workspace/...` path, byte for byte |

Each saved file's size is checked against the `byte_len` in the event. If the gateway client cannot be found, the
script falls back to the page itself: it types in the composer, reads the decrypted events through a `JSON.parse`
hook, and clicks the message's Download button.

Reference images go through the composer by default. `MUSE2API_API_REFS=1` sends them inline with `chat.stream`
instead (experimental); `MUSE2API_DOM_SEND=1` forces the composer for every message.

Measured on a Free account: a 10 s 720×1280 video takes about 1.5 min and about 2.65 % of the weekly quota; an image
costs about 0 %. Videos cap at 10 s per clip.

## Tests

```bash
python3 -m unittest discover -s tests -v   # offline: parsing and path logic, no account needed
```

## License

MIT
