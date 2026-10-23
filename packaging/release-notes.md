**Linux:** take the source code below (zip or tar.gz) and follow the README (docs/LINUX.md has the full Linux guide).

**Windows:** download one of these:

- `$setup`: the installer. It installs just for you (no admin prompt) and adds a
  Start Menu entry.
- `$portable`: the portable build. Unzip it anywhere and run `$app.exe` inside.

### "Windows protected your PC"

This build isn't code-signed, so Windows SmartScreen warns you the first time you run it.
Click **More info**, then **Run anyway**. You'll only see this once.

Antivirus programs sometimes flag files inside game installers too. The Windows guide explains
why, and what Nexus does about it: [docs/WINDOWS.md]($guide).

### Checksums (SHA-256)

Also attached as `SHA256SUMS`. On Windows, compare with
`certutil -hashfile <file> SHA256`; on Linux, `sha256sum -c SHA256SUMS`.

$checksums
