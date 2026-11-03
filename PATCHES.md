# Compatibility patches — exact spec

Everything below is reproduced mechanically by `setup_helper.py apply`.
No binaries are distributed; the patch is applied in place to a launcher
the user already installed. Only this exact build is supported —
whole-file SHA-256 gates every write.

## Recognized build

| | |
|---|---|
| Version | Elemental Fracture Launcher **3.6.0** |
| Size | 13,242,880 bytes |
| Original SHA-256 | `cb1590bb594adb75a0ec2fb0e6bad4e31d272cfd28ed05cc36dea820fe748eb6` |
| Patched SHA-256 | `c0774720ae6707563d59f38f533bbec891a837652f6389f20ee38795727f5021` |

Any other digest or size → refused. Unrecognized launcher versions are
never patched.

## Patch A — crash-bypass @ `0x157F0B` (5 bytes)

| | |
|---|---|
| Original | `E8 50 6D 55 00` |
| Patched | `31 C0 90 90 90` |
| Semantics | `call <rel32>` → `xor eax,eax; nop; nop; nop` |

The original instruction calls the function wrapping `RevokeDragDrop` —
the launcher registers OLE drag-drop at startup and that call site
crashes under CrossOver's wine build before the window appears.
Replacing the call with `xor eax,eax` + NOPs skips this one call site so
the launcher starts and its menus work; full OLE drag-drop behavior
under CrossOver is unsupported and untested.

## Patch B — debug channel @ `0xB031D8` (105 bytes, ASCII)

An embedded command-line string passed to the WebView2 browser process.
Space-padded to the same 105-byte slot — no length or relocation changes.

| | |
|---|---|
| Original | `--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --autoplay-policy=no-user-gesture-required` |
| Patched | `--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --remote-debugging-port=9339` |

Swapping `--autoplay-policy=...` for `--remote-debugging-port=9339`
makes the embedded browser expose Chrome DevTools on loopback :9339,
which is the channel this helper uses to drive the launcher's Tauri
backend (`window.__TAURI_INTERNALS__.invoke`).

## Wine registry change

`setup_helper.py apply` also writes one per-executable override in the
selected bottle's `user.reg` (via the bottle's own `wine reg`):

```
HKCU\Software\Wine\AppDefaults\g3-Win64-Test.exe\DllOverrides
    xinput1_3 = "native,builtin"
```

The game ships a native `xinput1_3.dll` (a mod loader) next to
`g3-Win64-Test.exe`. Wine's built-in `xinput1_3` would normally win
resolution, so the loader was bypassed. `native,builtin` makes the
on-disk DLL load first, verified live: `vmmap` shows the game-dir DLL
mapped in the game process. The previous value is preserved in the
journal and restored (or the value deleted if none existed) by
`undo`.
