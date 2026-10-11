# blackice-mcp

An MCP server for UGC Bundle development using BlackICE and REServer. It lets an AI assistant in your editor
control a server and (multiple) game clients. It can run console commands, read output and logs, build and
restart UGC bundles and start or stop processes.

## How it works

```
   Editor / AI assistant
            |  MCP over stdio
            v
   +---------------------+
   |    blackice-mcp     |   keeps track of connected processes,
   |       (hub)         |   forwards calls, starts new processes
   +---------------------+
            ^  local TCP, one JSON message per line
            |  processes actively connect to the hub
     +------+------+-------------+
     |             |             |
  REServer     BlackICE cl0  BlackICE cl1
  --mcp        --mcp         --mcp
```

Your editor starts the hub. Game processes started with `--mcp` connect to it.
The hub listens on `127.0.0.1` by default (use `--host` to change that).

## Setup

Add the hub to your editor's MCP config. The path depends on the editor. VS Code uses
`.vscode/mcp.json` while Claude Code expects `.mcp.json`, but the contents are the
same. Use `mcp.json.example` as a base.

The editor starts and stops the hub for you and if the hub restarts,
processes started with `--mcp` reconnect on their own.

### Running the hub by hand (usually not desired)

```sh
uv run blackice-mcp                     # listens on 127.0.0.1:8137
uv run blackice-mcp --port 9000         # use another port
uv run blackice-mcp --print-endpoint    # print the --mcp-addr (and --mcp-token) flags for a process
```

### Token

Off by default. The hub only listens locally, and anything that can reach it can already run code
as you, so a token wouldn't add much.

On a shared machine, other users can reach your local ports too and could connect a fake process.
Start the hub with `--token <value>` and pass the same value to processes with `--mcp-token`.

### Console window

On Windows, a server started by the hub gets its own console window, just like starting it
yourself. You can see its output and type into it, and closing the window stops the server. Pass
`--no-console` to the hub if you don't want it.

## Tools

Most tools take an optional `target`. That can be a pid, a realm (`server`, `client`), or `realm:slot`
(`client:0`). You can leave it out when only one process is connected. If the target matches more
than one process you get an error.

| Tool | Purpose |
|---|---|
| `hub_info` | Version, commit, pid, address, docs URL, platform and profiles of this hub |
| `list_processes` | Connected processes with realm, slot, pid, working dir, build commit and health |
| `process_state` | Details for one process |
| `console_exec` | Run a console command and return its output |
| `stream_read` | Console output and log lines since a cursor. `source` can limit it to `log` or `console` |
| `read_log` | Read a process's log file. `source` picks `main`, `console` (browser console messages) or `cef` (full browser log) |
| `bundle_list` | Bundles the server knows about |
| `bundle_start` / `bundle_stop` / `bundle_restart` | Start, stop or restart a bundle |
| `build_bundle` | Run `dotnet build` in a bundle project |
| `deploy_bundle` | Copy a built bundle into a server's `data/ugc/<name>` and list what was copied |
| `connect_client` | Connect a client to a server by profile name |
| `search_api` | Search the scripting API by name or class |
| `describe_api` | Signature, parameters and return type for one API member |
| `class_members` | All functions and properties on a class, including inherited ones |
| `list_profiles` | Configured install profiles |
| `configure_profile` | Save where a server or client is installed |
| `spawn_server` / `spawn_client` | Start a process that connects to this hub |
| `list_spawned` | Processes this hub started, and whether they've connected |
| `wait_for` | Wait until a process connects or reaches a phase (`menu`, `session_active`). Returns early if it dies |
| `stop_process` | Stop a process this hub started |
| `screenshot` | Take a PNG of a process's game window (Windows only) |

### Building and deploying bundles

The usual loop is `build_bundle` → `deploy_bundle` → `bundle_restart`.

`build_bundle` only runs `dotnet build`. If your bundle has other build steps, like a web UI or
generated files, run those yourself and check the `copied` list from `deploy_bundle`. If a build
fails because a nuget package can't be found, the result includes a hint about refreshing package
versions.

The bundle commands wait for the server to confirm the change. If it doesn't, you get
`confirmed: false` along with the server output from that time, which usually contains why.

### Process health and phases

