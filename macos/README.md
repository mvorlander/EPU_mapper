# macOS launcher

The macOS launcher is a small native `.app` wrapper around the shared Tkinter
launcher. It embeds the EPU Mapper source files but uses an existing EPU Mapper
Conda environment instead of bundling another Python runtime, which keeps the
application lightweight. The installed app can be moved and does not depend on
the repository remaining at its original path.

Build and install it for the current user:

```bash
./scripts/build_macos_launcher.sh --install
```

The builder automatically checks common `epu-mapper` and `EPU_mapping` Conda
environment locations. To select a specific environment:

```bash
./scripts/build_macos_launcher.sh \
  --python /path/to/conda/envs/epu-mapper/bin/python \
  --install
```

The installed application is `~/Applications/EPU Mapper.app`. It can be opened
from Finder or Spotlight. The generated copy in `dist/macos/` is ignored by Git.
When a session is started, the launcher immediately opens a preparation page in
the default browser. That page redirects to the dashboard as soon as the local
server is listening, so slow reads from network-mounted OffloadData remain
visible instead of looking like a failed launch.

The app stores launcher preferences in
`~/Library/Application Support/EPUMapperReview/` and writes startup diagnostics
to `~/Library/Logs/EPUMapperLauncher.log`.
