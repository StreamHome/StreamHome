# StreamHome scripts

The repository-root `install.sh` is the standalone unprivileged installer frontend.
Managed installations use the protected `streamhome` command and systemd service;
privileged lifecycle actions go through `/usr/local/libexec/streamhome/provision`.

`scripts/runtime/` contains the existing shell implementations for setup, start,
stop, restart, update and release checks. Repository-root entries retain their
names and arguments as compatibility wrappers. They resolve their location rather
than relying on the caller's working directory. The release checker includes both
wrappers and implementations in Bash syntax and ShellCheck validation.

The protected updater materializes the update implementation from the same exact
validated target commit. It supports older releases whose controller lives in the
root. The root update wrapper also supports older protected updaters that copy it
into `.run`: it obtains its internal implementation from the supplied exact target
commit, preserves the controller PID and inherited handoff descriptors, and never
substitutes an implementation from the current working tree.

The updater's fallback stop controller likewise obtains the wrapper, internal
stop body when present, and runtime helper from that same exact target commit.
Missing target components fail before invoking the controller; the temporary
materialized files are removed afterward.

These wrappers preserve existing compatibility commands. They do not add the
separately deferred `streamhome update`, `streamhome restart` or uninstall commands.
`test.sh` remains a stateful release checker: use the documented isolated backend
runner for development checks rather than running it against a valuable installation.
