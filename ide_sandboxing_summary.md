# Keeping an IDE away from your keys: options summary and decision notes

Updated 8 October 2026, revision 7. This supersedes the comparison in `ide_sandboxing.html` (7 October), adds Anthropic's `sandbox-runtime`, and is organised around the design chosen since: **JetBrains Gateway with its thin client in a small sandbox and the IDE backend in a virtual machine, one VM per project or a disposable one.** Nothing here has been tested hands-on yet. Figures were read from GitHub, npm, JetBrains documentation and a fresh clone on 8 October and will go stale.

## What changed since the HTML

- The threat is split into three cases with different answers, instead of one "sandbox the IDE" problem.
- The HTML rejected the split model (IDE window on the host, backend in a sandbox) because the backend can reach the client. That objection disappears when the client itself runs as a low-value principal. JetBrains Gateway is that split model as a supported product, so it is now the named option and has its own section (4), including what the client sandbox and the backend VM each need, the security model read for this design, the display question, and a comparison of per-zone, per-project and disposable VMs.
- Five candidates the HTML did not cover are added as building blocks: a second Unix user, the Flatpak IntelliJ with tightened overrides, a local Flatpak of the Gateway tarball, a VM with `waypipe`, and a private Xwayland inside the sandbox as a fallback for the thin client.
- The HTML's note that the IntelliJ Flatpak requests the whole host filesystem and all devices is out of date. The current manifest grants the home directory, and all-devices access was dropped on 1 October 2026. Section 4.3 spells out what "home" means for each sandbox mechanism, since the word hides three different things.
- `anthropics/sandbox-runtime` (`srt`) was examined. It is not a way to sandbox a graphical IDE. Its two possible roles here, a second sandbox around the agent inside the backend VM and a credential-injecting egress proxy, are parked under Deferred work until the main design is built and in use. Using it to sandbox Claude Code on the Mac as an interim step was considered and dropped; see Abandoned ideas.
- Credential findings from the current Mac are recorded in section 5, because they decide how bad any compromise is regardless of sandbox.
- Revision 4: the thin client's Wayland support was checked against the builds JetBrains ships, not against the blog. Clients from 2026.2 select Wayland automatically; the "work in progress" caveat applied to 2026.1 only (4.6).
- Revision 6: section 3 is reorganised by the part each tool plays in the design, replacing one nine-column table. The Toolbox App, which JetBrains names as Gateway's successor, is examined in 4.2 and found to be X11-only today. The Gateway launcher itself ships no Wayland option, unlike the client; one line in its VM options file covers it (4.6).
- Revision 7: agent sandboxing inside the backend VM, and the egress proxy that goes with it, are moved out of the main plan into a Deferred work section near the end. The main problem is the IDE and toolchain; the agent layer is judged better once the VMs are in daily use.

## 1. Three threats, three answers

| Threat | Example | Right answer | Engineering needed |
|---|---|---|---|
| Code from a stranger | Recruiter take-home, "review my repo" | Disposable VM with no shared folders, or a cloud environment in the browser. Delete afterwards. | None today (UTM on the Mac). With Gateway, a disposable clone of the backend image (section 4.7). |
| Compromised dependency in a project you work in daily | npm or PyPI package, Gradle plugin, a Git hook in a repo you cloned | Persistent backend VM per project, per-project credentials, egress allowlist, sandboxed client | This is the project. Sections 2 to 4. |
| Misled AI agent | Instructions planted in a README or comment steer Claude Code or the JetBrains assistant | Same as above, plus the agent's own sign-in and tool list kept small | The backend VM, where the agent runs. A second sandbox around the agent inside the VM is deferred work. |

The HTML's main point stands: opening a project is enough to run its code. VS Code tasks, Gradle and Maven sync, package install hooks and language servers all execute project code before you type anything. The IDE's own trust prompts (VS Code workspace trust with `task.allowAutomaticTasks` off, JetBrains Safe Mode for untrusted projects) reduce the automatic surface and cost nothing, but they are not a boundary.

## 2. Target architecture

```
Host: compositor, 1Password agent with an allowlist, hardware-backed keys, browser, router access
 │
 ├── Client sandbox (second Unix user, Bubblewrap, or a local Flatpak of the Gateway tarball), one per trust zone
 │     JetBrains Gateway launcher + JetBrains Client. Sees the Wayland socket and its own small home.
 │     Holds: the JetBrains account sign-in and an SSH key that opens only the backend VMs.
 │     Egress: the VM subnet and the JetBrains download and account hosts. Nothing else.
 │
 └── Backend VMs: one per project, cloned from a base image; disposable clones for stranger code
       IDE backend, indexing, builds, tests, toolchains, Claude Code, project code.
       Holds: a per-project deploy key or fine-grained token. (Holding none at all is deferred work.)
       Egress: NAT with an nftables or Incus ACL allowlist, or only a proxy on the host.
```

Why this beats both models in the HTML:

- **Kernel boundary where the hostile code runs.** The backend VM is the thing that executes project code, and it has its own kernel. The HTML's "whole IDE in a namespace sandbox" shares the host kernel with the hostile code.
- **Native window without Wayland-socket recipes for the whole IDE.** Only the thin client needs the socket. The client is a JetBrains app, so the same sandbox recipes apply, but it is smaller and it never runs project code.
- **A vendor-supported product on both sides.** JetBrains Remote Development over SSH is documented and maintained. The community Wayland-in-container recipes are not.
- **The trusted link is harmless.** JetBrains says information may be forwarded either way without user interaction. Forwarding into a sandbox that holds nothing is fine. SSH agent forwarding stays off in the client.

