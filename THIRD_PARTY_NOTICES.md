# Third-Party Notices

This package includes material derived from the third-party projects listed
below. Each remains under its own license; the full license text is either
reproduced here or shipped next to the derived code, as noted.

## jg

- Source: https://github.com/remotehostai/jg (version 1.0.1, commit
  `e4f464467130da8b60ee46f93bc6732f5c9bd196`)
- License: MIT
- Used in `code_puppy_core_plugins/jev_grep/`:
  - `chunks.py`: ported from `src/chunks.mjs` (line windows, Python AST
    partitioning, merging of short adjacent declarations)
  - `retrieve.py`: ported from `src/retrieve.mjs` (BM25 ranking, identifier
    term splitting, synonym groups)
  - `search.py`: adapted from `src/search.mjs` (overlapping-window
    de-duplication, narrower evidence block selection)

```
MIT License

Copyright (c) 2026 Remotehost

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## DeepSeek Harness

- Source: https://github.com/deepseek-ai/DeepSeek-Harness
- License: MIT, Copyright (c) 2026 DeepSeek
- Used in `code_puppy_core_plugins/spill/`: ported spill design. The full
  license text ships alongside it in `code_puppy_core_plugins/spill/LICENSE.deepseek`.

## llxprt-code

- Source: https://github.com/vybestack/llxprt-code
- License: Apache-2.0
- Used in `code_puppy_core_plugins/theme/`: the Green Screen theme's phosphor
  color values. Attribution only; no llxprt-code source code is included.
