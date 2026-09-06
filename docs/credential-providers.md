# Credential providers

The MCP servers read the controller password and API key from the environment.
An MCP client passes that environment to the server it spawns, and to every
other process it spawns, so a plain `UNIFI_PASSWORD` is inherited far more
widely than it needs to be. Two indirect spellings keep the value inside the
server process:

| Spelling | Value |
|----------|-------|
| `UNIFI_<SERVER>_PASSWORD_FILE` | A path to a file whose contents are the password. |
| `UNIFI_<SERVER>_PASSWORD_COMMAND` | An argv, run without a shell, whose stdout is the password. |

`UNIFI_<SERVER>_API_KEY_FILE` and `UNIFI_<SERVER>_API_KEY_COMMAND` work the same
way, as do the shared `UNIFI_PASSWORD_*` and `UNIFI_API_KEY_*` forms. Set exactly
one spelling per precedence level; two is refused as ambiguous rather than
resolved by a rule nobody remembers.

Every failure refuses startup with exit code 6 and names the variable. Nothing
the file or the helper produces is ever logged.

The file provider is the simpler one and covers the Docker and systemd secrets
convention. The rest of this page is the contract for the command provider,
which executes a program and therefore needs its boundaries written down.

## Supported platforms

| Platform | Status | Basis |
|----------|--------|-------|
| Linux | Supported | Verified end to end against a real provider, GnuPG 2.4.4: server start, controller authentication, one read-only call, and the failure cases below. |
| macOS | Unverified | Expected to work; `security find-generic-password -w` is the intended provider and the POSIX session and signal handling are the same as Linux. Nobody has run it. |
| Windows | Unverified | Process-tree termination takes a different path (`taskkill /T /F` on a new process group) that no test exercises. Treat as unsupported until someone runs it. |

"Unverified" is a statement about evidence, not a prediction of failure. Report a
platform that works and it moves up; the failure modes are all fail-closed, so a
platform that does not work refuses startup rather than running with a wrong
credential.

A provider that prompts interactively is not supported on any platform. The
helper's stdin is closed, so a helper that needs a passphrase, a PIN or a touch
confirmation fails immediately instead of hanging the server's startup or
reading the MCP client's protocol stream. Unlock the agent before the server
starts: `gpg-agent` with a cached passphrase, a logged-in `op` session, an
unlocked Keychain.

## Executable resolution

The command is split into an argv the way a shell would split it, using
`shlex`, and then run directly. No shell is involved, so `$VAR`, `~`, globs,
pipes and redirects reach the program as literal text. On Windows write paths
with forward slashes, because backslash is an escape character to `shlex`.

The first element is resolved under one rule:

- An **absolute path** is used as given.
- A **bare name** with no path separator is looked up on the `PATH` of the
  environment the server was started with.
- Anything else, such as `./helper.sh` or `bin/helper`, is **refused**. A
  relative path names a location relative to a working directory the operator
  did not choose, which is the working directory the MCP client picked.

This rule is specific to the command provider, because the helper is executed.
`_FILE` still resolves a relative path against the working directory the way any
other path-valued setting does; give it an absolute path anyway.

Prefer an absolute path. A bare name is convenient but depends on the `PATH`
your MCP client happened to export, which is often not your shell's `PATH`.

## Process lifecycle

- **Working directory.** The helper runs in a neutral directory, the filesystem
  root on POSIX and `%SystemRoot%` on Windows, never the working directory the
  server was started in. That directory belongs to whatever project the user
  opened in their editor, and it is on `sys.path` for a Python helper, so
  inheriting it would let that project decide what `python -m helper` imports.
  Every path your helper needs must be absolute.
- **Environment.** The helper receives the environment the server process was
  started with, restricted to the variable names present before any `.env` file
  was loaded. A `.env` in the working directory can therefore not introduce
  `PYTHONPATH`, `LD_PRELOAD` or similar into the helper. Provider settings such
  as `PASSWORD_STORE_DIR`, `GNUPGHOME` or `GPG_TTY` must be exported to the
  server, not written into a `.env`.
- **Trust.** The `_COMMAND` and `_FILE` variables themselves are honoured only
  when they came from the real process environment. A `.env` file may still
  supply a plain password, as it always could, but it cannot make the server run
  a program.
- **stdin** is closed. **stdout** is captured, capped at 64 KiB, and must be a
  single non-empty line after trailing newlines are dropped. **stderr** is
  discarded, not captured: a failing helper frequently prints the credential
  there, and no truncation rule makes that safe to log. Run the helper yourself
  in a terminal to see why it failed.
- **Timeout.** The helper gets 30 seconds. On expiry its entire process tree is
  terminated, not just the process that was started: it runs in its own session
  on POSIX and its own process group on Windows, so a descendant it left running
  in the background cannot outlive the server's startup or hold the output pipe
  open. `SIGTERM` is followed by `SIGKILL` after a short grace period.
- **Agents the provider starts are its own.** A provider may deliberately
  daemonise a long-lived agent and detach it: GnuPG starts `gpg-agent` on first
  use and leaves it running after the helper exits. That agent is outside the
  helper's session on purpose, so the timeout above does not reach it, and
  should not: killing the operator's credential agent is not the server's
  business. Stop or configure it through the provider's own tooling, for example
  `gpgconf --kill gpg-agent`.
- **Failure.** A non-zero exit, a timeout, an unresolvable executable, output
  that is empty, multi-line, oversized or not valid UTF-8, all refuse startup
  with exit code 6. The log names the variable, the executable and the status,
  and nothing else.

## Examples

Exporting these in the MCP client's `env` block, or in the shell that starts a
container, keeps the secret out of the client process:

```bash
# pass (Linux), absolute path to the binary
UNIFI_NETWORK_PASSWORD_COMMAND="/usr/bin/pass show unifi/admin"

# gpg directly, when the passphrase is already cached by gpg-agent
UNIFI_NETWORK_PASSWORD_COMMAND="/usr/bin/gpg --quiet --decrypt /etc/unifi/password.gpg"

# macOS Keychain
UNIFI_NETWORK_PASSWORD_COMMAND="/usr/bin/security find-generic-password -a admin -s unifi-mcp -w"

# 1Password CLI, with an already-signed-in session
UNIFI_NETWORK_PASSWORD_COMMAND="/usr/local/bin/op read op://Private/UniFi/password"
```

The file provider needs no contract beyond the file itself:

```bash
UNIFI_NETWORK_PASSWORD_FILE=/run/secrets/unifi_password
```

A secret file readable by group or others gets a warning; `chmod 600` it.