Costs: memory for several VMs (the Framework's 64 GB covers four or five running at once), the thin client's feature set, which lags the local IDE in places, and a version floor of 2026.2 for the client's native Wayland support (section 4.6). The first two are testable in a day.

Variant for later: Intel's Xe driver is adding SR-IOV GPU virtualization for Panther Lake, which the Framework 13 Pro uses. If it lands in the distribution kernel, a VM can get real GPU acceleration, which makes "whole IDE in the VM, shown through the VM console or waypipe" viable too. Not needed for the Gateway design, where the backend never draws.

## 3. Option catalogue

The chosen design (section 4) is assembled from parts, and most tools that get called "sandboxes" fit only one part. The parts:

- **Client sandbox:** holds the Gateway launcher and the JetBrains Client, draws on the host desktop, and must see only the Wayland socket and its own home.
- **Backend:** runs the IDE backend, toolchains, the agent and the project code. This is the part that needs a kernel boundary.
- **Egress control:** decides what leaves the backend and which credentials reach it.
- **Agent layer:** an optional second sandbox around the agent's commands inside the backend. Deferred work, listed here only so the catalogue is complete.

The HTML compared Bubblewrap, ai-jail and jailbee as if each were a complete answer. Below, each candidate is placed where it fits.

### 3.1 Client sandbox candidates

| Candidate | How it isolates | Display | What the client sees | Verdict |
|---|---|---|---|---|
| Second Unix user | File permissions and per-UID firewall rules; same kernel and compositor | Host Wayland socket shared with `setfacl` | Its own home; nothing of yours unless copied in | **Try first.** Simplest, and the egress rules are trivial. Needs the compositor to accept a connection from another UID: reported working on Sway, untested on GNOME and KDE. |
| Bubblewrap script | Namespaces with a policy you write; runs as you | You bind the socket in yourself | A fresh home with only what you bind | **Second.** Molodetskikh's script is the starting point. Needs the embedded-Chromium property (4.3). |
| Local Flatpak of the Gateway tarball | Bubblewrap under Flatpak's policy, from a manifest you write | `--socket=wayland` only | Private config and data under `~/.var/app/<id>`; the real home never granted | **Third.** Adds Flatpak's override system and the Wayland security-context marking. Per-app egress rules need cgroup matching because it runs as your UID. |
| ai-jail | Bubblewrap plus Landlock and seccomp, ready-made | `--display` binds only the Wayland socket | A fresh in-memory home; SSH only with `--ssh` | Built for terminal agents. Nobody has run a graphical client under it; the HTML's 1 to 3 hour estimate is optimistic. |
| Flatpak IntelliJ from Flathub, with overrides | Same mechanism as the row above, community manifest | `--socket=wayland` plus `fallback-x11`, so no X11 socket in a Wayland session | Private app data, but the real home, both agent sockets, the GPU and IPC are granted until removed with `flatpak override --user --nofilesystem=home --nosocket=ssh-auth --nosocket=gpg-agent --nosocket=fallback-x11 --unshare=ipc` | Only if the full IDE must run locally; Flathub has no Gateway package. Its value here is as the working precedent for a JetBrains app on a Wayland socket alone (4.3). |

Not a client sandbox: `sandbox-runtime`, which has no display code (the reasons are under Deferred work); Toolbx and Distrobox, which are not a boundary at all, as Fedora's own documentation says; and Dev Containers or Remote Development used as shipped, where the client runs as you, which is the problem this design fixes.

### 3.2 Backend candidates

| Candidate | Boundary | Verdict |
|---|---|---|
| VM per project, cloned from a base image, with disposable clones for stranger code | Separate kernel | **Chosen.** Incus as the manager (4.7). |
| VM per trust zone | Separate kernel, shared by the projects in a zone | Fallback if per-project turns out too heavy (4.7). |
| Podman or Incus container with the backend inside | Namespaces and a separate userland; shares the host kernel | Lighter than a VM and Gateway connects the same way. A kernel bug is a way out. |
| jailbee | Incus containers per branch with Docker nested inside; shares the host SSH and GPG agents by design | Read for ideas, do not depend on it (3.5). |
| VM with the full IDE inside, windows forwarded by `waypipe` over vsock | Separate kernel | Fallback if the thin client disappoints. Rendering glitches with a full IDE are the risk. |

### 3.3 Egress control and the agent layer (the last two rows are deferred)

| Candidate | What it gives | Verdict |
|---|---|---|
| Hypervisor NAT with an `nftables` or Incus ACL allowlist | A per-VM allowlist by address | **First phase.** Simple; no names, no credential handling. |
| `srt proxy` on the host with a decider | Per-request allow by name, TLS termination, credentials injected so the VM holds none | Deferred work. You write the decider; Git over HTTPS only. |
| `srt` inside the VM around the agent or builds | A per-command allowlist, violation logs, write blocks on hooks and rc files | Deferred work. A Fedora guest avoids its Ubuntu issues. |

### 3.4 Whole-system alternative

Qubes OS is this design as a finished operating system: a Xen VM per activity, split SSH and GPG, a firewall per qube, and no GPU acceleration. Check its hardware list for the Framework 13 Pro first; brand-new laptops usually wait. Not recommended now (section 7).

### 3.5 Maturity of the third-party tools

Figures from the HTML (7 October) except `srt` (8 October).

| Tool | Stars | Commit authors | Age and pace | Note |
|---|---|---|---|---|
| Bubblewrap | 8.5k | 78 | Since 2016, steady | In every distribution; Flatpak's engine |
| ai-jail | 1.1k | 37, one wrote most | Since March 2026, changes daily | |
| jailbee | 3 | 6, one wrote almost all | Since August 2026 | |
| `sandbox-runtime` | 5.5k | 45, Anthropic staff wrote most | Since October 2025, still 0.0.x | 151 open pull requests, 114 open issues |

## 4. JetBrains Gateway in detail

### 4.1 The pieces and what runs where

JetBrains describes Gateway as "a lightweight launcher that connects a remote server with your local machine, downloads backend components, and opens the project in JetBrains Client". Three processes matter:

- **JetBrains Gateway** is the launcher. It manages connections and, after you pick a host, downloads and starts the client that matches that host's backend. Its window hides to the tray by default.
- **JetBrains Client** is the UI, one process per environment. "The JetBrains Client's version is always equal to the version of the backend IDE." It is built on the IntelliJ platform and imports local IDE settings it finds on the client machine. Plugins on the client are limited to those that change the UI, keymaps and themes; "JetBrains Client is not designed to be extensible for the connection part." Functional plugins run on the backend and carry an "On Host" label, and the parts of a plugin that belong on the client are installed there. That last point matters for the design: **the backend can cause code to be installed into the client by design**, which is why the client runs in a sandbox and not as you.
- **The IDE backend** is a headless IDE on the Linux host, by default under `~/.cache/JetBrains/RemoteDev/dist`, started over SSH through `remote-dev-server.sh`. Indexing, analysis, builds, run and debug, the terminal and the AI assistant all run there. Gateway supports Linux backends only.

The connection is an SSH tunnel. Inside it the two sides speak TLS 1.3, the client checks the backend's certificate fingerprint and the backend accepts only a client that presents a one-time token. There are no relay servers. The editor uses the RD protocol from Rider, so typing is local and code intelligence is remote.

Licensing: Remote Development is "available in IntelliJ IDEA Ultimate edition only", and "the license will be asked on the local machine upon connecting to the remote IDE". So the client sandbox holds the JetBrains account sign-in, or an offline activation code, and that is the whole blast radius on the client side if a backend turns hostile.

### 4.2 Packaging, the Toolbox App, and the roadmap

- **Linux packaging:** an official `.tar.gz` (release and EAP), the Toolbox App, or the Gateway plugin bundled in IntelliJ IDEA Ultimate. There is no Flatpak or Snap of Gateway; Flathub carries only community builds of the IDEs. Nixpkgs packages `jetbrains.gateway`, which suggests the tarball is self-contained.
- **Do not use the bundled plugin from your host IntelliJ.** It works, but then the client runs as you. The standalone tarball inside the client sandbox is the point.
- **Gateway is on a deprecation path with no date.** The Toolbox help carries a "JetBrains Gateway migration guide" that says "JetBrains Gateway will be deprecated" and describes "moving the entry point of Remote Development into the Toolbox App (as opposed to the standalone Gateway application)". A JetBrains platform engineer said the same on the Platform forum in September 2025: "the Toolbox App is the recommended entry point for remote development connections". Gateway 2026.2.2 still shipped on 3 September 2026.
- **What the Toolbox App is.** An installer, updater and launcher for JetBrains IDEs. Since 2.6 (April 2025) it also does remote development: it runs your OpenSSH binary to reach the host, deploys a "Toolbox Agent" there (with its own Java runtime, and it "must be exactly the same version as the Toolbox App"), and that agent installs and manages IDE backends on the host. The JetBrains Client still draws the IDE locally. Since 3.3 (February 2026) Toolbox also installs a local background daemon, jetbrainsd, for `jetbrains://` links; 3.6 began moving SSH into that daemon, off by default as of July 2026. It supports macOS and Windows backends, which Gateway does not. It has no isolation features of its own: the host-side `environment.json` can restrict what the agent installs, which is policy, not a boundary.
- **Why Gateway now and Toolbox later.** The shipped builds were inspected on 8 October:

| | Gateway 2026.2.2 (3 September 2026) | Toolbox 3.8.1 (17 September 2026) | JetBrains Client 2026.2.3 |
|---|---|---|---|
| Toolkit option shipped | None | None, in 481 jars and the launcher | `-Dawt.toolkit.name=auto` |
| Wayland toolkit library in the bundled runtime | Present (JBR 25.0.4) | Present (JRE 25.0.3) | Present |
| Other display evidence | IntelliJ platform app | Compose and Skiko UI; bundles X11 libraries; its requirements list X11 packages; no Wayland statement anywhere | |
| In a Wayland-only sandbox today | Starts on X11 unless the option is added to its VM options (4.6) | Starts on X11; would need the private Xwayland fallback | Wayland |
| Resident component on the backend | None beyond the IDE backend | Toolbox Agent, version-locked to the app | |
| SSH client | JetBrains' own implementation | The OpenSSH binary, or jetbrainsd optionally | |

  Three consequences. Toolbox is X11-only today and nothing suggests Wayland work on it, so it would need the fallback that Gateway does not. Toolbox puts a version-locked agent in every backend VM, and Toolbox updates itself, so each update would ripple into every VM and every pre-baked image; Gateway's backend is pinned by its own version and needs nothing resident. Toolbox also wants more from the sandbox: a writable install tree, feed and download egress, a JetBrains account login that on GNOME looks for the secret service, and the daemon. One point in its favour: because it runs the real OpenSSH binary, `ssh_config` applies fully, including a `ProxyCommand` over vsock (4.7).
- **Triggers to revisit the launcher choice:** Gateway stops shipping, or the Toolbox App gains native Wayland. The design does not depend on which launcher starts the client, so the launcher is a replaceable part.
- **Known gaps:** paid plugins on the client were a long-standing gap; staff said support was "planned for 2025.3" and nothing in that thread confirms it shipped. Community posts from August 2026 report plugin failures in 2026.1.4 and 2026.2.0.1 and Copilot problems. Expect the client to lag the local IDE and budget a real working session to find out what matters to you.

### 4.3 What the client sandbox must provide

A checklist for whichever mechanism carries the client (second user first, Bubblewrap second, a local Flatpak of the Gateway tarball third):

- **Files:** the Gateway tarball, read-only; a private home with `~/.config/JetBrains`, `~/.cache/JetBrains` (the per-version client distributions and the JetBrains Runtime land there) and `~/.local/share/JetBrains` writable and persistent; fonts, locale, CA certificates, resolver configuration.
- **Sockets:** the Wayland socket and nothing else. No X11 socket (the HTML explains why), no D-Bus, no agent sockets. Without D-Bus, "open link" from the client does nothing; JetBrains already shows a confirmation dialog before opening any link on the client, and in this design the answer is no.
- **Secrets:** one SSH private key that opens only the backend VMs, kept in the sandbox home, and the JetBrains account token. The host agent is not visible. SSH agent forwarding is a checkbox under Settings, Tools, SSH Forwarding; the documentation describes it as something you select, which reads as opt-in, but the default is not stated, so verify it is off and keep it off. Keep `ForwardAgent` out of the sandbox's own `ssh_config`.
- **Egress:** the VM subnet, and the JetBrains hosts that serve the client, the runtime and the license check. The FAQ says "both remote server and local computer, or only the local one, must have a network connection" to "a list of JetBrains URLs". Take the exact list from the first test's proxy or firewall logs rather than guessing. With a second Unix user this is a few `nftables` rules keyed on the UID.
- **One client home per trust zone.** If a hostile backend installs a client-side plugin, it persists in that home and would meet the next backend you connect to from it. Give each zone its own sandbox home (or its own Unix user) and reset it when the zone's VMs are retired.
- **GPU:** not needed. The JetBrains Wayland toolkit renders in software by default (my understanding; the test confirms it).
- **Embedded Chromium:** JetBrains apps embed Chromium (JCEF) for some panels. Chromium's own sandbox cannot nest inside Bubblewrap, which is why the Flathub IntelliJ sets `ide.browser.jcef.sandbox.enable=false` in its `idea.properties`. A client under Bubblewrap needs the same property; a client running as a second Unix user does not.
- **Three meanings of "home" in this document.** They are easy to confuse and the difference is the whole point of the client sandbox.
  1. *A fresh home.* The second-user, Bubblewrap and ai-jail routes give the client a home directory that is a different directory from yours. Your real home does not exist in its view. This is the model the client sandbox uses.
  2. *A Flatpak home.* Flatpak keeps `HOME` pointing at your real home path but redirects the app's config, data and cache directories to `~/.var/app/<id>/`. Whether the app can see the rest of your real home depends on the manifest: with `--filesystem=home` it can, without it the real home is simply absent. The Flathub IntelliJ grants it; a manifest you write for the Gateway tarball would not.
  3. *A visible home.* `srt` binds your whole filesystem read-only and subtracts a deny list, so your real home is visible unless each secret is named. That is why `srt` is not the client sandbox (Deferred work).
- **Precedent:** the Flathub IntelliJ IDEA Community package runs the full IDE inside a Flatpak, which is a Bubblewrap sandbox with the freedesktop SDK as its userland. Its manifest (read 8 October) grants `--socket=wayland` and `--socket=fallback-x11`, so in a Wayland session the IDE sees no X11 socket at all; its README says that inside the sandbox "there is only Wayland", and the bundled runtime picks the Wayland toolkit automatically since 2026.1. What the precedent proves is the display side: a JetBrains JVM application drawing through a Wayland socket alone, inside a Bubblewrap sandbox, with its settings in a private directory, is a configuration in daily use. What it does not show as shipped is a closed filesystem: the package grants the real home directory, the SSH and GPG agent sockets, the GPU device, IPC and the network by default, so it can read `~/.ssh` and use your agent until the overrides in 3.1 are applied. The packagers are moving the same way: on 1 October 2026 they dropped all-devices access, the Docker and Podman sockets, the keyring, PulseAudio, notifications and the secrets service from the defaults.

### 4.4 What the backend VM must provide

- **Base system:** any recent glibc Linux (glibc 2.28 or newer since 2025.1; Alpine is not supported), an SSH server with the `sftp` subsystem enabled (Gateway deploys the backend over it), and a supported login shell. JetBrains' current minimum is four vCPUs and about 4 GB of RAM, with 5 GB of disk for the backend; earlier pages said 8 GB. Budget 8 GB per VM for JVM projects and stop VMs you are not using.
- **Backend installation, three routes:**
  1. Gateway downloads the backend from JetBrains into the VM. Needs VM egress to the JetBrains download hosts, at least for the install.
  2. Pre-install the backend in the VM base image and register it with `remote-dev-server.sh registerBackendLocationForGateway`. No download egress needed. **This is the route for cloned and disposable VMs**, and it keeps every clone on one known version.
  3. The air-gapped route: a self-hosted mirror of backends, clients and runtime, built with JetBrains' client downloader, with `productsInfoUrl`, `clientDownloadUrl`, `jreDownloadUrl` and `pgpPublicKeyUrl` set in `~/.config/JetBrains/RemoteDev/`. Overkill for one person, but it is how to keep the client sandbox's egress to a host you control.
- **Also in the VM:** toolchains, `uv` and friends, Claude Code, the project checkout, and either a per-project deploy key or fine-grained token or, as deferred work, nothing at all with a credential-injecting proxy on the host. Backend plugins are installed with `remote-dev-server installPlugins`, which needs Marketplace access, so install them in the base image.
- **State:** indexes and caches live in the VM. A persistent per-project VM keeps them. A disposable VM re-indexes on every use, which is minutes for a large project and seconds for a small one.

### 4.5 The security model, read for this design

From JetBrains' Remote Development security model page, with what each point means here:

- "Any information loaded by the backend may be forwarded to the client without further user interaction", and the reverse holds, with GitHub authorization named as a case that happens in the background. **Fine, because the client holds nothing worth forwarding.** It is the reason the client cannot run as you.
- Port forwarding from the backend to the local machine is on by default ("if the key is absent, the setting is considered true by default") and can be disabled per user or system-wide on the host side with flag files or registry keys. Reverse forwarding, local to remote, persists across reconnects and needs an explicit Allow; JetBrains says to use it "only with servers or individuals you fully trust". **Here the backend is the untrusted side.** Keep forward port forwarding for reaching a dev server, never allow a reverse forward, and consider disabling forwarding on the backend side for VMs that do not need it.
- Clipboard content "is sent to the backend only at the moment of paste" and the backend "can change the clipboard only during the actual copy". **Better than the raw Wayland socket**, where a focused app can read the clipboard at will.
- Link opening goes to the client with a confirmation dialog. **In a sandbox without D-Bus it cannot open anything anyway.**
- Plugins: see 4.1. **The client is extensible from the backend; one client home per zone limits the spread.**
- SSH agent forwarding: opt-in checkbox; keep it off (4.3).
- Telemetry follows the IDE's rules and is off by default in release builds.

### 4.6 The display question: Wayland in the thin client

This was the one open technical risk on the client side. It is now settled for the client by the builds JetBrains ships, with one line needed for the Gateway launcher. JetBrains made native Wayland the default in the IDEs in 2026.1 through a launcher option, `-Dawt.toolkit.name=auto`, which picks the Wayland toolkit when a Wayland connection succeeds and X11 otherwise. The February 2026 announcement added: "Wayland support for Remote Development mode is currently a work in progress. In the meantime, Remote Development mode will continue to operate as before and not enable native Wayland support automatically." That described 2026.1. On 8 October the last 2026.1 client, the current 2026.2 client and the current Gateway launcher were downloaded from JetBrains and compared:

| | 2026.1 client (build 261.27258.48) | 2026.2.3 client (build 262.10968.63, 16 September 2026) | Gateway 2026.2.2 launcher (build 262.10315.114, 3 September 2026) |
|---|---|---|---|
| Toolkit option in the shipped VM options | None, so the runtime defaults to X11 | `-Dawt.toolkit.name=auto` | None |
| Wayland toolkit library in the bundled runtime (`libawt_wlawt.so`) | Present | Present | Present |
| Code that overrides the toolkit at runtime | Not checked | None found in the 323 platform jars | None in the native launcher or scripts |

So a 2026.2 client behaves like the IDE: give it a Wayland socket and it draws natively; give it none and the X11 fallback fails visibly in a sandbox without an X11 socket, which is the failure mode you want. The known Wayland rough edges apply to the client as they do to the IDE: dialogs may not keep their position, there is no splash screen, some popups cannot leave the main window, and decorations may differ from the desktop. There is no field evidence either way on the client specifically, because almost nobody runs it this way yet.

What follows:

1. **Add one line for the Gateway launcher.** Gateway 2026.2.2 ships no toolkit option, so unless the runtime's own default has changed, its window starts on X11 and fails in the sandbox. Add `-Dawt.toolkit.name=auto` to `bin/gateway64.vmoptions` in the sandbox copy before the first launch; JetBrains says the manual option keeps working. The client, once launched, needs nothing.
2. **Pin backends to 2026.2 or later.** The client version always matches the backend, so a 2026.1 backend pulls a 2026.1 client that still defaults to X11.
3. **Keep the private Xwayland as the fallback only.** `xwayland-run` starts one X11 program on its own rootful Xwayland server, which is itself only a Wayland client of the host compositor, so the host X11 socket is never shared. The cost: the program lives inside one host window and the clipboard needs bridging with `wl-clipboard`. Packaged in Debian 13 and Fedora. Needed only for an older backend you cannot upgrade, or for the Toolbox App (4.2).
4. **Never share the host X11 socket.** Any X client can log keystrokes and read other windows.

The custom VM options file for the client is `jetbrains_client64.vmoptions` in the client distribution's `bin` directory under the sandbox home, should anything ever need forcing there.

### 4.7 VM topology: per zone, per project, or disposable

| | Per trust zone | Per project, persistent | Disposable |
|---|---|---|---|
| What it is | Two or three long-lived VMs: personal open source, client work, scratch | One VM per repository, cloned from a base image that already has the backend and toolchains | A clone of the same base image, used once and destroyed |
| Isolation | Between zones only; projects in a zone share a kernel and a home | Between projects | Strongest per task |
| Credential scope | Per-project tokens are still possible but live side by side | One deploy key or token per VM, so per project by construction | None, or a read-only token |
| Memory on a 64 GB host | Three VMs at 8 GB, always on | Four or five running at once; stop the rest | Transient |
| Indexes and caches | Built once per zone, shared | Built once per project, kept | Rebuilt every time |
| Operations | Manual | `incus copy base proj-x`, `incus snapshot` before risky updates, a cloud-init key per VM | `incus copy base tmp`, use, `incus delete -f tmp` |
| Fits | Daily work with low friction | Ongoing projects with different trust levels | Stranger code, one-off reviews, trying a dependency upgrade before adopting it |

Recommendation: **per-project persistent VMs cloned from one base image, plus disposable clones of the same image for stranger code.** Per zone is the fallback if per-project turns out heavier than expected in practice; the mechanics are identical, only the number of VMs differs.

Incus is the natural manager here: it is already installed on the Mac and in use against the `router26a` remote, it runs VMs with `--vm`, it has snapshots, copies, cloud-init for injecting the client's SSH public key, per-instance network ACLs for the egress allowlist, and a `.incus` DNS zone so Gateway can address a VM by name instead of an address that changes per clone. libvirt with virt-manager is the alternative and has the more direct path to a vsock device.

Details that follow from cloning:

- Every clone gets fresh SSH host keys. On a local hypervisor, accept new host keys per VM name or keep a per-VM known-hosts file; JetBrains' own fingerprint-and-token check is a separate layer on top.
- Gateway's recent-connections list fills with dead disposable VMs. Harmless.
- Pre-install the backend and the backend plugins in the base image (4.4, route 2) so a clone needs no egress at all to start working.

### 4.8 How to test it

In the rehearsal on the Mac, before the Framework arrives: VM A is an Ubuntu 26.04 desktop with GNOME on Wayland playing the host; VM B is an Ubuntu 26.04 server playing the backend, with the IntelliJ backend installed from the tarball and registered.

1. On VM A, create user `gw`, grant it the Wayland socket with `setfacl`, unpack the Gateway tarball in its home, add the toolkit line from 4.6 to its VM options, generate an SSH key there, and put the public key in VM B. Run Gateway as `gw` and connect to VM B.
2. Check the toolkit in Help, About, first in the Gateway window and then in the client; with a 2026.2 backend expect the Wayland toolkit in both. If the launcher reports X11, check its VM options line; if the client does, check the backend version. The private Xwayland in 4.6 is the fallback for either.
3. Run the canary (section 6) inside the client sandbox and inside the backend VM.
4. Work in it for a real session: a JVM project and a Python project, terminal, run and debug, a dev server through a forwarded port, Claude Code in the backend terminal, the plugins you depend on.
5. In the client, confirm SSH agent forwarding is off; in the backend terminal, confirm `ssh-add -l` finds no agent and `git push` over SSH fails without the deploy key.
6. Measure: backend RAM at rest and while indexing, time to first index for each project, time from `incus copy` to a connected client.
7. Clone VM B, connect to the clone, delete it, and confirm nothing in the client home changed besides the recent-connections list.

If the client fails steps 1 and 2 and the private Xwayland is unacceptable, the fallback is the `waypipe` row in section 3: the full IDE in the VM with windows forwarded, which trades the thin client's limits for `waypipe`'s.

## 5. Credentials: what is on the Mac today

Read on 7 October from the local configuration, not from key material.

- `ForwardAgent yes` is set on 5 of 10 SSH host entries, 4 of them in `~/.ssh/config.d`. Any of those hosts can use every key the agent offers while a session is open.
- The 1Password agent is the live agent (`~/.ssh/auth.sock`) and has no `agent.toml`, so it offers every key in the default vaults to any process that can reach the socket.
- A 2048-bit RSA key from April 2025 sits on disk at `~/.ssh/id_rsa`.
- `gh` is configured and holds a GitHub token, in the keychain or in its hosts file.
- Router and switch access runs through the same account and agent as development.

Plan, independent of which sandbox wins:

1. Agent forwarding off by default; enable per host only where a jump host truly needs it, and prefer `ProxyJump`.
2. An `agent.toml` allowlist for the 1Password agent, and a separate vault for network-gear keys that the agent does not offer to the development zone at all.
3. Hardware-backed keys where the far end supports them (`ed25519-sk` with touch on OpenSSH 8.2 or later). Network gear that cannot take them keeps classic keys in the restricted vault.
4. Retire or re-key the on-disk RSA key.
5. Nothing development-related runs in the principal that can reach routers. On the Framework that is a separate user or VM for operations work.

## 6. The canary check

One script, run inside every candidate (the client sandbox and the backend VM alike), reporting pass or fail per probe. It turns the effort estimates in the HTML into measurements.

- Read `~/.ssh`, `~/.config/gh`, `~/.config/1Password` of the real user.
- List keys through any agent socket in the environment or under the runtime directory, then try SSH to `github.com`.
- Reach one allowed hostname and one blocked hostname, by name and by IP.
- Open the X11 socket.
- List Wayland globals and flag screen-capture or clipboard-manager protocols.
- Talk to the session D-Bus.
- Open `/dev/dri`.
- Read the clipboard while another window has focus.
- Write to `.git/hooks` and to a shell rc file in the home it sees.
- Report the kernel it is on (`uname -r`), the user ID, and the namespaces in `/proc/self/ns`.

## 7. Decision

Two paths. A third, sandboxing only Claude Code on the Mac as an interim measure, was considered and dropped; it is recorded under Abandoned ideas.

**Path 1: JetBrains Gateway with a sandboxed client and VM backends on the Framework.** Per-project VMs cloned from a base image, disposable clones for stranger code, the client as a second Unix user with only the Wayland socket, and NAT plus an allowlist for egress. The agent layer and the credential-injecting proxy are deferred work. Effort: two to three days spread over the first weeks with the laptop, after one day of rehearsal in VMs on the Mac. This is the recommended end state. The display path is settled for 2026.2 clients and needs one VM options line for the Gateway launcher (4.6); the remaining unknown is whether the thin client's feature set is acceptable day to day, and the rehearsal settles it. The credential plan in section 5 is a prerequisite under any path, because it decides how bad a compromise is.

**Path 2: Qubes OS.** The same model as a finished operating system, at the cost of GPU acceleration and of hardware support on a laptop this new. Not recommended now; revisit if the Framework appears on the Qubes hardware list.

Recommendation: rehearse Path 1 on the Mac before the Framework ships, apply the credential plan alongside, and build Path 1 on the Framework. The rehearsal costs one day and removes most of the unknowns: whether the client runs under a second user on Wayland, and whether its feature set is acceptable for daily work.

## 8. Next steps, in order

| Step | What | Effort (estimate) | Decides |
|---|---|---|---|
| 1 | Apply the credential plan in section 5 | 2 to 3 hours | Nothing; it is needed under every design |
| 2 | Write the canary script | 1 to 2 hours | The yardstick for everything after |
| 3 | Two Ubuntu 26.04 VMs under UTM: a GNOME-on-Wayland "host" and a server "backend" with the IntelliJ backend pre-installed and registered | 2 hours | The rehearsal rig |
| 4 | Gateway as a second user on the "host" VM, connected to the "backend" VM; steps 1 to 3 of section 4.8 | Half a day | Whether the second-user socket sharing works on GNOME, and that the client comes up on the Wayland toolkit |
| 5 | A real working session through it; steps 4 to 6 of section 4.8 | Half a day | Whether the thin client is acceptable. If not, try `waypipe` from the backend VM. |
| 6 | Egress: NAT plus an allowlist keyed on the VM's interface; per-project deploy key inside the VM | 2 hours | Baseline egress control |
| 7 | On the Framework: Incus VMs from one base image, one per project; disposable clones; the client user with per-UID firewall rules | 1 day | The working setup |

Steps 1 and 2 can start today. Steps 3 to 6 do not need the Framework.

## Open questions carried forward

- Does GNOME's Mutter or KDE's KWin accept a Wayland connection from a second UID with only an ACL on the socket? (Reports exist for Sway.)
- If the second-user route fails, does a local Flatpak manifest around the Gateway tarball run the client cleanly, and does the compositor apply the security context to it?
- The 2026.2.3 client ships `-Dawt.toolkit.name=auto`. Does it come up cleanly on the Wayland toolkit under GNOME and KDE in practice, and which of the documented Wayland rough edges show up in the client?
- Does the Gateway 2026.2 launcher come up on Wayland once `-Dawt.toolkit.name=auto` is added to its VM options?
- Launcher triggers (4.2): has Gateway stopped shipping, or has the Toolbox App gained native Wayland? Either reopens the choice of launcher.
- Which Remote Development features and plugins that matter to you are missing or broken in the current client?
- Is the SSH agent forwarding checkbox off by default in a fresh client? The documentation reads as opt-in but does not say.
- Is Intel's SR-IOV GPU virtualization for Panther Lake in the Ubuntu 26.04 kernel? If so, the whole-IDE-in-VM variant becomes attractive.

## Deferred work

Parked until the design in section 4 is built and has been used for a while. Each item is worth doing, none is needed to get there, and each is easier to judge with the VMs in daily use.

### A second sandbox around the agent inside the backend VM

The backend VM already holds nothing valuable, so a sandbox around Claude Code or around builds inside it adds a per-command egress allowlist, violation logs, and write blocks on Git hooks and shell rc files: a layer against a misled agent, not a boundary against hostile code. The natural tool is Anthropic's [`sandbox-runtime`](https://github.com/anthropics/sandbox-runtime) (`srt`), the engine behind Claude Code's own sandbox. On Linux it wraps one command in Bubblewrap with a seccomp filter and routes its network through filtering proxies on the host. Facts verified from the source on 8 October (v0.0.79) that are worth keeping:

- It is not a client or IDE sandbox. There is no display code; its seccomp filter blocks the Unix socket a Wayland client needs; the whole host filesystem is bound read-only and then reduced by a deny list, which has to be complete; and writes to `.idea` and `.vscode` are blocked with no off switch (issue 159).
- On Ubuntu, AppArmor's user-namespace restriction and the archive's Bubblewrap break its seccomp helper (issues 74, 428 and 429, all open). A Fedora guest avoids the question.
- Maturity: 5.5k stars, Anthropic staff write most commits, still a 0.0.x version with a README warning that APIs and configuration formats may change.

When to pick it up: once a backend VM is in daily use and the agent's commands are the thing you worry about most.

### A credential-injecting egress proxy for the backend VM

`srt proxy` runs `srt`'s HTTP proxy alone, "for a host that runs the workload elsewhere (a VM, a container, another sandbox)". It accepts on a socket the host hands it, terminates every CONNECT, refuses opaque tunnels, and forwards a request only when a separate decider process allows it. The decider can attach a credential to an allowed request, so a fine-grained GitHub token lives on the host and is added to requests to `github.com` from a VM that never sees it. That answers two open questions from the HTML at once: outbound control by name, and per-project credentials without a chore. The costs: you write the decider (a small program speaking a documented framed-JSON protocol), the VM must trust the proxy's CA, and Git must use HTTPS remotes because SSH cannot pass.

The purest form is a VM with no network interface at all, reached over vsock, with the proxy as its only way out. `ssh` can use a `ProxyCommand` through `socat` to a vsock port and Gateway can read `~/.ssh/config`, but whether Gateway's own SSH implementation honours `ProxyCommand` is untested; the Toolbox App runs the OpenSSH binary, so there it would.

Effort: one to two days. When to pick it up: after the NAT allowlist from step 6 has been in use long enough to show what the VMs actually need to reach.

## Abandoned ideas

Recorded so the reasoning survives if circumstances change.

- **Claude Code sandbox today (dropped 8 October 2026).** Turning on Claude Code's built-in sandbox on the Mac as an interim measure, before the Gateway design exists. It wraps the agent's shell commands with Anthropic's `sandbox-runtime`, can deny reads of credential directories and restrict egress to a domain list, and would have taken a few hours. Dropped because it is a partial solution: it covers the agent's shell commands only, not the IDE, the agent's file tools, MCP servers or hooks, and the time is better spent on section 4. If revisited: the engine is [anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime) and the feature is documented at [Claude Code: Configure the sandboxed Bash tool](https://code.claude.com/docs/en/sandboxing). The settings that mattered were `sandbox.enabled`, `allowUnsandboxedCommands` set to false, `denyRead` for `~/.ssh`, `~/.config/gh` and `~/.config/1Password`, and a short `allowedDomains` list.

## Sources

Read on 8 October 2026 unless noted.

JetBrains Gateway and Remote Development:

- [JetBrains Gateway product page](https://www.jetbrains.com/remote-development/gateway/)
- [Install JetBrains Gateway](https://www.jetbrains.com/help/idea/jetbrains-gateway.html) (installation scenarios, Linux tarball)
- [Connect and work with JetBrains Gateway](https://www.jetbrains.com/help/idea/remote-development-a.html) (providers, SSH flow, backend location, `registerBackendLocationForGateway`)
- [Overview of remote development with the thin client](https://www.jetbrains.com/help/idea/remote-development-overview.html) (architecture, plugins on client and backend)
- [FAQ about remote development](https://www.jetbrains.com/help/idea/faq-about-remote-development.html) (licensing, Linux-only backends, network requirements, Ultimate only)
- [Remote development security model](https://www.jetbrains.com/help/idea/security-model.html) (forwarding, agent forwarding checkbox, port forwarding defaults, clipboard, links)
- [Fully offline mode](https://www.jetbrains.com/help/idea/fully-offline-mode.html) (self-hosted mirror settings)
- [System requirements for remote development](https://www.jetbrains.com/help/rider/2025.2/Prerequisites.html) (Rider 2025.2 page; CLion's equivalent is [here](https://www.jetbrains.com/help/clion/prerequisites.html))
- [Updated system requirements for Linux: glibc 2.28](https://blog.jetbrains.com/idea/2025/01/updated-system-requirements-for-linux-gnu-c-library-glibc/)
- [Wayland by default in 2026.1 EAP](https://blog.jetbrains.com/platform/2026/02/wayland-by-default-in-2026-1-eap/) (the `auto` toolkit, Remote Development excluded for now)
- [Seeking clarity on the JetBrains Remote Development ecosystem](https://platform.jetbrains.com/t/seeking-clarity-on-the-jetbrains-remote-development-ecosystem-client-gateway-code-with-me-and-the-future-roadmap/2723) (JetBrains Platform forum, staff answers of September 2025 and August 2026)
- [xwayland-run manual](https://www.mankier.com/1/xwayland-run) and [Phoronix on xwayland-run](https://www.phoronix.com/news/xwayland-run)
- JetBrains Client distributions `JetBrainsClient-261.27258.48.tar.gz` and `JetBrainsClient-262.10968.63.tar.gz`, `JetBrainsGateway-2026.2.2.tar.gz` and `jetbrains-toolbox-3.8.1.88030.tar.gz`, all from download.jetbrains.com, inspected on 8 October: VM options files, `product-info.json`, native launchers, bundled runtimes and the jars under `lib/`. Build numbers from the JetBrains releases API (`data.services.jetbrains.com/products/releases` with codes `IIU`, `GW` and `TBA`).
- Toolbox App: [JetBrains Gateway migration guide](https://www.jetbrains.com/help/toolbox-app/jetbrains-gateway-migrations-guide.html), [Offline remote development in the Toolbox App](https://www.jetbrains.com/help/toolbox-app/offline-remote-development-in-the-toolbox-app.html) (the Toolbox Agent and its version lock), [Toolbox App system requirements](https://www.jetbrains.com/help/toolbox-app/ta-system-requirements.html), [Toolbox App 2.6 with remote development](https://blog.jetbrains.com/toolbox-app/2025/04/toolbox-app-2-6-is-here-with-remote-development-support/), [Toolbox App 3.3 introducing jetbrainsd](https://blog.jetbrains.com/toolbox-app/2026/02/toolbox-app-3-3-introducing-jetbrainsd-an-improved-linux-experience-and-more/), [SSH connections moving to the JetBrains Daemon in 3.6](https://blog.jetbrains.com/toolbox-app/2026/06/ssh-connections-are-moving-to-jetbrains-daemon-in-the-toolbox-app-3-6-eap)

sandbox-runtime (deferred work):

- [anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime), its README, [docs/srt-proxy.md](https://github.com/anthropics/sandbox-runtime/blob/main/docs/srt-proxy.md), and the source files `src/sandbox/linux-sandbox-utils.ts`, `macos-sandbox-utils.ts`, `sandbox-utils.ts` and `vendor/seccomp-src/seccomp-unix-block.c` from a clone at v0.0.79
- sandbox-runtime issues [74](https://github.com/anthropics/sandbox-runtime/issues/74), [428](https://github.com/anthropics/sandbox-runtime/issues/428), [429](https://github.com/anthropics/sandbox-runtime/issues/429) (Ubuntu and AppArmor), [173](https://github.com/anthropics/sandbox-runtime/issues/173) (Chromium IPC on macOS), [159](https://github.com/anthropics/sandbox-runtime/issues/159) (`.idea` write block), [642](https://github.com/anthropics/sandbox-runtime/issues/642) (no SIGWINCH under `--new-session`)

Building blocks (7 October):

- [freedesktop bug 84817, another local user on a Wayland display](https://bugs.freedesktop.org/show_bug.cgi?id=84817) and an [Arch forum recipe with setfacl](https://bbs.archlinux.org/viewtopic.php?id=273201)
- [waypipe manual](https://manpages.debian.org/unstable/waypipe/waypipe.1.en.html) and its [vsock merge request](https://gitlab.freedesktop.org/mstoeckl/waypipe/-/merge_requests/16)
- [flatpak-override manual](https://www.man7.org/linux/man-pages/man1/flatpak-override.1.html) and the [Flathub IntelliJ IDEA Community packaging](https://github.com/flathub/com.jetbrains.IntelliJ-IDEA-Community)
- [Ubuntu certification, Framework Laptop 13 Pro](https://ubuntu.com/certified/platforms/15849) and [Phoronix on its shipping delay](https://www.phoronix.com/news/Framework-13-Pro-July)
- Everything cited in `ide_sandboxing.html` for the threat, the vendors' remote-development security notes, Wayland and graphics, the distributions, and the three tools compared there
