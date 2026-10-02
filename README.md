# 🌤️ sky-shader-studio

> *Lightweight GUI tool for live disassembly, editing, and recompiling Vulkan SPIR-V sky shaders.*
> Open a shader. Poke the light. Hit **F5**. Watch the sky change.

A Visual Studio–flavoured (Dark+) editor for the shader files of **Sky: Children of the Light** (`.spv` + `.ref`), written in plain Python + tkinter. No pip installs, no heavyweight IDE, no pain. Just you, a few thousand tiny GPU programs, and the urge to make the sunset a little more orange.

---

## ✨ Features

- **Edit `.spv` directly.** Double-click a binary shader, read it as SPIR-V assembly (or GLSL), change it, press `Ctrl+S` / `F5`, and it is compiled straight back into the `.spv`. The original is saved once as `.bak`.
- **Byte-exact round-trip.** Open → save without edits gives you a file *identical* to the original, so you only change what you meant to change.
- **Looks like home.** Dark+ theme, line numbers, syntax highlighting, find/replace, tabs, status bar, output and problems panels.
- **Go to definition.** `Ctrl+Click` (or `F12`) on any `%id` to jump to where it was born.
- **Real error reporting.** Compiler errors land in the *Problems* panel, double-click jumps to the offending line.
- **`.ref` editor.** Reflection files get a table view (rename uniforms, tweak the 8 bytes of fields) and a hex view.
- **Mass Decompile / Mass Compile.** Point it at a folder with thousands of shaders, pick a thread count, go make tea.
  - skips unchanged files, so recompiling only touches what you edited
  - optional `.bak` backups and `spirv-val` validation
  - progress bar, cancel button, per-file error log
- **Fast Explorer** with live filter, built to survive a folder of ~3,700 files.

## 🚀 Quick start

1. Install **Python 3.8+** (with tkinter, the standard Windows installer has it).
2. Get the SPIR-V tools: [Vulkan SDK](https://vulkan.lunarg.com), or drop these into a `tools/` folder next to the script:
   `spirv-dis`, `spirv-as`, `spirv-val`, `spirv-cross`, `glslangValidator`
3. Run:

```bash
python SkyShaderStudio.py
```

4. `Ctrl+Shift+O` → choose your `Shaders/Bin` folder → double-click a shader → create something beautiful.

Tool paths are auto-detected; if something is missing, `Tools → Settings` will show you what and let you fix it.

## ⌨️ Hotkeys

| Key | Action |
|---|---|
| `Ctrl+Shift+O` | Open folder |
| `Ctrl+S` / `F5` | Compile current shader to `.spv` |
| `F6` | Decompile current `.spv` to a text file |
| `F7` | Validate (`spirv-val`) |
| `Ctrl+Shift+D` | Mass decompile |
| `Ctrl+Shift+B` | Mass compile |
| `Ctrl+F` / `Ctrl+G` | Find & replace / Go to line |
| `Ctrl+/` | Toggle comment |
| `Ctrl+Click` / `F12` | Go to definition of `%id` |
| `Ctrl+B` / `Ctrl+J` | Toggle Explorer / Output panel |
| `Ctrl+Wheel` | Zoom |

## 🧭 Typical workflow

```text
Shaders/Bin ──► Mass Decompile ──► _decompiled/*.spvasm
                                        │  edit in your favourite editor (or here)
Shaders/Bin ◄── Mass Compile  ◄─────────┘  only changed files, with .bak safety net
```

## 🧪 Good to know

- **SPIR-V Assembly mode** keeps numeric ids (`%195`) to guarantee a byte-identical round-trip. *Friendly names* mode is prettier, but the output will differ from the original binary (still valid).
- **GLSL mode** is great for reading; recompiling it works, but it will not match the original byte for byte.
- If your edit changes uniform blocks or attributes, the matching `.ref` may need a manual update. The studio does not sync them for you (yet).
- Try your first mass-compile into a separate folder before overwriting the game's files.

## ⚠️ Disclaimer

This is an unofficial fan tool and is not affiliated with or endorsed by thatgamecompany. Modifying game files may violate the game's terms of service and can break your installation. Back up your files and use it at your own risk.

## 📜 License

See [LICENSE](LICENSE).

---

<p align="center"><i>Made with curiosity, a dark theme, and a soft spot for golden-hour skies. 🕯️</i></p>