`list_processes` shows each process as `alive`, `stalled`, `frozen` or `dead`. Processes that have
gone away stay listed under `dead` so you can still look into them. While a process is running,
use `stream_read`. After it dies, `read_log` reads its log file. For processes the hub started,
`list_spawned` also shows the exit code and signal (a `SIGTRAP` usually means a `VERIFY` failed).

Each process also actively reports what it's doing:

- clients: `initializing` → `menu` → `connecting` → `session_active`
- servers: `starting` → `active`

`connect_client` only works on a client sitting at `menu`. Bundle commands wait while any client
is `initializing` or `connecting`, and tell you which one. A client can't go back to the menu
after a session ends, so a `disconnected` or `failed` client has to be restarted.

`connect_client` looks up the server's address from its profile, so you don't need to worry about
correctly specificy addresses.

`stop_process` only stops processes the hub started. Processes you started yourself can be
controlled but not stopped from here.

## Scripting API docs

`search_api`, `describe_api` and `class_members` are optimized into a machine readable format.
They contain the same data as https://scripting.replay.re, but lookups are cached and more suitable
for LLMs to parse. The cache duration is currently set at one hour.

## Dev servers stay off the server list

A server started with `--mcp` always runs with `sv_lan` on, so it never shows up in the public
server list. This is itentional so agents don't accidentally expose something to the public that
the user didn't want.

## Profiles

Profiles tell the hub where your server and client are installed. There are none at first. The
assistant will ask you for the paths and save them with `configure_profile`, which checks that the
executable is there and won't overwrite an existing profile unless asked.

On Windows, if a client is installed in the launcher's default location at `%LOCALAPPDATA%\replay\blackice`,
`list_profiles` reports it and the assistant offers that path.

Profiles are saved here:

| Platform | Path |
|---|---|
| Linux | `~/.config/blackice-mcp/profiles.json` |
| Windows | `%LOCALAPPDATA%\replay\blackice-mcp\profiles.json` |

You can use `--profiles` to specify a different file.
`blackice-mcp --init` writes an example with two placeholder profiles:

```json
{
  "profiles": {
    "local-server": {
      "kind": "server",
      "install_dir": "/opt/server",
      "bindaddr": "[::]:20770"
    },
    "local-client": {
      "kind": "client",
      "install_dir": "C:/BlackICE"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `kind` | `server` expects `REServer`, `client` expects `BlackICE.exe` |
| `install_dir` | Install folder. Also used as the working directory, so `data/ugc`, `cfg/` and `logs/` are found relative to it |
| `bindaddr` | Server only. Passed as `--bindaddr` and used by `connect_client` |
| `voip_addr` | Server only. Passed as `--voip-addr` |
| `launcher` | Command to run the executable through, e.g. `["wine"]` |
| `extra_args` | Extra command-line arguments |

The hub adds the `--mcp` flags itself.

### Clients on Linux

The client is a Windows program, so on Linux it needs a `launcher`:

```json
{ "kind": "client", "install_dir": "/home/you/blackice", "launcher": ["wine"] }
{ "kind": "client", "install_dir": "/home/you/blackice", "launcher": ["proton", "run"] }
```

With a launcher set, the hub looks for the `.exe`.
Saving a Linux client profile without a launcher gives a warning.
Clients started through a Steam shortcut aren't supported.

### One server at a time

`spawn_server` refuses to start a second server while one is running, since both would try to use
the same port.

## Output stream

Console output and log lines are numbered in a single sequence, so `stream_read` returns them in
the order they happened:

```json
{"cursor": 4, "dropped": 0, "lines": [
  {"seq": 1, "source": "console", "module": "",           "text": "Loaded bundles:"},
  {"seq": 2, "source": "log",     "module": "server_ugc", "text": "Discovered new UgcBundle"}
]}
```

Pass `cursor` back to get the next lines. Each process keeps the last 8192 lines.
If you fall further behind, you get what's left and `dropped` says how many you missed.

`console_exec` returns the same format, containing only the output of that command.

## Permissions

Commands from the hub have the same permissions as typing them into the console. Anything you
can't change by hand can't be changed through the hub either, and you get an error.


## AI Disclaimer

This mcp hub has been created with assistance from AI tools. The code has been checked and corrected
by a human and the "other side" of this implementation in the server and client is strictly vetted
too. If you use this tool to create UGC content you are still responsible for it.
